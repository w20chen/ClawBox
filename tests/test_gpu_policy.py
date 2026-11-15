from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.spec import PolicySpec


class Lifecycle:
    resident = True


def coordinator(**options):
    policy = PolicySpec(
        name="gpu", admission="predicted", reclamation="snapshot_pause",
        eviction="gpu_aware_pressure", restore="reactive", **options,
    )
    return PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0,
                             operation_headroom_mib=0)


def waiting(c, sid="session", rid="request"):
    c.register(sid, Lifecycle())
    c.register_runtime(sid, Lifecycle())
    c.begin_model_wait(sid, rid)


def signal(c, *, sid="session", rid="request", event="queued", role="A", **fields):
    return c.observe_gpu_signal(sid, rid, {
        "request_id": rid, "session_id": sid, "event": event,
        "role": role, "engine_id": role, "source": "trace", **fields,
    })


def test_gpu_reclaim_prefers_longer_observed_prefill_without_predicting_decode(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator(gpu_reclaim_min_remaining_seconds=2)
    for sid in ("unknown", "short", "long"):
        waiting(c, sid)
    assert c.victim_for_restore("other") is None
    signal(c, sid="short", est_wait_s=3)
    signal(c, sid="long", est_wait_s=8)
    # Scheduling prefill is not completion of its work: keep the observed
    # queue+prefill estimate, reduced by elapsed time.
    now[0] += 1
    signal(c, sid="long", event="prefill_start")
    victim = c.victim_for_restore("other")
    assert victim.session_id == "long"
    c.release_victim(victim)
    c.set_eviction_eligible("long", True)
    signal(c, sid="long", event="first_token")
    assert c.victim_for_restore("other").session_id == "short"


@pytest.mark.parametrize("estimate", [None, True, -1, float("nan"), float("inf")])
def test_unknown_or_invalid_prefill_estimate_does_not_authorize_reclaim(estimate):
    c = coordinator()
    waiting(c)
    signal(c, est_wait_s=estimate)
    assert c.victim_for_restore("other") is None


@pytest.mark.parametrize("fields", [
    {"role": "D"}, {"layer": "kv_transfer"},
    {"signal_age_seconds": 6}, {"t": 20, "queued_since": 14},
])
def test_unmodelled_or_delayed_gpu_wait_does_not_authorize_reclaim(fields):
    c = coordinator(gpu_signal_max_age_seconds=5)
    waiting(c)
    signal(c, est_wait_s=30, **fields)
    assert c.victim_for_restore("other") is None


def test_prefill_estimate_expires_and_duplicate_signal_does_not_refresh_it(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator(gpu_signal_max_age_seconds=5)
    waiting(c)
    signal(c, est_wait_s=30, signal_sequence=7)
    now[0] += 6
    observed = signal(c, est_wait_s=30, signal_sequence=7)
    assert observed["reason"] == "duplicate_or_stale_signal"
    assert c.victim_for_restore("other") is None


def test_queued_rank_updates_refresh_estimate_but_log_and_recheck_only_on_edges(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator(gpu_reclaim_min_remaining_seconds=2)
    waiting(c)

    first = signal(c, est_wait_s=30, signal_sequence=1)
    assert first["should_log"] and first["should_recheck_pressure"]

    now[0] += 0.1
    rank_update = signal(c, est_wait_s=29, signal_sequence=2)
    assert not rank_update["should_log"]
    assert not rank_update["should_recheck_pressure"]
    assert rank_update["estimated_prefill_remaining_seconds"] == 29

    now[0] += 0.1
    threshold_crossing = signal(c, est_wait_s=1, signal_sequence=3)
    assert threshold_crossing["should_log"]
    assert threshold_crossing["should_recheck_pressure"]
    assert threshold_crossing["queued_observations_suppressed"] == 1

    now[0] += 0.1
    assert not signal(c, est_wait_s=1, signal_sequence=4)["should_log"]
    now[0] += 1.1
    summary = signal(c, est_wait_s=1, signal_sequence=5)
    assert summary["should_log"]
    assert not summary["should_recheck_pressure"]
    assert summary["queued_observations_suppressed"] == 1
    assert c.gpu_signal_metrics() == {
        "accepted_by_phase": {"queued": 5},
        "accepted_total": 5,
        "policy_log_records": 3,
        "policy_log_records_suppressed": 2,
        "queued_suppressed_since_last_record": 0,
        "pressure_rechecks": 2,
    }


def test_think_end_prewarms_only_once_and_never_the_prefill_leg():
    c = coordinator()
    waiting(c)
    assert not signal(c, event="think_end", role="P")["should_prewarm"]
    assert signal(c, event="think_end", role="D", source="trace")["should_prewarm"]
    assert not signal(c, event="think_end", role="D")["should_prewarm"]
    assert c.wait_for_gpu_prewarm("session", "request")
    # A late Tool completion and queue observation cannot remove the
    # prewarm protection and immediately evict the restored Runtime again.
    c.set_tool_active("session", False)
    signal(c, est_wait_s=30)
    assert c.victim_for_restore("other") is None
    c.complete_model_wait("session", "request")
    assert not c.wait_for_gpu_prewarm("session", "request")
    assert not signal(c, event="think_end")["accepted"]


def test_first_answer_token_prewarms_once_when_enabled():
    c = coordinator()
    waiting(c)
    assert not signal(c, event="first_token", role="P")["should_prewarm"]
    decision = signal(c, event="first_token", role="A")
    assert decision["should_prewarm"]
    assert decision["prewarm_trigger"] == "first_token"
    assert not signal(c, event="think_end", role="A")["should_prewarm"]
    assert c.wait_for_gpu_prewarm("session", "request")


def test_first_token_prewarm_can_be_disabled():
    c = coordinator(gpu_first_token_prewarm=False)
    waiting(c)
    assert not signal(c, event="first_token", role="A")["should_prewarm"]
    assert signal(c, event="think_end", role="A")["should_prewarm"]


@pytest.mark.parametrize("source", [None, "token", "unknown"])
def test_unobserved_think_end_does_not_change_wait_or_block_later_valid_boundary(source):
    c = coordinator()
    waiting(c)
    signal(c, est_wait_s=30, signal_sequence=1)
    event = dict(session_id="session", request_id="request", event="think_end",
                 role="A", engine_id="A", signal_sequence=2)
    if source is not None:
        event["source"] = source
    decision = c.observe_gpu_signal("session", "request", event)
    assert decision == dict(accepted=False, should_prewarm=False,
                            reason="unobserved_reasoning_boundary")
    assert not c.wait_for_gpu_prewarm("session", "request")
    victim = c.victim_for_restore("other")
    assert victim is not None
    c.release_victim(victim)
    assert signal(c, event="think_end", signal_sequence=3)["should_prewarm"]


def test_new_model_wait_rejects_previous_signals_and_clears_prewarm():
    c = coordinator()
    waiting(c)
    signal(c, event="think_end")
    c.begin_model_wait("session", "next")
    assert not signal(c, event="think_end")["accepted"]
    assert not c.wait_for_gpu_prewarm("session", "request")
    assert not c.wait_for_gpu_prewarm("session", "next")
    signal(c, rid="next", est_wait_s=30)
    assert c.victim_for_restore("other").session_id == "session"


def test_gpu_reclaim_preserves_tool_safety_and_request_binding():
    c = coordinator()
    waiting(c)
    assert not c.observe_gpu_signal("session", "request", {
        "session_id": "other", "event": "queued", "role": "A", "est_wait_s": 30,
    })["accepted"]
    assert c.victim_for_restore("other") is None
    signal(c, est_wait_s=30)
    c.set_tool_active("session", True)
    assert c.victim_for_restore("other") is None


def test_gpu_prewarm_waits_for_claimed_checkpoint_without_blocking_its_release():
    c = coordinator()
    waiting(c)
    signal(c, est_wait_s=30)
    victim = c.victim_for_restore("other")
    assert victim is not None
    assert signal(c, event="think_end")["should_prewarm"]
    entered = Event()

    def wait():
        entered.set()
        return c.wait_for_gpu_prewarm("session", "request")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(wait)
        assert entered.wait(1)
        assert not pending.done()
        c.release_victim(victim)
        assert pending.result(timeout=1)


def test_gpu_prewarm_wait_is_invalidated_by_model_completion():
    c = coordinator()
    waiting(c)
    signal(c, est_wait_s=30)
    victim = c.victim_for_restore("other")
    signal(c, event="think_end")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(c.wait_for_gpu_prewarm, "session", "request")
        completed = pool.submit(c.complete_model_wait, "session", "request")
        # complete_model_wait publishes response_ready before waiting for the
        # checkpoint to exit; both callbacks can then progress after release.
        with c._condition:
            assert c._condition.wait_for(lambda: c._sessions["session"].response_ready, timeout=1)
        c.release_victim(victim)
        assert not pending.result(timeout=1)
        completed.result(timeout=1)


def test_gpu_policy_does_not_change_wait_only_baseline():
    policy = PolicySpec(name="wait", admission="predicted", reclamation="snapshot_pause",
                        eviction="wait_aware_pressure", restore="reactive")
    c = PolicyCoordinator(policy, budget_mib=32, emergency_free_mib=0, operation_headroom_mib=0)
    waiting(c)
    assert not signal(c, event="think_end")["accepted"]
    assert c.victim_for_restore("other").session_id == "session"
