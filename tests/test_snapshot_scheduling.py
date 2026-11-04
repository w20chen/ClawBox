import os
from pathlib import Path

import pytest

from clawbox.experiments.qualification import host_profile_for, qualification_spec
from clawbox.experiments.inputs import validate_inputs, validate_launch_inputs
from clawbox.experiments.snapshot_host import init as init_host_plan, load as load_host_plan
from clawbox.experiments.snapshot_scheduling import (
    ParentPackage,
    SnapshotScheduler,
    SnapshotStorage,
    _copy_measured,
    _digest,
    _safe_arm_directory,
    cleanup_snapshot_run_storage,
)
from clawbox.experiments.spec import (
    SnapshotBranchingSpec,
    SnapshotSchedulingStorageSpec,
    expand_matrix,
    load_experiment,
)


ROOT = Path(__file__).resolve().parents[1]


def _scheduler() -> SnapshotScheduler:
    config = SnapshotBranchingSpec(
        pressure="compute-memory",
        parent_nodes=("node0", "node1"),
        branches_per_parent=1,
        arrival_batches=(2,),
        per_node_concurrency=1,
        per_node_memory_mib=4096,
        migration_threshold_seconds=0.25,
    )
    return SnapshotScheduler(["node0", "node1"], config, vm_memory_mib=3072)


def test_policies_use_distinct_affinity_load_and_snapshot_costs():
    scheduler = _scheduler()
    scheduler.nodes["node0"].active = 1
    feasible = {"node0": True, "node1": True}
    stage = {"node0": 0.0, "node1": 100.0}

    selected, _ = scheduler.choose(
        "node0", feasible=feasible, stage_seconds=stage, policy="affinity",
    )
    assert selected == "node0"
    scheduler.nodes[selected].waiting -= 1

    selected, _ = scheduler.choose(
        "node0", feasible=feasible, stage_seconds=stage, policy="load_aware",
    )
    assert selected == "node1"
    scheduler.nodes[selected].waiting -= 1

    selected, _ = scheduler.choose(
        "node0", feasible=feasible, stage_seconds=stage, policy="snapshot_aware",
    )
    assert selected == "node0"


def test_snapshot_aware_migrates_when_queue_savings_exceed_transfer_cost():
    scheduler = _scheduler()
    scheduler.nodes["node0"].active = 1
    scheduler.nodes["node0"].service_history.append(10.0)
    selected, evidence = scheduler.choose(
        "node0",
        feasible={"node0": True, "node1": True},
        stage_seconds={"node0": 0.0, "node1": 1.0},
        policy="snapshot_aware",
    )
    assert selected == "node1"
    assert evidence["candidates"]["node0"]["total_seconds"] > evidence["candidates"]["node1"]["total_seconds"]


def test_sparse_copy_preserves_bytes_and_digest(tmp_path: Path):
    source = tmp_path / "source.bin"
    target = tmp_path / "target.bin"
    with source.open("wb") as stream:
        stream.write(b"head")
        stream.seek(8 * 1024 * 1024)
        stream.write(b"tail")
    copied, seconds = _copy_measured(source, target)
    assert target.stat().st_size == source.stat().st_size
    assert _digest(target) == _digest(source)
    assert 0 < copied <= source.stat().st_size
    assert seconds >= 0
    if os.name == "posix" and hasattr(os, "SEEK_DATA"):
        assert copied < source.stat().st_size


def test_peer_direct_stages_one_copy_without_shared_io(tmp_path: Path):
    roots = {name: tmp_path / name for name in ("local0", "local1", "shared", "warm")}
    for root in roots.values():
        root.mkdir()
    spec = SnapshotSchedulingStorageSpec.model_construct(
        local_roots={"node0": str(roots["local0"]), "node1": str(roots["local1"])},
        shared_mounts={"node0": str(roots["shared"]), "node1": str(roots["shared"])},
        warm_root=str(roots["warm"]),
        local_capacity_mib=1024,
        shared_capacity_mib=1024,
        host_receipt=str(tmp_path / "receipt.json"),
    )
    storage = SnapshotStorage(
        spec, run_id="run-01", arm_id="arm-01", task_uid="task-01",
        transfer_mode="peer-direct", emit=lambda _event: None,
    )
    source = storage.local["node0"] / "parents" / "parent-00"
    rootfs = source / "rootfs" / "payload.bin"
    rootfs.parent.mkdir(parents=True)
    rootfs.write_bytes(b"peer-direct-payload")
    manifest = source / "snapshot-package.json"
    manifest.write_text("{}", encoding="utf-8")
    package = ParentPackage(
        "parent-00", "node0", "snap-00", 1, source, (rootfs,), (), (manifest,),
        {
            str(rootfs.relative_to(source)): _digest(rootfs),
            str(manifest.relative_to(source)): _digest(manifest),
        },
        "memory-digest", _digest(rootfs),
    )

    result = storage.stage(package, "node1")

    assert result.upload_bytes == 0
    assert result.download_bytes == len(b"peer-direct-payload")
    assert storage.shared_io_read_bytes == 0
    assert storage.shared_io_write_bytes == 0
    assert result.transfer_path == ["node0-local-xfs->node1-local-xfs"]
    target = (
        storage.local["node1"] / "cache" / package.key
        / "rootfs" / "rootfs" / "payload.bin"
    )
    assert target.read_bytes() == b"peer-direct-payload"


def test_arm_storage_is_isolated_by_immutable_run_id(tmp_path: Path):
    first = _safe_arm_directory(tmp_path, "run-01", "same-arm", "task-01")
    second = _safe_arm_directory(tmp_path, "run-02", "same-arm", "task-02")
    assert first != second
    assert first.relative_to(tmp_path).parts[-2:] == ("run-01", "same-arm")
    assert second.relative_to(tmp_path).parts[-2:] == ("run-02", "same-arm")


def test_interrupted_run_cleanup_removes_only_matching_run_storage(tmp_path: Path):
    roots = {
        name: tmp_path / name
        for name in ("local0", "local1", "shared", "warm", "cold")
    }
    for root in roots.values():
        root.mkdir()
    spec = SnapshotSchedulingStorageSpec.model_construct(
        local_roots={"node0": str(roots["local0"]), "node1": str(roots["local1"])},
        shared_mounts={"node0": str(roots["shared"]), "node1": str(roots["shared"])},
        warm_root=str(roots["warm"]),
        local_capacity_mib=1024,
        shared_capacity_mib=1024,
        host_receipt=str(tmp_path / "receipt.json"),
    )
    for root in roots.values():
        _safe_arm_directory(root, "run-01", "arm-01", "task-01")
        _safe_arm_directory(root, "run-02", "arm-01", "task-02")
    cleaned = cleanup_snapshot_run_storage(
        spec, run_id="run-01", arms={"arm-01": {"task_uid": "task-01"}},
        cold_snapshot_roots={"arm-01": str(roots["cold"])},
    )
    assert cleaned == ["arm-01"]
    assert all(not (root / "clawbox-snapshot-scheduling" / "run-01").exists()
               for root in roots.values())
    assert all((root / "clawbox-snapshot-scheduling" / "run-02" / "arm-01").exists()
               for root in roots.values())


def test_snapshot_branching_requires_an_absolute_cold_snapshot_root():
    raw = load_experiment(
        ROOT / "examples" / "experiments" / "snapshot-scheduling-correctness-kunpeng.yaml"
    ).model_dump(mode="json")
    raw.update(schema_version=3, design=None, suite=None, suite_ablations=False)
    raw["resources"]["cold_snapshot_root"] = None
    raw["policies"] = [raw["policies"][0]]
    with pytest.raises(ValueError, match="cold_snapshot_root"):
        from clawbox.experiments.spec import ExperimentSpec
        ExperimentSpec.model_validate(raw)


def test_public_host_plan_roundtrips_linux_paths_on_any_cli_host(tmp_path: Path):
    path = tmp_path / "snapshot-host.yaml"
    init_host_plan(path)
    plan = load_host_plan(path)
    assert plan.nodes["node0"].local_mount == "/mnt/clawbox-snapshot-node0"
    assert plan.nodes["node1"].shared_mount == "/mnt/clawbox-snapshot-shared-node1"


def test_formal_specs_are_fixed_16_by_8_with_two_repetitions():
    for name, pressure in (
        ("snapshot-scheduling-compute-kunpeng.yaml", "compute-memory"),
        ("snapshot-scheduling-disk-kunpeng.yaml", "local-disk"),
    ):
        spec = load_experiment(ROOT / "examples" / "experiments" / name)
        arms = expand_matrix(spec)
        assert len(arms) == 6
        assert {(arm.policy.name, arm.repetition) for arm in arms} == {
            (policy, repetition)
            for policy in ("affinity", "load-aware", "snapshot-aware")
            for repetition in (0, 1)
        }
        for arm in arms:
            scenario = arm.execution.snapshot_branching
            assert scenario.pressure == pressure
            assert len(scenario.parent_nodes) == 16
            assert scenario.parent_nodes.count("node0") == 12
            assert scenario.parent_nodes.count("node1") == 4
            assert scenario.branches_per_parent == 8
            assert sum(scenario.arrival_batches) == 128


def test_branching_manifest_is_hashed_without_replay_parsing():
    spec = load_experiment(
        ROOT / "examples" / "experiments" / "snapshot-scheduling-correctness-kunpeng.yaml"
    )
    for result in (validate_inputs(spec), validate_launch_inputs(spec)):
        assert len(result["traces"]) == 1
        assert result["traces"][0]["input_kind"] == "snapshot_branching_manifest"
        assert len(result["traces"][0]["sha256"]) == 64


def test_branching_qualification_is_bounded_but_covers_both_nodes():
    spec = load_experiment(
        ROOT / "examples" / "experiments" / "snapshot-scheduling-compute-kunpeng.yaml"
    )
    qualified = qualification_spec(spec)
    arms = expand_matrix(qualified)
    assert len(arms) == 3
    assert all(arm.concurrency == 4 for arm in arms)
    assert all(arm.execution.snapshot_branching.parent_nodes == ("node0", "node1") for arm in arms)
    assert all(arm.execution.snapshot_branching.branches_per_parent == 2 for arm in arms)
    demand = host_profile_for(spec)["cube_capacity_demand"]
    assert demand == {
        "vm_count": 64,
        "cpu_milli": 128000,
        "memory_mib": 196608,
        "max_vm_cpu_milli": 2000,
        "max_vm_memory_mib": 3072,
    }
