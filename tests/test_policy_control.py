from __future__ import annotations

import hashlib
import asyncio
import http.client
import json
import socket
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

import pytest

from clawbox.experiments.policy_control import PolicyControlServer, SessionLifecycle


@pytest.fixture(autouse=True, params=['asyncio', 'uvloop'])
def policy_loop_backend(request, monkeypatch):
    if request.param == 'uvloop':
        pytest.importorskip('uvloop')
    original = PolicyControlServer
    def server(**kwargs):
        kwargs.setdefault('event_loop', request.param)
        return original(**kwargs)
    monkeypatch.setitem(globals(), 'PolicyControlServer', server)


def _post(session, path: str, execution_id: str, *, digest: str | None = None, receipt=None, phase=None) -> dict:
    body = json.dumps({
        "session_id": session.session_id,
        "execution_id": execution_id,
        "command_sha256": digest or hashlib.sha256(execution_id.encode()).hexdigest(),
        "operation": "exec",
        **({'prediction_receipt': receipt} if receipt else {}),
        **({'phase': phase} if phase else {}),
        "logical_session_id": "logical-session",
        "session_key": "agent:main:child",
        "agent_id": "agent",
        "tool_call_id": "tool-call",
        "run_id": "run",
    }).encode()
    request = urllib.request.Request(
        session.url + path, data=body, method="POST",
        headers={"Authorization": f"Bearer {session.token}",
                 "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(request))


def test_request_timing_persists_outer_progress_wait_without_credentials(monkeypatch):
    rows = []
    saved = Event()
    def emit(row):
        rows.append(row)
        if row.get('path') == '/v1/tool/start':
            saved.set()
    server = PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                                 bind_host='127.0.0.1', bind_port=0)
    # This wait is outside record_tool_progress's inner callback span.
    monkeypatch.setattr(server.forecaster, 'start', lambda *_: time.sleep(.05))
    with server:
        session = server.register('timing', admit=lambda _: {}, complete=lambda _: {},
                                  start=lambda _: None, request_timing=emit)
        _post(session, '/v1/tool/admit', 'exec-timing', receipt='timing-receipt')
        assert _post(session, '/v1/tool/start', 'exec-timing')['status'] == 'STARTED'
        assert saved.wait(2)
        row = next(r for r in rows if r['path'] == '/v1/tool/start')
        assert row['callback_seconds'] >= .04
        assert row['latency_seconds'] >= row['callback_seconds']
        assert row['response_ready_seconds'] >= row['callback_seconds']
        assert row['callback_queue_seconds'] >= 0
        assert row['phase'] == 'start'
        assert row['execution_id'] == 'exec-timing'
        assert session.token not in json.dumps(rows)
        assert 'command_sha256' not in row and 'error' not in row
        _post(session, '/v1/tool/complete', 'exec-timing')


def test_knowledge_timing_persists_wait_and_failure_without_payload():
    rows, saved = [], Event()
    class Knowledge:
        async def request(self, operation, session, payload):
            await asyncio.sleep(.05)
            if operation == 'predict':
                raise ValueError('private request detail')
            return {'status': 'ok'}
    def emit(row):
        rows.append(row)
        saved.set()
    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        server.knowledge_manager = Knowledge()
        session = server.register('kb-timing', admit=lambda _: {}, complete=lambda _: {},
                                  request_timing=emit)
        assert _post(session, '/v1/kb/preview', 'preview')['status'] == 'ok'
        assert saved.wait(2)
        saved.clear()
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, '/v1/kb/predict', 'prediction')
        assert error.value.code == 400 and saved.wait(2)
        assert [row['status'] for row in rows] == [200, 400]
        for row in rows:
            assert .04 <= row['knowledge_seconds'] <= row['latency_seconds']
            assert 'command_sha256' not in row and 'error' not in row
        serialized = json.dumps(rows)
        assert session.token not in serialized and 'private request detail' not in serialized


def test_runtime_completion_is_authenticated_and_does_not_release_tool_admission():
    rows = []
    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        session = server.register('completion', admit=lambda _: {}, complete=lambda _: {},
                                  request_timing=rows.append)
        _post(session, '/v1/tool/admit', 'exec-a')
        for phase in ('agent', 'subagents'):
            first = _post(session, '/v1/runtime/completion', 'notice', phase=phase)
            assert first == {'status': 'NOTIFIED', 'phase': phase, 'duplicate': False}
            assert session.wait_runtime_completion(phase, 0)
            assert not session.wait_runtime_completion(phase, 0)
            assert _post(session, '/v1/runtime/completion', 'notice', phase=phase)['duplicate']
            assert not session.wait_runtime_completion(phase, 0)
        assert session.close(timeout=0) is False  # Wakeup does not complete the Tool.
        _post(session, '/v1/tool/complete', 'exec-a')
        assert session.close(timeout=1)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, '/v1/runtime/completion', 'notice', phase='agent')
        assert error.value.code == 401
        assert session.token not in json.dumps(rows)


def test_admission_thread_start_cannot_block_http_or_completion_wakeup(monkeypatch):
    entered, release = Event(), Event()
    original = Thread.start

    def delayed(thread):
        if thread.name.startswith('policy-admit_'):
            entered.set()
            assert release.wait(4)
        return original(thread)

    with PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                             bind_host='127.0.0.1', bind_port=0) as server:
        session = server.register('thread-start', admit=lambda _: {}, complete=lambda _: {})
        monkeypatch.setattr(Thread, 'start', delayed)
        results = []
        caller = Thread(target=lambda: results.append(_post(session, '/v1/tool/admit', 'exec-a')))
        caller.start()
        try:
            assert entered.wait(1)
            # An unrelated request must finish while admission submit still
            # holds concurrent.futures' process-wide thread-pool lock.
            request = urllib.request.Request(session.url+'/v1/runtime/completion',
                data=json.dumps(dict(session_id=session.session_id, phase='agent')).encode(),
                headers={'Authorization': 'Bearer '+session.token, 'Content-Type':'application/json'})
            with urllib.request.urlopen(request, timeout=1) as response:
                assert json.load(response)['status'] == 'NOTIFIED'
            assert session.wait_runtime_completion('agent', 0)
        finally:
            release.set()
            caller.join(3)
        assert len(results) == 1
        _post(session, '/v1/tool/complete', 'exec-a')


def test_blocked_forecast_does_not_block_maintenance_or_http_loop(monkeypatch):
    entered, release = Event(), Event()
    server = PolicyControlServer(advertise_host='127.0.0.1', advertised_port=0,
                                 bind_host='127.0.0.1', bind_port=0)
    def blocked_begin(_session, request, *_):
        if not request.get('prediction_receipt'):
            return
        with server.forecaster.lock:
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(server.forecaster, 'begin', blocked_begin)
    with server:
        slow = server.register('forecast', admit=lambda _: {}, complete=lambda _: {})
        phases = []
        fast = server.register('maintenance', admit=lambda _: {}, complete=lambda _: {},
                               start=lambda row: phases.append(row.get('phase', 'start')))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(_post, slow, '/v1/tool/admit', 'slow', receipt='receipt')
            try:
                assert entered.wait(2)
                started = time.monotonic()
                _post(fast, '/v1/tool/admit', 'maintenance')
                assert _post(fast, '/v1/tool/start', 'maintenance')['status'] == 'STARTED'
                _post(fast, '/v1/tool/start', 'maintenance', phase='end')
                _post(fast, '/v1/tool/complete', 'maintenance')
                assert phases == ['start', 'end']
                assert time.monotonic() - started < 1
                assert not pending.done()
            finally:
                release.set()
            pending.result(timeout=2)
        _post(slow, '/v1/tool/complete', 'slow')


def test_policy_control_is_idempotent_and_drains_inflight_completion() -> None:
    admitted: list[str] = []
    completed: list[str] = []
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    assert server.server.request_queue_size >= 1024
    with server:
        session = server.register(
            "session-a",
            admit=lambda request: admitted.append(request["execution_id"]) or {
                "decision": "ADMIT", "admitted_memory_mib": 128,
            },
            complete=lambda request: completed.append(request["execution_id"]) or {
                "status": "COMPLETED",
            },
        )
        first = _post(session, "/v1/tool/admit", "exec-a")
        duplicate = _post(session, "/v1/tool/admit", "exec-a")
        assert first["duplicate"] is False
        assert duplicate["duplicate"] is True
        assert admitted == ["exec-a"]
        assert session.lifecycle is SessionLifecycle.ACTIVE
        assert session.close(timeout=0.01) is False
        assert session.lifecycle is SessionLifecycle.DRAINING
        completion = _post(session, "/v1/tool/complete", "exec-a")
        assert completion["duplicate"] is False
        assert _post(session, "/v1/tool/complete", "exec-a")["duplicate"] is True
        assert completed == ["exec-a"]
        assert session.records()[0]["request"]["session_key"] == "agent:main:child"
        assert session.records()[0]["completion"]["tool_call_id"] == "tool-call"
        timing = session.records()[0]["timing"]
        assert timing["admission_started_monotonic_s"] <= timing[
            "admission_completed_monotonic_s"
        ]
        assert timing["completion_started_monotonic_s"] <= timing[
            "completion_completed_monotonic_s"
        ]
        assert timing["admission_service_seconds"] >= 0
        assert timing["completion_service_seconds"] >= 0
        record = session.records()[0]
        assert record["completion"]["session_id"] == "session-a"
        assert record["completion"]["command_sha256"] == hashlib.sha256(
            b"exec-a"
        ).hexdigest()
        assert record["completion"]["status"] == "COMPLETED"
        assert session.close(timeout=1)
        assert session.lifecycle is SessionLifecycle.CLOSED


def test_failed_session_can_be_discarded_after_guest_cleanup() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    with server:
        session = server.register("session-a", admit=lambda _request: {},
                                  complete=lambda _request: {})
        _post(session, "/v1/tool/admit", "exec-a")
        assert session.close(timeout=0) is False
        session.discard()
        assert session.lifecycle is SessionLifecycle.CLOSED
        assert server.session_count == 0
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, "/v1/tool/complete", "exec-a")
        assert error.value.code == 401


def test_policy_control_rejects_wrong_session_and_execution_id_reuse() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    with server:
        session = server.register("session-a", admit=lambda _request: {},
                                  complete=lambda _request: {})
        _post(session, "/v1/tool/admit", "exec-a", digest="a" * 64)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, "/v1/tool/admit", "exec-a", digest="b" * 64)
        assert error.value.code == 400

        body = json.dumps({"session_id": "session-b", "execution_id": "exec-b",
                           "command_sha256": "c" * 64}).encode()
        request = urllib.request.Request(
            session.url + "/v1/tool/admit", data=body, method="POST",
            headers={"Authorization": f"Bearer {session.token}"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 400
        assert session.close(timeout=1) is False  # exec-a remains admitted
        _post(session, "/v1/tool/complete", "exec-a", digest="a" * 64)
        assert session.close(timeout=1)


def test_policy_control_rejects_completion_for_a_different_command() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    with server:
        session = server.register("session-a", admit=lambda _request: {},
                                  complete=lambda _request: {})
        _post(session, "/v1/tool/admit", "exec-a", digest="a" * 64)
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, "/v1/tool/complete", "exec-a", digest="b" * 64)
        assert error.value.code == 400
        _post(session, "/v1/tool/complete", "exec-a", digest="a" * 64)
        assert session.close(timeout=1)


def test_policy_control_records_callback_failure_detail() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    with server:
        session = server.register(
            "session-a",
            admit=lambda _request: (_ for _ in ()).throw(OSError("route unavailable")),
            complete=lambda _request: {},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            _post(session, "/v1/tool/admit", "exec-a")
        assert error.value.code == 503
        for _ in range(50):
            if server.requests:
                break
            time.sleep(0.01)
        record = server.requests[-1]
        assert record["error_type"] == "OSError"
        assert record["error"] == "route unavailable"
        assert session.records()[0]["admission_error"] == {"type": "OSError", "message": "route unavailable"}


def test_progress_retries_reuse_connection_without_repeating_boundaries() -> None:
    boundaries = []
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    with server:
        session = server.register(
            "session-a", admit=lambda _: {"epoch": 1, "sandbox_id": "tool-a"},
            complete=lambda _: {}, start=lambda request: boundaries.append(request["phase"]),
        )
        connection = http.client.HTTPConnection("127.0.0.1", server.actual_port, timeout=2)
        body = {"session_id": session.session_id, "execution_id": "exec-a",
                "command_sha256": "a" * 64, "endpoint_epoch": 1,
                "endpoint_sandbox_id": "tool-a"}

        def post(path, **fields):
            connection.request("POST", path, json.dumps(body | fields),
                               {"Authorization": f"Bearer {session.token}"})
            response = connection.getresponse()
            assert response.status == 200
            return json.loads(response.read())

        try:
            post("/v1/tool/admit")
            socket_identity = connection.sock
            for phase in ("start", "end"):
                assert post("/v1/tool/start", phase=phase)["duplicate"] is False
                retry = post("/v1/tool/start", phase=phase)
                assert retry["duplicate"] is True
                assert retry["execution_id"] == "exec-a"
                assert connection.sock is socket_identity
            assert boundaries == ["start", "end"]
            post("/v1/tool/complete")
            assert session.close(timeout=1)
        finally:
            connection.close()


def test_policy_control_rolls_back_admission_when_response_connection_is_lost(monkeypatch) -> None:
    from clawbox.experiments.policy_http import PolicyHTTPServer

    admitted = Event()
    release = Event()
    disconnected = Event()
    abandoned: list[str] = []
    original_call = PolicyHTTPServer.__call__

    async def observe_disconnect(self, scope, receive, send):
        async def observed_receive():
            event = await receive()
            if event['type'] == 'http.disconnect':
                disconnected.set()
            return event
        await original_call(self, scope, observed_receive, send)

    monkeypatch.setattr(PolicyHTTPServer, '__call__', observe_disconnect)
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port

    def admit(request: dict) -> dict:
        admitted.set()
        release.wait(5)
        return {"decision": "ADMIT", "admitted_memory_mib": 128}

    with server:
        session = server.register(
            "session-a", admit=admit, complete=lambda _request: {},
            abandon=lambda request, _response: abandoned.append(request["execution_id"]),
        )
        body = json.dumps({
            "session_id": session.session_id,
            "execution_id": "exec-lost",
            "command_sha256": hashlib.sha256(b"exec-lost").hexdigest(),
            "operation": "exec",
        }).encode()
        request = (
            f"POST /v1/tool/admit HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {session.token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        client = socket.create_connection(("127.0.0.1", server.actual_port))
        client.sendall(request)
        assert admitted.wait(2)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        # Synchronize the scenario under test: the server has observed the
        # disconnect before admission completes. A local close alone does not
        # establish that ordering across the socket and callback threads.
        assert disconnected.wait(2)
        release.set()
        for _ in range(100):
            if abandoned:
                break
            time.sleep(0.01)
        assert abandoned == ["exec-lost"]
        assert session.records() == []
        assert session.close(timeout=1)
        assert any(record.get("admission_rolled_back") for record in server.requests)


def test_policy_control_c60_has_no_cross_session_head_of_line_blocking() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    finished: dict[str, float] = {}
    release_long = Event()

    def admit(request: dict) -> dict:
        if request["execution_id"] == "long":
            release_long.wait(10)
        finished[request["execution_id"]] = time.monotonic()
        return {"decision": "ADMIT"}

    with server:
        sessions = [server.register(f"session-{index:02d}", admit=admit,
                                    complete=lambda _request: {}) for index in range(60)]
        with ThreadPoolExecutor(max_workers=60) as pool:
            long = pool.submit(_post, sessions[0], "/v1/tool/admit", "long")
            time.sleep(0.02)
            short = [pool.submit(_post, sessions[index], "/v1/tool/admit", f"short-{index}")
                     for index in range(1, 60)]
            try:
                for future in short:
                    assert future.result()["decision"] == "ADMIT"
            finally:
                release_long.set()
            assert long.result()["decision"] == "ADMIT"
        for index, session in enumerate(sessions):
            execution_id = "long" if index == 0 else f"short-{index}"
            _post(session, "/v1/tool/complete", execution_id)
            assert session.close(timeout=1)
        assert server.session_count == 0
        assert list(finished)[-1] == "long"


def test_slow_forecasts_cannot_starve_tool_boundaries() -> None:
    """Optional ETA work must not consume the command acknowledgement lane."""
    release, saturated, counter_lock = Event(), Event(), Lock()
    entered = 0
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0, forecast_workers=4)
    # Keep the saturation probe small; exercise the real HTTP server/executors.
    server.server.boundaries._max_workers = 4

    def slow_forecast(_session, receipts):
        nonlocal entered
        with counter_lock:
            entered += 1
            if entered == 4:
                saturated.set()
        assert release.wait(10)
        return {"tools": {r: {"remaining_ms": None} for r in receipts}, "valid_for_ms": 2000}

    server.forecaster.snapshot = slow_forecast
    with server:
        session = server.register("forecast-load", admit=lambda _: {"epoch": 1, "sandbox_id": "tool"},
                                  complete=lambda _: {}, start=lambda _: None)
        body = {"session_id": session.session_id, "execution_id": "exec-a",
                "command_sha256": "a" * 64, "endpoint_epoch": 1, "endpoint_sandbox_id": "tool"}

        def post(path, payload, timeout=5):
            request = urllib.request.Request(session.url + path, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {session.token}", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)

        post("/v1/tool/admit", body)
        with ThreadPoolExecutor(max_workers=4) as pool:
            forecasts = [pool.submit(post, "/v1/tool/forecast", {"receipts": [str(i)]}) for i in range(4)]
            try:
                assert saturated.wait(3)
                assert post("/v1/tool/start", body | {"phase": "start"}, timeout=1)["status"] == "STARTED"
                assert post("/v1/tool/start", body | {"phase": "end"}, timeout=1)["status"] == "STARTED"
                assert post("/v1/tool/complete", body, timeout=1)["status"] == "COMPLETED"
                busy = post("/v1/tool/forecast", {"receipts": ["overflow"]}, timeout=1)
                assert busy["tools"]["overflow"]["remaining_ms"] is None
                assert busy["tools"]["overflow"]["reason"] == "forecast_capacity_busy"
            finally:
                release.set()
            for future in forecasts:
                future.result()
        assert entered == 4  # Overflow never queues stale work for later.
        assert session.close(timeout=1)


def test_observation_lock_does_not_block_other_sessions() -> None:
    from types import SimpleNamespace

    release, locked = Event(), Event()
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.knowledge_manager = SimpleNamespace(claim=lambda *_: {})
    with server:
        blocked = server.register("blocked", admit=lambda _: {}, complete=lambda _: {})
        healthy = server.register("healthy", admit=lambda _: {}, complete=lambda _: {})

        def hold_session():
            with blocked._state.condition:
                locked.set()
                assert release.wait(5)

        with ThreadPoolExecutor(max_workers=3) as pool:
            holder = pool.submit(hold_session)
            assert locked.wait(1)
            observation = pool.submit(_post, blocked, "/v1/kb/observe", "unknown")
            try:
                time.sleep(.1)  # Allow HTTP validation to reach the held lock.
                registration = pool.submit(_post, healthy, "/v1/tool/admit", "good")
                assert registration.result(timeout=1)["duplicate"] is False
                assert not observation.done()
            finally:
                release.set()
            holder.result()
            with pytest.raises(urllib.error.HTTPError) as error:
                observation.result()
            assert error.value.code == 400
        _post(healthy, "/v1/tool/complete", "good")
        assert blocked.close(timeout=1)
        assert healthy.close(timeout=1)


def test_cancelled_forecast_retains_capacity_until_thread_finishes() -> None:
    import asyncio

    release, entered = Event(), Event()
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0, forecast_workers=1)

    def slow_forecast(*_):
        entered.set()
        assert release.wait(5)
        return {"tools": {}, "valid_for_ms": 2000}

    server.forecaster.snapshot = slow_forecast

    async def exercise():
        task = asyncio.create_task(server.server._forecast("s", ["first"], asyncio.Event(), {}))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        busy = await server.server._forecast("s", ["second"], asyncio.Event(), {})
        assert busy["tools"]["second"]["reason"] == "forecast_capacity_busy"
        release.set()
        await asyncio.gather(*(asyncio.wrap_future(f) for f in server.server._forecast_pending))
        assert (await server.server._forecast("s", [], asyncio.Event(), {}))["tools"] == {}

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        server.server.server_close()
