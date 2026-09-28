from pathlib import Path
import pytest

from clawbox.experiments.memory import NumaNodeMemorySampler, read_numa_meminfo
from clawbox.experiments.memory import CgroupMemorySampler, NumaCgroupMemorySampler


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


def test_local_cache_reclaim_excludes_shmem_and_keeps_actual_accounting(tmp_path: Path) -> None:
    for name, value in {"memory.max": "1024000", "memory.current": "200000",
                        "memory.stat": "file 100000\nshmem 30000\n"}.items():
        (tmp_path / name).write_text(value)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 10000 kB\nMemAvailable: 7000 kB\n")
    sampler = CgroupMemorySampler(tmp_path, capacity_bytes=1024000,
                                  meminfo=meminfo, storage=tmp_path)
    result = sampler.reclaim_file_cache(maximum_bytes=80000)
    assert (tmp_path / "memory.reclaim").read_text() == "70000"
    assert result["observed_net_reclaimed_bytes"] == 0
    assert sampler.current()[0] == 200000


def test_numa_cgroup_separates_local_and_shared_live_memory(tmp_path: Path) -> None:
    group = tmp_path / "group"
    group.mkdir()
    (group / "memory.max").write_text("1000")
    (group / "memory.current").write_text("700")
    (group / "memory.events").write_text("oom_kill 0\n")
    (group / "cpuset.mems.effective").write_text("0-1")
    (group / "memory.numa_stat").write_text(
        "inactive_anon N0=200 N1=100\nactive_anon N0=100 N1=100\n"
        "inactive_file N0=50 N1=50\nactive_file N0=0 N1=0\n"
        "unevictable N0=0 N1=0\n"
    )
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 10000 kB\nMemAvailable: 7000 kB\n")
    sampler = NumaCgroupMemorySampler(
        group, local_capacity_bytes=360, total_capacity_bytes=1000,
        local_numa_node=0, shared_numa_node=1,
        meminfo=meminfo, storage=tmp_path,
    )
    # 100 bytes are not attributable by NUMA and are conservatively LOCAL.
    assert sampler.tier_usage() == (450, 250, 700)
    assert sampler.current() == (450, 7000 * 1024)
    assert sampler.observe()["shared_live_used_bytes"] == 250
    sampler.start()
    meminfo.write_text("MemTotal: 10000 kB\nMemAvailable: 6000 kB\n")
    summary = sampler.stop()
    assert summary.host_baseline_used_bytes == 3000 * 1024
    assert summary.host_peak_used_delta_bytes == 1000 * 1024
    assert summary.host_min_used_delta_bytes == 0
