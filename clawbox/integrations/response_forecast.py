"""GPU-side observations for the current request; no scheduling or clock changes.

The output length is the recorded current-call length, an explicit oracle input
for this trace experiment. No recorded duration, reasoning boundary or future
call is consulted. Times target native complete-response readiness, before HTTP
delivery and CPU VM restoration.
"""
from collections import deque
from contextlib import asynccontextmanager
import asyncio
import math
import time


PROTOCOL = 1


class ResponseForecaster:
    def __init__(self, service, *, interval=0.5, window=5.0, validity=2.0):
        if not all(math.isfinite(v) and v > 0 for v in (interval, window, validity)) or interval >= validity:
            raise ValueError('forecast requires positive window and 0 < interval < validity')
        self.service = service
        self.interval, self.window, self.validity = interval, window, validity
        self.requests = {}
        self.rates = {}
        self.publish = service.feed.publish

    def _state(self, rid):
        return self.requests.setdefault(rid, dict(points=deque(), phase=None, last_emit=-math.inf,
                                                  remaining=None, last_observation=-math.inf))

    def _emit(self, event, phase, remaining=None, *, reason=None, force=False, **fields):
        st = self._state(event['request_id'])
        now = float(event['t'])
        if not force and phase == st['phase'] and now - st['last_emit'] < self.interval:
            return
        st.update(phase=phase, last_emit=now, remaining=remaining, last_observation=now,
                  identity={k: event[k] for k in ('request_id', 'session_id', 'engine_id', 'role')})
        self.publish(dict(st['identity'], event='response_forecast', t=now,
            wall_t=self.service.epoch0 + self.service.clock.wall(now) - self.service.clock.w0,
            forecast_protocol=PROTOCOL, phase=phase, remaining_s=remaining,
            valid_for_s=self.validity, unavailable_reason=reason,
            target='native_response_ready', source='trace_length_online_rate',
            output_length_source='recorded_current_call', **fields))

    def observe(self, event):
        """Called after publishing an unchanged native signal."""
        rid, phase = event['request_id'], event['event']
        st = self._state(rid)
        if phase in {'queued', 'prefill_start', 'generation_blocked'}:
            st['points'].clear()
            if phase == 'queued' and event.get('layer') != 'kv_transfer' and event['role'] != 'D':
                wait = event.get('est_wait_s')
                rate = self.rates.get(event['engine_id'])
                active = self.service.active.get(rid)
                call = active.get('call') if active else None
                total = self.service.registry.requests[rid].ref.output_tokens
                if (type(wait) in (int, float) and math.isfinite(wait) and wait >= 0 and rate
                        and event['t'] - rate[0] <= self.window and call is not None):
                    age = max(0., event['t'] - event.get('queued_since', event['t']))
                    remaining = max(0., wait-age) + max(0, total-call.n_out)*rate[1]
                    remaining += self.service.sim.client_return_s(call)
                    self._emit(event, 'queued', remaining, force=True,
                               rate_s_per_token=rate[1], output_tokens=call.n_out)
                    return
            self._emit(event, 'blocked' if phase != 'prefill_start' else 'prefill',
                       reason=event.get('reason') or event.get('layer') or 'prefill_remaining_unknown', force=True)
        elif phase == 'finish' and event['role'] in {'A', 'D'}:
            # Finish may be cancellation. Actual completion/delivery owns cleanup.
            self._emit(event, 'finished', reason='awaiting_complete_response', force=True)

    def output(self, call, count):
        if call.kind == 'prefill' or call.aborted:
            return
        now = self.service.sim.t
        st = self._state(call.request_id)
        points = st['points']
        points.append((now, call.n_out))
        while len(points) > 2 and points[1][0] < now-self.window:
            points.popleft()
        rate = None
        if len(points) > 1 and now > points[0][0] and call.n_out > points[0][1]:
            rate = (now-points[0][0]) / (call.n_out-points[0][1])
            self.rates[call.engine.engine_id] = (now, rate)
        left = max(0, call.O-call.n_out)
        remaining = (left*(rate or 0.) + self.service.sim.client_return_s(call)
                     if rate is not None or left == 0 else None)
        event = dict(request_id=call.request_id, session_id=call.job,
                     engine_id=call.engine.engine_id, role=call.engine.role, t=now)
        self._emit(event, 'decoding', remaining,
                   reason='decode_rate_unavailable' if remaining is None else None,
                   force=left == 0, output_tokens=call.n_out, rate_s_per_token=rate)

    def expire(self):
        now = self.service.clock.virtual(time.monotonic())
        for rid, st in list(self.requests.items()):
            if rid not in self.service.active:
                self.requests.pop(rid, None)
            elif st['remaining'] is not None and now-st['last_observation'] > self.validity:
                self._emit(dict(st['identity'], t=now), 'unknown',
                           reason='no_fresh_generation_observation', force=True)

    def install(self):
        feed, sim = self.service.feed, self.service.sim
        def publish(event):
            self.publish(event)
            self.observe(event)
        feed.publish = publish
        output = sim.on_output_token
        def on_output(call, count):
            output(call, count)
            self.output(call, count)
        sim.on_output_token = on_output
        # Native decode-span optimization also calls on_output_token. Observe it
        # without flushing spans or inserting events into the simulated heap.
        for engine in sim.engines:
            scheduler = engine.v.sched
            original = scheduler._preempt_request
            def preempt(request, *args, _original=original, _engine=engine, **kwargs):
                result = _original(request, *args, **kwargs)
                call = _engine.reqs[request.request_id]
                self.observe(dict(request_id=call.request_id, session_id=call.job,
                    engine_id=_engine.engine_id, role=_engine.role, t=sim.t,
                    event='generation_blocked', reason='preempted'))
                return result
            scheduler._preempt_request = preempt


def attach_response_forecasts(app, *, interval=0.5, window=5.0, validity=2.0):
    upstream = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with upstream(application):
            service = application.state.service
            observer = ResponseForecaster(service, interval=interval, window=window, validity=validity)
            observer.install()
            service.clawbox_forecast = dict(protocol=PROTOCOL, interval_seconds=interval,
                window_seconds=window, validity_seconds=validity,
                target='native_response_ready', output_length_source='recorded_current_call')
            async def expiry():
                while True:
                    await asyncio.sleep(interval)
                    observer.expire()
            task = asyncio.create_task(expiry())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    app.router.lifespan_context = lifespan
    return app
