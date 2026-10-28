import time
import mmap
from pathlib import Path

import pytest

from clawbox.experiments.worker import CgroupCpuSampler

from clawbox.experiments.memory import (
    numa_allocation_counter_deltas,
    read_numa_allocation_counters,
)


def write_process_rss(process, pages=25, start=7):
    (process / 'status').write_text(f'VmRSS:\t{pages * mmap.PAGESIZE // 1024} kB\n')
    (process / 'statm').write_text(f'10000 {pages} 0 0 0 0 0\n')
    fields = ['0'] * 50
    fields[0], fields[19], fields[21] = 'S', str(start), str(pages)
    (process / 'stat').write_text(f'{process.name} (cube process) ' + ' '.join(fields))


def test_cgroup_cpu_sampler_reports_actual_cores_and_throttling(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "cpuset.cpus.effective").write_text("0-3,8-11", encoding="ascii")
    stat = tmp_path / "cpu.stat"
    stat.write_text(
        "usage_usec 1000000\nuser_usec 700000\nsystem_usec 300000\n"
        "nr_throttled 2\nthrottled_usec 10000\n",
        encoding="ascii",
    )
    clock = iter((10.0, 11.0, 12.0))
    monkeypatch.setattr("clawbox.experiments.worker.time.monotonic", lambda: next(clock))
    sampler = CgroupCpuSampler(tmp_path)
    stat.write_text(
        "usage_usec 5000000\nuser_usec 3500000\nsystem_usec 1500000\n"
        "nr_throttled 5\nthrottled_usec 40000\n",
        encoding="ascii",
    )
    sample = sampler.sample()
    assert sample["capacity_cores"] == 8
    assert sample["average_cores"] == pytest.approx(4.0)
    assert sample["utilization_fraction"] == pytest.approx(0.5)
    stat.write_text(
        "usage_usec 7000000\nuser_usec 4900000\nsystem_usec 2100000\n"
        "nr_throttled 7\nthrottled_usec 55000\n",
        encoding="ascii",
    )
    summary = sampler.summary()
    assert summary["mean_cores"] == pytest.approx(3.0)
    assert summary["peak_interval_cores"] == pytest.approx(4.0)
    assert summary["mean_utilization_fraction"] == pytest.approx(3 / 8)
    assert summary["usage_usec_delta"] == 6_000_000
    assert summary["nr_throttled_delta"] == 5
    assert summary["throttled_usec_delta"] == 45_000


def test_numa_allocation_counters_remain_page_events_not_bytes(tmp_path: Path) -> None:
    root = tmp_path / "nodes"
    for node, values in {
        "node0": "numa_hit 10\nnuma_miss 2\nother_node 1\n",
        "node1": "numa_hit 20\nnuma_miss 3\nother_node 4\n",
    }.items():
        path = root / node / "numastat"
        path.parent.mkdir(parents=True)
        path.write_text(values, encoding="ascii")
    before = read_numa_allocation_counters(root)
    (root / "node0" / "numastat").write_text(
        "numa_hit 15\nnuma_miss 4\nother_node 2\n", encoding="ascii"
    )
    after = read_numa_allocation_counters(root)

    assert numa_allocation_counter_deltas(before, after) == {
        "node0": {"numa_hit": 5, "numa_miss": 2, "other_node": 1},
        "node1": {"numa_hit": 0, "numa_miss": 0, "other_node": 0},
    }
    assert numa_allocation_counter_deltas(after, before) is None

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


def test_light_lifecycle_observation_preserves_memory_totals(tmp_path, monkeypatch):
    from clawbox.experiments import memory
    group = tmp_path/'group'
    group.mkdir()
    (group/'memory.current').write_text('1000')
    (group/'memory.numa_stat').write_text('active_anon N0=600 N1=300\n')
    sampler = memory.NumaCgroupMemorySampler.__new__(memory.NumaCgroupMemorySampler)
    sampler.cgroup = group
    sampler.meminfo = tmp_path/'meminfo'
    sampler.meminfo.write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
    sampler.total, sampler.baseline_used = 800, 0
    sampler.local_numa_node, sampler.local_numa_nodes, sampler.shared_numa_node = 0, (0,), 1
    monkeypatch.setattr(memory, 'cgroup_memory_evidence',
                        lambda _: pytest.fail('light lifecycle observation must skip raw dumps'))
    sample = sampler.observe(details=False)
    assert sample['local_used_bytes'] == 700
    assert sample['shared_live_used_bytes'] == 300
    assert sample['combined_live_used_bytes'] == 1000
    assert sample['host_available_bytes'] == 400*1024
    assert 'cgroup_memory' not in sample


def test_sandbox_rss_sampler_reports_execution_increment(tmp_path: Path) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"containerd-shim-cube\0sandbox-a\0")
    status = process / "status"
    status.write_text("Name:\tcube\nVmRSS:\t100 kB\n", encoding="ascii")
    assert sandbox_process_rss_bytes("sandbox-a", proc_root=tmp_path) == 100 * 1024
    write_process_rss(process)
    sampler = SandboxRSSSampler("sandbox-a", proc_root=tmp_path, interval_s=0.005)
    sampler.start()
    write_process_rss(process, pages=85)
    time.sleep(0.02)
    result = sampler.stop()
    assert result["validity"] == "valid"
    assert result["host_vm_rss_baseline_bytes"] == 25 * mmap.PAGESIZE
    assert result["host_vm_rss_peak_bytes"] == 85 * mmap.PAGESIZE
    assert result["host_vm_rss_execution_increment_bytes"] == 60 * mmap.PAGESIZE


def test_rss_sampling_does_not_rescan_host_processes(tmp_path: Path, monkeypatch) -> None:
    process = tmp_path / "123"
    process.mkdir()
    (process / "cmdline").write_bytes(b"cube\0sandbox-a\0")
    (process / "status").write_text("VmRSS:\t100 kB\n")
    write_process_rss(process)
    sampler = SandboxRSSSampler("sandbox-a", proc_root=tmp_path)
    def unexpected_scan(self):
        raise AssertionError("sampling must use the discovered VM processes")
    monkeypatch.setattr(Path, "iterdir", unexpected_scan)
    sampler.start()
    assert sampler.stop()["host_vm_rss_peak_bytes"] == 25 * mmap.PAGESIZE
    # A reused PID must not silently become a measurement of another process.
    (process / "cmdline").write_bytes(b"unrelated\0")
    assert sandbox_process_rss_bytes("sandbox-a", process_paths=(process,)) is None


def test_host_cgroup_increment_is_distinct_from_rss_and_missing_samples_are_unknown(tmp_path):
    process = tmp_path / '123'; process.mkdir()
    (process / 'cmdline').write_bytes(b'cube\0sandbox-a\0')
    (process / 'status').write_text('VmRSS:\t100 kB\n')
    write_process_rss(process)
    group = tmp_path / 'group'; group.mkdir()
    charge = group / 'memory.current'; charge.write_text('1000000')
    (group/'cgroup.procs').write_text('123\n')
    sampler = SandboxRSSSampler('sandbox-a', proc_root=tmp_path, cgroup=group)
    charge.write_text('3000000')
    measured = sampler.stop()
    assert measured['host_cgroup_execution_increment_bytes'] == 2000000
    assert measured['host_vm_rss_execution_increment_bytes'] == 0  # unchanged RSS
    assert 'actual_host_execution_increment_bytes' not in measured
    missing = SandboxRSSSampler('sandbox-a', proc_root=tmp_path, cgroup=group)
    charge.unlink()
    result = missing.stop()
    assert result['host_cgroup_execution_increment_bytes'] is None
    assert result['host_cgroup_error']


def test_prepared_vm_sampling_discovers_only_its_cgroup_processes(tmp_path, monkeypatch):
    proc = tmp_path/'proc'; proc.mkdir()
    process = proc/'123'; process.mkdir()
    (process/'cmdline').write_bytes(b'cube\0sandbox-a\0')
    (process/'status').write_text('VmRSS:\t100 kB\n')
    write_process_rss(process)
    group = tmp_path/'vm'; group.mkdir()
    (group/'memory.current').write_text('1000')
    (group/'cgroup.procs').write_text('123\n')
    def unexpected_scan(_):
        raise AssertionError('known VM cgroup must not scan global /proc')
    monkeypatch.setattr(Path, 'iterdir', unexpected_scan)
    sampler = SandboxRSSSampler('sandbox-a', proc_root=proc, cgroup=group)
    assert sampler.stop()['host_vm_rss_baseline_bytes'] == 25*mmap.PAGESIZE


def test_active_sampler_reads_fresh_charge_without_reopening_counter(tmp_path, monkeypatch):
    process = tmp_path/'123'; process.mkdir()
    (process/'cmdline').write_bytes(b'cube\0sandbox-a\0')
    write_process_rss(process)
    group = tmp_path/'group'; group.mkdir()
    charge = group/'memory.current'; charge.write_text('1000')
    (group/'cgroup.procs').write_text('123\n')
    sampler = SandboxRSSSampler('sandbox-a', proc_root=tmp_path, cgroup=group)
    sampler.start()
    descriptor = sampler._charge_reader
    charge.write_text('3000')
    read_text = Path.read_text
    def read(path, *args, **kwargs):
        if path == charge:
            pytest.fail('active sampling must use the retained counter descriptor')
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    result = sampler.stop()
    assert result['host_cgroup_execution_increment_bytes'] == 2000
    assert descriptor.closed


def test_rss_reader_rejects_replaced_process_and_closes_files(tmp_path):
    from clawbox.experiments.memory import ProcessRSSReader
    process = tmp_path/'123'; process.mkdir()
    write_process_rss(process, start=8)
    reader = ProcessRSSReader((process,), {('123', 7): {}})
    assert reader.read() is None
    assert reader.files == []


@pytest.mark.skipif(not Path('/proc/self/statm').exists(), reason='Linux proc FD semantics')
def test_rss_proc_descriptor_does_not_read_a_replacement_pid():
    import subprocess
    import sys
    from clawbox.experiments.memory import ProcessRSSReader
    process = subprocess.Popen([sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(30)'], stdout=subprocess.PIPE)
    try:
        assert process.stdout.readline().strip() == b'ready'
        path = Path('/proc')/str(process.pid)
        fields = (path/'stat').read_text().rsplit(')', 1)[1].split()
        reader = ProcessRSSReader((path,), {(path.name, int(fields[19])): {}})
        try:
            rss = int(next(row.split()[1] for row in (path/'status').read_text().splitlines() if row.startswith('VmRSS:'))) * 1024
            assert reader.read() == rss
            process.terminate(); process.wait(timeout=5)
            assert reader.read() is None
        finally:
            reader.close()
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
