"""Short, real CubeSandbox qualification before a formal experiment."""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
import os
import hashlib
import shutil
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from clawbox.cube import (
    CubeSandboxClient, CubeSandboxLifecycle, OwnedSandboxJournal, Ownership,
)
from clawbox.lab import apply_environment, doctor
from clawbox.replay.trace import load_trace

from .results import RunStatus
from .inputs import validate_inputs
from .snapshot_pool import WarmSnapshotPool
from .numa_borrow import SandboxNumaBorrower, read_numa_lru_bytes
from .spec import (
    ExperimentSpec, InferenceBackend, ReclamationPolicy, expand_matrix,
    load_workload_cases, spec_digest,
)
from .supervisor import ExperimentSupervisor, process_is_alive, process_identity, terminate_process_tree
from .worker import atomic_json


QUALIFICATION_SCHEMA_VERSION = 10


def qualification_input_manifest(spec: ExperimentSpec) -> dict[str, Any]:
    """Bind a receipt to the exact mutable files used by the specification."""
    inputs = validate_inputs(spec)
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
    }


def implementation_digest() -> str:
    """Bind qualification to the exact supported Python implementation tree."""
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "clawbox").rglob("*.py"))
    if not files:
        raise RuntimeError(f"ClawBox source tree is unavailable below {root}")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def default_receipt_path(spec_path: Path) -> Path:
    return spec_path.with_name(spec_path.name + ".qualification.json")


def host_profile_for(spec: ExperimentSpec) -> dict[str, Any]:
    def sandbox(value: Any) -> dict[str, Any]:
        return {"template_id": value.template, "memory_mib": value.memory_mib,
                "vcpu": value.vcpu}

    return {
        "node": spec.resources.target_node,
        "compute_nodes": [n.model_dump() for n in spec.resources.compute_nodes],
        "runtime": sandbox(spec.runtime),
        "sandbox": sandbox(spec.sandbox),
        "tool_sandboxes": [
            sandbox(case.sandbox or spec.sandbox)
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


def require_host_ready(spec: ExperimentSpec, *, current_images: bool = True) -> None:
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


def snapshot_roundtrip_test(spec: ExperimentSpec, *, artifact_root: Path, _node=None,
                            _template: str | None = None) -> dict[str, Any]:
    if spec.schema_version == 4:
        return design_probes(spec, snapshot_roundtrip_test, artifact_root=artifact_root)
    """Exercise checkpoint/restore and placement on every compute node."""
    if _template is None:
        sandboxes = {}
        for case in load_workload_cases(spec.workload):
            sandbox = case.sandbox or spec.sandbox
            sandboxes.setdefault(sandbox.template, sandbox)
        if len(sandboxes) > 1:
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
    level = concurrency or max(spec.execution.concurrency_levels)
    if level < 1:
        raise ValueError("qualification concurrency must be positive")
    cases = load_workload_cases(spec.workload)
    for selected in cases:
        trace_value = selected.replay_trace_reference or selected.source_reference
        if not trace_value:
            raise ValueError(f"qualification case {selected.case_id} has no replay trace")
        actions = load_trace(Path(trace_value))
        first_output = actions[0].output if isinstance(actions[0].output, dict) else {}
        if not first_output.get("tool_calls"):
            raise ValueError(
                f"qualification case {selected.case_id} must issue a native Tool call in its first model step"
            )
    inference = spec.inference.model_copy(update={
        "configuration": {
            **spec.inference.configuration,
            "max_model_steps": 1,
        },
    })
    workload = spec.workload.model_copy(update={
        "cases": tuple(cases), "repetitions": 1,
    })
    execution = spec.execution.model_copy(update={
        "concurrency_levels": (max(level, len(cases)),),
        "randomized_order": False,
        "stabilization_seconds": min(spec.execution.stabilization_seconds, 1.0),
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


def template_base_sharing_test(spec: ExperimentSpec, *, artifact_root: Path) -> dict[str, Any]:
    if spec.schema_version == 4:
        return design_probes(spec, template_base_sharing_test, artifact_root=artifact_root)
    """Prove two VMs physically share a large file-backed template mapping."""
    required = spec.resources.template_base_sharing == "required"
    if not required:
        return {"required": False, "passed": True, "supported": False,
                "reason": "resources.template_base_sharing=disabled"}
    artifact_root.mkdir(parents=True, exist_ok=False)
    cases = load_workload_cases(spec.workload)
    templates = {}
    for case in cases:
        sandbox = case.sandbox or spec.sandbox
        templates.setdefault(sandbox.template, sandbox)
    client = CubeSandboxClient(
        journal=OwnedSandboxJournal(artifact_root / "owned-sandboxes.jsonl")
    )
    run_id = "qualification-base-" + uuid.uuid4().hex[:12]
    evidence = {}
    lifecycles = []
    primary_error = None
    try:
        for template, sandbox in templates.items():
            pair = []
            for index in range(2):
                lifecycle = CubeSandboxLifecycle(
                    client, template=template, node_name=spec.resources.target_node,
                    ownership=Ownership(
                        run_id, "template-base", run_id, spec.experiment_id,
                        f"{run_id}-{len(lifecycles)}", "qualification-template-base",
                    ),
                    allow_internet_access=sandbox.allow_internet_access,
                )
                lifecycle.start()
                lifecycles.append(lifecycle)
                pair.append(lifecycle)
            maps = []
            for lifecycle in pair:
                maps.append(_sandbox_file_mappings(lifecycle.sandbox_id))
            common = []
            minimum_kib = sandbox.memory_mib * 1024 // 2
            for identity in maps[0].keys() & maps[1].keys():
                left, right = maps[0][identity], maps[1][identity]
                # The immutable root filesystem is shared too, but it is not
                # evidence for cross-VM RAM Base sharing.  Cube template RAM
                # volumes have a dedicated *-memory object in the volume store.
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
            evidence[template] = common
    except BaseException as exc:
        primary_error = exc
    cleanup_errors = []
    for lifecycle in reversed(lifecycles):
        try:
            lifecycle.close()
        except Exception as exc:
            cleanup_errors.append(f"{type(exc).__name__}: {exc}")
    try:
        client.kill_owned_sandboxes(run_id)
    except Exception as exc:
        cleanup_errors.append(f"owned cleanup: {type(exc).__name__}: {exc}")
    result = {
        "required": True, "passed": primary_error is None and not cleanup_errors,
        "supported": primary_error is None, "templates": evidence,
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
) -> dict[str, Any]:
    require_host_ready(spec)
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
        "input_manifest": qualification_input_manifest(spec),
        "implementation_digest": implementation_digest(),
        "qualification_spec_digest": spec_digest(qualified),
        "qualified_unix_s": time.time(),
        "concurrency": max(qualified.execution.concurrency_levels),
        "target_concurrency": max(spec.execution.concurrency_levels),
        "scope": "capability_smoke",
        "policy_names": [policy.name for policy in qualified.policies],
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "tool_image_digests": sorted({
            (case.sandbox or spec.sandbox).image_digest
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
        "source_spec_digest": spec_digest(spec),
        "input_manifest": qualification_input_manifest(spec),
        "implementation_digest": implementation_digest(),
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "tool_image_digests": sorted({
            (case.sandbox or spec.sandbox).image_digest
            for case in load_workload_cases(spec.workload)
        }, key=lambda value: value or ""),
        "status": "succeeded",
        "cleanup_verified": True,
        "target_concurrency": max(spec.execution.concurrency_levels),
        "scope": "capability_smoke",
    }
    mismatches = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    # Concurrency is the actual executed smoke size, not evidence of target
    # performance. Explicit --qualification-concurrency may bound this gate;
    # every workload case must still execute, and all real mechanism probes
    # and exact source/input/spec checks remain mandatory.
    if receipt.get("concurrency", 0) < len(load_workload_cases(spec.workload)):
        mismatches.append("concurrency")
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
                'template_base_sharing', 'runtime_poll_interval_seconds')
        identity = {key: resources[key] for key in keys}
        identity['snapshot_enabled'] = policy.reclamation.value == 'snapshot_pause'
        digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:16]
        policies[policy.name] = digest
        if digest not in groups:
            groups[digest] = probe(child, artifact_root=artifact_root / digest)
    return {'required': any(p.get('required', True) for p in groups.values()),
            'passed': all(p.get('passed') is True for p in groups.values()),
            'by_policy': policies, 'probes': groups}
