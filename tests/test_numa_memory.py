from pathlib import Path

from clawbox.experiments.memory import NumaNodeMemorySampler, read_numa_meminfo


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
