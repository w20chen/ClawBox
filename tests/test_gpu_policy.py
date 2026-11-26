from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.spec import PolicySpec


class Lifecycle:
    resident = True

    def __init__(self):
        self.next_use = []

    def set_snapshot_next_use(self, deadline, revision=0, *, expires_at=None):
        self.next_use.append((deadline, revision, expires_at))


def coordinator(**options):
    options.setdefault('gpu_prewarm', 'eta')
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


def forecast():
    return dict(event="response_forecast", role="A", engine_id="A",
                source="trace_length_online_rate", forecast_protocol=1,
                target="native_response_ready", output_length_source="recorded_current_call",
                phase="decoding", remaining_s=1., valid_for_s=2.)


def signal(c, *, sid="session", rid="request", **fields):
    event = forecast()
    event.update(request_id=rid, session_id=sid, **fields)
    return c.observe_gpu_signal(sid, rid, event)


def test_reclaim_uses_longest_fresh_response_eta(monkeypatch):
    now = [10.]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator(gpu_reclaim_min_remaining_seconds=3)
    for sid in ("unknown", "short", "long"):
        waiting(c, sid)
    signal(c, sid="short", remaining_s=5)
    signal(c, sid="long", remaining_s=20)
    victim = c.victim_for_restore("other")
    assert victim.session_id == "long"
    c.release_victim(victim)
    c.set_eviction_eligible("long", True)
    signal(c, sid="long", remaining_s=None, phase="blocked")
    assert c.victim_for_restore("other").session_id == "short"
    now[0] += 3
    assert c.victim_for_restore("other") is None


@pytest.mark.parametrize("fields", [
    dict(remaining_s=None), dict(remaining_s=True), dict(remaining_s=-1),
    dict(remaining_s=float("nan")), dict(remaining_s=float("inf")),
    dict(valid_for_s=0), dict(valid_for_s=float("nan")),
    dict(signal_age_seconds=3), dict(phase="blocked"), dict(target="first_token"),
    dict(source="trace"), dict(output_length_source="unknown"),
])
def test_unusable_forecast_cannot_reclaim_or_prewarm(fields):
    c = coordinator()
    waiting(c)
    signal(c, **fields)
    assert c.victim_for_restore("other") is None
    assert not c.wait_for_gpu_prewarm("session", "request")


@pytest.mark.parametrize("event", ["queued", "prefill_start", "first_token", "think_start", "think_end", "finish"])
def test_native_boundaries_do_not_invent_eta_or_trigger_full_restore(event):
    c = coordinator()
    waiting(c)
    assert not signal(c, event=event, remaining_s=30)["should_prewarm"]
    assert c.victim_for_restore("other") is None


def test_duplicate_forecast_does_not_renew_expiry(monkeypatch):
    now = [10.]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator()
    waiting(c)
    signal(c, remaining_s=30, signal_sequence=7)
    now[0] += 2
    assert signal(c, remaining_s=30, signal_sequence=7)["reason"] == "duplicate_or_stale_signal"
    assert c.victim_for_restore("other") is None


@pytest.mark.parametrize("invalidation", [
    dict(remaining_s=None, phase="blocked"), dict(remaining_s=30),
    dict(event="prefill_start"), dict(event="queued", layer="kv_transfer"),
])
def test_pending_prewarm_revalidates_before_claim(invalidation):
    c = coordinator()
    waiting(c)
    assert signal(c)["should_prewarm"]
    signal(c, **invalidation)
    assert not c.wait_for_gpu_prewarm("session", "request", claim=True)


def test_committed_prewarm_protects_residency_and_retries_deferred_tool():
    c = coordinator()
    waiting(c)
    assert signal(c)["should_prewarm"]
    assert c.wait_for_gpu_prewarm("session", "request", claim=True)
    signal(c, remaining_s=30)
    c.set_tool_active("session", False)
    assert c.victim_for_restore("other") is None
    assert signal(c)["should_prewarm"]
    c.complete_gpu_prewarm("session", "request")
    assert not signal(c)["should_prewarm"]
    c.complete_model_wait("session", "request")
    assert not signal(c)["accepted"]


def test_gpu_reclaim_preserves_tool_safety_and_request_binding():
    c = coordinator()
    waiting(c)
    assert not c.observe_gpu_signal("session", "request", {
        "session_id": "other", "event": "queued", "role": "A", "est_wait_s": 30,
    })["accepted"]
    assert c.victim_for_restore("other") is None
    signal(c, remaining_s=30)
    c.set_tool_active("session", True)
    assert c.victim_for_restore("other") is None


def test_gpu_prewarm_waits_for_claimed_checkpoint_without_blocking_its_release():
    c = coordinator()
    waiting(c)
    signal(c, remaining_s=30)
    victim = c.victim_for_restore("other")
    assert victim is not None
    assert signal(c, remaining_s=1.)["should_prewarm"]
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
    signal(c, remaining_s=30)
    victim = c.victim_for_restore("other")
    signal(c, remaining_s=1.)
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
    assert not signal(c, remaining_s=1.)["accepted"]
    assert c.victim_for_restore("other").session_id == "session"


def test_next_use_deadline_is_age_adjusted_and_published_to_both_vms(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("clawbox.experiments.policy.time.monotonic", lambda: now[0])
    c = coordinator(snapshot_next_use_residency=True)
    waiting(c)
    state = c._sessions["session"]
    assert state.lifecycle.next_use[-1][0] is None
    assert state.runtime_lifecycle.next_use[-1][0] is None

    signal(c, remaining_s=20, signal_age_seconds=.5, signal_sequence=1)
    assert state.lifecycle.next_use[-1][0] == pytest.approx(119.5)
    assert state.runtime_lifecycle.next_use[-1][0] == pytest.approx(119.5)

    now[0] = 102
    signal(c, remaining_s=5, signal_sequence=2)
    assert state.lifecycle.next_use[-1][0] == pytest.approx(107)
    assert state.runtime_lifecycle.next_use[-1][0] == pytest.approx(107)

    signal(c, remaining_s=None, signal_sequence=3)
    assert state.lifecycle.next_use[-1][0] is None
    c.complete_model_wait("session", "request")
    assert state.runtime_lifecycle.next_use[-1][0] is None


def test_next_use_publication_is_outside_global_progress_lock():
    entered = Event()
    release = Event()

    class BlockingLifecycle(Lifecycle):
        def set_snapshot_next_use(self, deadline, revision=0, *, expires_at=None):
            if deadline is not None:
                entered.set()
                assert release.wait(1)
            super().set_snapshot_next_use(deadline, revision, expires_at=expires_at)

    c = coordinator(snapshot_next_use_residency=True)
    c.register("session", BlockingLifecycle())
    c.register_runtime("session", Lifecycle())
    c.begin_model_wait("session", "request")
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(signal, c, remaining_s=20)
        assert entered.wait(1)
        acquired = Event()

        def take_progress_lock():
            with c.demand.progress_lock:
                acquired.set()

        lock_user = executor.submit(take_progress_lock)
        assert acquired.wait(.1)
        release.set()
        assert pending.result(timeout=1)["accepted"]
        lock_user.result(timeout=1)
