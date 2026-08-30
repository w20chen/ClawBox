from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from clawbox.cell.controller import GROUP, PLURAL, VERSION
from clawbox.controller.kubernetes_backend import dns_label
from clawbox.experiments import ExperimentSpec, expand_matrix, validate_workflow
from clawbox.experiments.results import ResultEnvelope, RunStatus, failure_category_for, utcnow


MAX_LLM_EGRESS_CIDRS = 32


def production_workflow(
    *, profile: str, concurrency: int, timeout_seconds: int, command_timeout_seconds: int,
):
    """Describe the accepted Cell without changing the existing launcher mechanics."""
    spec = ExperimentSpec.model_validate({
        "workload": {"source": "swe_rebench", "input": "launcher task list"},
        "agent": {"driver": "openclaw"}, "inference": {"backends": ["api"]},
        "sandbox": {"backend": "kubernetes", "tool_transport": "ssh"},
        "scheduling": {"baselines": ["fixed-resident"]},
        "execution": {"concurrency": concurrency, "timeout_seconds": timeout_seconds,
                      "command_timeout_seconds": command_timeout_seconds},
        "resources": {"profile": profile},
    })
    return expand_matrix(spec)[0]


def normalize_llm_egress_cidrs(cidrs: list[str]) -> list[str]:
    """Return unique canonical LLM egress networks in stable address order."""
    if not cidrs:
        raise ValueError("at least one LLM egress CIDR is required")
    networks: dict[tuple[int, int, int], str] = {}
    for value in cidrs:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"invalid LLM egress CIDR {value!r}: {exc}") from exc
        if network.prefixlen == 0:
            raise ValueError("unrestricted LLM egress CIDRs are forbidden")
        key = (network.version, int(network.network_address), network.prefixlen)
        networks[key] = str(network)
    if len(networks) > MAX_LLM_EGRESS_CIDRS:
        raise ValueError(f"at most {MAX_LLM_EGRESS_CIDRS} LLM egress CIDRs are allowed")
    return [networks[key] for key in sorted(networks)]


def resolve_llm_egress_host(host: str) -> list[str]:
    """Resolve a provider hostname to an auditable, fail-closed IPv4 snapshot."""
    host = host.strip().rstrip(".")
    if not host or "://" in host or "/" in host or ":" in host:
        raise ValueError("LLM egress host must be a hostname without a scheme, path, or port")
    try:
        answers = socket.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"cannot resolve LLM egress host {host!r}: {exc}") from exc
    addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    if not addresses:
        raise ValueError(f"LLM egress host {host!r} returned no IPv4 addresses")
    non_global = sorted(str(address) for address in addresses if not address.is_global)
    if non_global:
        raise ValueError(
            f"LLM egress host {host!r} resolved to non-public IPv4 addresses: {', '.join(non_global)}"
        )
    return normalize_llm_egress_cidrs([f"{address}/32" for address in addresses])


def run_label(run_id: str) -> str:
    """Preserve selector-friendly run IDs; hash only unsafe label values."""
    if len(run_id) <= 63 and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", run_id):
        return run_id
    return dns_label(run_id)


@dataclass(frozen=True)
class BenchmarkTask:
    instance_id: str
    image: str
    problem_statement: str
    base_commit: str = ""
    hint_text: str = ""
    repository: str = ""


def load_tasks(path: Path) -> list[BenchmarkTask]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw if isinstance(raw, list) else raw.get("tasks", raw.get("instances", []))
    if not isinstance(records, list):
        raise ValueError("tasks/instances must be a list")
    tasks: list[BenchmarkTask] = []
    for record in records:
        instance_id = str(record.get("instance_id") or record.get("task_id") or record.get("id") or "").strip()
        image = str(record.get("image") or record.get("docker_image") or record.get("image_name") or "").strip()
        if record.get("image_tag") and image and ":" not in image.rsplit("/", 1)[-1]:
            image = f"{image}:{record['image_tag']}"
        problem = str(record.get("problem_statement") or record.get("problem") or record.get("prompt") or "")
        if not instance_id or not image or not problem:
            raise ValueError("every task requires instance_id, original image, and problem_statement")
        tasks.append(BenchmarkTask(
            instance_id, image, problem,
            str(record.get("base_commit") or ""),
            str(record.get("hint_text") or record.get("hints_text") or record.get("hint") or ""),
            str(record.get("repo") or record.get("repository") or "").strip(),
        ))
    return tasks


def load_arm64_mapping(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("ARM64 image mapping must be a JSON object")
    mapping: dict[str, str] = {}
    for original, value in raw.items():
        if not isinstance(value, dict) or value.get("status") != "supported":
            continue
        image = str(value.get("arm64_image") or "")
        if value.get("platform") != "linux/arm64" or not str(value.get("recipe_revision") or ""):
            raise ValueError(f"supported mapping for {original} lacks its arm64 platform/recipe provenance")
        if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", image):
            raise ValueError(f"supported mapping for {original} is not immutable")
        mapping[str(original)] = image
    return mapping


def resolve_arm64_tasks(tasks: list[BenchmarkTask], mapping: dict[str, str]) -> list[BenchmarkTask]:
    resolved: list[BenchmarkTask] = []
    missing: list[str] = []
    for task in tasks:
        image = mapping.get(task.image)
        if image is None:
            missing.append(f"{task.instance_id}={task.image}")
        else:
            resolved.append(replace(task, image=image))
    if missing:
        raise ValueError(
            "tasks have no supported native arm64 mapping (no fallback is allowed): " + ", ".join(missing)
        )
    return resolved


def select_arm64_tasks(
    tasks: list[BenchmarkTask], mapping: dict[str, str], sample: int | None = None,
) -> list[BenchmarkTask]:
    """Apply the requested input sample before enforcing ARM64 coverage."""
    if sample is not None:
        if sample < 1:
            raise ValueError("sample must be >= 1")
        tasks = tasks[:sample]
    return resolve_arm64_tasks(tasks, mapping)


def render_sandbox_task(
    task: BenchmarkTask, *, namespace: str, llm_secret: str,
    llm_egress_cidr: str, profile: str = "small", timeout_seconds: int = 1800,
    command_timeout_seconds: int = 300, output_limit_bytes: int = 4 * 1024**2,
    tool_egress_cidrs: list[str] | None = None, run_id: str = "run",
    llm_egress_cidrs: list[str] | None = None,
    tenant_id: str = "benchmark",
) -> dict[str, Any]:
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", task.image):
        raise ValueError("SandboxTask tool image must be an immutable arm64 digest")
    # Child names append up to "-runtime-egress" (15 characters).
    name = dns_label(f"{run_id}-{task.instance_id}", prefix="swe-", max_length=48)
    effective_llm_cidrs = normalize_llm_egress_cidrs(llm_egress_cidrs or [llm_egress_cidr])
    if llm_egress_cidr != effective_llm_cidrs[0]:
        raise ValueError("llm_egress_cidr must equal the first canonical llm_egress_cidrs entry")
    tenant_id = tenant_id.strip()
    if not tenant_id:
        raise ValueError("tenant_id is required for tenant-scoped KB isolation")
    annotations = {"clawbox.openai.com/original-instance-id": task.instance_id}
    if task.repository:
        annotations["clawbox.openai.com/repository"] = task.repository
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "SandboxTask",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "clawbox-swe-rebench",
                "app.kubernetes.io/managed-by": "clawbox-benchmark-launcher",
                "clawbox.openai.com/run": run_label(run_id),
                "clawbox.openai.com/instance": dns_label(task.instance_id),
                "clawbox.openai.com/tenant": dns_label(tenant_id),
            },
            "annotations": annotations,
        },
        "spec": {
            "toolImage": task.image,
            "problemStatement": task.problem_statement,
            "baseCommit": task.base_commit,
            "hintText": task.hint_text,
            "llmSecretName": llm_secret,
            "llmEgressCIDR": llm_egress_cidr,
            "llmEgressCIDRs": effective_llm_cidrs,
            "llmEgressPort": 443,
            "toolEgressCIDRs": tool_egress_cidrs or [],
            "profile": profile,
            "timeoutSeconds": timeout_seconds,
            "commandTimeoutSeconds": command_timeout_seconds,
            "outputLimitBytes": output_limit_bytes,
        },
    }


class KubernetesBenchmarkLauncher:
    def __init__(self, core=None, custom=None, node_api=None):
        if core is None or custom is None or node_api is None:
            from kubernetes import client, config
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            core = client.CoreV1Api()
            custom = client.CustomObjectsApi()
            node_api = client.NodeV1Api()
        self.core, self.custom, self.node_api = core, custom, node_api

    def run(self, tasks: list[BenchmarkTask], *, parallelism: int, **options: Any) -> list[dict[str, Any]]:
        workflow = production_workflow(
            profile=str(options.get("profile", "small")), concurrency=parallelism,
            timeout_seconds=int(options.get("timeout_seconds", 1800)),
            command_timeout_seconds=int(options.get("command_timeout_seconds", 300)),
        )
        options["_resolved_workflow"] = workflow
        self._preflight(**options)
        if not options.get("run_id"):
            options["run_id"] = f"{time.strftime('%Y%m%d%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = {pool.submit(self._run_one, task, **options): task for task in tasks}
            return [future.result() for future in as_completed(futures)]

    def _preflight(self, **options: Any) -> None:
        namespace = options["namespace"]
        llm_secret = options["llm_secret"]
        runtime_class = options.get("runtime_class", "kata-fc-arm64")
        if runtime_class != "kata-fc-arm64":
            raise RuntimeError("the benchmark launcher only accepts kata-fc-arm64")
        try:
            self.core.read_namespace(namespace)
            secret = self.core.read_namespaced_secret(llm_secret, namespace)
            keys = set((getattr(secret, "data", None) or {}).keys())
            required = {"llm-api-key", "llm-upstream-base-url", "llm-model", "openclaw-model-ref"}
            if missing := sorted(required - keys):
                raise RuntimeError(f"LLM Secret {namespace}/{llm_secret} is missing keys: {', '.join(missing)}")
            runtime = self.node_api.read_runtime_class(runtime_class)
            handler = getattr(runtime, "handler", None)
            overhead = getattr(runtime, "overhead", None)
            if handler != runtime_class or not getattr(overhead, "pod_fixed", None):
                raise RuntimeError("kata-fc-arm64 handler and Pod overhead must pass FC-4 before submission")
            self.custom.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, limit=1)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Kubernetes SandboxTask preflight failed: {type(exc).__name__}: {exc}") from exc

    def _run_one(self, task: BenchmarkTask, **options: Any) -> dict[str, Any]:
        started_at = utcnow()
        namespace = options["namespace"]
        manifest_options = {
            key: value for key, value in options.items()
            if key != "runtime_class" and not key.startswith("_")
        }
        manifest = render_sandbox_task(task, **manifest_options)
        name = manifest["metadata"]["name"]
        try:
            self.custom.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, manifest)
        except Exception as exc:
            if getattr(exc, "status", None) != 409:
                raise
            existing = self.custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
            if existing.get("spec") != manifest["spec"]:
                raise RuntimeError(f"SandboxTask name collision with different spec: {namespace}/{name}") from exc
        deadline = time.monotonic() + int(options.get("timeout_seconds", 1800)) + 900
        while time.monotonic() < deadline:
            current = self.custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
            status = current.get("status") or {}
            if status.get("phase") == "Cleaned":
                outcome = str(status.get("outcome", "Failed"))
                legacy_status = outcome.lower().replace("timedout", "timed-out")
                standard_status = {
                    "succeeded": RunStatus.SUCCEEDED, "failed": RunStatus.FAILED,
                    "timed-out": RunStatus.TIMED_OUT, "cancelled": RunStatus.CANCELLED,
                }.get(legacy_status, RunStatus.FAILED)
                base_workflow = options["_resolved_workflow"]
                workflow = base_workflow.model_copy(update={
                    "sandbox": base_workflow.sandbox.model_copy(update={"tool_image": task.image}),
                })
                capability = validate_workflow(workflow)
                envelope = ResultEnvelope(
                    run_id=str(options["run_id"]), case_id=task.instance_id, baseline=workflow.baseline,
                    classification=capability.classification, resolved_workflow=workflow,
                    provenance={key: value for key, value in {
                        "repository": task.repository, "base_commit": task.base_commit,
                        "tool_image": task.image,
                    }.items() if value},
                    status=standard_status, failure_category=failure_category_for(
                        standard_status, str(status.get("reason", "")),
                    ), started_at=started_at, completed_at=utcnow(), metrics={},
                    artifacts={"sandbox_task": name},
                    backend_details={
                        "topology": "Runtime + Tool", "prediction": "shadow-only",
                        "runtime_image_source": "Cell controller configuration",
                        "llm_secret": options["llm_secret"],
                    },
                )
                return {
                    "task_id": task.instance_id,
                    "sandbox_task": name,
                    "status": legacy_status,
                    "reason": status.get("reason", ""),
                    "resolved_workflow": workflow.model_dump(mode="json"),
                    "result_envelope": envelope.model_dump(mode="json"),
                }
            time.sleep(2)
        raise TimeoutError(f"SandboxTask {namespace}/{name} did not reach Cleaned")


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit SWE-ReBench tasks as two-VM SandboxTask Cells")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--arm64-map", type=Path, required=True)
    parser.add_argument("--namespace", default="clawbox-benchmarks")
    parser.add_argument("--llm-secret", default="clawbox-llm")
    llm_egress = parser.add_mutually_exclusive_group(required=True)
    llm_egress.add_argument(
        "--llm-egress-cidr", action="append",
        help="canonical provider CIDR; repeat to allow multiple addresses",
    )
    llm_egress.add_argument(
        "--llm-egress-host",
        help="provider hostname resolved to all public IPv4 /32 networks before submission",
    )
    parser.add_argument("--tool-egress-cidr", action="append", default=[])
    parser.add_argument("--runtime-class", default=os.getenv("KUBERNETES_RUNTIME_CLASS", "kata-fc-arm64"))
    parser.add_argument("--profile", choices=("small", "medium", "large"), default="small")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--command-timeout-seconds", type=int, default=300)
    parser.add_argument("--output-limit-bytes", type=int, default=4 * 1024**2)
    parser.add_argument("--run-id", help="stable id for an intentional retry; generated when omitted")
    parser.add_argument(
        "--tenant", default=os.getenv("CLAWBOX_BENCHMARK_TENANT", "benchmark"),
        help="stable tenant identity used to isolate and share KB generations",
    )
    args = parser.parse_args()
    if args.parallelism < 1:
        parser.error("--parallelism must be >= 1")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be >= 1")
    if args.command_timeout_seconds < 1:
        parser.error("--command-timeout-seconds must be >= 1")
    try:
        tasks = select_arm64_tasks(
            load_tasks(args.tasks), load_arm64_mapping(args.arm64_map), args.sample,
        )
        llm_egress_cidrs = (
            resolve_llm_egress_host(args.llm_egress_host)
            if args.llm_egress_host
            else normalize_llm_egress_cidrs(args.llm_egress_cidr)
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.llm_egress_host:
        print(
            f"resolved {args.llm_egress_host} to LLM egress CIDRs: "
            + ", ".join(llm_egress_cidrs),
            flush=True,
        )
    try:
        results = KubernetesBenchmarkLauncher().run(
            tasks, parallelism=args.parallelism, namespace=args.namespace,
            llm_secret=args.llm_secret, llm_egress_cidr=llm_egress_cidrs[0],
            llm_egress_cidrs=llm_egress_cidrs,
            runtime_class=args.runtime_class, profile=args.profile,
            timeout_seconds=args.timeout_seconds,
            command_timeout_seconds=args.command_timeout_seconds,
            output_limit_bytes=args.output_limit_bytes,
            tool_egress_cidrs=args.tool_egress_cidr,
            run_id=args.run_id,
            tenant_id=args.tenant,
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if any(item["status"] != "succeeded" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
