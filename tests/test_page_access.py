from array import array
import json
import os
from pathlib import Path
import struct
import zlib
import pytest

from clawbox.experiments.page_access import (
    PAGEMAP_PRESENT,
    SAMPLE_HEADER,
    SAMPLE_MAGIC,
    _PageIdleSegment,
    discover_virtual_ranges,
)
from clawbox.experiments.spec import SnapshotPageProfileSpec


def _fixture(tmp_path: Path):
    page = os.sysconf("SC_PAGE_SIZE")
    snapshot = tmp_path / "warm" / "vm" / "tool-g1.mem"
    layer = Path(str(snapshot) + ".layers") / "000000.mem"
    layer.parent.mkdir(parents=True)
    layer.write_bytes(b"\0" * (2 * page))
    Path(str(snapshot) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 2 * page,
        "layers": [{
            "name": "000000.mem",
            "ranges": [{"gpa": page, "offset": 0, "length": 2 * page}],
        }],
    }), encoding="utf-8")

    pid = 123
    proc = tmp_path / "proc"
    (proc / str(pid)).mkdir(parents=True)
    stat = layer.stat()
    device = f"{os.major(stat.st_dev):x}:{os.minor(stat.st_dev):x}"
    hva = 0x100000
    (proc / str(pid) / "maps").write_text(
        f"{hva:x}-{hva + 2 * page:x} rw-p 00000000 {device} {stat.st_ino} {layer}\n",
        encoding="utf-8",
    )
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.procs").write_text(str(pid), encoding="ascii")
    return page, pid, hva, proc, cgroup, snapshot


@pytest.mark.skipif(os.name != "posix", reason="Linux pagemap fixture")
def test_discovers_gpa_ranges_and_storage_tier(tmp_path):
    page, pid, hva, proc, cgroup, snapshot = _fixture(tmp_path)
    ranges, pids = discover_virtual_ranges(
        cgroup=cgroup,
        snapshot=snapshot,
        proc_root=proc,
        warm_root=tmp_path / "warm",
        cold_root=tmp_path / "cold",
    )
    assert pids == [pid]
    assert [(item.hva, item.gpa, item.length) for item in ranges] == [
        (hva, page, 2 * page),
    ]
    assert ranges[0].layer_index == 0
    assert ranges[0].layer_name == "000000.mem"
    assert ranges[0].layer_tier == "warm"


@pytest.mark.skipif(os.name != "posix", reason="Linux pagemap fixture")
def test_counts_accessed_sampling_windows_without_per_page_event_log(tmp_path):
    page, pid, hva, proc, cgroup, snapshot = _fixture(tmp_path)
    ranges, pids = discover_virtual_ranges(
        cgroup=cgroup, snapshot=snapshot, proc_root=proc,
        warm_root=tmp_path / "warm",
    )

    pagemap = bytearray((hva // page + 2) * 8)
    struct.pack_into("<Q", pagemap, hva // page * 8, PAGEMAP_PRESENT | 64)
    struct.pack_into("<Q", pagemap, (hva // page + 1) * 8, PAGEMAP_PRESENT | 65)
    (proc / str(pid) / "pagemap").write_bytes(pagemap)
    page_idle = tmp_path / "page-idle"
    page_idle.write_bytes(b"\0" * 24)

    output = tmp_path / "output"
    segment = _PageIdleSegment(
        output=output, ranges=ranges, pids=pids, generation=1, tier="warm",
        interval_s=100, max_windows=1, proc_root=proc, page_idle=page_idle,
        snapshot=snapshot,
    )
    segment.start()
    # PFN 64 was referenced (idle bit cleared); PFN 65 remained idle.
    with page_idle.open("r+b") as stream:
        stream.seek(8)
        stream.write(struct.pack("<Q", 1 << 1))
    sample = segment.sample()
    summary = segment.stop(reason="test")

    assert sample["present_pages"] == 2
    assert sample["accessed_pages"] == 1
    assert summary["windows"] == 1
    access = array("H")
    access.frombytes(zlib.decompress((output / "access-counts.u16.zlib").read_bytes()))
    present = array("H")
    present.frombytes(zlib.decompress((output / "present-counts.u16.zlib").read_bytes()))
    assert list(access) == [0, 1, 0]
    assert list(present) == [0, 1, 1]
    samples = (output / "samples.bin").read_bytes()
    assert samples.startswith(SAMPLE_MAGIC)
    assert len(samples) == len(SAMPLE_MAGIC) + SAMPLE_HEADER.size


def test_forced_profile_requires_an_explicit_tier_schedule():
    try:
        SnapshotPageProfileSpec(
            case_ids=("case-1",), force_model_wait_checkpoints=True,
        )
    except ValueError as exc:
        assert "checkpoint_tiers" in str(exc)
    else:
        raise AssertionError("missing checkpoint tier schedule was accepted")

