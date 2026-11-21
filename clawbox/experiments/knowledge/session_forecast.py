"""Join ClawTune's logical Session state with ClawBox's execution boundaries."""
import asyncio
from contextlib import suppress
from copy import copy

import httpx
from clawtune_sidecar.session_state import SessionStateManager, session_key, _runtime_key


def call_key(owner, tool_id):
    return tuple(owner.get(k) for k in ('gateway_id', 'runtime_id', 'session_id', 'run_id')) + (tool_id,)


class HostSessionStateManager(SessionStateManager):
    def __init__(self, publish, predictor, **kwargs):
        super().__init__(publish, **kwargs)
        self.predictor = predictor
        self.host = {}
        self.reading_snapshot = False
        self.pending_calibration = {}

    def observe(self, message):
        self.reading_snapshot = True
        try:
            return super().observe(message)
        finally:
            self.reading_snapshot = False

    def _resolve_target(self, session, call_id, observed_at):
        if self.reading_snapshot and session.target_batch_id is not None:
            if session.target_settled_at is not None:
                self.pending_calibration[session_key(session.owner)] = (
                    session.owner['run_id'], call_id, session.target_settled_at)
            session.target_settled_at = None
        super()._resolve_target(session, call_id, observed_at)

    def model_started(self, owner, call_id, observed_at):
        pending = self.pending_calibration.pop(session_key(owner), None)
        if pending and pending[:2] == (owner.get('run_id'), call_id) and observed_at >= pending[2]:
            self.handoff_history[_runtime_key(owner)].append((observed_at-pending[2])*1000)
        super().model_started(owner, call_id, observed_at)

    def tool_completed(self, owner, observed_at):
        session = self.sessions.get(session_key(owner))
        if session and session.owner['run_id'] == owner.get('run_id'):
            batch = session.observation.get('batch')
            if batch and any(t['tool_call_id'] == owner.get('tool_call_id') for t in batch['tools']):
                session.completed_at[owner['tool_call_id']] = observed_at
                session.target_settled_at = (max(session.completed_at.values())
                    if all(t['tool_call_id'] in session.completed_at for t in batch['tools']) else None)
                self._update(session_key(owner), session)

    def _forecast(self, session, phase, tools, now):
        if phase != 'TOOLS':
            # A coalesced/recovered snapshot has no original settled timestamp.
            # Do not manufacture a fresh handoff countdown from its arrival.
            if phase == 'HANDOFF' and session.target_settled_at is None:
                return (None, None, 'post_tools', 'tool_return_time_unavailable', None), (phase,)
            return super()._forecast(session, phase, tools, now)
        proxy = copy(session)
        proxy.started_at, proxy.progress = {}, {}
        effective, expiries = [], []
        for tool in tools:
            if tool['settled'] or tool['tool_call_id'] in session.completed_at:
                effective.append(tool)
                continue
            if not tool['foreground']:
                reason = 'logical_return_may_precede_execution_end'
                return (None, None, 'pre_execution', reason, None), (phase, tool['tool_call_id'], reason)
            key = call_key(session.owner, tool['tool_call_id'])
            entry = self.host.get(key)
            reason = 'host_admission_forecast_unavailable'
            if entry is not None:
                observed, expires, value = entry
                reason = value.get('reason') or reason
                if expires <= now:
                    reason = 'host_admission_forecast_stale'
                elif value.get('remaining_ms') is not None:
                    effective.append({**tool, 'started': True,
                                      'predicted_duration_ms': value['remaining_ms']})
                    proxy.started_at[tool['tool_call_id']] = observed
                    expiries.append(expires)
                    continue
            return (None, None, 'pre_execution', reason, None), (phase, key, reason)
        # Each host forecast includes its own queue dependencies. Sum would
        # double-count predecessor execution already present in a later wait.
        proxy.state = {**session.state, 'execution_mode': 'parallel'}
        forecast, inputs = super()._forecast(proxy, phase, effective, now)
        deadline, expires, basis, reason, source = forecast
        if expires is not None:
            expires = min(expires, *expiries)
        return (deadline, expires, basis, reason, source), (inputs, tuple(expiries))

    def _reset_batch(self, session, batch, observed_at):
        super()._reset_batch(session, batch, observed_at)
        # Logical start timestamps are irrelevant to host execution. Snapshot
        # settlement without the original completion event is not calibration.
        session.completed_at.clear()
        session.target_settled_at = None

    def _track_tools(self, session, previous, data, observed_at):
        # Only tool.completed signals, with their source times, may populate
        # completed_at. Complete snapshots still determine the logical phase.
        batch = data.get('batch')
        if batch and all(t['tool_call_id'] in session.completed_at for t in batch['tools']):
            session.target_settled_at = max(session.completed_at.values())


def install_host_session_forecasts(app, state):
    """Install before app startup; keep upstream contracts and dependency pinned."""
    from fastapi import HTTPException, Request
    import hmac

    @app.post('/v1/clawbox/session-register')
    async def register_logical_session(request: Request):
        token = state.config.auth_token
        if token and not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            raise HTTPException(status_code=401, detail='invalid sidecar token')
        data = await request.json()
        key = data.get('session_key')
        if not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise HTTPException(status_code=422, detail='session_key must be a nonempty string of at most 512 characters')
        response = await asyncio.to_thread(state.predictor.client.post, '/v1/runtime/session', json=data)
        response.raise_for_status()
        return response.json()

    if not state.config.admission_eta:
        return
    signals = app.state.signals
    manager = HostSessionStateManager(signals.publish, state.predictor)
    signals.sessions = manager
    task = None

    async def poll():
        async with httpx.AsyncClient(base_url=state.config.host_kb_endpoint.rstrip('/'),
                headers={'Authorization': 'Bearer ' + state.config.host_kb_token},
                timeout=1., trust_env=False) as client:
            while True:
                calls = {}
                for session in list(manager.sessions.values()):
                    phase, _, _, _, tools = manager._phase(session, manager.clock())
                    if phase != 'TOOLS':
                        continue
                    for tool in tools:
                        if not tool['settled'] and tool['tool_call_id'] not in session.completed_at:
                            key = call_key(session.owner, tool['tool_call_id'])
                            receipt = state.predictor.forecast_receipts.get(key)
                            if receipt:
                                calls[key] = receipt
                if calls:
                    observed = manager.clock()
                    try:
                        response = await client.post('/v1/tool/forecast', json={'receipts': list(set(calls.values()))})
                        response.raise_for_status()
                        data = response.json()
                        expires = observed + min(2000, data['valid_for_ms']) / 1000
                        manager.host = {key: (observed, expires, data['tools'][receipt])
                                        for key, receipt in calls.items()}
                        manager.tick()
                    except (httpx.HTTPError, ValueError, KeyError, TypeError):
                        # The old cache expires; do not fall back to hook-start ETA.
                        pass
                else:
                    manager.host.clear()
                await asyncio.sleep(1.)

    @app.on_event('startup')
    async def start_forecasts():
        nonlocal task
        task = asyncio.create_task(poll())

    @app.on_event('shutdown')
    async def stop_forecasts():
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
