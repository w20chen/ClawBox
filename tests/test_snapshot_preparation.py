"""Ordering, cancellation and resource bounds for snapshot preparation."""
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from clawbox.experiments.page_prefetch import SnapshotPreparationSession, SnapshotPagePrefetchController
from clawbox.experiments.spec import SnapshotPageManagementSpec


def session(**options):
    spec = SnapshotPageManagementSpec(prefetch_budget_mib=128, mode="state_driven", **options)
    calls, cancelled = [], []
    controller = SimpleNamespace(spec=spec,
        submit=lambda vm, **kw: (calls.append((vm, kw)) or True),
        cancel=lambda vm, request: cancelled.append((vm, request)))
    class VM:
        preparation_generation = None
        preparation_cold_generation = None
        cold_ready = None
        def set_snapshot_cold_ready(self, callback):
            self.cold_ready = callback
    runtime, tool = VM(), VM()
    state = SnapshotPreparationSession(controller, session_id="s", case_id="c", runtime=runtime, tool=tool)
    state.begin("r")
    return state, runtime, tool, calls, cancelled


@pytest.mark.parametrize("event_first", [True, False])
def test_checkpoint_and_phase_order_converge_without_duplicate_work(event_first):
    state, runtime, tool, calls, _ = session()
    if not event_first:
        runtime.preparation_generation = tool.preparation_generation = 1
        runtime.preparation_cold_generation = tool.preparation_cold_generation = 1
        state.checkpoint_ready()
    state.observe("r", {"event": "prefill_start"})
    if event_first:
        assert not calls
        runtime.preparation_generation = tool.preparation_generation = 1
        runtime.preparation_cold_generation = tool.preparation_cold_generation = 1
        state.checkpoint_ready()
    state.checkpoint_ready()
    state.observe("r", {"event": "prefill_start"})
    assert len(calls) == 1 and calls[0][0] is runtime
    assert calls[0][1]["signal"]["expected_generation"] == 1
    state.observe("r", {"event": "first_token"})
    assert len(calls) == 2 and calls[1][0] is tool


@pytest.mark.parametrize("end", ["finish", "cancel", "close"])
def test_demand_or_cleanup_before_checkpoint_prevents_late_preparation(end):
    state, runtime, tool, calls, _ = session()
    state.observe("r", {"event": "prefill_start"})
    getattr(state, end)(*(() if end == "close" else ("r",)))
    runtime.preparation_generation = tool.preparation_generation = 1
    runtime.preparation_cold_generation = tool.preparation_cold_generation = 1
    state.checkpoint_ready()
    state.observe("r", {"event": "first_token"})
    assert not calls


def test_tool_demand_and_disabled_tool_do_not_prepare_tool():
    state, runtime, tool, calls, cancelled = session(tool_trigger_event="disabled")
    runtime.preparation_generation = tool.preparation_generation = 1
    runtime.preparation_cold_generation = tool.preparation_cold_generation = 1
    state.observe("r", {"event": "first_token"})
    assert not calls
    state.tool_demand()
    assert (tool, "r") in cancelled
    state.observe("r", {"event": "prefill_start"})
    assert len(calls) == 1 and calls[0][0] is runtime


def test_invalid_or_stale_event_does_not_latch():
    state, runtime, _, calls, _ = session()
    for age in (True, -1, 10, float("nan"), float("inf")):
        state.observe("r", {"event": "prefill_start", "signal_age_seconds": age})
    runtime.preparation_generation = 1
    runtime.preparation_cold_generation = 1
    state.checkpoint_ready()
    assert not calls


def test_legacy_event_mode_does_not_retry_after_commit():
    state, runtime, _, calls, _ = session()
    state.controller.spec = SnapshotPageManagementSpec(prefetch_budget_mib=128)
    state.observe("r", {"event": "prefill_start"})
    runtime.preparation_generation = 1
    state.checkpoint_ready()
    assert not calls


def test_state_driven_waits_for_cold_publication_and_submits_once():
    state, runtime, _, calls, _ = session()
    runtime.preparation_generation = 4
    state.observe("r", {"event": "prefill_start"})
    state.checkpoint_ready()
    assert not calls
    runtime.preparation_cold_generation = 4
    runtime.cold_ready(4)
    runtime.cold_ready(4)
    assert len(calls) == 1
    assert calls[0][1]["signal"]["expected_generation"] == 4
    assert calls[0][1]["signal"]["preparation_reason"] == "cold_snapshot_ready"


def test_state_driven_submits_immediately_when_checkpoint_is_already_cold():
    state, runtime, _, calls, _ = session()
    runtime.preparation_generation = runtime.preparation_cold_generation = 7
    state.observe("r", {"event": "prefill_start"})
    assert len(calls) == 1
    assert calls[0][1]["signal"]["expected_generation"] == 7


def test_cold_publication_requires_current_generation():
    state, runtime, _, calls, _ = session()
    runtime.preparation_generation = 4
    state.observe("r", {"event": "prefill_start"})
    state.checkpoint_ready()
    runtime.preparation_cold_generation = 5
    runtime.cold_ready(4)
    assert not calls
    runtime.cold_ready(5)
    assert len(calls) == 1
    assert calls[0][1]["signal"]["expected_generation"] == 5


@pytest.mark.parametrize("end", ["finish", "cancel", "close"])
def test_wait_ending_after_warm_checkpoint_prevents_cold_retry(end):
    state, runtime, _, calls, _ = session()
    runtime.preparation_generation = 4
    state.observe("r", {"event": "prefill_start"})
    state.checkpoint_ready()
    getattr(state, end)(*(() if end == "close" else ("r",)))
    runtime.preparation_cold_generation = 4
    if runtime.cold_ready is not None:
        runtime.cold_ready(4)
    assert not calls
    if end == "close":
        assert runtime.cold_ready is None


def test_gpu_event_mode_does_not_retry_on_cold_publication():
    state, runtime, _, calls, _ = session()
    state.controller.spec = SnapshotPageManagementSpec(prefetch_budget_mib=128)
    state.observe("r", {"event": "prefill_start"})
    runtime.preparation_generation = runtime.preparation_cold_generation = 1
    runtime.cold_ready(1)
    assert not calls


def test_expansion_budget_skips_before_decompression(tmp_path, monkeypatch):
    from test_page_prefetch import _bare_lifecycle
    from clawbox.experiments.spec_types import SnapshotTier
    lifecycle = _bare_lifecycle(tmp_path, SnapshotTier.COLD)
    Path(lifecycle._replica_memory_path + ".zstd-pages.json").write_text(
        json.dumps({
            "metrics": {"decompression_allocated_bytes": 2 * 1024 * 1024},
        }), encoding="utf-8")
    monkeypatch.setattr("clawbox.cube.snapshot_compression.decompress_snapshot",
                        lambda *a, **k: pytest.fail("budget must be checked before expanding"))
    controller = SnapshotPagePrefetchController(
        SnapshotPageManagementSpec(prefetch_budget_mib=128, expansion_budget_mib=1), emit=lambda _: None)
    try:
        result = lifecycle.prepare_snapshot_pages(controller=controller, cancel=Event(), context={"request_id": "r"})
        assert result["reason"] == "expansion_budget"
    finally:
        controller.close(wait=True, cancel=True)


def test_generation_change_skips_stale_preparation(tmp_path):
    from test_page_prefetch import _bare_lifecycle
    from clawbox.experiments.spec_types import SnapshotTier
    lifecycle = _bare_lifecycle(tmp_path, SnapshotTier.COLD)
    result = lifecycle.prepare_snapshot_pages(controller=None, cancel=Event(), context={"expected_generation": 0})
    assert result["reason"] == "snapshot_generation_changed"


def test_checkpoint_notifies_preparation_only_after_releasing_pair_fence():
    from clawbox.experiments.policy import PolicyCoordinator
    from clawbox.experiments.spec import PolicySpec
    class VM:
        resident = True
        def checkpoint_and_evict(self, **kw):
            self.resident = False
            return 0.1
    policy = PolicySpec(name="wait", admission="off", reclamation="snapshot_pause",
                        eviction="wait_aware_pressure", restore="reactive", runtime_snapshot=True)
    coordinator = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0, operation_headroom_mib=0)
    tool, runtime, observed = VM(), VM(), []
    coordinator.register("s", tool)
    coordinator.register_runtime("s", runtime, snapshot_ready=lambda: observed.append(
        (tool.resident, runtime.resident, coordinator._sessions["s"].eviction_in_progress)))
    coordinator.begin_model_wait("s", "r")
    assert coordinator.evict_one_for_watermark()
    assert observed == [(False, False, False)]
