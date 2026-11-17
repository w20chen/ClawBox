from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace

import pytest

from clawbox.cube.lifecycle import CubeSandboxLifecycle, SandboxState
from clawbox.experiments.page_prefetch import (
    HotRange,
    SnapshotPagePrefetchController,
    _load_hotset,
    resolve_hot_ranges,
)
from clawbox.experiments.snapshot_pool import SnapshotKey
from clawbox.experiments.spec_types import SnapshotTier


def _write_hotset(path: Path) -> None:
    path.write_text(json.dumps({
        "schema_version": 1,
        "page_size": 4096,
        "roles": {
            "runtime": [{"gpa": 0, "length": 4096, "score": 0.75}],
            "tool": [{"gpa": 4096, "length": 4096, "score": 0.5}],
        },
    }), encoding="utf-8")


def _spec(manifest: Path, **overrides):
    values = {
        "hotset_manifest": str(manifest),
        "io_workers": 1,
        "signal_max_age_seconds": 5.0,
        "per_vm_prefetch_mib": 1,
        "max_inflight_mib": 1,
        "prefetch_budget_mib": 1,
        "max_ranges_per_vm": 16,
        "prefetch_bandwidth_mib_s": 1024.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_hotset_manifest_is_strict_and_sorted(tmp_path):
    manifest = tmp_path / "hotset.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "page_size": 4096,
        "roles": {
            "runtime": [
                {"gpa": 4096, "length": 4096, "score": 0.25},
                {"gpa": 0, "length": 4096, "score": 1.0},
            ],
            "tool": [{"gpa": 0, "length": 4096, "score": 0.5}],
        },
    }), encoding="utf-8")

    hotsets, digest = _load_hotset(manifest)

    assert [item.gpa for item in hotsets["runtime"]] == [0, 4096]
    assert len(digest) == 64
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["roles"]["tool"][0]["gpa"] = 1
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid range"):
        _load_hotset(manifest)


def test_resolve_hot_ranges_deduplicates_hardlinks_and_applies_caps(tmp_path):
    page = 4096
    snapshot = tmp_path / "guest.mem"
    layers = tmp_path / "guest.mem.layers"
    layers.mkdir()
    shared = tmp_path / "shared.mem"
    shared.write_bytes(b"A" * (8 * page))
    alias = tmp_path / "shared-alias.mem"
    os.link(shared, alias)
    private = layers / "000002.mem"
    private.write_bytes(b"B" * (8 * page))
    os.link(private, snapshot)
    Path(str(snapshot) + ".lineage.json").write_text(json.dumps({
        "schema_version": 2,
        "logical_bytes": 8 * page,
        "layers": [
            {"name": "000000.mem", "external_path": str(shared),
             "ranges": [{"gpa": 0, "offset": 0, "length": 4 * page}]},
            {"name": "000001.mem", "external_path": str(alias),
             "ranges": [{"gpa": 0, "offset": 0, "length": 4 * page}]},
            {"name": "000002.mem",
             "ranges": [{"gpa": 4 * page, "offset": 4 * page, "length": 4 * page}]},
        ],
    }), encoding="utf-8")

    selected, metrics = resolve_hot_ranges(
        snapshot, (HotRange(0, 8 * page, 1.0),),
        byte_budget=6 * page, max_ranges=8,
    )

    assert metrics["resolved_fragment_count"] == 3
    assert metrics["deduplicated_fragment_count"] == 2
    assert metrics["selected_bytes"] == 6 * page
    assert sum(item.length for item in selected) == 6 * page
    limited, limited_metrics = resolve_hot_ranges(
        snapshot, (HotRange(0, 8 * page, 1.0),),
        byte_budget=8 * page, max_ranges=1,
    )
    assert len(limited) == 1
    assert limited_metrics["selected_range_count"] == 1


def test_demand_cancellation_stops_a_pending_preparation(tmp_path):
    manifest = tmp_path / "hotset.json"
    _write_hotset(manifest)
    emitted = []
    completed = Event()

    class Lifecycle:
        def prepare_snapshot_pages(self, *, controller, cancel, context):
            assert cancel.wait(2)
            return {"status": "cancelled", "reason": "demand"}

    lifecycle = Lifecycle()
    controller = SnapshotPagePrefetchController(
        _spec(manifest), emit=lambda event: (emitted.append(dict(event)), completed.set()),
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


def test_controller_propagates_background_failures(tmp_path):
    manifest = tmp_path / "hotset.json"
    _write_hotset(manifest)
    completed = Event()

    class Lifecycle:
        def prepare_snapshot_pages(self, **_kwargs):
            raise RuntimeError("broken lineage")

    controller = SnapshotPagePrefetchController(
        _spec(manifest), emit=lambda _event: completed.set(),
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
