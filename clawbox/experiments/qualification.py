"""Short, real CubeSandbox qualification before a formal experiment."""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
import os
import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from clawbox.cube import (
    CubeSandboxClient, CubeSandboxLifecycle, OwnedSandboxJournal, Ownership,
)
from clawbox.lab import apply_environment, doctor
from clawbox.replay.trace import load_trace

from .results import RunStatus
from .snapshot_pool import WarmSnapshotPool
from .numa_borrow import SandboxNumaBorrower
from .spec import (
    ExperimentSpec, InferenceBackend, ReclamationPolicy, expand_matrix,
    load_workload_cases, spec_digest,
)
from .supervisor import ExperimentSupervisor, process_is_alive, process_identity, terminate_process_tree
from .worker import atomic_json


QUALIFICATION_SCHEMA_VERSION = 4


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
        "pool_memory_budget_mib": node.high_watermark_mib,
    })})


def snapshot_roundtrip_test(spec: ExperimentSpec, *, artifact_root: Path, _node=None) -> dict[str, Any]:
    """Exercise checkpoint/restore and placement on every compute node."""
    if spec.resources.compute_nodes:
        nodes = {node.node_id: snapshot_roundtrip_test(
            _node_spec(spec, node), artifact_root=artifact_root / node.node_id, _node=node,
        ) for node in spec.resources.compute_nodes}
        result = {"required": any(r["required"] for r in nodes.values()),
                  "passed": all(r["passed"] for r in nodes.values()), "nodes": nodes,
                  "mechanism": spec.resources.snapshot_mechanism, "storage": spec.resources.snapshot_storage,
                  "incremental_delta_verified": all(r.get("incremental_delta_verified", False) for r in nodes.values())}
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
        WarmSnapshotPool(spec.resources.warm_memory_capacity_mib * 1024 * 1024)
        if spec.resources.warm_memory_capacity_mib > 0 else None
    )
    lifecycle = CubeSandboxLifecycle(
        client,
        template=spec.sandbox.template,
        node_name=spec.resources.target_node,
        ownership=ownership,
        allow_internet_access=spec.sandbox.allow_internet_access,
        role="tool",
        warm_snapshot_root=spec.resources.warm_snapshot_root,
        cold_snapshot_root=spec.resources.cold_snapshot_root,
        snapshot_pool=pool,
        snapshot_reservation_bytes=(spec.sandbox.memory_mib + 256) * 1024 * 1024,
        snapshot_mechanism=spec.resources.snapshot_mechanism,
        snapshot_storage=spec.resources.snapshot_storage,
        lazy_restore=True,
        numa_borrower=SandboxNumaBorrower(
            Path(spec.resources.local_memory_cgroup), local_node=spec.resources.local_numa_node,
            shared_node=spec.resources.warm_numa_node, shared_pool=pool,
            cpus=_node.cpus if _node else None, node_id=_node.node_id if _node else None,
        ) if spec.resources.shared_memory_borrow_limit_mib is not None else None,
    )
    primary_error: BaseException | None = None
    incremental_delta_verified = False
    try:
        lifecycle.start()
        before = lifecycle.sandbox.commands.run("printf qualification-before", timeout=30)
        if before.exit_code or before.stdout != "qualification-before":
            raise RuntimeError("snapshot probe could not execute before checkpoint")
        generations = 2 if spec.resources.snapshot_mechanism == "incremental-cow" else 1
        for generation in range(1, generations + 1):
            marker = f"qualification-state-{generation}"
            lifecycle.sandbox.files.write("/tmp/clawbox-qualification-state", marker)
            lifecycle.checkpoint_and_evict()
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
    try:
        lifecycle.close()
    except Exception as exc:
        cleanup_errors.append(f"lifecycle close: {type(exc).__name__}: {exc}")
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
        "timings": lifecycle.timings,
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
        raise primary_error
    if cleanup_errors:
        raise RuntimeError("snapshot probe cleanup failed: " + "; ".join(cleanup_errors))
    return {
        "required": True,
        "passed": True,
        "mechanism": spec.resources.snapshot_mechanism,
        "storage": spec.resources.snapshot_storage,
        "incremental_delta_verified": incremental_delta_verified,
        "timings": lifecycle.timings,
        "placements": lifecycle.numa_borrower.placements if lifecycle.numa_borrower else [],
        "artifact_root": str(artifact_root),
    }


def live_borrow_roundtrip_test(
    spec: ExperimentSpec, *, artifact_root: Path, _node=None,
) -> dict[str, Any]:
    """Prove a VM on each compute node can borrow and return without interruption."""
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
    limit_mib = spec.resources.shared_memory_borrow_limit_mib
    if not limit_mib:
        return {"required": False, "passed": True, "borrow_limit_bytes": 0,
                "shared_capacity_bytes": spec.resources.warm_memory_capacity_mib * 1024 * 1024}
    probe_id = "qualification-borrow-" + uuid.uuid4().hex[:12]
    artifact_root.mkdir(parents=True, exist_ok=False)
    journal = OwnedSandboxJournal(artifact_root / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal)
    shared_pool = WarmSnapshotPool(
        spec.resources.warm_memory_capacity_mib * 1024 * 1024,
        borrow_capacity_bytes=limit_mib * 1024 * 1024,
    )
    borrower = SandboxNumaBorrower(
        Path(spec.resources.local_memory_cgroup),
        local_node=spec.resources.local_numa_node,
        shared_node=spec.resources.warm_numa_node,
        shared_pool=shared_pool,
        cpus=_node.cpus if _node else None, node_id=_node.node_id if _node else None,
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
    borrow_record = return_record = None
    try:
        lifecycle.start()
        before = lifecycle.sandbox.commands.run("printf local-ready", timeout=30)
        if before.exit_code or before.stdout != "local-ready":
            raise RuntimeError("live-borrow probe could not execute on LOCAL")
        borrow_record = lifecycle.borrow_shared()
        during = lifecycle.sandbox.commands.run("printf shared-alive", timeout=30)
        if during.exit_code or during.stdout != "shared-alive":
            raise RuntimeError("VM did not execute while using shared memory")
        if borrow_record.shared_bytes_after <= borrow_record.shared_bytes_before:
            raise RuntimeError("shared NUMA residency did not increase after live borrow")
        return_record = lifecycle.return_local_memory()
        after = lifecycle.sandbox.commands.run("printf local-again", timeout=30)
        if after.exit_code or after.stdout != "local-again":
            raise RuntimeError("VM did not execute after returning to LOCAL")
        if return_record.local_bytes_after <= return_record.local_bytes_before:
            raise RuntimeError("compute NUMA residency did not increase after return")
        if shared_pool.borrowed_bytes:
            raise RuntimeError("live-borrow reservation remained after return")
    except BaseException as exc:
        primary_error = exc
    cleanup_errors: list[str] = []
    try:
        lifecycle.close()
    except Exception as exc:
        cleanup_errors.append(f"lifecycle close: {type(exc).__name__}: {exc}")
    try:
        client.kill_owned_sandboxes(probe_id)
    except Exception as exc:
        cleanup_errors.append(f"owned cleanup: {type(exc).__name__}: {exc}")
    remaining = client.list_owned_sandboxes(probe_id)
    if remaining:
        cleanup_errors.append(f"owned sandboxes remain: {len(remaining)}")
    result = {
        "required": True,
        "passed": primary_error is None and not cleanup_errors,
        "borrow_limit_bytes": limit_mib * 1024 * 1024,
        "shared_capacity_bytes": spec.resources.warm_memory_capacity_mib * 1024 * 1024,
        "placements": borrower.placements,
        "borrow": asdict(borrow_record) if borrow_record is not None else None,
        "return": asdict(return_record) if return_record is not None else None,
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
    selected = cases[0]
    trace_value = selected.replay_trace_reference or selected.source_reference
    if not trace_value:
        raise ValueError("qualification case has no replay trace")
    actions = load_trace(Path(trace_value))
    first_output = actions[0].output if isinstance(actions[0].output, dict) else {}
    if not first_output.get("tool_calls"):
        raise ValueError(
            "qualification trace must issue a native Tool call in its first model step"
        )
    inference = spec.inference.model_copy(update={
        "configuration": {
            **spec.inference.configuration,
            "max_model_steps": 1,
        },
    })
    workload = spec.workload.model_copy(update={
        "cases": (selected,), "repetitions": 1,
    })
    execution = spec.execution.model_copy(update={
        "concurrency_levels": (level,),
        "randomized_order": False,
        "arm_timeout_seconds": min(spec.execution.arm_timeout_seconds, 600),
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
    snapshot_test = snapshot_roundtrip_test(
        spec, artifact_root=probe_root / "snapshot",
    )
    borrow_test = live_borrow_roundtrip_test(
        spec, artifact_root=probe_root / "live-borrow",
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
        "implementation_digest": implementation_digest(),
        "qualification_spec_digest": spec_digest(qualified),
        "qualified_unix_s": time.time(),
        "concurrency": max(qualified.execution.concurrency_levels),
        "policy_names": [policy.name for policy in qualified.policies],
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "run_root": str(run_root),
        "supervision_fault_test": fault_test,
        "snapshot_roundtrip_test": snapshot_test,
        "live_borrow_roundtrip_test": borrow_test,
        "arm_ids": [arm.arm_id for arm in expand_matrix(qualified)],
        "cleanup_verified": all(
            item.correctness.get("cleanup_verified") is True for item in results
        ) and snapshot_test["passed"] and borrow_test["passed"],
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
        "implementation_digest": implementation_digest(),
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "status": "succeeded",
        "cleanup_verified": True,
    }
    mismatches = [
        key for key, value in expected.items() if receipt.get(key) != value
    ]
    if receipt.get("concurrency", 0) < max(spec.execution.concurrency_levels):
        mismatches.append("concurrency")
    if receipt.get("policy_names") != [policy.name for policy in spec.policies]:
        mismatches.append("policy_names")
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
    if mismatches:
        raise ValueError(
            "qualification receipt does not cover this experiment: "
            + ", ".join(dict.fromkeys(mismatches))
        )
    return receipt
