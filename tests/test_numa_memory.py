from pathlib import Path
import pytest

from clawbox.experiments.memory import NumaNodeMemorySampler, read_numa_meminfo
from clawbox.experiments.memory import CgroupMemorySampler


def test_numa_meminfo_and_baseline_delta(tmp_path: Path) -> None:
    root = tmp_path / "sys"
    path = root / "devices/system/node/node1/meminfo"
    path.parent.mkdir(parents=True)
    path.write_text(
        "Node 1 MemTotal:       1000 kB\nNode 1 MemFree:         700 kB\n",
        encoding="ascii",
    )
    assert read_numa_meminfo(path) == (1000 * 1024, 700 * 1024)
    sampler = NumaNodeMemorySampler(1, sys_root=root, vmstat=tmp_path / "vmstat",
                                    storage=tmp_path)
    path.write_text(
        "Node 1 MemTotal:       1000 kB\nNode 1 MemFree:         600 kB\n",
        encoding="ascii",
    )
    assert sampler.current() == (100 * 1024, 600 * 1024)
    observation = sampler.observe()
    assert observation["numa_node"] == 1
    assert observation["experiment_used_delta_bytes"] == 100 * 1024


def test_local_cgroup_uses_absolute_usage_and_host_emergency_guard(tmp_path: Path) -> None:
    group = tmp_path / "group"
    group.mkdir()
    for name, value in {"memory.max": "1024000", "memory.current": "200000",
                        "cpuset.mems.effective": "0"}.items():
        (group / name).write_text(value)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 10000 kB\nMemAvailable: 7000 kB\n")
    sampler = CgroupMemorySampler(group, capacity_bytes=1024000, numa_node=0,
                                  meminfo=meminfo, storage=tmp_path)
    assert sampler.current() == (200000, 7000 * 1024)
    assert sampler.observe()["local_used_bytes"] == 200000
    (group / "memory.current").write_text("100000")
    assert sampler.current()[0] == 100000  # never baseline-subtracted
    with pytest.raises(ValueError, match="memory.max"):
        CgroupMemorySampler(group, capacity_bytes=1, storage=tmp_path)
    with pytest.raises(ValueError, match="cpuset"):
        CgroupMemorySampler(group, capacity_bytes=1024000, numa_node=1, storage=tmp_path)
