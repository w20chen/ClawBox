from pathlib import Path

from clawbox.experiments.memory import NodeMemorySampler, read_vmstat_counter


def test_vmstat_oom_counter_and_memory_observation_are_explicit(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    vmstat = tmp_path / "vmstat"
    meminfo.write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n", encoding="ascii")
    vmstat.write_text("pgfault 10\noom_kill 3\n", encoding="ascii")
    sampler = NodeMemorySampler(
        meminfo=meminfo, vmstat=vmstat, storage=tmp_path, interval_s=0.01,
    )
    observation = sampler.observe()
    assert observation == {
        "metric": "host_meminfo_memavailable",
        "host_mem_total_bytes": 1000 * 1024,
        "host_used_bytes": 600 * 1024,
        "host_available_bytes": 400 * 1024,
        "experiment_used_delta_bytes": 0,
    }
    assert read_vmstat_counter(vmstat, "oom_kill") == 3
    vmstat.write_text("oom_kill 5\n", encoding="ascii")
    summary = sampler.stop()
    assert summary.host_oom_kill_events == 2
