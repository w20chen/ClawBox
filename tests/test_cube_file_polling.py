from __future__ import annotations

import sys
from types import SimpleNamespace

import httpx
import pytest

from clawbox.cube import CubeCommandExecutor, CubeSandboxClient


class FilesystemNotFoundError(Exception):
    pass


@pytest.fixture
def native_filesystem(monkeypatch):
    monkeypatch.setitem(sys.modules, "cubesandbox", SimpleNamespace(
        FilesystemNotFoundError=FilesystemNotFoundError,
    ))


@pytest.mark.parametrize("entry, expected", [({}, False), ({"size": "0"}, False),
                                           ({"size": "2"}, True)])
def test_native_file_poll_uses_stat_with_timeout_without_command_or_thread(
    native_filesystem, monkeypatch, entry, expected,
):
    calls = []

    def stat(path, *, timeout):
        calls.append((path, timeout))
        return entry

    monkeypatch.setattr("clawbox.cube.client.threading.Thread", lambda **_: pytest.fail(
        "native filesystem polling must not create a thread",
    ))
    sandbox = SimpleNamespace(files=SimpleNamespace(stat=stat))
    assert CubeSandboxClient.file_nonempty(sandbox, "/state/agent.exit", timeout_s=10) is expected
    assert calls == [("/state/agent.exit", 10)]


def test_native_file_poll_missing_file_does_not_retry(native_filesystem, monkeypatch):
    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", lambda _: pytest.fail(
        "a missing completion file is not a transport failure",
    ))

    def stat(*_args, **_kwargs):
        raise FilesystemNotFoundError()

    sandbox = SimpleNamespace(files=SimpleNamespace(stat=stat))
    assert CubeSandboxClient.file_nonempty(sandbox, "/state/agent.exit", timeout_s=10) is False


@pytest.mark.parametrize("entry, expected", [({"size": "2"}, True), ({"size": "0"}, False),
                                           (None, False)])
def test_native_file_poll_recovers_from_transient_504(
    native_filesystem, monkeypatch, entry, expected,
):
    calls, delays = [], []
    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", delays.append)

    def stat(path, *, timeout):
        calls.append((path, timeout))
        if len(calls) == 1:
            request = httpx.Request("POST", "http://cube/filesystem.Stat")
            raise httpx.HTTPStatusError("HTTP 504", request=request,
                                        response=httpx.Response(504, request=request))
        if entry is None:
            raise FilesystemNotFoundError()
        return entry

    sandbox = SimpleNamespace(files=SimpleNamespace(stat=stat))
    assert CubeSandboxClient.file_nonempty(sandbox, "/state/agent.exit", timeout_s=10) is expected
    assert calls == [("/state/agent.exit", 10)] * 2
    assert delays == [0.1]


@pytest.mark.parametrize("error", [httpx.ReadTimeout("stalled"), OSError("permission denied")])
def test_native_file_poll_propagates_final_failure_after_bounded_retries(
    native_filesystem, monkeypatch, error,
):
    calls, delays = [], []
    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", delays.append)

    def stat(path, *, timeout):
        calls.append((path, timeout))
        raise error

    sandbox = SimpleNamespace(files=SimpleNamespace(stat=stat))
    with pytest.raises(RuntimeError, match="read-only Cube API operation failed after 8 attempts") as exc:
        CubeSandboxClient.file_nonempty(sandbox, "/state/agent.exit", timeout_s=10)
    assert exc.value.__cause__ is error
    assert calls == [("/state/agent.exit", 10)] * 8
    assert delays == [0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0]


def test_executor_file_poll_uses_restored_handle():
    calls = []
    current = [object()]
    client = SimpleNamespace(file_nonempty=lambda handle, path, **kwargs: calls.append(
        (handle, path, kwargs),
    ) or True)
    executor = CubeCommandExecutor(client, lambda: current[0])
    original = current[0]
    assert executor.file_nonempty("/state/agent.exit", 10)
    current[0] = object()
    assert executor.file_nonempty("/state/agent.exit", 10)
    assert calls[0][0] is original
    assert calls[1] == (current[0], "/state/agent.exit", {"timeout_s": 10})


@pytest.mark.parametrize("sizes", [[2], [0, 2], [0, 0, 0, 2]])
def test_completion_watch_handles_existing_file_subscribe_race_and_empty_create(
    native_filesystem, sizes,
):
    from contextlib import contextmanager
    sizes = iter(sizes)
    closed = []

    @contextmanager
    def watch_dir(path, *, timeout):
        assert path == "/state/completion" and 0 < timeout <= 10
        try:
            yield iter([SimpleNamespace(name="agent.exit"), SimpleNamespace(name="agent.exit")])
        finally:
            closed.append(True)

    files = SimpleNamespace(
        stat=lambda *args, **kwargs: {"size": next(sizes)}, watch_dir=watch_dir,
    )
    assert CubeSandboxClient.wait_file_nonempty(
        SimpleNamespace(files=files), "/state/completion/agent.exit", timeout_s=10,
    )


def test_completion_watch_closes_idle_stream_and_does_not_hide_permission_errors(native_filesystem):
    from contextlib import contextmanager
    closed = []
    errors = [httpx.ReadTimeout("idle"), OSError("permission denied")]

    @contextmanager
    def watch_dir(*args, **kwargs):
        try:
            def events():
                raise errors.pop(0)
                yield
            yield events()
        finally:
            closed.append(True)

    sandbox = SimpleNamespace(files=SimpleNamespace(
        stat=lambda *args, **kwargs: {"size": 0}, watch_dir=watch_dir,
    ))
    assert not CubeSandboxClient.wait_file_nonempty(sandbox, "/state/agent.exit", timeout_s=10)
    with pytest.raises(OSError, match="permission denied"):
        CubeSandboxClient.wait_file_nonempty(sandbox, "/state/agent.exit", timeout_s=10)
    assert len(closed) == 2
