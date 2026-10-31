#!/usr/bin/env python3
"""Overlay one validated collector on existing SWE images and register templates.

This preserves every task workspace and dependency layer.  It changes only the
ClawTune guest source/contracts, collector helper, Tool Bridge, and Tool entrypoint
copied from an immutable, already validated Tool image.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any


READY = {"ready", "completed", "succeeded", "success"}
FAILED = {"failed", "error", "cancelled"}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        path.name + f".{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")
    return cleaned[:96] or hashlib.sha256(value.encode()).hexdigest()[:24]


def immutable_digest(image: str) -> str:
    inspected = json.loads(subprocess.check_output(
        ["docker", "image", "inspect", image], text=True,
    ))
    if len(inspected) != 1:
        raise RuntimeError(f"expected one image for {image!r}")
    digests = inspected[0].get("RepoDigests") or []
    if "@sha256:" in image:
        return image
    if not digests:
        raise RuntimeError(f"image has no immutable registry digest: {image}")
    return str(digests[0])


def image_content_manifest(image: str) -> dict[str, Any]:
    paths = (
        "/opt/clawtune-guest/services/sidecar",
        "/opt/clawtune-guest/contracts",
        "/opt/clawtune-guest/tools",
        "/opt/clawbox/clawbox",
        "/opt/clawtune/cache/agent-sched-bench",
        "/usr/local/bin/tool-bridge",
        "/usr/local/bin/cube-tool-entrypoint.sh",
    )
    command = "find " + " ".join(paths) + " -type f -exec sha256sum {} + | LC_ALL=C sort"
    output = subprocess.check_output(
        ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image, "-lc", command],
        text=True,
    )
    config = json.loads(subprocess.check_output(
        ["docker", "image", "inspect", image], text=True,
    ))[0].get("Config") or {}
    environment = dict(value.split("=", 1) for value in config.get("Env") or [])
    return {
        "source_image": immutable_digest(image),
        "files_sha256": output.splitlines(),
        "manifest_sha256": hashlib.sha256(output.encode()).hexdigest(),
        "tool_prewarm": environment.get("CLAWBOX_TOOL_PREWARM", "1") == "1",
        "tool_prewarm_timeout_seconds": int(environment.get("CLAWBOX_TOOL_PREWARM_TIMEOUT_SECONDS", "120")),
    }


def overlay_dockerfile(collector_image: str, clawtune_revision: str, *,
                       tool_prewarm: bool = True, tool_prewarm_timeout_seconds: int = 120) -> str:
    return f"""ARG BASE_IMAGE
FROM {collector_image} AS collector
FROM ${{BASE_IMAGE}}
USER root
RUN rm -rf /opt/clawtune-guest/services/sidecar /opt/clawtune-guest/contracts /opt/clawtune-guest/tools /opt/clawtune/cache/agent-sched-bench
COPY --from=collector /opt/clawtune-guest/services/sidecar /opt/clawtune-guest/services/sidecar
COPY --from=collector /opt/clawtune-guest/contracts /opt/clawtune-guest/contracts
COPY --from=collector /opt/clawbox/clawbox /opt/clawbox/clawbox
ENV PYTHONPATH=/opt/clawbox:/opt/clawtune-guest/services/sidecar/src
COPY --from=collector /opt/clawtune-guest/tools /opt/clawtune-guest/tools
COPY --from=collector /opt/clawtune/cache/agent-sched-bench /opt/clawtune/cache/agent-sched-bench
COPY --from=collector /usr/local/bin/tool-bridge /usr/local/bin/tool-bridge
COPY --from=collector /usr/local/bin/cube-tool-entrypoint.sh /usr/local/bin/cube-tool-entrypoint.sh
RUN chmod 755 /usr/local/bin/tool-bridge /usr/local/bin/cube-tool-entrypoint.sh
ENV CLAWBOX_TOOL_PREWARM={int(tool_prewarm)} \\
    CLAWBOX_TOOL_PREWARM_TIMEOUT_SECONDS={tool_prewarm_timeout_seconds} \\
    CLAWTUNE_REVISION={clawtune_revision} \\
    XDG_CACHE_HOME=/opt/clawtune/cache \\
    BCC_KERNEL_SOURCE=/lib/modules/6.18.28/build \\
    CLAWTUNE_GUEST_COLLECTOR_HELPER=/opt/clawtune-guest/tools/guest_collector_server.py \\
    CLAWTUNE_GUEST_COLLECTOR_PYTHON=/opt/clawtune/venv/bin/python
LABEL org.opencontainers.image.source.clawtune.revision={clawtune_revision}
LABEL io.clawbox.host-kb.protocol="clawtune.host-kb.v1"
WORKDIR /testbed
ENTRYPOINT [\"/usr/local/bin/cube-tool-entrypoint.sh\"]
"""


def retire_replaced_template(record: dict[str, Any], template_api, not_found_error) -> None:
    """Delete only the recorded old template after verifying its replacement."""
    if record.get("old_template_retired_at"):
        return
    old_id, new_id = record["old_template_id"], record["new_template_id"]
    if old_id == new_id or record.get("status") != "template-ready":
        raise RuntimeError("refusing template retirement without a distinct READY replacement")
    replacement = template_api.get(new_id)
    if (str(replacement.status).lower() not in READY
            or replacement.image_info != record["new_image"]):
        raise RuntimeError("replacement template is not READY with the recorded image")
    try:
        old = template_api.get(old_id)
    except not_found_error:
        # Resume after deletion succeeded but before the state write completed.
        record["old_template_retired_at"] = time.time()
        return
    if old.image_info != record["old_image"]:
        raise RuntimeError("old template image changed; refusing retirement")
    template_api.delete(old_id)
    record["old_template_retired_at"] = time.time()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--collector-image", required=True,
                        help="immutable validated Tool image supplying collector files")
    parser.add_argument("--clawtune-revision", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--registry", default="127.0.0.1:5000/clawbox/swe-latency")
    parser.add_argument("--node", required=True)
    parser.add_argument("--expected-kernel-version", required=True)
    parser.add_argument("--expected-count", type=int, default=503)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--batch-size", type=int, default=0,
        help="tasks per batch; 0 processes the full workload as one batch",
    )
    parser.add_argument(
        "--prune-build-cache-between-batches", action="store_true",
        help="delete all unused Docker build cache after each completed non-final batch",
    )
    parser.add_argument("--minimum-free-gib", type=float, default=64.0)
    parser.add_argument(
        "--storage-check-path", action="append", default=[], type=Path,
        help="additional filesystem path whose free space must stay above the floor",
    )
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--retire-replaced-templates", action="store_true",
        help="delete each recorded old template through CubeSandbox after its replacement is READY; preserve Docker images",
    )
    parser.add_argument(
        "--reregister-case", action="append", default=[], metavar="CASE_ID",
        help=(
            "register a new immutable template for this already-completed case "
            "while reusing its published image; repeat for multiple cases"
        ),
    )
    parser.add_argument(
        "--reregister-generation",
        help=(
            "stable operator-chosen generation for --reregister-case; repeat the "
            "same value to resume without registering another template"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.batch_size < 0:
        raise SystemExit("--batch-size must be zero or positive")
    if args.minimum_free_gib <= 0:
        raise SystemExit("--minimum-free-gib must be positive")
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    tasks = workload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != args.expected_count:
        raise SystemExit(
            f"workload must contain exactly {args.expected_count} tasks; got "
            f"{len(tasks) if isinstance(tasks, list) else 'no task list'}"
        )
    reregister_cases = set(args.reregister_case)
    if bool(reregister_cases) != bool(args.reregister_generation):
        raise SystemExit(
            "--reregister-case and --reregister-generation must be supplied together"
        )
    if args.reregister_generation and not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,64}", args.reregister_generation,
    ):
        raise SystemExit(
            "--reregister-generation must be 1-64 letters, digits, dots, underscores, or hyphens"
        )
    workload_case_ids = {str(task.get("case_id")) for task in tasks}
    unknown_reregister_cases = reregister_cases - workload_case_ids
    if unknown_reregister_cases:
        raise SystemExit(
            "--reregister-case is not in the workload: "
            + ", ".join(sorted(unknown_reregister_cases))
        )

    from cubesandbox import Template, TemplateNotFoundError
    from clawbox.cube.api_retry import read_with_backoff

    args.output_dir.mkdir(parents=True, exist_ok=True)
    lock_stream = (args.output_dir / "collector-refresh.lock").open("a+")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise SystemExit(
            f"another collector refresh owns {args.output_dir}; wait for it or stop "
            "that exact process before resuming"
        ) from exc
    logs = args.output_dir / "logs"
    logs.mkdir(exist_ok=True)
    state_path = args.output_dir / "collector-update-state.json"
    state: dict[str, Any] = (
        json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    )
    state_lock = threading.Lock()
    collector_image = immutable_digest(args.collector_image)
    manifest = image_content_manifest(collector_image)
    dockerfile = overlay_dockerfile(collector_image, args.clawtune_revision,
        tool_prewarm=manifest['tool_prewarm'],
        tool_prewarm_timeout_seconds=manifest['tool_prewarm_timeout_seconds'])
    recipe = {
        "schema_version": 1,
        "collector": manifest,
        "clawtune_revision": args.clawtune_revision,
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "workload_sha256": hashlib.sha256(args.workload.read_bytes()).hexdigest(),
        "expected_kernel_version": args.expected_kernel_version,
    }
    recipe_digest = hashlib.sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    atomic_json(args.output_dir / "collector-package-manifest.json", recipe)

    def save(case_id: str, record: dict[str, Any]) -> None:
        with state_lock:
            state[case_id] = record
            atomic_json(state_path, state)

    def storage_safe() -> None:
        required = args.minimum_free_gib * 1024**3
        for path in dict.fromkeys((Path("/"), args.output_dir, *args.storage_check_path)):
            if not path.exists():
                raise RuntimeError(f"storage check path does not exist: {path}")
            free = shutil.disk_usage(path).free
            if free < required:
                raise RuntimeError(
                    f"free-space floor violated at {path}: "
                    f"{free / 1024**3:.1f} GiB < {args.minimum_free_gib:.1f} GiB"
                )

    def retire(record: dict[str, Any]) -> None:
        if args.retire_replaced_templates:
            retire_replaced_template(record, Template, TemplateNotFoundError)
            save(record["case_id"], record)

    def update(task: dict[str, Any]) -> dict[str, Any]:
        case_id = str(task["case_id"])
        reregister = case_id in reregister_cases
        old_sandbox = task.get("sandbox") or {}
        old_image = str(old_sandbox.get("source_image_reference") or "")
        old_template = str(old_sandbox.get("template_id") or "")
        if "@sha256:" not in old_image or not old_template:
            raise RuntimeError(f"{case_id}: workload lacks immutable image/template identity")
        previous = dict(state.get(case_id) or {})
        if (
            previous.get("status") == "template-ready"
            and previous.get("recipe_digest") == recipe_digest
            and not reregister
        ):
            retire(previous)
            return previous
        if reregister and previous.get("status") == "template-ready":
            if previous.get("template_reregister_generation") == args.reregister_generation:
                return previous
            if previous.get("recipe_digest") != recipe_digest:
                raise RuntimeError(
                    f"{case_id}: completed state uses a different collector recipe; "
                    "refresh it normally before re-registering its template"
                )
            source_template_id = str(previous.get("new_template_id") or "")
            if not source_template_id or not previous.get("new_image"):
                raise RuntimeError(
                    f"{case_id}: completed image/template state is unavailable for re-registration"
                )
            previous["template_reregister_source_template_id"] = source_template_id
            previous["template_reregister_generation"] = args.reregister_generation
            previous["template_retry_index"] = int(
                previous.get("template_retry_index") or 0
            ) + 1
            previous.pop("new_template_id", None)
            previous.pop("build_id", None)
            previous.pop("error", None)
            previous["status"] = "image-published"
        if (
            previous.get("status") == "failed"
            and str(previous.get("error") or "").startswith("template build failed:")
        ):
            previous.pop("new_template_id", None)
            previous.pop("build_id", None)
            previous.pop("error", None)
            previous["template_retry_index"] = int(
                previous.get("template_retry_index") or 0
            ) + 1
        record = {
            **previous,
            "case_id": case_id,
            "status": "started",
            "attempts": int(previous.get("attempts") or 0) + 1,
            "started_at": time.time(),
            "recipe_digest": recipe_digest,
            "old_image": old_image,
            "old_image_digest": old_sandbox.get("image_digest"),
            "old_template_id": old_template,
            "collector_revision": args.clawtune_revision,
            "collector_manifest_sha256": manifest["manifest_sha256"],
            "architecture": old_sandbox.get("architecture"),
            "testbed_identity": {
                "git_head": (task.get("metadata") or {}).get("environment_git_head"),
                "source_manifest": (task.get("metadata") or {}).get("source_image_manifest"),
                "source_sha256": (task.get("metadata") or {}).get("source_sha256"),
                "preflight_command": old_sandbox.get("preflight_command"),
            },
        }
        save(case_id, record)
        try:
            storage_safe()
            tag = f"{args.registry}:{safe_name(case_id)}-{args.clawtune_revision[:12]}"
            if not record.get("new_image"):
                with tempfile.TemporaryDirectory(prefix="clawbox-collector-overlay-") as temp:
                    context = Path(temp)
                    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
                    with (logs / f"{safe_name(case_id)}.log").open("a", encoding="utf-8") as log:
                        subprocess.run(
                            ["docker", "build", "--platform", "linux/arm64", "--pull=false",
                             "--build-arg", f"BASE_IMAGE={old_image}", "-t", tag, str(context)],
                            stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800,
                        )
                        subprocess.run(
                            ["docker", "push", tag], stdout=log, stderr=subprocess.STDOUT,
                            check=True, timeout=1800,
                        )
                record["new_image"] = immutable_digest(tag)
                record["new_image_digest"] = record["new_image"].rsplit("@", 1)[-1]
                record["status"] = "image-published"
                save(case_id, record)

            retry_index = int(record.get("template_retry_index") or 0)
            alias_material = case_id + recipe_digest
            if retry_index:
                alias_material += f":retry:{retry_index}"
            alias = "slat-" + hashlib.sha256(
                alias_material.encode()
            ).hexdigest()[:20]
            template_id = str(record.get("new_template_id") or "")
            build_id = str(record.get("build_id") or "")
            reused_ready_template = False
            if not template_id:
                matches = [item for item in read_with_backoff(
                    Template.list, label="collector refresh template reconciliation"
                ) if item.name == alias]
                if len(matches) > 1:
                    raise RuntimeError(f"ambiguous existing template alias: {alias}")
                if matches:
                    template_id = matches[0].template_id
                    reused_ready_template = str(matches[0].status).lower() in READY
                else:
                    built = Template.build(
                        name=alias, image=record["new_image"], nodes=[args.node],
                        cpu_count=int(old_sandbox.get("vcpu") or 2) * 1000,
                        memory_mb=int(old_sandbox.get("memory_mib") or 3072),
                        writable_layer_size="20G", exposed_ports=[49983, 2222],
                        probe_port=49983,
                        command=["/usr/local/bin/cube-tool-entrypoint.sh"],
                    )
                    template_id = built.template_id
                    build_id = built.build_id
                    record["build_id"] = build_id
                record["new_template_id"] = template_id
                record["status"] = "template-building"
                save(case_id, record)

            deadline = time.monotonic() + 1800
            if not reused_ready_template:
                if not build_id:
                    raise RuntimeError(
                        f"template {template_id} is not READY and has no resumable build ID"
                    )
                while True:
                    build = read_with_backoff(
                        lambda: Template.get_build_status(template_id, build_id),
                        label="collector refresh template build status",
                    )
                    status = str(build.status).lower()
                    if status in READY:
                        break
                    if status in FAILED:
                        detail = getattr(build, "error_message", None) or getattr(
                            build, "message", None
                        )
                        raise RuntimeError(f"template build failed: {detail or build!r}")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"template did not become READY in 1800s: {template_id}"
                        )
                    time.sleep(5)
            info = read_with_backoff(
                lambda: Template.get(template_id), label="collector refresh ready template",
            )
            kernels = {str(item.get("kernel_version") or "") for item in info.replicas or []}
            expected_kernels = {args.expected_kernel_version}
            if reregister:
                source_template_id = str(
                    record.get("template_reregister_source_template_id") or ""
                )
                if not source_template_id:
                    raise RuntimeError(
                        f"{case_id}: re-registration source template is missing from saved state"
                    )
                source_info = read_with_backoff(
                    lambda: Template.get(source_template_id),
                    label="collector refresh replaced template identity",
                )
                expected_kernels = {
                    str(item.get("kernel_version") or "")
                    for item in source_info.replicas or []
                }
                if not expected_kernels or expected_kernels == {""}:
                    raise RuntimeError(
                        f"{case_id}: replaced template has no kernel identity"
                    )
            if kernels != expected_kernels:
                raise RuntimeError(
                    f"template kernel mismatch: observed={sorted(kernels)} "
                    f"expected={sorted(expected_kernels)}"
                )
            if str(info.image_info) != record["new_image"]:
                raise RuntimeError("template image provenance differs from the published digest")
            record.update(status="template-ready", finished_at=time.time())
            record.pop("error", None)
            save(case_id, record)
            retire(record)
            print(case_id, record["status"], template_id, flush=True)
            return record
        except Exception as exc:
            record.update(status="failed", error=str(exc)[:2000], finished_at=time.time())
            save(case_id, record)
            print(case_id, "failed", record["error"], flush=True)
            if args.fail_fast:
                raise
            return record

    batch_size = args.batch_size or len(tasks)
    records: list[dict[str, Any]] = []
    for start in range(0, len(tasks), batch_size):
        batch = tasks[start:start + batch_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            records.extend(pool.map(update, batch))
        finished = start + len(batch)
        print(f"collector refresh batch complete: {finished}/{len(tasks)}", flush=True)
        if args.prune_build_cache_between_batches and finished < len(tasks):
            prune_log = logs / "docker-build-cache-prune.log"
            print("pruning unused Docker build cache between batches", flush=True)
            with prune_log.open("a", encoding="utf-8") as log:
                log.write(f"\n=== after {finished}/{len(tasks)} tasks at {time.time()} ===\n")
                subprocess.run(
                    ["docker", "builder", "prune", "--all", "--force"],
                    stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800,
                )
            storage_safe()

    failed = [item for item in records if item.get("status") != "template-ready"]
    if failed:
        print(f"collector refresh incomplete: {len(failed)} of {len(records)} failed", flush=True)
        return 1

    refreshed = copy.deepcopy(workload)
    by_case = {str(item["case_id"]): item for item in records}
    for task in refreshed["tasks"]:
        record = by_case[str(task["case_id"])]
        sandbox = task["sandbox"]
        sandbox["template_id"] = record["new_template_id"]
        sandbox["source_image_reference"] = record["new_image"]
        sandbox["image_digest"] = record["new_image_digest"]
        metadata = task.setdefault("metadata", {})
        metadata["previous_cube_template_id"] = record["old_template_id"]
        metadata["previous_environment_image_digest"] = record["old_image_digest"]
        metadata["cube_template_id"] = record["new_template_id"]
        metadata["environment_image_id"] = record["new_image_digest"]
        metadata["collector_receipt"] = {
            "schema_version": 2,
            "architecture": sandbox.get("architecture"),
            "clawtune_revision": args.clawtune_revision,
            "collector_manifest_sha256": manifest["manifest_sha256"],
            "source_image": collector_image,
        }
        metadata["readiness"] = "collector-template-ready-unqualified"
    output_workload = args.output_dir / "workload-collector-refreshed.json"
    atomic_json(output_workload, refreshed)
    catalog = {
        "schema_version": 1,
        "generated_at": time.time(),
        "case_count": len(records),
        "recipe_digest": recipe_digest,
        "collector_package_manifest": str(args.output_dir / "collector-package-manifest.json"),
        "workload": str(output_workload),
        "workload_sha256": hashlib.sha256(output_workload.read_bytes()).hexdigest(),
        "records": records,
    }
    atomic_json(args.output_dir / "catalog.json", catalog)
    print(f"collector refresh complete: {len(records)} templates; workload={output_workload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
