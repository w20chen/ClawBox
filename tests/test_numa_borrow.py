from pathlib import Path
import shutil
from threading import Thread
import time

import pytest

from clawbox.experiments.numa_borrow import SandboxNumaBorrower, read_numa_lru_bytes
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
    with pytest.raises(RuntimeError, match='reservation retained'):
        b.release_destroyed('sandbox-abc')
    (leaf/'memory.numa_stat').write_text('active_anon N0=80 N1=0\n')
    assert b.return_local('sandbox-abc').reservation_released
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
