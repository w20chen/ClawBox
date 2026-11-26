from types import SimpleNamespace as NS

import pytest

from clawbox.integrations.response_forecast import ResponseForecaster


def setup():
    records, outputs = [], []
    engine = NS(engine_id='A0', role='A', reqs={})
    engine.v = NS(sched=NS(_preempt_request=lambda r: None))
    call = NS(kind='agg', aborted=False, request_id='r', job='s', O=10,
              n_out=0, engine=engine, reasoning_output_tokens=None)
    engine.reqs['native-r'] = call
    sim = NS(t=0., engines=[engine], client_return_s=lambda c: .2,
             on_output_token=lambda c, n: outputs.append((c.n_out, n)))
    service = NS(feed=NS(publish=records.append), sim=sim,
                 epoch0=100., clock=NS(wall=lambda t: t, w0=0., virtual=lambda t: sim.t),
                 active={'r': {'call': call}},
                 registry=NS(requests={'r': NS(ref=NS(output_tokens=10))}))
    observer = ResponseForecaster(service)
    observer.install()
    return observer, service, call, records, outputs


def native(service, event, **fields):
    record = dict(request_id='r', session_id='s', engine_id='A0',
                  role='A', event=event, t=service.sim.t)
    service.feed.publish(record | fields)


def test_missing_reasoning_uses_current_length_and_observed_rate_without_mutating_native_output():
    observer, service, call, records, outputs = setup()
    native(service, 'prefill_start')
    assert records[-1]['remaining_s'] is None
    native(service, 'first_token')
    call.n_out = 1
    service.sim.on_output_token(call, 1)
    assert records[-1]['unavailable_reason'] == 'decode_rate_unavailable'
    service.sim.t = 1.
    call.n_out = 3
    service.sim.on_output_token(call, 2)
    result = records[-1]
    assert result['remaining_s'] == pytest.approx(3.7)
    assert result['output_length_source'] == 'recorded_current_call'
    assert result['target'] == 'native_response_ready'
    assert result['rate_s_per_token'] == .5
    assert outputs == [(1, 1), (3, 2)]
    assert call.n_out == 3 and call.O == 10 and service.sim.t == 1.
    assert not any(r['event'].startswith('think') for r in records)
    # The same hook receives token counts from the native decode-span fast path.
    service.sim.t = 4.5
    call.n_out = 10
    service.sim.on_output_token(call, 7)
    assert records[-1]['remaining_s'] == .2


def test_queue_estimate_combines_fresh_engine_rate_and_queue_age():
    observer, service, call, records, _ = setup()
    observer.rates['A0'] = (0., .5)
    service.sim.t = 1.
    native(service, 'queued', est_wait_s=3., queued_since=0.)
    assert records[-1]['remaining_s'] == pytest.approx(7.2)
    native(service, 'prefill_start', recompute=True)
    assert records[-1]['remaining_s'] is None
    native(service, 'queued', layer='kv_transfer', est_wait_s=10.)
    assert records[-1]['remaining_s'] is None
    service.sim.t = 9.
    native(service, 'queued', est_wait_s=3.)
    assert records[-1]['remaining_s'] is None


def test_preemption_and_observation_expiry_invalidate_eta():
    observer, service, call, records, _ = setup()
    for t, n in [(0., 1), (1., 3)]:
        service.sim.t, call.n_out = t, n
        service.sim.on_output_token(call, n)
    call.engine.v.sched._preempt_request(NS(request_id='native-r'))
    assert records[-1]['phase'] == 'blocked'
    assert records[-1]['unavailable_reason'] == 'preempted'
    assert records[-1]['remaining_s'] is None
    for t, n in [(2., 4), (3., 6)]:
        service.sim.t, call.n_out = t, n
        service.sim.on_output_token(call, 2)
    assert records[-1]['remaining_s'] == pytest.approx(2.2)
    service.sim.t = 5.1
    observer.expire()
    assert records[-1]['phase'] == 'unknown' and records[-1]['remaining_s'] is None
    service.active.clear()
    observer.expire()
    assert not observer.requests


def test_p_leg_output_and_finish_never_claim_full_response():
    _, service, call, records, outputs = setup()
    call.kind = 'prefill'
    service.sim.on_output_token(call, 1)
    assert not records and outputs == [(0, 1)]
    native(service, 'finish', role='P', reason='done')
    assert len(records) == 1 and records[0]['event'] == 'finish'
