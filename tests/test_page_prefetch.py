from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from clawbox.cube.lifecycle import CubeSandboxLifecycle, SandboxState
from clawbox.experiments.page_prefetch import (
    FileRange,
    SnapshotPagePrefetchController,
    resolve_lineage_ranges,
    select_shared_ranges,
)
from clawbox.experiments.snapshot_pool import SnapshotKey
from clawbox.experiments.spec import SnapshotPageManagementSpec
from clawbox.experiments.spec_types import SnapshotTier


def _spec(**overrides):
    values = {
        "candidate_source": "shared_lineage",
        "io_workers": 1,
        "signal_max_age_seconds": 5.0,
        "per_vm_prefetch_mib": 1,
        "max_inflight_mib": 1,
        "prefetch_budget_mib": 1,
        "max_ranges_per_vm": 16,
        "prefetch_bandwidth_mib_s": 1024.0,
        "candidate_chunk_kib": 128,
        "batch_window_milliseconds": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _lineage(snapshot: Path, layers: list[dict], logical: int) -> None:
    Path(str(snapshot) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": logical,
        "layers": layers,
    }), encoding="utf-8")


def test_spec_uses_only_task_independent_shared_lineage_candidates():
    spec = SnapshotPageManagementSpec(prefetch_budget_mib=1024)
    assert spec.candidate_source == "shared_lineage"
    assert spec.candidate_chunk_kib == 128
    assert spec.batch_window_milliseconds == 25
    with pytest.raises(ValueError, match="power of two"):
        SnapshotPageManagementSpec(
            prefetch_budget_mib=1024, candidate_chunk_kib=12,
        )


def test_newest_lineage_version_excludes_overlapped_ancestor_pages(tmp_path):
    page = 4096
    snapshot = tmp_path / "guest.mem"
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    base = tmp_path / "base.mem"
    base.write_bytes(b"A" * (4 * page))
    head = layers / "000001.mem"
    head.write_bytes(b"B" * (4 * page))
    os.link(head, snapshot)
    _lineage(snapshot, [
        {"name": "000000.mem", "external_path": str(base),
         "ranges": [{"gpa": 0, "offset": 0, "length": 4 * page}]},
        {"name": "000001.mem",
         "ranges": [{"gpa": page, "offset": page, "length": 2 * page}]},
    ], 4 * page)

    selected, metrics = resolve_lineage_ranges(snapshot, chunk_bytes=page)

    physical = {(item.path, item.offset) for item in selected}
    assert (head, page) in physical and (head, 2 * page) in physical
    assert (base, page) not in physical and (base, 2 * page) not in physical
    assert metrics["stale_version_bytes_excluded"] == 2 * page
    assert metrics["authoritative_bytes"] == 4 * page


def test_shared_physical_range_wins_and_is_read_once(tmp_path):
    page = 4096
    shared = tmp_path / "shared.mem"
    shared.write_bytes(b"S" * (4 * page))
    private_a = tmp_path / "a.mem"
    private_b = tmp_path / "b.mem"
    private_a.write_bytes(b"A" * page)
    private_b.write_bytes(b"B" * page)
    common = FileRange(shared, 0, page, 0)
    selected, metrics = select_shared_ranges({
        1: (common, FileRange(private_a, 0, page, 1)),
        2: (common, FileRange(private_b, 0, page, 1)),
    }, byte_budget=page, max_ranges=1)

    assert selected == [(common, (1, 2))]
    assert metrics[1]["selected_bytes"] == page
    assert metrics[2]["selected_bytes"] == page
    assert metrics[1]["max_selected_shared_waiters"] == 2


def test_equal_benefit_selection_is_not_file_prefix_order(tmp_path):
    page = 4096
    backing = tmp_path / "lineage.mem"
    backing.write_bytes(b"X" * (128 * page))
    candidates = tuple(FileRange(backing, offset, page, 0) for offset in range(0, 128 * page, page))
    selected, _metrics = select_shared_ranges({1: candidates}, byte_budget=16 * page, max_ranges=16)
    offsets = sorted(item.offset for item, _tokens in selected)
    assert len(offsets) == 16
    assert offsets != list(range(0, 16 * page, page))


def test_controller_batches_two_waiters_and_reads_shared_inode_once(tmp_path):
    page = 4096
    shared = tmp_path / "shared.mem"
    shared.write_bytes(b"S" * page)
    snapshots = [tmp_path / "a.mem", tmp_path / "b.mem"]
    for snapshot in snapshots:
        _lineage(snapshot, [{
            "name": "000000.mem", "external_path": str(shared),
            "ranges": [{"gpa": 0, "offset": 0, "length": page}],
        }], page)
    emitted = []
    completed = Event()

    class Lifecycle:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def prepare_snapshot_pages(self, *, controller, cancel, context):
            return controller.prefetch_snapshot(self.snapshot, context["role"], cancel)

    controller = SnapshotPagePrefetchController(
        _spec(
            io_workers=2, prefetch_budget_mib=2, max_inflight_mib=1,
            batch_window_milliseconds=100,
        ),
        emit=lambda event: (
            emitted.append(dict(event)),
            completed.set() if len(emitted) == 2 else None,
        ),
    )
    lifecycles = [Lifecycle(snapshot) for snapshot in snapshots]
    try:
        for index, lifecycle in enumerate(lifecycles):
            assert controller.submit(
                lifecycle, session_id=f"s{index}", case_id=f"c{index}",
                role="runtime", request_id=f"r{index}",
                signal={"event": "prefill_start", "signal_age_seconds": 0},
            )
        assert completed.wait(3)
        controller.check()
    finally:
        controller.close(wait=True, cancel=True)
    assert sum(item["physical_read_bytes"] for item in emitted) == page
    assert [item["read_bytes"] for item in emitted] == [page, page]
    assert all(item["batch_waiter_count"] == 2 for item in emitted)


def test_demand_cancellation_stops_a_pending_preparation():
    emitted = []
    completed = Event()

    class Lifecycle:
        def prepare_snapshot_pages(self, *, controller, cancel, context):
            assert cancel.wait(2)
            return {"status": "cancelled", "reason": "demand"}

    lifecycle = Lifecycle()
    controller = SnapshotPagePrefetchController(
        _spec(), emit=lambda event: (emitted.append(dict(event)), completed.set()),
    )
    try:
        assert controller.submit(
            lifecycle, session_id="s", case_id="c", role="runtime",
            request_id="r", signal={"event": "prefill_start", "signal_age_seconds": 0},
        )
        assert controller.cancel(lifecycle, "r")
        assert completed.wait(3)
        controller.check()
    finally:
        controller.close(wait=True, cancel=True)
    assert emitted[0]["status"] == "cancelled"


def test_controller_propagates_background_failures():
    completed = Event()

    class Lifecycle:
        def prepare_snapshot_pages(self, **_kwargs):
            raise RuntimeError("broken lineage")

    controller = SnapshotPagePrefetchController(
        _spec(), emit=lambda _event: completed.set(),
    )
    try:
        assert controller.submit(
            Lifecycle(), session_id="s", case_id="c", role="tool",
            request_id="r", signal={"event": "prefill_start", "signal_age_seconds": 0},
        )
        assert completed.wait(3)
        with pytest.raises(RuntimeError, match="preparation failed"):
            controller.check()
    finally:
        controller.close(wait=True, cancel=True)


def _bare_lifecycle(tmp_path: Path, tier: SnapshotTier) -> CubeSandboxLifecycle:
    lifecycle = CubeSandboxLifecycle.__new__(CubeSandboxLifecycle)
    lifecycle._lock = RLock()
    lifecycle.snapshot_pool = None
    lifecycle._snapshot_key = SnapshotKey("session", "runtime", 1)
    lifecycle._state = SandboxState.SWAPPED
    lifecycle._snapshot_tier = tier
    lifecycle._generation = 1
    lifecycle.cold_snapshot_root = str(tmp_path)
    lifecycle.sandbox_id = "sandbox"
    lifecycle.role = "runtime"
    lifecycle._replica_memory_path = str(tmp_path / "runtime-g1.mem")
    lifecycle.snapshot_compression = "zstd-page"
    lifecycle.snapshot_compression_binary = "/bin/unused"
    lifecycle.snapshot_mechanism = "incremental-cow"
    lifecycle._compression_reuse_manifest = None
    return lifecycle


def test_warm_snapshot_preparation_is_explicitly_skipped(tmp_path):
    lifecycle = _bare_lifecycle(tmp_path, SnapshotTier.WARM)
    result = lifecycle.prepare_snapshot_pages(
        controller=None, cancel=Event(), context={},
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "warm_snapshot"


def test_cold_preparation_preserves_compression_reuse_manifest(tmp_path, monkeypatch):
    lifecycle = _bare_lifecycle(tmp_path, SnapshotTier.COLD)
    reuse = tmp_path / "reuse.json"

    def decompress(path, *, binary, retain_for_reuse):
        assert path == Path(lifecycle._replica_memory_path)
        assert retain_for_reuse is True
        return {"reuse_manifest_path": str(reuse), "decompressed_bytes": 4096}

    monkeypatch.setattr(
        "clawbox.cube.snapshot_compression.decompress_snapshot", decompress,
    )

    class Controller:
        def prefetch_snapshot(self, snapshot, role, cancel):
            assert snapshot == Path(lifecycle._replica_memory_path)
            assert role == "runtime"
            return {"read_bytes": 4096, "cancelled_for_demand": False}

    result = lifecycle.prepare_snapshot_pages(
        controller=Controller(), cancel=Event(), context={},
    )
    assert result["status"] == "ok"
    assert result["read_bytes"] == 4096
    assert lifecycle._compression_reuse_manifest == str(reuse)
