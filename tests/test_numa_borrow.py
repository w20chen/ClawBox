from pathlib import Path
import shutil
import subprocess
from threading import Thread
import time

import pytest

from clawbox.experiments.numa_borrow import (
    NumaBorrowUnavailable,
    SandboxNumaBorrower,
    read_numa_anon_bytes,
    read_numa_lru_bytes,
)
from clawbox.experiments.snapshot_pool import WarmSnapshotPool


def make_tree(tmp_path: Path):
    cgroup = tmp_path / "sys/fs/cgroup/cube_sandbox/sandbox"
    leaf = cgroup / "42"
    leaf.mkdir(parents=True)
    (cgroup / "memory.current").write_text("120")
    (cgroup / "memory.numa_stat").write_text(
        "inactive_anon N0=40 N1=10\nactive_anon N0=20 N1=10\n"
        "inactive_file N0=10 N1=5\nactive_file N0=10 N1=5\n"
        "unevictable N0=0 N1=0\n"
    )
    (leaf / "cpuset.mems").write_text("0")
    (leaf / "cpuset.mems.effective").write_text("0")
    (leaf / "memory.current").write_text("80")
    (leaf / "memory.numa_stat").write_text("active_anon N0=80 N1=0\n")
    (leaf / "cgroup.events").write_text("populated 0\n")
    proc = tmp_path / "proc/7"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"qemu\x00sandbox-abc")
    (proc / "cgroup").write_text("0::/cube_sandbox/sandbox/42\n")
    return cgroup, leaf, tmp_path / "proc"


def test_numa_usage_assigns_unattributed_charge_to_local(tmp_path: Path) -> None:
    cgroup, _, _ = make_tree(tmp_path)
    resident, unattributed = read_numa_lru_bytes(cgroup)
    assert resident == {0: 80, 1: 30}
    assert unattributed == 10


def test_anonymous_numa_usage_excludes_shared_file_cache(tmp_path: Path) -> None:
    _, leaf, _ = make_tree(tmp_path)
    (leaf / "memory.numa_stat").write_text(
        "inactive_anon N0=1 N1=20\nactive_anon N0=1 N1=40\n"
        "inactive_file N0=100 N1=0\nactive_file N0=100 N1=0\n"
    )
    assert read_numa_anon_bytes(leaf) == {0: 2, 1: 60}


def test_verified_vm_lookup_does_not_rescan_all_processes(tmp_path, monkeypatch):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(root, local_node=0, shared_node=1,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup')
    assert borrower._leaf_for('sandbox-abc') == leaf
    original = Path.iterdir
    def forbidden_scan(path):
        if path == proc:
            raise AssertionError('cached VM lookup scanned /proc')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', forbidden_scan)
    assert borrower._leaf_for('sandbox-abc') == leaf


def test_preallocated_leaf_avoids_initial_process_scan_and_is_reused(tmp_path, monkeypatch):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(root, local_node=0, shared_node=1,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup')
    original = Path.iterdir
    def forbidden_scan(path):
        if path == proc:
            raise AssertionError('preallocated VM lookup scanned /proc')
        return original(path)
    monkeypatch.setattr(Path, 'iterdir', forbidden_scan)
    borrower.pin_local('sandbox-abc', verified_cgroup=leaf)
    assert borrower._leaf_for('sandbox-abc') == leaf


def test_preallocated_leaf_must_be_inside_borrower_domain(tmp_path):
    root, _leaf, proc = make_tree(tmp_path)
    outside = tmp_path/'outside'
    outside.mkdir()
    (outside/'cpuset.mems').write_text('0')
    borrower = SandboxNumaBorrower(root, local_node=0, shared_node=1,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup')
    with pytest.raises(RuntimeError, match='outside the configured domain'):
        borrower.pin_local('sandbox-abc', verified_cgroup=outside)


def test_cached_vm_lookup_rechecks_pid_identity_and_cgroup(tmp_path):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(root, local_node=0, shared_node=1,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup')
    assert borrower._leaf_for('sandbox-abc') == leaf
    replacement = root/'43'
    replacement.mkdir()
    (replacement/'cpuset.mems').write_text('0')
    (proc/'7/cgroup').write_text('0::/cube_sandbox/sandbox/43\n')
    assert borrower._leaf_for('sandbox-abc') == replacement
    # PID reuse must not retain the old VM's authority over this leaf.
    (proc/'7/cmdline').write_bytes(b'qemu\0other-sandbox')
    (proc/'8').mkdir()
    (proc/'8/cmdline').write_bytes(b'qemu\0sandbox-abc')
    (proc/'8/cgroup').write_text('0::/cube_sandbox/sandbox/42\n')
    assert borrower._leaf_for('sandbox-abc') == leaf


def test_live_borrow_rebinds_leaf_and_conserves_pool(tmp_path: Path) -> None:
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)

    def write(path: Path, value: str) -> None:
        assert path == leaf / "cpuset.mems"
        path.write_text(value)
        (leaf / "cpuset.mems.effective").write_text(value)

    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / "sys/fs/cgroup", writer=write,
    )
    record = borrower.borrow("sandbox-abc", 60)
    assert record.target_node == 1
    assert record.effective_mems_before == "0"
    assert record.effective_mems_after == "1"
    assert record.physical_effect == "allocation_policy_changed_no_immediate_shared_growth"
    assert borrower.residency("sandbox-abc").charged_bytes == 80
    assert pool.borrowed_bytes == 60
    assert borrower.borrowed() == ("sandbox-abc",)
    borrower.return_local("sandbox-abc")
    assert pool.borrowed_bytes == 0
    assert leaf.joinpath("cpuset.mems.effective").read_text() == "0"


def test_return_keeps_reservation_for_residual_remote_pages(tmp_path):
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    def write(path, value):
        path.write_text(value)
        path.with_name(path.name + '.effective').write_text(value)
    b = SandboxNumaBorrower(cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path/'sys/fs/cgroup', writer=write)
    b.borrow('sandbox-abc', 60)
    (leaf/'memory.numa_stat').write_text('active_anon N0=50 N1=30\n')
    record = b.return_local('sandbox-abc')
    assert not record.reservation_released and record.shared_bytes_after == 30
    assert record.shared_reclaimed_bytes == 0
    assert record.physical_effect == 'local_policy_restored_residual_shared_residency'
    assert pool.borrowed_bytes == 60
    again = b.borrow('sandbox-abc', 60)
    assert again.reservation_reused
    assert again.effective_mems_after == '1'
    assert pool.borrowed_bytes == 60
    with pytest.raises(RuntimeError, match='already using shared'):
        b.borrow('sandbox-abc', 60)
    assert not b.return_local('sandbox-abc').reservation_released
    with pytest.raises(RuntimeError, match='reservation retained'):
        b.release_destroyed('sandbox-abc')
    (leaf/'memory.numa_stat').write_text('active_anon N0=80 N1=0\n')
    assert b.return_local('sandbox-abc').reservation_released
    assert pool.borrowed_bytes == 0


def test_local_pin_accepts_native_broad_cpuset_without_rewriting_it(tmp_path):
    root,leaf,proc=make_tree(tmp_path)
    (leaf/'cpuset.cpus.effective').write_text('0-3')
    (leaf/'cpuset.mems.effective').write_text('0-1')
    writes=[]
    def write(path,value):
        writes.append(path.name)
        path.write_text(value)
        path.with_name(path.name+'.effective').write_text(value)
    borrower=SandboxNumaBorrower(root,local_node=0,shared_node=1,cpus='0-1',
        shared_pool=WarmSnapshotPool(128),proc_root=proc,cgroup_mount=tmp_path/'sys/fs/cgroup',writer=write)
    borrower.pin_local('sandbox-abc')
    borrower._verify_cpus(leaf)
    assert (leaf/'cpuset.mems.effective').read_text()=='0-1'
    assert writes==[]


def test_local_pin_ignores_remote_file_cache_but_rejects_remote_anonymous_pages(tmp_path):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(
        root, local_node=0, shared_node=1, cpus=None,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup',
    )
    (leaf/'memory.numa_stat').write_text('active_file N0=1 N1=100\n')
    borrower.pin_local('sandbox-abc')
    assert borrower.placements[-1]['residency_basis'] == 'insufficient_anonymous_sample_initial_create'
    assert borrower.placements[-1]['local_residency_fraction'] is None
    (leaf/'memory.numa_stat').write_text(
        'active_anon N0=1 N1=100\nactive_file N0=100 N1=0\n'
    )
    borrower._initial_placement_verified.clear()
    (leaf/'memory.numa_stat').write_text(
        'active_anon N0=1 N1=16777216\nactive_file N0=100 N1=0\n'
    )
    with pytest.raises(RuntimeError, match='LOCAL placement did not take effect'):
        borrower.pin_local('sandbox-abc')


def test_local_pin_does_not_misclassify_warm_snapshot_pages_after_restore(tmp_path):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(
        root, local_node=0, shared_node=1, cpus=None,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup',
    )
    borrower.pin_local('sandbox-abc')
    (leaf/'memory.numa_stat').write_text('active_anon N0=1 N1=100\n')
    borrower.pin_local('sandbox-abc')
    assert borrower.placements[-1]['residency_basis'] == 'restore_policy_only'


def test_local_pin_reuses_fresh_priority_placement_evidence(monkeypatch, tmp_path):
    root, leaf, proc = make_tree(tmp_path)
    borrower = SandboxNumaBorrower(
        root, local_node=0, shared_node=1, cpus=None,
        shared_pool=WarmSnapshotPool(128), proc_root=proc,
        cgroup_mount=tmp_path/'sys/fs/cgroup',
    )
    monkeypatch.setattr(
        'clawbox.experiments.numa_borrow.read_numa_lru_bytes',
        lambda _path: pytest.fail('duplicate NUMA residency read'),
    )
    monkeypatch.setattr(
        'clawbox.experiments.numa_borrow.read_numa_anon_bytes',
        lambda _path: pytest.fail('duplicate NUMA anonymous read'),
    )
    borrower.pin_local(
        'sandbox-abc', verified_cgroup=leaf,
        verified_placement={
            'actual_residency_bytes': {0: 24 * 1024 * 1024},
            'actual_anon_residency_bytes': {0: 16 * 1024 * 1024},
            'unattributed_bytes': 4096,
        },
    )
    assert borrower.placements[-1]['actual_residency_bytes'] == {0: 24 * 1024 * 1024}
    assert borrower.placements[-1]['local_residency_fraction'] == 1.0


def test_local_pin_rejects_cgroup_that_excludes_requested_cpus(tmp_path):
    root,leaf,proc=make_tree(tmp_path)
    (leaf/'cpuset.cpus.effective').write_text('2-3')
    borrower=SandboxNumaBorrower(root,local_node=0,shared_node=1,cpus='0-1',
        shared_pool=WarmSnapshotPool(128),proc_root=proc,cgroup_mount=tmp_path/'sys/fs/cgroup')
    with pytest.raises(RuntimeError, match='excludes requested CPUs'):
        borrower.pin_local('sandbox-abc')


def test_local_pin_accepts_retained_borrow_reservation_after_restore(tmp_path):
    root, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)

    def write(path, value):
        path.write_text(value)
        path.with_name(path.name + '.effective').write_text(value)

    borrower = SandboxNumaBorrower(
        root, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / 'sys/fs/cgroup', writer=write,
    )
    borrower.borrow('sandbox-abc', 60)
    (leaf / 'memory.numa_stat').write_text('active_anon N0=50 N1=1\n')
    assert not borrower.return_local('sandbox-abc').reservation_released
    borrower.pin_local('sandbox-abc', verified_cgroup=leaf)
    assert borrower.borrowed() == ('sandbox-abc',)
    assert pool.borrowed_bytes == 60
    (leaf / 'memory.numa_stat').write_text('active_anon N0=50 N1=0\n')
    borrower.pin_local('sandbox-abc', verified_cgroup=leaf)
    assert borrower.borrowed() == ()
    assert pool.borrowed_bytes == 0


def test_post_write_failure_keeps_capacity_until_verified_cleanup(tmp_path):
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    def write(path, value):
        if value == '0':
            raise OSError('rollback failed')
        path.write_text(value)
        (leaf/'cpuset.mems.effective').write_text('0-1')
        (leaf/'memory.numa_stat').write_text('active_anon N0=50 N1=30\n')
    b = SandboxNumaBorrower(cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path/'sys/fs/cgroup', writer=write)
    with pytest.raises(RuntimeError, match='did not take effect'):
        b.borrow('sandbox-abc', 60)
    assert pool.borrowed_bytes == 60 and b.borrowed() == ('sandbox-abc',)
    (leaf/'memory.numa_stat').write_text('active_anon N0=0 N1=0\n')
    (leaf/'memory.current').write_text('0')
    b.release_destroyed('sandbox-abc')
    assert pool.borrowed_bytes == 0


def test_removed_leaf_releases_only_at_parent_pre_borrow_baseline(tmp_path):
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)

    def write(path, value):
        path.write_text(value)
        path.with_name(path.name + '.effective').write_text(value)

    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / 'sys/fs/cgroup', writer=write,
    )
    borrower.borrow('sandbox-abc', 60)
    shutil.rmtree(leaf)
    (cgroup / 'memory.numa_stat').write_text(
        'active_anon N0=80 N1=50\n'
    )
    (cgroup / 'memory.current').write_text('130')
    with pytest.raises(RuntimeError, match='pre-borrow baseline'):
        borrower.release_destroyed('sandbox-abc')
    assert pool.borrowed_bytes == 60

    # N1=30 was already present before this VM borrowed. It must not poison
    # later qualification runs or be charged to this destroyed VM.
    (cgroup / 'memory.numa_stat').write_text(
        'active_anon N0=80 N1=30\n'
    )
    (cgroup / 'memory.current').write_text('110')
    borrower.release_destroyed('sandbox-abc')
    assert pool.borrowed_bytes == 0


def test_destroy_cleanup_waits_for_delayed_cgroup_removal(tmp_path):
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)

    def write(path, value):
        path.write_text(value)
        path.with_name(path.name + '.effective').write_text(value)

    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / 'sys/fs/cgroup', writer=write,
        cleanup_timeout_seconds=0.5,
    )
    borrower.borrow('sandbox-abc', 60)

    def remove_leaf():
        time.sleep(0.05)
        shutil.rmtree(leaf)

    remover = Thread(target=remove_leaf)
    remover.start()
    borrower.release_destroyed('sandbox-abc')
    remover.join()
    assert pool.borrowed_bytes == 0


def test_destroy_cleanup_uses_privileged_reclaim_for_empty_leaf(tmp_path):
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    (leaf / 'memory.reclaim').write_text('')
    writes = []

    def write(path, value):
        writes.append((path, value))
        if path.name == 'cpuset.mems':
            path.write_text(value)
            path.with_name(path.name + '.effective').write_text(value)
        elif path.name == 'memory.reclaim':
            # Model the kernel reclaiming the destroyed VM's remote cache.
            (leaf / 'memory.numa_stat').write_text(
                'active_anon N0=50 N1=0\n'
            )
            (leaf / 'memory.current').write_text('50')

    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / 'sys/fs/cgroup', writer=write,
    )
    borrower.borrow('sandbox-abc', 60)
    (leaf / 'memory.numa_stat').write_text('active_anon N0=50 N1=30\n')

    borrower.release_destroyed('sandbox-abc')

    assert (leaf / 'memory.reclaim', '30') in writes
    assert pool.borrowed_bytes == 0


def test_borrow_failure_rolls_back_capacity(tmp_path: Path) -> None:
    cgroup, _, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / "sys/fs/cgroup",
        writer=lambda *_: (_ for _ in ()).throw(OSError("denied")),
    )
    with pytest.raises(OSError, match="denied"):
        borrower.borrow("sandbox-abc", 60)
    assert pool.borrowed_bytes == 0


def test_timed_out_borrow_is_recoverable_after_verified_local_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clawbox.experiments.numa_borrow import _NumaRebindTimeout
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)

    def write(path, value):
        if value == "1":
            raise _NumaRebindTimeout("timed out updating cpuset.mems")
        (leaf / "cpuset.mems").write_text("0")
        (leaf / "cpuset.mems.effective").write_text("0")
    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / "sys/fs/cgroup",
        cleanup_timeout_seconds=30, writer=write,
    )

    with pytest.raises(NumaBorrowUnavailable, match="timed out updating"):
        borrower.borrow("sandbox-abc", 60)

    assert pool.borrowed_bytes == 0
    assert borrower.borrowed() == ()


@pytest.mark.parametrize("failure", ["timeout", "killed", "failed"])
def test_incomplete_process_migration_never_accepts_effective_mask_or_queues_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    cgroup, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=64)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        # Linux publishes the new mask before its migration queue finishes.
        (leaf / "cpuset.mems.effective").write_text("1")
        assert command[-1] == "1"
        assert command[-3].endswith("numa_rebind.py")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 31)
        return subprocess.CompletedProcess(command, 137 if failure == "killed" else 1, "", "incomplete")

    monkeypatch.setattr(subprocess, "run", run)
    borrower = SandboxNumaBorrower(
        cgroup, local_node=0, shared_node=1, shared_pool=pool,
        proc_root=proc, cgroup_mount=tmp_path / "sys/fs/cgroup",
        cleanup_timeout_seconds=30,
    )
    with pytest.raises(RuntimeError, match="retain the reservation"):
        borrower.borrow("sandbox-abc", 60)
    assert len(calls) == 1
    assert pool.borrowed_bytes == 60
    assert borrower.borrowed() == ("sandbox-abc",)


def test_busy_shared_rebind_defers_borrow_without_reserving(tmp_path):
    from threading import Event
    root, leaf, proc = make_tree(tmp_path)
    pool = WarmSnapshotPool(128, borrow_capacity_bytes=128)
    borrower = SandboxNumaBorrower(root, local_node=0, shared_node=1,
        shared_pool=pool, proc_root=proc, cgroup_mount=tmp_path/'sys/fs/cgroup',
        writer=lambda *_: pytest.fail("busy rebind must not change the VM"))
    held, release = Event(), Event()
    def hold_other_domain_rebind():
        with pool.rebind_lock:
            held.set()
            release.wait(5)
    thread = Thread(target=hold_other_domain_rebind)
    thread.start()
    assert held.wait(2)
    try:
        with pytest.raises(NumaBorrowUnavailable, match="in progress"):
            borrower.borrow('sandbox-abc', 80)
        assert pool.borrowed_bytes == 0
        assert (leaf/'cpuset.mems').read_text() == '0'
        assert pool.snapshot()['active_transfers'] == {}
    finally:
        release.set()
        thread.join(2)
