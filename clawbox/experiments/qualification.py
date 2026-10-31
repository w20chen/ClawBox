"""Short, real CubeSandbox qualification before a formal experiment."""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
import os
import hashlib
import json
import shutil
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Any

from clawbox.cube import (
    CubeSandboxClient, CubeSandboxLifecycle, OwnedSandboxJournal, Ownership,
)
from clawbox.lab import apply_environment, doctor
from .results import RunStatus
from .inputs import _saved_trace_info, validate_inputs, validate_launch_inputs
from .prediction import prediction_input_paths
from .snapshot_pool import WarmSnapshotPool
from .numa_borrow import SandboxNumaBorrower, read_numa_lru_bytes
from .spec import (
    ExperimentSpec, InferenceBackend, ReclamationPolicy, canonical_json,
    expand_matrix, load_workload_cases, resolve_case_sandbox, spec_digest,
)
from .spec_types import AdmissionPolicy, ArrivalSchedule
from .supervisor import ExperimentSupervisor, process_is_alive, process_identity, terminate_process_tree
from .worker import atomic_json


QUALIFICATION_SCHEMA_VERSION = 15
_TEMPLATE_PROBE_TRANSIENT_RETRY_LIMIT = 2


def _retryable_template_probe_error(exc: BaseException) -> bool:
    """Recognize Cube's explicit failed-create transport errors, not ambiguous I/O."""
    message = str(exc)
    return (
        type(exc).__name__ == "ApiError"
        and (
            (
                "ttrpc err:" in message
                and (
                    "Receive packet timeout" in message
                    or "Send packet to sender error" in message
                )
            )
            or "Recv len invalid:0" in message
        )
    )


def qualification_input_manifest(
    spec: ExperimentSpec, *, validated_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a receipt to the exact mutable files used by the specification."""
    inputs = validated_inputs if validated_inputs is not None else validate_inputs(spec)
    sharing_artifact = spec.resources.template_base_sharing_artifact
    return {
        "traces": [
            {
                "case_id": item["case_id"],
                "path": item["path"],
                "sha256": item["sha256"],
            }
            for item in inputs["traces"]
        ],
        "prediction_files": [
            {
                "path": value,
                "sha256": hashlib.sha256(Path(value).read_bytes()).hexdigest(),
            }
            for value in inputs["prediction_files"]
        ],
        "template_base_sharing_artifact": (
            {
                "path": sharing_artifact,
                "sha256": hashlib.sha256(Path(sharing_artifact).read_bytes()).hexdigest(),
            }
            if sharing_artifact else None
        ),
    }


def current_qualification_input_manifest(spec: ExperimentSpec) -> dict[str, Any]:
    """Hash receipt-bound inputs without repeating semantic prediction checks.

    A successful receipt already records that the same implementation parsed
    every trace and resolved every command.  Reuse only needs to prove that the
    specification and each mutable input byte are unchanged.
    """
    if spec.schema_version == 4:
        from .spec import effective_spec

        manifests = [
            current_qualification_input_manifest(effective_spec(spec, policy))
            for policy in spec.policies
        ]
        traces = manifests[0]["traces"]
        prediction_files = sorted(
            {
                item["path"]: item
                for manifest in manifests
                for item in manifest["prediction_files"]
            }.values(),
            key=lambda item: item["path"],
        )
    else:
        traces = []
        if spec.inference.backend is InferenceBackend.REPLAY:
            for case in load_workload_cases(spec.workload):
                path = Path(case.replay_trace_reference or case.source_reference)
                saved = _saved_trace_info(case, path)
                traces.append({
                    "case_id": case.case_id,
                    "path": str(path),
                    "sha256": (
                        saved["sha256"]
                        if saved is not None
                        else hashlib.sha256(path.read_bytes()).hexdigest()
                    ),
                })
        prediction_files = []
        admissions = {policy.admission for policy in spec.policies}
        if (
            spec.resources.prediction_artifact
            and (
                AdmissionPolicy.PREDICTED in admissions
                or spec.resources.observe_predictions
                or spec.resources.prediction_mode == 'host-live'
            )
        ):
            path = Path(spec.resources.prediction_artifact)
            paths = prediction_input_paths(path, spec.resources.prediction_mode)
            prediction_files = [
                {
                    "path": str(value),
                    "sha256": hashlib.sha256(value.read_bytes()).hexdigest(),
                }
                for value in paths
            ]
    sharing_artifact = spec.resources.template_base_sharing_artifact
    return {
        "traces": traces,
        "prediction_files": prediction_files,
        "template_base_sharing_artifact": (
            {
                "path": sharing_artifact,
                "sha256": hashlib.sha256(Path(sharing_artifact).read_bytes()).hexdigest(),
            }
            if sharing_artifact else None
        ),
    }


def implementation_digest() -> str:
    """Return the maintained capability-contract version identity.

    Receipt compatibility is intentionally explicit.  Changes to VM execution,
    ToolBridge/policy handling, prediction integration, or qualification must
    bump ``QUALIFICATION_SCHEMA_VERSION``.  Unrelated CLI, reporting, test, and
    documentation edits must not make users rerun hundreds of real-VM smokes.
    """
    return hashlib.sha256(
        f"clawbox-qualification-contract-v{QUALIFICATION_SCHEMA_VERSION}".encode()
    ).hexdigest()


def qualification_contract_digest(spec: ExperimentSpec) -> str:
    """Identify capability inputs while excluding formal scheduling identity."""
    raw = spec.model_dump(mode="json")
    raw.pop("experiment_id", None)
    raw.pop("validation", None)
    raw.pop("design", None)
    raw.pop("suite", None)
    raw.pop("suite_ablations", None)
    workload = raw.get("workload") or {}
    workload.pop("repetitions", None)
    execution = raw.get("execution") or {}
    for key in (
        "concurrency_levels",
        "randomized_order",
        "random_seed",
        "stabilization_seconds",
        "arrival_schedule",
        "stagger_interval_seconds",
        "poisson_arrival_window_fraction",
        "poisson_arrival_window_seconds",
        "sandbox_create_concurrency",
        "dependency_install_concurrency",
    ):
        execution.pop(key, None)
    configuration = (raw.get("inference") or {}).get("configuration") or {}
    configuration.pop("time_scale", None)
    configuration.pop("max_model_steps", None)
    # Notification wait bounds change observation overhead, not VM capability.
    configuration.pop("runtime_completion_watch_seconds", None)
    resources = raw.get("resources") or {}
    # These bound concurrent work and queue wait; they do not change whether
    # the already-qualified snapshot and NUMA operations are supported.
    resources.pop("pool_transfer_concurrency", None)
    resources.pop("snapshot_transfer_concurrency", None)
    resources.pop("pool_transfer_timeout_seconds", None)
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


def default_receipt_path(spec_path: Path) -> Path:
    return spec_path.with_name(spec_path.name + ".qualification.json")


def host_profile_for(spec: ExperimentSpec) -> dict[str, Any]:
    def sandbox(value: Any) -> dict[str, Any]:
        return {"template_id": value.template, "memory_mib": value.memory_mib,
                "vcpu": value.vcpu}

    # A conservative envelope: every offered session may own both VMs. This
    # checks Cube's independent quota, not a claim about resident concurrency.
    tools = [
        resolve_case_sandbox(case, spec.sandbox)
        for case in load_workload_cases(spec.workload)
    ]
    count = max(spec.execution.concurrency_levels)
    demand = dict(
        vm_count=count * 2,
        cpu_milli=count * (spec.runtime.vcpu + max(t.vcpu for t in tools)) * 1000,
        memory_mib=count * (spec.runtime.memory_mib + max(t.memory_mib for t in tools)),
        max_vm_cpu_milli=max(spec.runtime.vcpu, *(t.vcpu for t in tools)) * 1000,
        max_vm_memory_mib=max(spec.runtime.memory_mib, *(t.memory_mib for t in tools)),
    )
    return {
        "cube_capacity_demand": demand,
        "cubelet_storage_root": spec.resources.cubelet_storage_root,
        "cube_cow_headroom_per_vm_mib": (
            spec.resources.cube_cow_headroom_per_vm_mib
        ),
        "node": spec.resources.target_node,
        "compute_nodes": [n.model_dump() for n in spec.resources.compute_nodes],
        "runtime": sandbox(spec.runtime),
        "sandbox": sandbox(spec.sandbox),
        "tool_sandboxes": [
            sandbox(resolve_case_sandbox(case, spec.sandbox))
            for case in load_workload_cases(spec.workload)
        ],
        "local_memory_cgroup": spec.resources.local_memory_cgroup,
        "local_memory_capacity_mib": spec.resources.local_memory_capacity_mib,
        "local_memory_low_watermark_mib": (
            spec.resources.local_memory_low_watermark_mib
        ),
        "local_memory_high_watermark_mib": (
            spec.resources.local_memory_high_watermark_mib
        ),
        "shared_memory_borrow_limit_mib": (
            spec.resources.shared_memory_borrow_limit_mib
        ),
        "local_numa_node": spec.resources.local_numa_node,
        "warm_root": spec.resources.warm_snapshot_root,
        "warm_capacity_mib": spec.resources.warm_memory_capacity_mib,
        "warm_numa_node": spec.resources.warm_numa_node,
    }


def require_empty_vm_domain(path: str | Path) -> None:
    """Fail early when stale Cube processes would invalidate a new arm."""
    root = Path(path)
    events_path = root / "cgroup.events"
    if not events_path.is_file():
        return
    events = dict(
        line.split(maxsplit=1)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if len(line.split(maxsplit=1)) == 2
    )
    if events.get("populated") != "0":
        raise RuntimeError(
            f"physical VM domain {root} is populated before qualification; "
            "inspect CubeAPI sandboxes and descendant cgroup.procs, clean only "
            "the owning stale run, verify cgroup.events populated=0, then retry"
        )


def require_host_ready(spec: ExperimentSpec, *, current_images: bool = True) -> None:
    require_empty_vm_domain(spec.resources.local_memory_cgroup)
    from .pressure import PressureControl
    # Check all requested arms before any VM qualification probes or trial runs.
    for policy in spec.policies:
        if policy.runtime_control.adaptive:
            PressureControl(
                spec.resources.local_memory_cgroup, mode=policy.runtime_control,
                options=spec.resources.pressure_control,
                memory_protection=policy.memory_protection,
                cpu_release_boost=policy.cpu_release_boost,
            ).check_prerequisites()
    if spec.schema_version == 4:
        from .spec import ResourcesSpec
        # Host configuration is common, regardless of an arm disabling borrowing.
        # Do not carry an OS-only arm's derived lifecycle policy into this
        # physical profile: the common environment can still define a shared
        # NUMA pool even though that arm never borrows from it.
        physical = ResourcesSpec.model_validate({
            **spec.resources.model_dump(mode='json'),
            'lifecycle_admission': 'configured-capacity',
            'resident_capacity_accounting': False, 'lifecycle_reservation_scale': 1.0,
            **spec.design['environment']['resources'],
        })
        spec = spec.model_copy(update={'schema_version': 3, 'resources': physical})
    profile = host_profile_for(spec)
    apply_environment(profile)
    sibling_clawtune = Path(__file__).resolve().parents[3] / "ClawTune"
    if sibling_clawtune.is_dir():
        os.environ.setdefault("CLAWTUNE_ROOT", str(sibling_clawtune))
    warm = spec.resources.warm_memory_capacity_mib > 0 and any(
        policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
        for policy in spec.policies
    )
    failures = doctor(profile, warm=warm, current_images=current_images)
    if failures:
        raise RuntimeError("experiment host checks failed: " + ", ".join(failures))


def supervision_fault_test() -> dict[str, Any]:
    """Prove this host can forcibly terminate a wedged worker tree."""
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=(os.name != "nt"),
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt" else 0
        ),
    )
    identity = process_identity(process.pid)
    terminate_process_tree(process.pid, grace_seconds=2)
    if process_is_alive(identity):
        raise RuntimeError("worker termination fault test left a live process")
    return {"passed": True, "duration_seconds": time.monotonic() - started}


def _node_spec(spec, node):
    return spec.model_copy(update={"resources": spec.resources.model_copy(update={
        "compute_nodes": (), "local_numa_node": node.numa_node,
        "local_memory_capacity_mib": node.memory_capacity_mib,
        "local_memory_low_watermark_mib": node.low_watermark_mib,
        "local_memory_high_watermark_mib": node.high_watermark_mib,
        "pool_memory_budget_mib": spec.resources.compute_node_budget_mib(node),
    })})


def _snapshot_sandbox_representatives(
    spec: ExperimentSpec,
) -> tuple[int, dict[str, Any]]:
    """Pick one Tool template for each snapshot-relevant VM shape.

    Qualification later creates and executes every workload template. Repeating
    the same backend checkpoint round-trip for hundreds of task-specific images
    adds no mechanism coverage; memory size, CPU shape, and architecture are the
    relevant configured differences.
    """
    by_template: dict[str, Any] = {}
    for case in load_workload_cases(spec.workload):
        sandbox = resolve_case_sandbox(case, spec.sandbox)
        by_template.setdefault(sandbox.template, sandbox)
    representatives: dict[tuple[int, int, str], tuple[str, Any]] = {}
    for template, sandbox in sorted(by_template.items()):
        shape = (sandbox.memory_mib, sandbox.vcpu, sandbox.architecture)
        representatives.setdefault(shape, (template, sandbox))
    return len(by_template), {
        template: sandbox for template, sandbox in representatives.values()
    }


def snapshot_roundtrip_test(spec: ExperimentSpec, *, artifact_root: Path, _node=None,
                            _template: str | None = None) -> dict[str, Any]:
    if spec.schema_version == 4:
        return design_probes(spec, snapshot_roundtrip_test, artifact_root=artifact_root)
    """Exercise checkpoint/restore and placement on every compute node."""
    if _template is None:
        observed_template_count, sandboxes = _snapshot_sandbox_representatives(spec)
        if observed_template_count > 1 or (
            sandboxes and next(iter(sandboxes)) != spec.sandbox.template
        ):
            templates = {
                template: snapshot_roundtrip_test(
                    spec.model_copy(update={"sandbox": sandbox}),
                    artifact_root=artifact_root / template,
                    _template=template,
                )
                for template, sandbox in sandboxes.items()
            }
            result = {
                "required": any(item["required"] for item in templates.values()),
                "passed": all(item["passed"] for item in templates.values()),
                "templates": templates,
                "observed_template_count": observed_template_count,
                "representative_template_count": len(templates),
                "representative_basis": [
                    "memory_mib", "vcpu", "architecture",
                ],
                "all_templates_exercised_by_qualification_replay": True,
                "mechanism": spec.resources.snapshot_mechanism,
                "storage": spec.resources.snapshot_storage,
                "incremental_delta_verified": all(
                    item.get("incremental_delta_verified", False)
                    for item in templates.values()
                ),
                "cold_relocation_verified": all(
                    item.get("cold_relocation_verified") is True
                    for item in templates.values()
                ) if spec.resources.snapshot_storage == "tiered"
                and spec.resources.warm_memory_capacity_mib > 0 else None,
            }
            artifact_root.mkdir(parents=True, exist_ok=True)
            atomic_json(artifact_root / "result.json", result)
            return result
    if spec.resources.compute_nodes:
        nodes = {node.node_id: snapshot_roundtrip_test(
            _node_spec(spec, node), artifact_root=artifact_root / node.node_id,
            _node=node, _template=_template or spec.sandbox.template,
        ) for node in spec.resources.compute_nodes}
        result = {"required": any(r["required"] for r in nodes.values()),
                  "passed": all(r["passed"] for r in nodes.values()), "nodes": nodes,
                  "mechanism": spec.resources.snapshot_mechanism, "storage": spec.resources.snapshot_storage,
                  "incremental_delta_verified": all(r.get("incremental_delta_verified", False) for r in nodes.values()),
                  "cold_relocation_verified": all(
                      r.get("cold_relocation_verified") is True for r in nodes.values()
                  ) if spec.resources.snapshot_storage == "tiered"
                  and spec.resources.warm_memory_capacity_mib > 0 else None}
        atomic_json(artifact_root / "result.json", result)
        return result
    required = any(
        policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
        for policy in spec.policies
    )
    if not required:
        return {"required": False, "passed": True}
    probe_id = "qualification-snapshot-" + uuid.uuid4().hex[:12]
    artifact_root.mkdir(parents=True, exist_ok=False)
    journal = OwnedSandboxJournal(artifact_root / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal)
    ownership = Ownership(
        run_id=probe_id,
        attempt_id="snapshot-roundtrip",
        task_uid=probe_id,
        experiment_id=spec.experiment_id,
        session_id=probe_id,
        policy_name="qualification-snapshot-roundtrip",
    )
    pool = (
        WarmSnapshotPool(spec.resources.warm_memory_capacity_mib * 1024 * 1024,
                         transfer_concurrency=spec.resources.pool_transfer_concurrency,
                         snapshot_transfer_concurrency=spec.resources.snapshot_transfer_concurrency,
                         transfer_timeout_seconds=spec.resources.pool_transfer_timeout_seconds)
        if spec.resources.warm_memory_capacity_mib > 0 else None
    )
    def make_lifecycle(sandbox, role):
        return CubeSandboxLifecycle(
            client,
            template=sandbox.template,
            node_name=spec.resources.target_node,
            ownership=ownership,
            allow_internet_access=sandbox.allow_internet_access,
            role=role,
            warm_snapshot_root=spec.resources.warm_snapshot_root,
            cold_snapshot_root=spec.resources.cold_snapshot_root,
            snapshot_pool=pool,
            default_snapshot_tier=spec.resources.default_snapshot_tier,
            snapshot_reservation_bytes=(sandbox.memory_mib + 256) * 1024 * 1024,
            snapshot_mechanism=spec.resources.snapshot_mechanism,
            snapshot_storage=spec.resources.snapshot_storage,
            lazy_restore=spec.resources.lazy_restore,
            numa_borrower=SandboxNumaBorrower(
                Path(spec.resources.local_memory_cgroup), local_node=spec.resources.local_numa_node,
                shared_node=spec.resources.warm_numa_node, shared_pool=pool,
                cpus=_node.cpus if _node else None, node_id=_node.node_id if _node else None,
                cleanup_timeout_seconds=spec.resources.pool_transfer_timeout_seconds,
            ) if spec.resources.shared_memory_borrow_limit_mib is not None else None,
        )
    lifecycle = make_lifecycle(spec.sandbox, "tool")
    pair_policy = next((policy for policy in spec.policies
                       if policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                       and policy.runtime_snapshot), None)
    runtime = make_lifecycle(spec.runtime, "runtime") if pair_policy else None
    coordinator = None
    runtime_pid = None
    primary_error: BaseException | None = None
    incremental_delta_verified = False
    cold_relocation_verified: bool | None = None
    try:
        lifecycle.start()
        before = lifecycle.sandbox.commands.run("printf qualification-before", timeout=30)
        if before.exit_code or before.stdout != "qualification-before":
            raise RuntimeError("snapshot probe could not execute before checkpoint")
        if runtime is not None:
            from .policy import PolicyCoordinator
            runtime.start()
            process = runtime.sandbox.commands.run(
                "nohup sleep 600 >/dev/null 2>&1 & echo $!", timeout=30)
            runtime_pid = process.stdout.strip()
            if process.exit_code or not runtime_pid.isdigit():
                raise RuntimeError("Runtime snapshot probe could not start PID witness")
            coordinator = PolicyCoordinator(pair_policy,
                budget_mib=spec.resources.pool_memory_budget_mib,
                emergency_free_mib=0, operation_headroom_mib=0)
            coordinator.register(probe_id, lifecycle)
            coordinator.register_runtime(probe_id, runtime)
        generations = 2 if spec.resources.snapshot_mechanism == "incremental-cow" else 1
        for generation in range(1, generations + 1):
            marker = f"qualification-state-{generation}"
            lifecycle.sandbox.files.write("/tmp/clawbox-qualification-state", marker)
            if coordinator is not None:
                coordinator.begin_model_wait(probe_id, str(generation))
                if not coordinator.evict_one_for_watermark():
                    raise RuntimeError("snapshot probe cannot reserve capacity for the configured VM pair")
                if lifecycle.resident or runtime.resident:
                    raise RuntimeError("pair checkpoint left a VM resident")
            else:
                lifecycle.checkpoint_and_evict()
            if (
                generation == generations
                and spec.resources.snapshot_storage == "tiered"
                and pool is not None
                and spec.resources.snapshot_destination != 'cold'
            ):
                committed = int(pool.snapshot()["committed_bytes"])
                if committed <= 0 or committed > pool.capacity_bytes:
                    raise RuntimeError("WARM snapshot pool has invalid committed usage")
                # Force the authoritative final WARM generation through the
                # production spill callback. This specifically proves that an
                # external template RAM Base is materialized in self-contained
                # COLD lineage before the volatile WARM files are removed.
                pool.spill_for_admission(pool.capacity_bytes - committed + 1)
                cold_relocation_verified = True
            if coordinator is not None:
                coordinator.complete_model_wait(probe_id, str(generation))
                runtime.restore()
                alive = runtime.sandbox.commands.run(f"kill -0 {runtime_pid}", timeout=30)
                if alive.exit_code:
                    raise RuntimeError("Runtime PID did not survive pair checkpoint/restore")
                if lifecycle.resident:
                    raise RuntimeError("Runtime response restore unexpectedly restored Tool")
            lifecycle.restore()
            if lifecycle.sandbox.files.read("/tmp/clawbox-qualification-state") != marker:
                raise RuntimeError(
                    f"snapshot probe lost guest state after generation {generation}"
                )
        if spec.resources.snapshot_mechanism == "incremental-cow":
            checkpoints = [
                item for item in lifecycle.timings
                if item.get("operation") == "checkpoint"
                and item.get("status") == "ok"
            ]
            if len(checkpoints) != 2:
                raise RuntimeError("incremental snapshot probe did not publish two generations")
            metrics = checkpoints[-1].get("snapshot_metrics") or {}
            logical = metrics.get("logical_bytes")
            transferred = metrics.get("transferred_bytes")
            incremental_delta_verified = (
                metrics.get("full_base") is False
                and int(metrics.get("lineage_layers", 0)) >= 2
                and type(logical) is int and logical > 0
                and type(transferred) is int and 0 <= transferred < logical
            )
            if not incremental_delta_verified:
                raise RuntimeError(
                    "second checkpoint was not a smaller incremental RAM delta"
                )
    except BaseException as exc:
        primary_error = exc
    cleanup_errors: list[str] = []
    for vm in (runtime, lifecycle):
        if vm is not None:
            try:
                vm.close()
            except Exception as exc:
                cleanup_errors.append(f"{vm.role} lifecycle close: {type(exc).__name__}: {exc}")
    try:
        client.kill_owned_sandboxes(probe_id)
    except Exception as exc:
        cleanup_errors.append(f"owned cleanup: {type(exc).__name__}: {exc}")
    remaining = client.list_owned_sandboxes(probe_id)
    if remaining:
        cleanup_errors.append(f"owned sandboxes remain: {len(remaining)}")
    atomic_json(artifact_root / "result.json", {
        "required": True,
        "passed": primary_error is None and not cleanup_errors,
        "mechanism": spec.resources.snapshot_mechanism,
        "storage": spec.resources.snapshot_storage,
        "incremental_delta_verified": incremental_delta_verified,
        "cold_relocation_verified": cold_relocation_verified,
        "timings": lifecycle.timings,
        "pair_checkpoint_verified": bool(runtime is not None and primary_error is None),
        "runtime_pid": runtime_pid,
        "runtime_timings": runtime.timings if runtime is not None else [],
        "placements": lifecycle.numa_borrower.placements if lifecycle.numa_borrower else [],
        "failure": (
            f"{type(primary_error).__name__}: {primary_error}"
            if primary_error is not None else None
        ),
        "cleanup_errors": cleanup_errors,
    })
    if primary_error is not None:
        if cleanup_errors:
            primary_error.add_note("snapshot probe cleanup: " + "; ".join(cleanup_errors))
        if isinstance(primary_error, Exception):
            raise RuntimeError(
                f"snapshot qualification failed: {primary_error}. "
                f"Inspect {artifact_root / 'result.json'} and its cleanup_errors. "
                "Verify snapshot paths/capacity and the installed CubeSandbox patch set "
                "(docs/installation.md), then rerun experiment qualify. "
                "An external-Base WARM-to-COLD failure requires a backend that "
                "materializes the template Base; do not bypass qualification."
            ) from primary_error
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("snapshot probe cleanup failed: " + "; ".join(cleanup_errors))
    return {
        "required": True,
        "passed": True,
        "mechanism": spec.resources.snapshot_mechanism,
        "storage": spec.resources.snapshot_storage,
        "incremental_delta_verified": incremental_delta_verified,
        "cold_relocation_verified": cold_relocation_verified,
        "timings": lifecycle.timings,
        "pair_checkpoint_verified": bool(runtime is not None and primary_error is None),
        "runtime_pid": runtime_pid,
        "runtime_timings": runtime.timings if runtime is not None else [],
        "placements": lifecycle.numa_borrower.placements if lifecycle.numa_borrower else [],
        "artifact_root": str(artifact_root),
    }


def _capture_borrow_residency(borrower, sandbox_id, destination):
    """Save raw host evidence independently of a successful rebind response."""
    leaf = borrower._leaf_for(sandbox_id)
    destination.mkdir(parents=True)
    for name in ('memory.numa_stat', 'memory.current', 'cpuset.cpus.effective',
                 'cpuset.mems.effective', 'cgroup.procs'):
        (destination/name).write_text((leaf/name).read_text())
    pids = (leaf/'cgroup.procs').read_text().split()
    for pid in pids:
        for name in ('numa_maps', 'status'):
            try:
                (destination/f'{pid}-{name}').write_text(Path(f'/proc/{pid}/{name}').read_text())
            except (FileNotFoundError, ProcessLookupError):
                pass
    numastat = shutil.which('numastat')
    available = numastat is not None and bool(pids)
    if available:
        result = subprocess.run([numastat, '-p', *pids], capture_output=True, text=True, timeout=30)
        (destination/'numastat.txt').write_text(result.stdout+result.stderr)
        available = result.returncode == 0
    resident, unattributed = read_numa_lru_bytes(leaf)
    return {'resident_bytes': resident, 'unattributed_bytes': unattributed,
            'numastat_available': available, 'artifact_root': str(destination)}


def _borrow_allocation_command(probe_id: str, maximum_mib: int) -> str:
    import shlex
    return (
        "free=$(df -Pm /dev/shm | awk 'NR==2{print $4}'); "
        "test -n \"$free\" && test \"$free\" -ge 5 || exit 1; "
        f"count=$((free-4)); test \"$count\" -gt {maximum_mib} && count={maximum_mib}; "
        f"dd if=/dev/zero of={shlex.quote('/dev/shm/' + probe_id)} bs=1048576 count=\"$count\" conv=excl >/dev/null && "
        "printf 'CLAWBOX_ALLOCATION_MIB=%s\\n' \"$count\""
    )


def live_borrow_roundtrip_test(
    spec: ExperimentSpec, *, artifact_root: Path, _node=None,
) -> dict[str, Any]:
    if spec.schema_version == 4:
        return design_probes(spec, live_borrow_roundtrip_test, artifact_root=artifact_root)
    """Prove a VM on each compute node can borrow and return without interruption."""
    limit_mib = spec.resources.shared_memory_borrow_limit_mib
    if not limit_mib:
        return {"required": False, "passed": True, "borrow_limit_bytes": 0,
                "shared_capacity_bytes": spec.resources.warm_memory_capacity_mib * 1024 * 1024}
    if spec.resources.compute_nodes:
        nodes = {node.node_id: live_borrow_roundtrip_test(
            _node_spec(spec, node), artifact_root=artifact_root / node.node_id, _node=node,
        ) for node in spec.resources.compute_nodes}
        result = {"required": bool(spec.resources.shared_memory_borrow_limit_mib),
                  "passed": all(r["passed"] for r in nodes.values()), "nodes": nodes,
                  "borrow_limit_bytes": spec.resources.shared_memory_borrow_limit_mib * 1024 * 1024,
                  "shared_capacity_bytes": spec.resources.warm_memory_capacity_mib * 1024 * 1024}
        atomic_json(artifact_root / "result.json", result)
        return result
    probe_id = "qualification-borrow-" + uuid.uuid4().hex[:12]
    artifact_root.mkdir(parents=True, exist_ok=False)
    journal = OwnedSandboxJournal(artifact_root / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal)
    shared_pool = WarmSnapshotPool(
        spec.resources.warm_memory_capacity_mib * 1024 * 1024,
        borrow_capacity_bytes=limit_mib * 1024 * 1024,
        transfer_concurrency=spec.resources.pool_transfer_concurrency,
        snapshot_transfer_concurrency=spec.resources.snapshot_transfer_concurrency,
        transfer_timeout_seconds=spec.resources.pool_transfer_timeout_seconds,
    )
    borrower = SandboxNumaBorrower(
        Path(spec.resources.local_memory_cgroup),
        local_node=spec.resources.local_numa_node,
        shared_node=spec.resources.warm_numa_node,
        shared_pool=shared_pool,
        cpus=_node.cpus if _node else None, node_id=_node.node_id if _node else None,
        cleanup_timeout_seconds=spec.resources.pool_transfer_timeout_seconds,
    )
    lifecycle = CubeSandboxLifecycle(
        client,
        template=spec.sandbox.template,
        node_name=spec.resources.target_node,
        ownership=Ownership(
            run_id=probe_id, attempt_id="live-borrow-roundtrip",
            task_uid=probe_id, experiment_id=spec.experiment_id,
            session_id=probe_id, policy_name="qualification-live-borrow",
        ),
        allow_internet_access=spec.sandbox.allow_internet_access,
        role="tool", numa_borrower=borrower,
        configured_memory_bytes=spec.sandbox.memory_mib * 1024 * 1024,
    )
    primary_error: BaseException | None = None
    borrow_record = return_record = reborrow_record = second_return_record = None
    observations = {}
    allocation_mib = min(128, max(1, spec.sandbox.memory_mib // 16))
    try:
        lifecycle.start()
        before = lifecycle.sandbox.commands.run("printf local-ready", timeout=30)
        if before.exit_code or before.stdout != "local-ready":
            raise RuntimeError("live-borrow probe could not execute on LOCAL")
        observations['local'] = _capture_borrow_residency(borrower, lifecycle.sandbox_id, artifact_root/'local')
        borrow_record = lifecycle.borrow_shared()
        during = lifecycle.sandbox.commands.run("printf shared-alive", timeout=30)
        if during.exit_code or during.stdout != "shared-alive":
            raise RuntimeError("VM did not execute while using shared memory")
        observations['rebound'] = _capture_borrow_residency(borrower, lifecycle.sandbox_id, artifact_root/'rebound')
        # This is a qualification workload, separate from the benchmark trace.
        # Keep the file until the post-allocation host observation; no repeated
        # writes or workload amplification are applied to formal experiments.
        allocation = lifecycle.sandbox.commands.run(
            _borrow_allocation_command(probe_id, allocation_mib), timeout=30)
        if allocation.exit_code:
            raise RuntimeError('new-allocation probe failed; Tool template needs dd and writable /dev/shm with '
                               'at least 5 MiB free (4 MiB is left unused)')
        marker = 'CLAWBOX_ALLOCATION_MIB='
        actual = next((line for line in allocation.stdout.splitlines() if line.startswith(marker)), '')
        if not actual or not actual.removeprefix(marker).isdigit():
            raise RuntimeError('new-allocation probe did not report its allocated MiB')
        allocated_mib = int(actual.removeprefix(marker))
        if not 1 <= allocated_mib <= allocation_mib:
            raise RuntimeError('new-allocation probe reported bytes outside its configured bound')
        allocation_mib = allocated_mib
        observations['allocated'] = _capture_borrow_residency(borrower, lifecycle.sandbox_id, artifact_root/'allocated')
        if observations['allocated']['resident_bytes'].get(borrower.shared_node, 0) <= borrow_record.shared_bytes_before:
            raise RuntimeError("shared NUMA residency did not increase after live borrow and allocation")
        return_record = lifecycle.return_local_memory()
        observations['returned'] = _capture_borrow_residency(borrower, lifecycle.sandbox_id, artifact_root/'returned')
        after = lifecycle.sandbox.commands.run("printf local-again", timeout=30)
        if after.exit_code or after.stdout != "local-again":
            raise RuntimeError("VM did not execute after returning to LOCAL")
        if return_record.local_bytes_after <= return_record.local_bytes_before:
            raise RuntimeError("compute NUMA residency did not increase after return")
        expected_retained = (return_record.reserved_bytes if return_record.shared_bytes_after else 0)
        if shared_pool.borrowed_bytes != expected_retained:
            raise RuntimeError("live-borrow reservation does not match residual shared residency after return")
        if expected_retained:
            reborrow_record = lifecycle.borrow_shared()
            if not reborrow_record.reservation_reused or shared_pool.borrowed_bytes != expected_retained:
                raise RuntimeError('LOCAL VM could not reuse its retained shared reservation')
            again = lifecycle.sandbox.commands.run('printf shared-again', timeout=30)
            if again.exit_code or again.stdout != 'shared-again':
                raise RuntimeError('VM did not execute after reusing its shared reservation')
            second_return_record = lifecycle.return_local_memory()
            expected = second_return_record.reserved_bytes if second_return_record.shared_bytes_after else 0
            if shared_pool.borrowed_bytes != expected:
                raise RuntimeError('reborrow changed conservative shared capacity accounting')
    except BaseException as exc:
        primary_error = exc
    cleanup_errors: list[str] = []
    lifecycle_close_error: str | None = None
    try:
        lifecycle.close()
    except Exception as exc:
        lifecycle_close_error = f"lifecycle close: {type(exc).__name__}: {exc}"
    try:
        client.kill_owned_sandboxes(probe_id)
    except Exception as exc:
        cleanup_errors.append(f"owned cleanup: {type(exc).__name__}: {exc}")
    if borrower.borrowed():
        release_error = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                borrower.release_destroyed(lifecycle.sandbox_id or probe_id)
                release_error = None
                break
            except Exception as exc:
                release_error = exc
                time.sleep(.5)
        if release_error is not None:
            cleanup_errors.append(
                f"borrow accounting cleanup: {type(release_error).__name__}: {release_error}"
            )
    if lifecycle_close_error is not None and borrower.borrowed():
        cleanup_errors.insert(0, lifecycle_close_error)
    remaining = client.list_owned_sandboxes(probe_id)
    if remaining:
        cleanup_errors.append(f"owned sandboxes remain: {len(remaining)}")
    if shared_pool.borrowed_bytes:
        cleanup_errors.append("shared live reservation remains after VM cleanup")
    result = {
        "required": True,
        "passed": primary_error is None and not cleanup_errors,
        "borrow_limit_bytes": limit_mib * 1024 * 1024,
        "shared_capacity_bytes": spec.resources.warm_memory_capacity_mib * 1024 * 1024,
        "placements": borrower.placements,
        "borrow": asdict(borrow_record) if borrow_record is not None else None,
        "return": asdict(return_record) if return_record is not None else None,
        "reborrow": asdict(reborrow_record) if reborrow_record is not None else None,
        "second_return": asdict(second_return_record) if second_return_record is not None else None,
        "residency_observations": observations,
        "allocation_probe_mib": allocation_mib,
        "allocation_probe_scope": "guest_tmpfs_growth; host net residency is not exact migration or page-fault attribution",
        "failure": (
            f"{type(primary_error).__name__}: {primary_error}"
            if primary_error is not None else None
        ),
        "cleanup_errors": cleanup_errors,
    }
    atomic_json(artifact_root / "result.json", result)
    if primary_error is not None:
        if cleanup_errors:
            primary_error.add_note("live-borrow cleanup: " + "; ".join(cleanup_errors))
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("live-borrow cleanup failed: " + "; ".join(cleanup_errors))
    return {**result, "artifact_root": str(artifact_root)}


def qualification_spec(spec: ExperimentSpec, *, concurrency: int | None = None) -> ExperimentSpec:
    if spec.inference.backend is not InferenceBackend.REPLAY:
        raise ValueError("qualification requires inference.backend=replay")
    cases = load_workload_cases(spec.workload)
    level = (
        concurrency
        if concurrency is not None
        else min(
            max(spec.execution.concurrency_levels),
            spec.execution.sandbox_create_concurrency,
            len(cases),
        )
    )
    if level < 1:
        raise ValueError("qualification concurrency must be positive")
    inference = spec.inference.model_copy(update={
        "configuration": {
            **spec.inference.configuration,
            # Qualification proves that each environment can launch and
            # execute its first native Tool call.  Replaying the recorded LLM
            # delay before that call adds hours for a heterogeneous corpus but
            # does not strengthen the capability proof.  Formal runs retain
            # the configured time scale from the source spec.
            "time_scale": 0.0,
            "max_model_steps": 1,
            "qualification_max_parallel_sessions": level,
        },
    })
    workload = spec.workload.model_copy(update={
        "cases": tuple(cases), "repetitions": 1,
    })
    execution = spec.execution.model_copy(update={
        # One session per case proves heterogeneous coverage. The worker-pool
        # bound above limits how many of those sessions are live at once.
        "concurrency_levels": (max(level, len(cases)),),
        "randomized_order": False,
        "stabilization_seconds": min(spec.execution.stabilization_seconds, 1.0),
        "arrival_schedule": ArrivalSchedule.BURST,
        "stagger_interval_seconds": 0.0,
    })
    validation = spec.validation.model_copy(update={"command": "true"})
    return spec.model_copy(update={
        "experiment_id": spec.experiment_id + "-qualification",
        "workload": workload,
        "inference": inference,
        "execution": execution,
        "validation": validation,
    })


def _sandbox_pids(sandbox_id: str) -> list[int]:
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if sandbox_id.encode() in (entry / "cmdline").read_bytes():
                result.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return result


def _file_mappings(pid: int) -> dict[tuple[str, int], dict[str, int | str]]:
    """Return file-backed smaps totals keyed by device/inode identity."""
    header = re.compile(
        r"^[0-9a-f]+-[0-9a-f]+\s+\S+\s+\S+\s+(\S+)\s+(\d+)\s*(.*)$"
    )
    result: dict[tuple[str, int], dict[str, int | str]] = {}
    current = None
    for line in Path(f"/proc/{pid}/smaps").read_text(errors="replace").splitlines():
        match = header.match(line)
        if match:
            inode = int(match.group(2))
            path = match.group(3).strip()
            current = None
            if inode and path.startswith("/"):
                key = (match.group(1), inode)
                current = result.setdefault(
                    key, {"path": path, "size_kib": 0, "rss_kib": 0,
                          "pss_kib": 0, "shared_clean_kib": 0},
                )
            continue
        if current is None or ":" not in line:
            continue
        name, _, value = line.partition(":")
        field = {
            "Size": "size_kib", "Rss": "rss_kib", "Pss": "pss_kib",
            "Shared_Clean": "shared_clean_kib",
        }.get(name)
        if field:
            current[field] = int(value.split()[0])
    return result


def _sandbox_file_mappings(sandbox_id: str) -> dict[tuple[str, int], dict[str, int | str]]:
    """Aggregate each mapping exactly once per matching VMM process."""
    aggregate: dict[tuple[str, int], dict[str, int | str]] = {}
    for pid in _sandbox_pids(sandbox_id):
        for identity, item in _file_mappings(pid).items():
            target = aggregate.get(identity)
            if target is None:
                aggregate[identity] = dict(item)
                continue
            for field in ("size_kib", "rss_kib", "pss_kib", "shared_clean_kib"):
                target[field] = int(target[field]) + int(item[field])
    return aggregate


def _same_template_ram_mapping(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_path = Path(str(left["path"]))
    right_path = Path(str(right["path"]))
    return (
        left_path == right_path
        and left_path.name.endswith("-memory")
        and "volumes" in left_path.parts
    )


def _template_sharing_identities(spec: ExperimentSpec) -> dict[str, dict[str, Any]]:
    identities: dict[str, dict[str, Any]] = {}
    for case in load_workload_cases(spec.workload):
        sandbox = resolve_case_sandbox(case, spec.sandbox)
        identity = {
            "template_id": sandbox.template,
            "image_digest": sandbox.image_digest,
            "source_image_reference": sandbox.source_image_reference,
            "vcpu": sandbox.vcpu,
            "memory_mib": sandbox.memory_mib,
            "architecture": sandbox.architecture,
        }
        previous = identities.setdefault(sandbox.template, identity)
        if previous != identity:
            raise ValueError(
                f"Tool template {sandbox.template} has conflicting immutable identities"
            )
    return dict(sorted(identities.items()))


def _template_sharing_backend_identity() -> dict[str, Any]:
    paths = (
        Path("/usr/local/services/cubetoolbox/Cubelet/bin/cubelet"),
        Path("/usr/local/services/cubetoolbox/Cubelet/config/config.toml"),
        Path("/usr/local/services/cubetoolbox/Cubelet/dynamicconf/conf.yaml"),
    )
    files = {}
    for path in paths:
        if not path.is_file():
            raise ValueError(f"template-sharing proof cannot identify Cube backend: missing {path}")
        files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files}


def write_template_base_sharing_artifact(
    spec: ExperimentSpec, result: dict[str, Any], *, artifact_root: Path, output: Path,
) -> dict[str, Any]:
    """Freeze an all-template physical-sharing proof for reuse on this backend."""
    expected = _template_sharing_identities(spec)
    probes = list((result.get("probes") or {}).values()) or [result]
    required = [probe for probe in probes if probe.get("required") is True]
    if not required or any(probe.get("passed") is not True for probe in required):
        raise ValueError("template-sharing artifact requires passed real-VM probes")
    evidence_hashes = {}
    for probe in required:
        evidence = probe.get("templates") or {}
        if set(evidence) != set(expected):
            raise ValueError("template-sharing proof does not cover the exact Tool templates")
        for template, rows in evidence.items():
            digest = hashlib.sha256(json.dumps(
                rows, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            previous = evidence_hashes.setdefault(template, digest)
            if previous != digest:
                raise ValueError(f"template-sharing probes disagree for {template}")
    proof_files = []
    for path in sorted(artifact_root.rglob("result.json")):
        proof_files.append({
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    for probe in required:
        for item in probe.get("prior_result_files", []):
            path = Path(item.get("path", ""))
            digest = item.get("sha256")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError(f"prior template-sharing result differs or is missing: {path}")
            resolved = str(path.resolve())
            if not any(existing["path"] == resolved for existing in proof_files):
                proof_files.append({"path": resolved, "sha256": digest})
    if not proof_files:
        raise ValueError("template-sharing proof has no persisted result.json")
    payload = {
        "schema": "clawbox_template_base_sharing_v1",
        "passed": True,
        "generated_unix_s": time.time(),
        "target_node": spec.resources.target_node,
        "backend_identity": _template_sharing_backend_identity(),
        "templates": {
            template: {**identity, "mapping_evidence_sha256": evidence_hashes[template]}
            for template, identity in expected.items()
        },
        "template_count": len(expected),
        "proof_files": proof_files,
        "probe_summary": [{
            key: probe.get(key) for key in (
                "template_count", "max_parallel_templates", "live_probe_vm_bound",
                "peak_live_probe_vms", "template_count_reused",
                "template_count_probed_this_attempt", "template_retry_attempts", "metric",
            )
        } for probe in required],
    }
    if output.exists():
        raise FileExistsError(f"refusing to replace template-sharing artifact {output}")
    atomic_json(output, payload)
    return payload


def validate_template_base_sharing_artifact(
    spec: ExperimentSpec, path: Path,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read template-sharing artifact {path}: {exc}") from exc
    if payload.get("schema") != "clawbox_template_base_sharing_v1" or payload.get("passed") is not True:
        raise ValueError("template-sharing artifact schema/status mismatch")
    if payload.get("target_node") != spec.resources.target_node:
        raise ValueError("template-sharing artifact target node differs")
    expected = _template_sharing_identities(spec)
    observed = payload.get("templates") or {}
    stripped = {
        template: {key: value for key, value in identity.items()
                   if key != "mapping_evidence_sha256"}
        for template, identity in observed.items()
    }
    if stripped != expected or payload.get("template_count") != len(expected):
        raise ValueError("template-sharing artifact does not match the exact Tool templates")
    for template, identity in observed.items():
        digest = identity.get("mapping_evidence_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"template-sharing artifact lacks mapping evidence for {template}")
    proof_files = payload.get("proof_files") or []
    if not proof_files:
        raise ValueError("template-sharing artifact has no raw proof files")
    for item in proof_files:
        proof = Path(item.get("path", ""))
        if not proof.is_file() or hashlib.sha256(proof.read_bytes()).hexdigest() != item.get("sha256"):
            raise ValueError(f"template-sharing raw proof differs or is missing: {proof}")
    return payload


def _validated_prior_template_evidence(
    path: Path, templates: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Load only complete mappings from a cleanup-safe earlier proof attempt."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read prior template-sharing result {path}: {exc}") from exc
    if raw.get("cleanup_errors"):
        raise ValueError(f"prior template-sharing result has cleanup errors: {path}")
    observed = raw.get("templates")
    if not isinstance(observed, dict):
        raise ValueError(f"prior template-sharing result has no template mappings: {path}")
    validated: dict[str, list[dict[str, Any]]] = {}
    for template, rows in observed.items():
        sandbox = templates.get(template)
        if sandbox is None:
            raise ValueError(
                f"prior template-sharing result contains an unexpected template {template}: {path}"
            )
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"prior template-sharing result has empty evidence for {template}")
        minimum_kib = sandbox.memory_mib * 1024 // 2
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("device_inode"), list):
                raise ValueError(f"prior template-sharing evidence is malformed for {template}")
            left, right = row.get("vm_0"), row.get("vm_1")
            if not isinstance(left, dict) or not isinstance(right, dict):
                raise ValueError(f"prior template-sharing VM evidence is missing for {template}")
            if not _same_template_ram_mapping(left, right):
                raise ValueError(f"prior template-sharing paths differ for {template}")
            if template not in str(left.get("path", "")):
                raise ValueError(f"prior template-sharing path does not identify {template}")
            if min(int(left.get("size_kib", 0)), int(right.get("size_kib", 0))) < minimum_kib:
                raise ValueError(f"prior template-sharing mapping is too small for {template}")
            if min(
                int(left.get("shared_clean_kib", 0)),
                int(right.get("shared_clean_kib", 0)),
            ) <= 0:
                raise ValueError(f"prior template-sharing mapping is not shared for {template}")
        validated[template] = rows
    resolved = path.resolve()
    return validated, {
        "path": str(resolved),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def template_base_sharing_test(
    spec: ExperimentSpec, *, artifact_root: Path,
    prior_results: tuple[Path, ...] = (),
) -> dict[str, Any]:
    if spec.schema_version == 4:
        return design_probes(
            spec,
            lambda child, *, artifact_root: template_base_sharing_test(
                child, artifact_root=artifact_root, prior_results=prior_results,
            ),
            artifact_root=artifact_root,
        )
    """Prove two VMs physically share a large file-backed template mapping."""
    required = spec.resources.template_base_sharing == "required"
    if not required:
        return {"required": False, "passed": True, "supported": False,
                "reason": "resources.template_base_sharing=disabled"}
    if spec.resources.template_base_sharing_artifact:
        path = Path(spec.resources.template_base_sharing_artifact)
        payload = validate_template_base_sharing_artifact(spec, path)
        return {
            "required": True, "passed": True, "supported": True, "reused": True,
            "artifact": str(path),
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "template_count": payload["template_count"],
        }
    artifact_root.mkdir(parents=True, exist_ok=False)
    cases = load_workload_cases(spec.workload)
    templates = {}
    for case in cases:
        sandbox = resolve_case_sandbox(case, spec.sandbox)
        templates.setdefault(sandbox.template, sandbox)
    journal = OwnedSandboxJournal(artifact_root / "owned-sandboxes.jsonl")
    run_id = "qualification-base-" + uuid.uuid4().hex[:12]
    evidence: dict[str, list[dict[str, Any]]] = {}
    prior_result_files: list[dict[str, str]] = []
    for path in prior_results:
        reused, receipt = _validated_prior_template_evidence(path, templates)
        for template, rows in reused.items():
            evidence.setdefault(template, rows)
        prior_result_files.append(receipt)
    reused_template_count = len(evidence)
    pending_templates = [
        (index, template, sandbox)
        for index, (template, sandbox) in enumerate(templates.items())
        if template not in evidence
    ]
    primary_error = None
    worker_count = min(spec.execution.sandbox_create_concurrency, len(pending_templates))
    activity_lock = Lock()
    active_probe_vms = 0
    peak_probe_vms = 0

    def verify_template(
        index: int, template: str, sandbox: Any, retry: int = 0,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Verify one template pair, then release it before another pair accumulates."""
        nonlocal active_probe_vms, peak_probe_vms
        session_prefix = f"{run_id}-{index}-r{retry}"
        client = CubeSandboxClient(
            journal=journal,
            request_timeout_s=spec.execution.cube_request_timeout_seconds,
        )
        pair = []
        failure: BaseException | None = None
        common: list[dict[str, Any]] = []
        try:
            for pair_index in range(2):
                lifecycle = CubeSandboxLifecycle(
                    client, template=template, node_name=spec.resources.target_node,
                    ownership=Ownership(
                        run_id, "template-base", run_id, spec.experiment_id,
                        f"{session_prefix}-{pair_index}", "qualification-template-base",
                    ),
                    allow_internet_access=sandbox.allow_internet_access,
                )
                lifecycle.start()
                pair.append(lifecycle)
                with activity_lock:
                    active_probe_vms += 1
                    peak_probe_vms = max(peak_probe_vms, active_probe_vms)
            maps = [_sandbox_file_mappings(lifecycle.sandbox_id) for lifecycle in pair]
            minimum_kib = sandbox.memory_mib * 1024 // 2
            for identity in maps[0].keys() & maps[1].keys():
                left, right = maps[0][identity], maps[1][identity]
                if not _same_template_ram_mapping(left, right):
                    continue
                if min(left["size_kib"], right["size_kib"]) < minimum_kib:
                    continue
                if min(left["shared_clean_kib"], right["shared_clean_kib"]) <= 0:
                    continue
                common.append({"device_inode": list(identity), "vm_0": left, "vm_1": right})
            if not common:
                raise RuntimeError(
                    f"template {template} has no physically shared large file-backed RAM mapping"
                )
        except BaseException as exc:
            failure = exc
        cleanup_errors = []
        for lifecycle in reversed(pair):
            try:
                lifecycle.close()
            except Exception as exc:
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                with activity_lock:
                    active_probe_vms -= 1
        if failure is not None:
            if cleanup_errors:
                failure.add_note("template-pair cleanup: " + "; ".join(cleanup_errors))
            raise failure
        if cleanup_errors:
            raise RuntimeError("template-pair cleanup failed: " + "; ".join(cleanup_errors))
        return template, common

    retryable_failures: dict[str, tuple[int, Any, list[str], BaseException]] = {}
    terminal_failures: list[BaseException] = []
    retry_attempts = 0
    try:
        if pending_templates:
            with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="template-sharing",
            ) as pool:
                futures = {
                    pool.submit(verify_template, index, template, sandbox): (
                        index, template, sandbox,
                    )
                    for index, template, sandbox in pending_templates
                }
                for future in as_completed(futures):
                    index, template, sandbox = futures[future]
                    try:
                        verified_template, common = future.result()
                    except Exception as exc:
                        if _retryable_template_probe_error(exc):
                            retryable_failures[template] = (
                                index, sandbox,
                                [f"{type(exc).__name__}: {exc}"], exc,
                            )
                        else:
                            terminal_failures.append(exc)
                    else:
                        evidence[verified_template] = common
        for template, (index, sandbox, errors, last_error) in retryable_failures.items():
            for retry in range(1, _TEMPLATE_PROBE_TRANSIENT_RETRY_LIMIT + 1):
                retry_attempts += 1
                try:
                    verified_template, common = verify_template(
                        index, template, sandbox, retry,
                    )
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                    last_error = exc
                    if not _retryable_template_probe_error(exc):
                        break
                else:
                    evidence[verified_template] = common
                    break
            if template not in evidence:
                terminal_failures.append(last_error)
        if terminal_failures:
            primary_error = terminal_failures[0]
        elif set(evidence) != set(templates):
            primary_error = RuntimeError(
                f"template-sharing proof is incomplete: {len(evidence)}/{len(templates)} templates"
            )
    except BaseException as exc:
        primary_error = exc
    cleanup_errors = []
    try:
        CubeSandboxClient(journal=journal).kill_owned_sandboxes(run_id)
    except Exception as exc:
        cleanup_errors.append(f"owned cleanup: {type(exc).__name__}: {exc}")
    result = {
        "required": True, "passed": primary_error is None and not cleanup_errors,
        "supported": primary_error is None, "templates": evidence,
        "template_count": len(templates),
        "max_parallel_templates": worker_count,
        "live_probe_vm_bound": worker_count * 2,
        "peak_live_probe_vms": peak_probe_vms,
        "template_count_reused": reused_template_count,
        "template_count_probed_this_attempt": len(pending_templates),
        "prior_result_files": prior_result_files,
        "template_retry_attempts": retry_attempts,
        "template_retry_errors": {
            template: errors
            for template, (_index, _sandbox, errors, _last_error)
            in retryable_failures.items()
        },
        "metric": "Linux smaps Shared_Clean/Pss for common Cube *-memory device+inode mapping",
        "failure": f"{type(primary_error).__name__}: {primary_error}" if primary_error else None,
        "cleanup_errors": cleanup_errors,
    }
    atomic_json(artifact_root / "result.json", result)
    if primary_error:
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("template sharing probe cleanup failed: " + "; ".join(cleanup_errors))
    return result


def qualify(
    spec: ExperimentSpec, *, output_base: Path, receipt_path: Path,
    concurrency: int | None = None,
    validated_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_host_ready(spec)
    validated_inputs = (
        validated_inputs
        if validated_inputs is not None
        else validate_launch_inputs(spec)
    )
    fault_test = supervision_fault_test()
    qualified = qualification_spec(spec, concurrency=concurrency)
    run_id = f"qualification-{uuid.uuid4().hex[:12]}"
    attempt_id = f"attempt-{uuid.uuid4().hex[:12]}"
    run_root = output_base / run_id
    probe_root = output_base / ".qualification-probes" / run_id
    from .runtime_qualification import runtime_roundtrip_test
    runtime_test = runtime_roundtrip_test(spec, artifact_root=probe_root / 'runtime-control')
    snapshot_test = snapshot_roundtrip_test(
        spec, artifact_root=probe_root / "snapshot",
    )
    borrow_test = live_borrow_roundtrip_test(
        spec, artifact_root=probe_root / "live-borrow",
    )
    template_sharing_test = template_base_sharing_test(
        spec, artifact_root=probe_root / "template-base-sharing",
    )
    results = ExperimentSupervisor(
        qualified, run_id=run_id, attempt_id=attempt_id,
        owner_id=run_id, output_root=run_root,
    ).run()
    if not results or any(item.status is not RunStatus.SUCCEEDED for item in results):
        raise RuntimeError(f"qualification failed; inspect {run_root}")
    receipt = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "source_spec_digest": spec_digest(spec),
        "qualification_contract_digest": qualification_contract_digest(spec),
        "input_manifest": qualification_input_manifest(
            spec, validated_inputs=validated_inputs,
        ),
        "implementation_digest": implementation_digest(),
        "qualification_spec_digest": spec_digest(qualified),
        "qualified_unix_s": time.time(),
        "concurrency": int(
            qualified.inference.configuration["qualification_max_parallel_sessions"]
        ),
        "qualified_session_count": max(qualified.execution.concurrency_levels),
        "qualified_case_ids": [case.case_id for case in load_workload_cases(spec.workload)],
        "target_concurrency": max(spec.execution.concurrency_levels),
        "scope": "capability_smoke",
        "policy_names": [policy.name for policy in qualified.policies],
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "tool_image_digests": sorted({
            resolve_case_sandbox(case, spec.sandbox).image_digest
            for case in load_workload_cases(spec.workload)
        }, key=lambda value: value or ""),
        "run_root": str(run_root),
        "supervision_fault_test": fault_test,
        "snapshot_roundtrip_test": snapshot_test,
        "live_borrow_roundtrip_test": borrow_test,
        "runtime_control_test": runtime_test,
        "template_base_sharing_test": template_sharing_test,
        "arm_ids": [arm.arm_id for arm in expand_matrix(qualified)],
        "cleanup_verified": all(
            item.correctness.get("cleanup_verified") is True for item in results
        ) and snapshot_test["passed"] and borrow_test["passed"] and runtime_test['passed']
        and template_sharing_test["passed"],
        "status": "succeeded",
    }
    if not receipt["cleanup_verified"]:
        raise RuntimeError("qualification cleanup was not verified")
    atomic_json(receipt_path, receipt)
    return receipt


def validate_receipt(spec: ExperimentSpec, path: Path) -> dict[str, Any]:
    import json

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read qualification receipt {path}: {exc}") from exc
    expected = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "input_manifest": current_qualification_input_manifest(spec),
        "implementation_digest": implementation_digest(),
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "tool_image_digests": sorted({
            resolve_case_sandbox(case, spec.sandbox).image_digest
            for case in load_workload_cases(spec.workload)
        }, key=lambda value: value or ""),
        "status": "succeeded",
        "cleanup_verified": True,
        "scope": "capability_smoke",
    }
    mismatches = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    # Concurrency is the maximum simultaneous smoke size, not the number of
    # covered cases or evidence of target performance. Separate receipt fields
    # prove that one qualification session executed for every workload case.
    if receipt.get("concurrency", 0) < 1:
        mismatches.append("concurrency")
    cases = load_workload_cases(spec.workload)
    if receipt.get("qualified_session_count", 0) < len(cases):
        mismatches.append("qualified_session_count")
    if receipt.get("qualified_case_ids") != [case.case_id for case in cases]:
        mismatches.append("qualified_case_ids")
    if receipt.get("policy_names") != [policy.name for policy in spec.policies]:
        mismatches.append("policy_names")
    if spec.schema_version == 4:
        from .spec import effective_spec
        for policy in spec.policies:
            child = effective_spec(spec, policy)
            evidence = {}
            for key in ('runtime_control_test', 'snapshot_roundtrip_test',
                        'live_borrow_roundtrip_test', 'template_base_sharing_test'):
                group = receipt.get(key, {})
                probe_id = group.get('by_policy', {}).get(policy.name)
                evidence[key] = group.get('probes', {}).get(probe_id, {})
            mismatches.extend(policy.name + ':' + key for key in _probe_mismatches(child, evidence))
    else:
        mismatches.extend(_probe_mismatches(spec, receipt))
    if mismatches:
        raise ValueError(
            "qualification receipt does not cover this experiment: "
            + ", ".join(dict.fromkeys(mismatches))
        )
    return receipt


def _probe_mismatches(spec, receipt):
    mismatches = []
    if spec.resources.local_memory_cgroup and not (
        isinstance(receipt.get('runtime_control_test'), dict)
        and receipt['runtime_control_test'].get('passed') is True
        and receipt['runtime_control_test'].get('neutral_placement_verified') is True
        and (not any(p.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE for p in spec.policies)
             or receipt['runtime_control_test'].get('restore_verified') is True)
    ):
        mismatches.append('runtime_control_test')
    if spec.resources.template_base_sharing == "required" and not (
        isinstance(receipt.get("template_base_sharing_test"), dict)
        and receipt["template_base_sharing_test"].get("passed") is True
        and receipt["template_base_sharing_test"].get("supported") is True
    ):
        mismatches.append("template_base_sharing_test")
    if any(
        policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
        for policy in spec.policies
    ) and not (
        isinstance(receipt.get("snapshot_roundtrip_test"), dict)
        and receipt["snapshot_roundtrip_test"].get("passed") is True
        and receipt["snapshot_roundtrip_test"].get("mechanism")
        == spec.resources.snapshot_mechanism
        and receipt["snapshot_roundtrip_test"].get("storage")
        == spec.resources.snapshot_storage
        and (
            spec.resources.snapshot_mechanism != "incremental-cow"
            or receipt["snapshot_roundtrip_test"].get(
                "incremental_delta_verified"
            ) is True
        )
        and (
            spec.resources.snapshot_storage != "tiered"
            or spec.resources.warm_memory_capacity_mib <= 0
            or spec.resources.snapshot_destination == 'cold'
            or receipt["snapshot_roundtrip_test"].get(
                "cold_relocation_verified"
            ) is True
        )
    ):
        mismatches.append("snapshot_roundtrip_test")
    if spec.resources.shared_memory_borrow_limit_mib is not None and not (
        isinstance(receipt.get("live_borrow_roundtrip_test"), dict)
        and receipt["live_borrow_roundtrip_test"].get("passed") is True
        and receipt["live_borrow_roundtrip_test"].get("borrow_limit_bytes")
        == spec.resources.shared_memory_borrow_limit_mib * 1024 * 1024
        and receipt["live_borrow_roundtrip_test"].get("shared_capacity_bytes")
        == spec.resources.warm_memory_capacity_mib * 1024 * 1024
    ):
        mismatches.append("live_borrow_roundtrip_test")
    return mismatches


def design_probes(spec, probe, *, artifact_root):
    """Qualify every distinct effective mechanism, reusing identical probes."""
    from .spec import effective_spec, canonical_json
    groups, policies = {}, {}
    for policy in spec.policies:
        child = effective_spec(spec, policy)
        resources = child.resources.model_dump(mode='json')
        # Prediction amounts and controller tuning do not alter snapshot/borrow
        # semantics. Hash only the settings consumed by these probes.
        keys = ('compute_nodes', 'target_node', 'local_memory_cgroup', 'local_numa_node',
                'warm_numa_node', 'warm_memory_capacity_mib', 'warm_snapshot_root',
                'cold_snapshot_root', 'snapshot_mechanism', 'snapshot_storage',
                'snapshot_destination', 'lazy_restore', 'shared_memory_borrow_limit_mib',
                'pool_transfer_concurrency', 'pool_transfer_timeout_seconds',
                'template_base_sharing', 'template_base_sharing_artifact',
                'runtime_poll_interval_seconds')
        identity = {key: resources[key] for key in keys}
        identity['snapshot_enabled'] = policy.reclamation.value == 'snapshot_pause'
        digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:16]
        policies[policy.name] = digest
        if digest not in groups:
            groups[digest] = probe(child, artifact_root=artifact_root / digest)
    return {'required': any(p.get('required', True) for p in groups.values()),
            'passed': all(p.get('passed') is True for p in groups.values()),
            'by_policy': policies, 'probes': groups}
