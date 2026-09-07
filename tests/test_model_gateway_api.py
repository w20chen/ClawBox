from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clawbox.replay.model_gateway import ModelGateway, _canonical_replay_input


def test_replay_divergence_is_persisted_before_store_directory_exists(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "type": "action", "action_type": "llm_call", "action_id": "llm-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0.1,
        "data": {
            "raw_request": {"messages": [{"role": "user", "content": "expected"}]},
            "raw_response": {"content": "ok"},
        },
    }) + "\n", encoding="utf-8")
    gateway = ModelGateway(
        tmp_path / "not-created" / "session.json", mode="replay", trace=trace,
    )

    with pytest.raises(ValueError, match="replay request diverged at model step 0"):
        gateway.complete({
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "actual"}],
        })

    rejection = tmp_path / "not-created" / "session.rejected-request-0000.json"
    record = json.loads(rejection.read_text(encoding="utf-8"))
    assert record["model_step"] == 0
    assert record["actual"] != record["expected"]
    assert record["actual_sha256"] != record["expected_sha256"]
    assert record["reason"] == "canonical_request_mismatch"


def test_replay_trace_exhaustion_persists_the_unexpected_request(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "type": "action", "action_type": "llm_call", "action_id": "llm-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0.1,
        "data": {
            "raw_request": {"messages": [{"role": "user", "content": "first"}]},
            "raw_response": {"content": "first response"},
        },
    }) + "\n", encoding="utf-8")
    gateway = ModelGateway(
        tmp_path / "not-created" / "session.json", mode="replay", trace=trace,
    )
    status, _content_type, _body, request_id = gateway.complete_http({
        "model": "recorded-model",
        "messages": [{"role": "user", "content": "first"}],
    })
    assert status == 200
    gateway.mark_delivery(request_id, delivered=True)

    unexpected = {
        "model": "recorded-model",
        "messages": [{"role": "user", "content": "second"}],
    }
    with pytest.raises(ValueError, match="more model calls"):
        gateway.complete(unexpected)

    rejection = tmp_path / "not-created" / "session.rejected-request-0001.json"
    record = json.loads(rejection.read_text(encoding="utf-8"))
    assert record["model_step"] == 1
    assert record["reason"] == "trace_exhausted"
    assert record["expected"] is None
    assert record["expected_sha256"] is None
    assert record["actual"]["messages"][0]["content"] == "second"
    with pytest.raises(ValueError, match="already diverged: trace_exhausted"):
        gateway.complete({
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "first"}],
        })
    assert gateway.replay_completeness()["replay_failure"] == "trace_exhausted"


def test_replay_request_mismatch_poison_session(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "type": "action", "action_type": "llm_call", "action_id": "llm-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0,
        "data": {
            "raw_request": {"messages": [{"role": "user", "content": "expected"}]},
            "raw_response": {"content": "ok"},
        },
    }) + "\n", encoding="utf-8")
    gateway = ModelGateway(tmp_path / "session.json", mode="replay", trace=trace)

    with pytest.raises(ValueError, match="diverged at model step 0"):
        gateway.complete({
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "wrong"}],
        })
    with pytest.raises(ValueError, match="already diverged"):
        gateway.complete({
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "expected"}],
        })
    verdict = gateway.replay_completeness()
    assert verdict["replay_failure"] == "canonical_request_mismatch"
    assert verdict["complete"] is False


def test_replay_canonicalization_masks_openclaw_session_workspace(
    tmp_path: Path,
) -> None:
    expected = (
        "Files resolve under /state/openclaw/arm-a-0000/runtime-workspace.\n"
        "Runtime: agent=main | session=agent:main:explicit:arm-a-0000 "
        "| sessionId=arm-a-0000 | host=runtime"
    )
    actual = expected.replace("arm-a-0000", "arm-b-0007").replace(
        "host=runtime", "host=tpl-new",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "type": "action", "action_type": "llm_call", "action_id": "llm-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0,
        "data": {
            "raw_request": {"messages": [{"role": "system", "content": expected}]},
            "raw_response": {"content": "ok"},
        },
    }) + "\n", encoding="utf-8")
    gateway = ModelGateway(tmp_path / "session.json", mode="replay", trace=trace)

    status, _content_type, _body = gateway.complete({
        "model": "recorded-model",
        "messages": [{"role": "system", "content": actual}],
    })

    assert status == 200
    assert gateway.records()[0]["replay_input_match"] is True


def test_replay_canonicalization_masks_ls_metadata_not_listing_content(
    tmp_path: Path,
) -> None:
    expected = (
        "total 8\n"
        "drwxr-xr-x. 3 1001 1001 4096 Aug 31 19:16 .\n"
        "drwxr-xr-x 3 root root 4096 Aug 31 19:16 .clawbox\n"
        "-rw-r--r--. 1 1001 1001 34 Aug 17 04:01 setup.cfg\n"
    )
    actual = (
        "total 8\n"
        "drwxrwxrwx. 1 10001 10001 4096 Sep  6 04:55 .\n"
        "-rw-r--r--. 1 10001 10001 34 Aug 17 04:01 setup.cfg\n"
    )
    assert _canonical_replay_input(expected) == _canonical_replay_input(actual)
    assert _canonical_replay_input(actual.replace("setup.cfg", "other.cfg")) != (
        _canonical_replay_input(expected)
    )


def test_replay_canonicalization_masks_clawbox_git_status_scratch_directory() -> None:
    expected = "?? .clawbox/\n?? .gitconfig\n?? openclaw-ssh-shared-session/\n"
    actual = "?? .gitconfig\n?? openclaw-ssh-shared-session/\n"
    assert _canonical_replay_input(expected) == _canonical_replay_input(actual)


def test_replay_canonicalization_sorts_search_results_but_keeps_content() -> None:
    expected = "header\n./tests/a.py:9:needle\n./src/a.py:2:needle\n"
    actual = "header\n./src/a.py:2:needle\n./tests/a.py:9:needle\n"
    assert _canonical_replay_input(expected) == _canonical_replay_input(actual)
    assert _canonical_replay_input(expected.rstrip("\n")) == (
        _canonical_replay_input(actual.rstrip("\n"))
    )
    assert _canonical_replay_input(expected.replace("./", "")) == (
        _canonical_replay_input(actual.replace("./", ""))
    )
    assert _canonical_replay_input(actual.replace("needle\n", "changed\n", 1)) != (
        _canonical_replay_input(expected)
    )


def test_replay_pip_network_exception_is_scoped_to_probe() -> None:
    def request(output: str, command: str = "timeout 12 pip install -q pytest 2>&1 | tail -2") -> dict:
        return {"messages": [
            {"role": "assistant", "tool_calls": [{"id": "pip-probe", "function": {
                "name": "exec", "arguments": json.dumps({"command": command}),
            }}]},
            {"role": "tool", "tool_call_id": "pip-probe", "content": output},
        ]}
    suffix = "ModuleNotFoundError: No module named 'pytest'\n(Command exited with code 1)"
    expected = "ERROR: No matching distribution found for pytest\n" + suffix
    actual = "WARNING: Retrying (Retry(total=4)) after connection broken: /simple/pytest/\n" + suffix
    assert _canonical_replay_input(request(expected)) == _canonical_replay_input(request(actual))
    assert _canonical_replay_input(request(expected, "pip install pytest")) != _canonical_replay_input(request(actual, "pip install pytest"))
    assert _canonical_replay_input(request(expected)) != _canonical_replay_input(request(actual.replace("code 1", "code 0")))
    assert _canonical_replay_input(request(expected)) != _canonical_replay_input(request(actual.replace("'pytest'", "'sly'")))


def test_replay_filesystem_probe_exception_keeps_other_paths_and_errors():
    from clawbox.replay.model_gateway import _REC_A_FILESYSTEM_PROBE

    def request(content, command=_REC_A_FILESYSTEM_PROBE):
        return {"messages": [
            {"role": "assistant", "tool_calls": [{"id": "listing", "function": {
                "name": "exec", "arguments": json.dumps({"command": command}),
            }}]},
            {"role": "tool", "tool_call_id": "listing", "content": content},
        ]}

    prefix = "/opt/sly\n---django---\n---pytest---\n/opt/pytest\n---wheels---\n"
    expected = prefix + "/opt/pip.whl\n/usr/share/python-wheels/pip-22.0.2-py3-none-any.whl\n---pipcache---"
    actual = prefix + "/root/.cache/pip/wheels/48/cc/f2/37f85b0cde9f0cc404f270b26e733847c886d0e4b750c0d7c5/sly-0.3-py3-none-any.whl\n/opt/pip.whl\n---pipcache---"
    assert _canonical_replay_input(request(expected)) == _canonical_replay_input(request(actual))
    assert _canonical_replay_input(request(expected)) != _canonical_replay_input(request(actual.replace("/opt/pip.whl", "/opt/other.whl")))
    assert _canonical_replay_input(request(expected)) != _canonical_replay_input(request(actual + "\n(Command exited with code 1)"))
    assert _canonical_replay_input(request(expected, "find /")) != _canonical_replay_input(request(actual, "find /"))


def test_api_gateway_forwards_model_and_keeps_upstream_credential_server_side(
    tmp_path: Path,
) -> None:
    requests: list[tuple[dict, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            requests.append((payload, self.headers.get("Authorization", "")))
            body = json.dumps({
                "id": "chatcmpl-test",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "api-path-ok",
                }, "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = ModelGateway(
        tmp_path / "store.json", mode="api",
        upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        upstream_api_key="test-upstream-secret", upstream_model="server-model",
        timeout_s=5, request_namespace="api-session",
    )
    try:
        status, content_type, body, request_id = gateway.complete_http({
            "model": "guest-model",
            "messages": [{"role": "user", "content": "hello"}],
        })
        gateway.mark_delivery(request_id, delivered=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["choices"][0]["message"]["content"] == "api-path-ok"
    assert requests == [(
        {"model": "server-model", "messages": [{"role": "user", "content": "hello"}]},
        "Bearer test-upstream-secret",
    )]
    record = gateway.records()[0]
    assert record["production_attempts"] == 1
    assert record["delivered"] is True
    assert gateway.replay_completeness()["complete"] is True


def test_api_gateway_preserves_upstream_error_status(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = b'{"error":{"type":"authentication_error"}}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = ModelGateway(
        tmp_path / "store.json", mode="api",
        upstream_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        upstream_api_key="invalid", upstream_model="server-model", timeout_s=5,
    )
    try:
        with pytest.raises(RuntimeError, match="upstream model returned HTTP 401"):
            gateway.complete_http({
                "model": "guest-model",
                "messages": [{"role": "user", "content": "hello"}],
            })
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    record = gateway.records()[0]
    assert record["status_code"] == 401
    assert record["error"] == "upstream model returned HTTP 401"
    assert gateway.replay_completeness()["complete"] is False
