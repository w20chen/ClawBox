from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from trace_fixtures import llm_spans, write_spans

from clawbox.replay.model_gateway import ModelGateway, _canonical_replay_input


def test_replay_divergence_is_persisted_before_store_directory_exists(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    write_spans(trace, llm_spans([{"role": "user", "content": "expected"}], {"content": "ok"}, duration_ms=100))
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
    write_spans(trace, llm_spans([{"role": "user", "content": "first"}], {"content": "first response"}, duration_ms=100))
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
    write_spans(trace, llm_spans([{"role": "user", "content": "expected"}], {"content": "ok"}, duration_ms=100))
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
    write_spans(trace, llm_spans([{"role": "system", "content": expected}], {"content": "ok"}, duration_ms=100))
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


def test_replay_preserves_network_errors_and_installed_file_differences():
    def request(content):
        return {"messages": [{"role": "tool", "tool_call_id": "probe", "content": content}]}
    assert _canonical_replay_input(request("ERROR: No matching distribution found for pytest")) != (
        _canonical_replay_input(request("WARNING: Retrying /simple/pytest/")))
    assert _canonical_replay_input(request("/usr/share/python-wheels/pip.whl")) != (
        _canonical_replay_input(request("/root/.cache/pip/wheels/sly.whl")))


def test_replay_normalizes_python_temporary_names_without_hiding_errors():
    expected = "wheel -w '/tmp/tmpd7qz0166' returned non-zero exit status 1"
    actual = "wheel -w '/tmp/tmpl__ihy9p' returned non-zero exit status 1"
    assert _canonical_replay_input(expected) == _canonical_replay_input(actual)
    assert _canonical_replay_input(expected) != _canonical_replay_input(actual.replace("status 1", "status 2"))
    assert _canonical_replay_input("/tmp/project-a") != _canonical_replay_input("/tmp/project-b")


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
