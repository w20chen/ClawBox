from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from clawbox.cell.controller import GROUP, PLURAL, VERSION
from clawbox.controller.kubernetes_backend import dns_label


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


def render_sandbox_task(
    task: BenchmarkTask, *, namespace: str, llm_secret: str,
    llm_egress_cidr: str, profile: str = "small", timeout_seconds: int = 1800,
    command_timeout_seconds: int = 300, output_limit_bytes: int = 4 * 1024**2,
    tool_egress_cidrs: list[str] | None = None, run_id: str = "run",
) -> dict[str, Any]:
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", task.image):
        raise ValueError("SandboxTask tool image must be an immutable arm64 digest")
    # Child names append up to "-runtime-egress" (15 characters).
    name = dns_label(f"{run_id}-{task.instance_id}", prefix="swe-", max_length=48)
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
            },
            "annotations": {"clawbox.openai.com/original-instance-id": task.instance_id},
        },
        "spec": {
            "toolImage": task.image,
            "problemStatement": task.problem_statement,
            "baseCommit": task.base_commit,
            "hintText": task.hint_text,
            "llmSecretName": llm_secret,
            "llmEgressCIDR": llm_egress_cidr,
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
        namespace = options["namespace"]
        manifest_options = {key: value for key, value in options.items() if key != "runtime_class"}
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
                return {
                    "task_id": task.instance_id,
                    "sandbox_task": name,
                    "status": outcome.lower().replace("timedout", "timed-out"),
                    "reason": status.get("reason", ""),
                }
            time.sleep(2)
        raise TimeoutError(f"SandboxTask {namespace}/{name} did not reach Cleaned")


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit SWE-ReBench tasks as two-VM SandboxTask Cells")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--arm64-map", type=Path, required=True)
    parser.add_argument("--namespace", default="clawbox-benchmarks")
    parser.add_argument("--llm-secret", default="clawbox-llm")
    parser.add_argument("--llm-egress-cidr", required=True)
    parser.add_argument("--tool-egress-cidr", action="append", default=[])
    parser.add_argument("--runtime-class", default=os.getenv("KUBERNETES_RUNTIME_CLASS", "kata-fc-arm64"))
    parser.add_argument("--profile", choices=("small", "medium", "large"), default="small")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--command-timeout-seconds", type=int, default=300)
    parser.add_argument("--output-limit-bytes", type=int, default=4 * 1024**2)
    parser.add_argument("--run-id", help="stable id for an intentional retry; generated when omitted")
    args = parser.parse_args()
    if args.parallelism < 1:
        parser.error("--parallelism must be >= 1")
    tasks = resolve_arm64_tasks(load_tasks(args.tasks), load_arm64_mapping(args.arm64_map))
    if args.sample is not None:
        tasks = tasks[:args.sample]
    results = KubernetesBenchmarkLauncher().run(
        tasks, parallelism=args.parallelism, namespace=args.namespace,
        llm_secret=args.llm_secret, llm_egress_cidr=args.llm_egress_cidr,
        runtime_class=args.runtime_class, profile=args.profile,
        timeout_seconds=args.timeout_seconds,
        command_timeout_seconds=args.command_timeout_seconds,
        output_limit_bytes=args.output_limit_bytes,
        tool_egress_cidrs=args.tool_egress_cidr,
        run_id=args.run_id,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    if any(item["status"] != "succeeded" for item in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
