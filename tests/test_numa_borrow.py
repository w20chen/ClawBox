from pathlib import Path

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
    assert pool.borrowed_bytes == 60
    assert borrower.borrowed() == ("sandbox-abc",)
    borrower.return_local("sandbox-abc")
    assert pool.borrowed_bytes == 0
    assert leaf.joinpath("cpuset.mems.effective").read_text() == "0"


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
