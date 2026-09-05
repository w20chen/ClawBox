import time
from pathlib import Path

from clawbox.experiments.memory import (
    NodeMemorySampler, SandboxRSSSampler, read_vmstat_counter,
    sandbox_process_rss_bytes,
)


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


def test_sandbox_rss_sampler_reports_execution_increment(tmp_path: Path) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"containerd-shim-cube\0sandbox-a\0")
    status = process / "status"
    status.write_text("Name:\tcube\nVmRSS:\t100 kB\n", encoding="ascii")
    assert sandbox_process_rss_bytes("sandbox-a", proc_root=tmp_path) == 100 * 1024
    sampler = SandboxRSSSampler("sandbox-a", proc_root=tmp_path, interval_s=0.005)
    sampler.start()
    status.write_text("Name:\tcube\nVmRSS:\t340 kB\n", encoding="ascii")
    time.sleep(0.02)
    result = sampler.stop()
    assert result["validity"] == "valid"
    assert result["host_vm_rss_baseline_bytes"] == 100 * 1024
    assert result["host_vm_rss_peak_bytes"] == 340 * 1024
    assert result["actual_host_execution_increment_bytes"] == 240 * 1024
