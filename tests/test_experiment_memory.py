import time
from pathlib import Path

from clawbox.experiments.memory import (
    NodeMemorySampler, SandboxRSSSampler, read_vmstat_counter,
    sandbox_process_rss_bytes,
    sandbox_process_fault_counters, sandbox_process_counter_deltas,
)


def test_host_fault_counters_handle_comm_parentheses_and_pid_reuse(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"cube\0sandbox-a\0")

    def write_stat(start, minor, major):
        fields = ["0"] * 20
        fields[0], fields[7], fields[9], fields[19] = "S", str(minor), str(major), str(start)
        (process / "stat").write_text("123 (cube (worker)) " + " ".join(fields), encoding="ascii")

    write_stat(500, 100, 2)
    (process / "io").write_text("read_bytes: 4096\n", encoding="ascii")
    before = sandbox_process_fault_counters("sandbox-a", (process,))
    write_stat(500, 130, 5)
    (process / "io").write_text("read_bytes: 12288\n", encoding="ascii")
    after = sandbox_process_fault_counters("sandbox-a", (process,))
    assert sandbox_process_counter_deltas(before, after) == {
        "host_vm_minor_faults_delta": 30,
        "host_vm_major_faults_delta": 3,
        "host_vm_disk_read_bytes_delta": 8192,
    }
    write_stat(501, 140, 6)
    reused = sandbox_process_fault_counters("sandbox-a", (process,))
    assert all(value is None for value in sandbox_process_counter_deltas(before, reused).values())
    (process / "stat").unlink()
    assert sandbox_process_fault_counters("sandbox-a", (process,)) is None


def test_fault_counters_do_not_require_io_permission(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"cube\0sandbox-a\0")
    (process / "stat").write_text("123 (cube) " + " ".join(["0"] * 20), encoding="ascii")
    sample = sandbox_process_fault_counters("sandbox-a", (process,))
    delta = sandbox_process_counter_deltas(sample, sample)
    assert delta["host_vm_minor_faults_delta"] == 0
    assert delta["host_vm_major_faults_delta"] == 0
    assert delta["host_vm_disk_read_bytes_delta"] is None


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


def test_rss_sampling_does_not_rescan_host_processes(tmp_path: Path, monkeypatch) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"cube\0sandbox-a\0")
    (process / "status").write_text("VmRSS:\t100 kB\n")
    sampler = SandboxRSSSampler("sandbox-a", proc_root=tmp_path)
    def unexpected_scan(self):
        raise AssertionError("sampling must use the discovered VM processes")
    monkeypatch.setattr(Path, "iterdir", unexpected_scan)
    sampler.start()
    assert sampler.stop()["host_vm_rss_peak_bytes"] == 100 * 1024
    # A reused PID must not silently become a measurement of another process.
    (process / "cmdline").write_bytes(b"unrelated\0")
    assert sandbox_process_rss_bytes("sandbox-a", process_paths=(process,)) is None
