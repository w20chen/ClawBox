from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from clawbox.experiments.policy_control import PolicyControlServer, SessionLifecycle


def _post(session, path: str, execution_id: str, *, digest: str | None = None) -> dict:
    body = json.dumps({
        "session_id": session.session_id,
        "execution_id": execution_id,
        "command_sha256": digest or hashlib.sha256(execution_id.encode()).hexdigest(),
        "operation": "exec",
    }).encode()
    request = urllib.request.Request(
        session.url + path, data=body, method="POST",
        headers={"Authorization": f"Bearer {session.token}",
                 "Content-Type": "application/json"},
    )
    return json.load(urllib.request.urlopen(request))


def test_policy_control_is_idempotent_and_drains_inflight_completion() -> None:
    admitted: list[str] = []
    completed: list[str] = []
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
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
        timing = session.records()[0]["timing"]
        assert timing["admission_started_monotonic_s"] <= timing[
            "admission_completed_monotonic_s"
        ]
        assert timing["completion_started_monotonic_s"] <= timing[
            "completion_completed_monotonic_s"
        ]
        assert timing["admission_service_seconds"] >= 0
        assert timing["completion_service_seconds"] >= 0
        assert session.close(timeout=1)
        assert session.lifecycle is SessionLifecycle.CLOSED


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


def test_policy_control_c60_has_no_cross_session_head_of_line_blocking() -> None:
    server = PolicyControlServer(advertise_host="127.0.0.1", advertised_port=0,
                                 bind_host="127.0.0.1", bind_port=0)
    server.advertised_port = server.actual_port
    finished: dict[str, float] = {}

    def admit(request: dict) -> dict:
        time.sleep(0.35 if request["execution_id"] == "long" else 0.005)
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
            for future in short:
                assert future.result()["decision"] == "ADMIT"
            assert long.result()["decision"] == "ADMIT"
        assert max(finished[f"short-{index}"] for index in range(1, 60)) < finished["long"]
        for index, session in enumerate(sessions):
            execution_id = "long" if index == 0 else f"short-{index}"
            _post(session, "/v1/tool/complete", execution_id)
            assert session.close(timeout=1)
        assert server.session_count == 0
