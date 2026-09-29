from pathlib import Path
from types import SimpleNamespace

import pytest

from clawbox.experiments.host import init_config, load_config
from clawbox.experiments.memory import NumaCgroupMemorySampler
from clawbox.experiments.numa_borrow import SandboxNumaBorrower
from clawbox.experiments.snapshot_pool import WarmCapacityError, WarmSnapshotPool
from clawbox.experiments.spec import ResourcesSpec, ReclamationPolicy
from clawbox.experiments.topology import ComputeNode, place_session
from clawbox.experiments.worker import WatermarkController


def nodes():
    return tuple(ComputeNode(node_id=f"node{i}", numa_node=i, cpus=f"{i*80}-{i*80+79}",
                             memory_capacity_mib=36864, low_watermark_mib=28672,
                             high_watermark_mib=32768) for i in (0, 1))


def resources(**changes):
    return ResourcesSpec(**(dict(compute_nodes=nodes(), target_node="host", pool_memory_budget_mib=65536,
                                emergency_free_memory_mib=512, local_memory_cgroup="/vm",
                                warm_numa_node=2, warm_memory_capacity_mib=131072,
                                shared_memory_borrow_limit_mib=65536) | changes))


def test_host_init_and_resource_aggregation(tmp_path):
    path = tmp_path / "host.yaml"
    init_config(path)
    config = load_config(path)
    assert [n.numa_node for n in config.compute_nodes] == [0, 1]
    assert config.warm_node == 2
    assert "local_gib:" not in path.read_text()
    assert resources().local_memory_capacity_mib == 73728
    assert resources().local_memory_high_watermark_mib == 65536
    with pytest.raises(ValueError, match="sum"):
        resources(local_memory_capacity_mib=36864)
    with pytest.raises(ValueError, match="shared"):
        resources(warm_numa_node=1)
    with pytest.raises(ValueError, match="CPU"):
        resources(compute_nodes=(nodes()[0], nodes()[1].model_copy(update={"cpus": "0-79"})))


def test_session_placement_is_stable_and_can_be_explicit():
    assert [place_session(nodes(), i).node_id for i in range(4)] == ["node0", "node1", "node0", "node1"]
    assert place_session(nodes(), 0, ("node1", "node0")).numa_node == 1


def test_configure_and_validate_explicit_mapping(tmp_path):
    from clawbox.experiments.configure import configure_experiment, experiment_overview
    from clawbox.experiments.spec import ExperimentSpec
    spec = configure_experiment(
        Path("examples/experiments/getting-started.yaml"),
        compute_nodes=[n.model_dump() for n in nodes()],
        placement_policy="explicit", session_compute_nodes=["node1", "node0"],
        concurrency="1,2", pool_memory_gib=64, warm_numa_node=2,
        warm_memory_capacity_mib=131072, shared_memory_borrow_limit_mib=65536,
        local_memory_cgroup="/vm",
    )
    assert experiment_overview(spec)["session_compute_nodes"] == ["node1", "node0"]
    import yaml
    path = tmp_path / "explicit.yaml"
    path.write_text(yaml.safe_dump(spec.model_dump(mode="json")))
    round_robin = configure_experiment(path, placement_policy="round_robin")
    assert round_robin.execution.session_compute_nodes == ()
    raw = spec.model_dump(mode="json")
    raw["execution"]["session_compute_nodes"] = ["node0"]
    with pytest.raises(ValueError, match="one node ID"):
        ExperimentSpec.model_validate(raw)
    raw["execution"]["session_compute_nodes"] = ["node0", "missing"]
    with pytest.raises(ValueError, match="unknown"):
        ExperimentSpec.model_validate(raw)


def test_node_pressure_is_independent_and_aggregate_does_not_double_count(tmp_path):
    gib = 1024**3
    for name, value in {"memory.max": str(136*gib), "memory.current": str(36*gib),
                        "cpuset.mems.effective": "0-2", "memory.events": "oom 0\n",
                        "memory.numa_stat": f"active_anon N0={33*gib} N1={gib} N2={gib}\n"}.items():
        (tmp_path / name).write_text(value)
    samplers = [NumaCgroupMemorySampler(tmp_path, local_capacity_bytes=36*gib,
                total_capacity_bytes=136*gib, local_numa_node=i, shared_numa_node=2,
                storage=tmp_path) for i in (0, 1)]
    aggregate = NumaCgroupMemorySampler(tmp_path, local_capacity_bytes=72*gib,
                total_capacity_bytes=136*gib, local_numa_node=0, local_numa_nodes=(0, 1),
                shared_numa_node=2, storage=tmp_path)
    # Unattributed kernel charge is conservatively charged to each admission
    # domain, but only once in the physical aggregate.
    assert samplers[0].tier_usage() == (34*gib, gib, 36*gib)
    assert samplers[1].tier_usage()[0] == 2*gib
    assert aggregate.tier_usage()[0] == 35*gib
    blocked = [False, False]
    pool = WarmSnapshotPool(128*gib, borrow_capacity_bytes=64*gib)
    for i in (0, 1):
        coordinator = SimpleNamespace(set_new_session_admission_blocked=lambda b, i=i: blocked.__setitem__(i, b))
        arm = SimpleNamespace(resources=resources(), policy=SimpleNamespace(reclamation=ReclamationPolicy.RESIDENT))
        arm.resources = arm.resources.model_copy(update={"local_memory_low_watermark_mib": 28672,
            "local_memory_high_watermark_mib": 32768, "local_memory_capacity_mib": 36864})
        controller = WatermarkController(arm, coordinator, samplers[i],
            SimpleNamespace(shared_pool=pool, node_id=f"node{i}"), SimpleNamespace(write=lambda row: None))
        controller._sample_once(.2, False)
    assert blocked == [True, False]


def test_two_nodes_share_borrow_capacity_and_keep_cpu_placement(tmp_path):
    group = tmp_path / "group"
    group.mkdir()
    (group / "memory.current").write_text("0")
    (group / "memory.numa_stat").write_text("active_anon N0=0 N1=0 N2=0\n")
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    borrowers = []
    for i in (0, 1):
        leaf = group / str(i)
        leaf.mkdir()
        def write(path, value):
            path.write_text(value)
            path.with_name(path.name + ".effective").write_text(value)
        b = SandboxNumaBorrower(group, local_node=i, shared_node=2, shared_pool=pool,
                               cpus=nodes()[i].cpus, node_id=f"node{i}", writer=write)
        b._leaf_for = lambda sandbox_id, leaf=leaf: leaf
        b.pin_local(f"vm{i}")
        borrowers.append(b)
    borrowers[0].borrow("vm0", 40)
    with pytest.raises(WarmCapacityError):
        borrowers[1].borrow("vm1", 40)
    assert (group / "1/cpuset.mems.effective").read_text() == "1"
    borrowers[0].return_local("vm0")
    borrowers[1].borrow("vm1", 40)
    assert (group / "1/cpuset.cpus.effective").read_text() == "80-159"
    assert (group / "1/cpuset.mems.effective").read_text() == "2"
    borrowers[1].release_destroyed("vm1")
    borrowers[1].pin_local("vm1-restored")
    assert borrowers[1].placements[-1]["effective_mems"] == "1"
    (group / "1/cpuset.cpus.effective").write_text("0-79")
    with pytest.raises(RuntimeError, match="CPU pin"):
        borrowers[1].borrow("vm1-restored", 40)
    assert pool.borrowed_bytes == 0
