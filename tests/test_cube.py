from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests import ConnectionError as RequestsConnectionError, Timeout as RequestsTimeout
from trace_fixtures import llm_spans, write_spans

from clawbox.cube import (
    CubeCommandExecutor,
    CubeSandboxClient,
    CubeSandboxLifecycle,
    CubeSandboxTcpEndpoint,
    OwnedSandboxJournal,
    Ownership,
)
from clawbox.experiments import BASELINES, ExperimentSpec
import clawbox.experiments.worker as worker_module
from clawbox.experiments.openclaw_driver import NativeSSHRoute
from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.snapshot_pool import (
    SnapshotKey,
    WarmCapacityError,
    WarmSnapshotPool,
    WarmSnapshotTooLarge,
)
from clawbox.experiments.spec_types import SnapshotTier
from clawbox.experiments.worker import ExperimentWorker


class _Files:
    def __init__(self) -> None:
        self.content: dict[str, str] = {}

    def read(self, path: str) -> str:
        return self.content[path]

    def write(self, path: str, data: str) -> None:
        self.content[path] = data


def test_runtime_native_ssh_warmup_verifies_the_tool_identity() -> None:
    calls = []

    class RuntimeExecutor:
        def execute(self, command, timeout):
            calls.append((command, timeout))
            return SimpleNamespace(exit_code=0, stdout="session-1\n", stderr="")

    attempts = worker_module.warm_runtime_ssh_route(
        RuntimeExecutor(),
        NativeSSHRoute("tool-1", 22, 1, "192.0.2.20", 22022),
        "session-1",
    )

    assert attempts == 1
    assert len(calls) == 1
    command, timeout = calls[0]
    assert "HostKeyAlias=clawbox-tool-tool-1" in command
    assert "-p 22022 executor@192.0.2.20" in command
    assert "/state/openclaw/session-1/ssh/id_ed25519" in command
    assert timeout == 30


def test_session_pair_starts_tool_and_runtime_concurrently() -> None:
    barrier = threading.Barrier(2)

    class Lifecycle:
        def __init__(self, value: float) -> None:
            self.value = value

        def start(self) -> float:
            barrier.wait(timeout=1)
            return self.value

    tool, runtime = worker_module.start_sandbox_pair(
        Lifecycle(1.25), Lifecycle(2.5),
    )
    assert tool[0] == 1.25
    assert runtime[0] == 2.5
    assert abs(tool[1] - runtime[1]) < 0.25
    assert tool[1] <= tool[2]
    assert runtime[1] <= runtime[2]


@pytest.mark.parametrize('succeeds', [False, True])
def test_runtime_ssh_warmup_records_execution_and_retry_time(monkeypatch, succeeds):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(worker_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(worker_module.time, 'sleep', lambda duration: clock.__setitem__(0, clock[0] + duration))

    class Executor:
        def execute(self, command, timeout):
            calls.append(command)
            clock[0] += 3.0
            if len(calls) == 1:
                raise TimeoutError('transport timeout')
            return SimpleNamespace(exit_code=0, stdout='session-1' if succeeds else 'stale-session', stderr='')

    timing = {}
    def run():
        return worker_module.warm_runtime_ssh_route(
            Executor(), NativeSSHRoute('tool-1', 22, 1, '192.0.2.20', 22022),
            'session-1', attempt_limit=2, timing=timing)
    if succeeds:
        assert run() == 2
    else:
        with pytest.raises(RuntimeError, match='intended Tool identity'):
            run()
    first, second = timing['attempt_observations']
    assert first['error_type'] == 'TimeoutError'
    assert first['execute_seconds'] == second['execute_seconds'] == 3.0
    assert first['retry_sleep_seconds'] == 0.5
    assert second['retry_sleep_seconds'] == 0.0
    assert second['identity_matches'] is succeeds


def test_snapshot_retention_value_uses_logical_state_not_private_delta() -> None:
    lifecycle = object.__new__(CubeSandboxLifecycle)
    lifecycle.snapshot_reuse_probability = 0.5
    lifecycle.warm_restore_seconds_per_gib = 0.1
    lifecycle.cold_restore_seconds_per_gib = 0.7

    assert lifecycle._retention_value_seconds(4 * 1024**3) == pytest.approx(1.2)


def test_next_use_update_does_not_wait_for_lifecycle_io_lock():
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template="tpl",
        node_name="node-a", ownership=_owner(),
    )
    held = threading.Event()
    release = threading.Event()

    def hold_lifecycle_lock():
        with lifecycle._lock:
            held.set()
            assert release.wait(1)

    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(hold_lifecycle_lock)
        assert held.wait(1)
        lifecycle.set_snapshot_next_use(123.0, revision=1)
        assert lifecycle._snapshot_next_use_monotonic_s == 123.0
        release.set()
        holder.result(timeout=1)


class _Commands:
    def __init__(self, owner) -> None:
        self.owner = owner

    def run(self, command: str, **kwargs):
        self.owner.command_calls.append((command, kwargs))
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")


class _Sandbox:
    items: dict[str, "_Sandbox"] = {}
    create_kwargs: dict = {}

    def __init__(self, data=None, config=None) -> None:
        data = data or {}
        self.sandbox_id = data.get("sandboxID", "sb-1")
        self.template_id = data.get("templateID", "tpl-1")
        self.metadata = data.get("metadata", {})
        self.state = data.get("state", "running")
        self.files = _Files()
        self.command_calls = []
        self.commands = _Commands(self)
        self.network_updates = []

    @classmethod
    def create(cls, **kwargs):
        cls.create_kwargs = kwargs
        item = cls({"sandboxID": f"sb-{len(cls.items) + 1}", "templateID": kwargs["template"],
                    "metadata": kwargs["metadata"]})
        cls.items[item.sandbox_id] = item
        return item

    @classmethod
    def connect(cls, sandbox_id, **kwargs):
        item = cls.items[sandbox_id]
        item.state = "running"
        return item

    @classmethod
    def list_v2(cls, **kwargs):
        return [{"sandboxID": item.sandbox_id, "templateID": item.template_id,
                 "metadata": item.metadata, "state": item.state} for item in cls.items.values()]

    @classmethod
    def delete_snapshot(cls, snapshot_id, **kwargs):
        cls.snapshots.discard(snapshot_id)

    snapshots: set[str] = set()

    def create_snapshot(self, name=None):
        snapshot_id = f"snap-{len(self.snapshots) + 1}"
        self.snapshots.add(snapshot_id)
        return SimpleNamespace(snapshot_id=snapshot_id, names=[name] if name else [])

    def pause(self, wait=True, **kwargs):
        self.state = "paused"
        if kwargs:
            if kwargs.get("snapshot_mechanism") == "incremental-cow":
                memory = Path(kwargs["memory_snapshot_path"])
                memory.parent.mkdir(parents=True, exist_ok=True)
                memory.write_bytes(bytes(4096))
                layers = Path(str(memory) + ".layers")
                layers.mkdir()
                (layers / "000000.mem").hardlink_to(memory)
                Path(str(memory) + ".lineage.json").write_text(json.dumps({
                    "schema_version": 1, "logical_bytes": 4096,
                    "transferred_bytes": 4096, "full_base": True,
                    "layers": [{"name": "000000.mem"}],
                }))
            return {
                "memory_snapshot_path": kwargs["memory_snapshot_path"],
                "logical_bytes": 1024, "allocated_bytes": 1024,
                "transferred_bytes": 1024,
                "generation": kwargs["snapshot_generation"],
                "tier": kwargs["snapshot_tier"],
            }

    def relocate_snapshot(self, **kwargs):
        return {
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
            "generation": kwargs["snapshot_generation"],
            "tier": kwargs["snapshot_tier"],
        }

    def get_tcp_endpoint(self, container_port):
        ordinal = int(self.sandbox_id.rsplit("-", 1)[-1]) if "-" in self.sandbox_id else 1
        return SimpleNamespace(
            sandbox_id=self.sandbox_id,
            container_port=container_port,
            address=f"192.0.2.{ordinal}:20{ordinal:03d}",
        )

    def update_network(self, network):
        self.network_updates.append(network)

    def kill(self):
        type(self).items.pop(self.sandbox_id, None)


def _owner() -> Ownership:
    return Ownership("run", "attempt", "task", "experiment", "session", "policy")


def test_cube_client_pins_node_disables_automatic_lifecycle_and_journals(tmp_path: Path) -> None:
    _Sandbox.items = {}
    journal = OwnedSandboxJournal(tmp_path / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal, sandbox_class=_Sandbox)
    sandbox = client.create_sandbox(template="tpl-arm64", node_name="node-a", ownership=_owner())
    assert _Sandbox.create_kwargs["timeout"] == -1
    assert _Sandbox.create_kwargs["lifecycle"] == {"on_timeout": "kill", "auto_resume": False}
    assert _Sandbox.create_kwargs["distribution_scope"] == ["node-a"]
    assert journal.sandbox_ids(task_uid="task") == [sandbox.sandbox_id]


def test_create_trace_separates_sdk_and_durable_journal(tmp_path: Path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(
        journal=OwnedSandboxJournal(tmp_path / "owned.jsonl"), sandbox_class=_Sandbox,
    )
    lifecycle = CubeSandboxLifecycle(client, template="tpl", node_name="node-a", ownership=_owner())
    lifecycle.start()
    attempts = lifecycle.timings[-1]["create_attempts"]
    assert len(attempts) == 1
    stages = {row["stage"]: row for row in attempts[0]["spans"]}
    assert set(stages) == {
        "sdk_create", "journal_lock_wait", "journal_lock_hold", "journal_open",
        "journal_write_flush", "journal_fsync", "journal_close",
    }
    assert stages["sdk_create"]["completed_monotonic_s"] <= stages["journal_lock_wait"]["started_monotonic_s"]
    assert stages["journal_lock_hold"]["started_monotonic_s"] <= stages["journal_fsync"]["started_monotonic_s"]
    assert stages["journal_fsync"]["completed_monotonic_s"] <= stages["journal_lock_hold"]["completed_monotonic_s"]
    assert all(row["duration_s"] >= 0 and row["thread_cpu_s"] >= 0 for row in stages.values())


def test_journal_trace_keeps_fsync_failure_and_releases_lock(tmp_path: Path, monkeypatch) -> None:
    import clawbox.cube.client as client_module
    journal = OwnedSandboxJournal(tmp_path / "owned.jsonl")
    trace = []
    def fail_fsync(_fd):
        raise OSError("injected flush failure")
    with monkeypatch.context() as patch:
        patch.setattr(client_module.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="injected flush failure"):
            journal.record("sandbox-1", _owner(), create_trace=trace)
    assert next(row for row in trace if row["stage"] == "journal_fsync")["error_type"] == "OSError"
    assert journal._lock.acquire(blocking=False)
    journal._lock.release()
    journal.record("sandbox-2", _owner())
    assert journal.sandbox_ids(task_uid="task") == ["sandbox-1", "sandbox-2"]


def test_lifecycle_retries_definite_capacity_rejection_outside_transfer_slot(monkeypatch) -> None:
    import clawbox.cube.lifecycle as lifecycle_module

    class CapacitySandbox(_Sandbox):
        attempts = 0

        @classmethod
        def create(cls, **kwargs):
            cls.attempts += 1
            if cls.attempts == 1:
                raise RuntimeError("CubeMaster returned error code 130597: no more resource")
            return super().create(**kwargs)

    class TransferPool:
        active = 0
        entries = 0

        @contextmanager
        def transfer(self, operation):
            assert operation == "create"
            self.entries += 1
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1

    CapacitySandbox.items = {}
    pool = TransferPool()
    sleeps = []
    monkeypatch.setattr(lifecycle_module.random, "uniform", lambda _low, high: high)

    def sleep(delay):
        assert pool.active == 0
        sleeps.append(delay)

    monkeypatch.setattr(lifecycle_module.time, "sleep", sleep)
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=CapacitySandbox),
        template="tpl", node_name="node", ownership=_owner(), snapshot_pool=pool,
    )

    lifecycle.start()

    assert CapacitySandbox.attempts == 2
    assert pool.entries == 2
    assert sleeps == [0.25]
    stages = lifecycle.timings[-1]["stage_seconds"]
    assert stages["create_capacity_retry_count"] == 1
    assert stages["create_capacity_retry_sleep_s"] == 0.25


def test_lifecycle_capacity_retry_can_be_disabled(monkeypatch) -> None:
    class CapacitySandbox(_Sandbox):
        attempts = 0

        @classmethod
        def create(cls, **_kwargs):
            cls.attempts += 1
            raise RuntimeError("CubeMaster returned error code 130597: no more resource")

    client = CubeSandboxClient(
        sandbox_class=CapacitySandbox, create_capacity_retry_seconds=0,
    )
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node", ownership=_owner(),
    )
    with pytest.raises(RuntimeError, match="130597"):
        lifecycle.start()
    assert CapacitySandbox.attempts == 1


@pytest.mark.parametrize("failure", [RequestsTimeout("lost"), RequestsConnectionError("lost"),
                                     408, 502, 503, 504])
def test_cube_client_reconciles_a_timed_out_create_by_unique_ownership(
    tmp_path: Path, failure,
) -> None:
    class TimedOutSandbox(_Sandbox):
        create_calls = 0

        @classmethod
        def create(cls, **kwargs):
            cls.create_calls += 1
            item = cls({
                "sandboxID": "sb-recovered", "templateID": kwargs["template"],
                "metadata": kwargs["metadata"],
            })
            cls.items[item.sandbox_id] = item
            if isinstance(failure, int):
                error = RuntimeError("SDK HTTP failure")
                error.status_code = failure
                raise error
            raise failure

    TimedOutSandbox.items = {}
    journal = OwnedSandboxJournal(tmp_path / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal, sandbox_class=TimedOutSandbox)

    sandbox = client.create_sandbox(
        template="tpl-arm64", node_name="node-a", ownership=_owner(),
    )

    assert sandbox.sandbox_id == "sb-recovered"
    assert journal.sandbox_ids(task_uid="task") == ["sb-recovered"]
    assert TimedOutSandbox.create_calls == 1
    assert sandbox.command_calls == [("/bin/true", {"timeout": 5})]


@pytest.mark.parametrize("becomes_ready", [True, False])
def test_create_recovery_requires_guest_readiness_and_preserves_cleanup(
    tmp_path, monkeypatch, becomes_ready,
):
    import clawbox.cube.api_retry as retry_module
    monkeypatch.setattr(retry_module.time, "sleep", lambda _: None)

    class RestoringSandbox(_Sandbox):
        probes = 0
        creates = 0

        def __init__(self, data=None, config=None):
            super().__init__(data, config)
            self.commands = SimpleNamespace(run=self.probe)

        def probe(self, command, **kwargs):
            type(self).probes += 1
            assert command == "/bin/true"
            if not becomes_ready or type(self).probes == 1:
                raise RequestsConnectionError("guest still restoring")
            return SimpleNamespace(exit_code=0)

        @classmethod
        def create(cls, **kwargs):
            cls.creates += 1
            cls.items = {"sb-restoring": cls({
                "sandboxID": "sb-restoring", "metadata": kwargs["metadata"],
            })}
            raise RequestsTimeout("response deadline")

    journal = OwnedSandboxJournal(tmp_path / "owned.jsonl")
    client = CubeSandboxClient(sandbox_class=RestoringSandbox, journal=journal)
    if becomes_ready:
        assert client.create_sandbox(
            template="tpl", node_name="node", ownership=_owner(),
        ).sandbox_id == "sb-restoring"
        assert RestoringSandbox.probes == 2
    else:
        with pytest.raises(RuntimeError, match="guest readiness"):
            client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    assert RestoringSandbox.creates == 1
    assert journal.sandbox_ids(task_uid="task") == ["sb-restoring"]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, None])
def test_create_definite_rejection_does_not_reconcile(status):
    error = RuntimeError("rejected")
    error.status_code = status

    class RejectedSandbox:
        @classmethod
        def create(cls, **kwargs):
            raise error

        @classmethod
        def list_v2(cls, **kwargs):
            pytest.fail("definite rejection must not be hidden by reconciliation")

    with pytest.raises(RuntimeError) as caught:
        CubeSandboxClient(sandbox_class=RejectedSandbox).create_sandbox(
            template="tpl", node_name="node", ownership=_owner(),
        )
    assert caught.value is error


@pytest.mark.parametrize("matching", [0, 2])
def test_create_reconciliation_rejects_missing_or_ambiguous_ownership(monkeypatch, matching):
    import clawbox.cube.client as module
    monkeypatch.setattr(module, "read_with_backoff", lambda operation, **kwargs: operation())

    class AmbiguousSandbox:
        calls = 0

        @classmethod
        def create(cls, **kwargs):
            cls.calls += 1
            raise RequestsTimeout("lost response")

        @classmethod
        def list_v2(cls, **kwargs):
            return [{"sandboxID": str(i), "metadata": _owner().metadata()}
                    for i in range(matching)] + [{"sandboxID": "unrelated", "metadata": {}}]

    with pytest.raises(RuntimeError, match="ownership reconciliation failed"):
        CubeSandboxClient(sandbox_class=AmbiguousSandbox).create_sandbox(
            template="tpl", node_name="node", ownership=_owner(),
        )
    assert AmbiguousSandbox.calls == 1


def test_cube_client_adds_only_explicit_narrow_egress_allowlist() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    client.create_sandbox(
        template="tpl", node_name="node-a", ownership=_owner(),
        network_allow_out=["10.244.1.23/32"],
        network_deny_out=["0.0.0.0/0"],
    )
    assert _Sandbox.create_kwargs["network"] == {
        "allow_out": ["10.244.1.23/32"], "deny_out": ["0.0.0.0/0"],
    }


def test_cube_client_consumes_semantic_tcp_endpoint_and_checks_identity() -> None:
    class ClosableSandbox(_Sandbox):
        closed = 0

        def close(self):
            type(self).closed += 1

    sandbox = ClosableSandbox({"sandboxID": "sb-7"})
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    endpoint = client.get_tcp_endpoint(sandbox)
    assert endpoint == CubeSandboxTcpEndpoint(
        sandbox_id="sb-7", container_port=2222, address="192.0.2.7:20007",
    )
    assert ClosableSandbox.closed == 1

    class WrongEndpoint(_Sandbox):
        def get_tcp_endpoint(self, container_port):
            return SimpleNamespace(
                sandbox_id="another-sandbox", container_port=container_port,
                address="192.0.2.99:20099",
            )

    try:
        client.get_tcp_endpoint(WrongEndpoint({"sandboxID": "sb-8"}))
    except RuntimeError as exc:
        assert "identity mismatch" in str(exc)
    else:
        raise AssertionError("endpoint identity mismatch was not rejected")


def test_cube_client_retries_endpoint_publication_after_create() -> None:
    class EventuallyPublishedSandbox(_Sandbox):
        endpoint_calls = 0

        def get_tcp_endpoint(self, container_port):
            type(self).endpoint_calls += 1
            if type(self).endpoint_calls < 3:
                raise RuntimeError("HTTP 404: route not published yet")
            return super().get_tcp_endpoint(container_port)

    EventuallyPublishedSandbox.items = {}
    client = CubeSandboxClient(
        sandbox_class=EventuallyPublishedSandbox,
        tcp_endpoint_attempts=3,
        tcp_endpoint_initial_delay_s=0,
        tcp_endpoint_max_delay_s=0,
    )
    sandbox = EventuallyPublishedSandbox({"sandboxID": "sb-7"})
    endpoint = client.get_tcp_endpoint(sandbox)
    assert endpoint.address == "192.0.2.7:20007"
    assert EventuallyPublishedSandbox.endpoint_calls == 3


def test_cube_client_reads_state_from_existing_handle_without_full_inventory() -> None:
    class DirectInfoSandbox(_Sandbox):
        @classmethod
        def list_v2(cls, **kwargs):
            raise AssertionError("state lookup must not scan the full inventory")

        def get_info(self):
            return {"state": "PAUSED"}

    client = CubeSandboxClient(sandbox_class=DirectInfoSandbox)
    assert client.get_sandbox_state(DirectInfoSandbox({"sandboxID": "sb-direct"})) == "paused"


def test_cube_client_reads_state_by_id_without_page_limited_inventory() -> None:
    class DirectInfoSandbox(_Sandbox):
        closed = 0

        @classmethod
        def list_v2(cls, **kwargs):
            raise AssertionError("state lookup must not scan a bounded inventory page")

        def get_info(self):
            return {"state": "PAUSED"}

        def close(self):
            type(self).closed += 1

    client = CubeSandboxClient(sandbox_class=DirectInfoSandbox)

    assert client.get_sandbox_state("sb-direct") == "paused"
    assert DirectInfoSandbox.closed == 1


def test_lifecycle_refreshes_runtime_network_only_for_a_new_endpoint_host() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        allow_internet_access=False,
        network_allow_out=["192.0.2.10/32"],
        network_deny_out=["0.0.0.0/0"],
    )
    lifecycle.start()
    assert lifecycle.ensure_network_allow_out("192.0.2.10/32") is False
    assert lifecycle.ensure_network_allow_out("192.0.2.11/32") is True
    assert lifecycle.sandbox.network_updates == [{
        "allow_internet_access": False,
        "allow_out": ["192.0.2.10/32", "192.0.2.11/32"],
        "deny_out": ["0.0.0.0/0"],
    }]
    assert lifecycle.ensure_network_allow_out("192.0.2.11/32") is False
    lifecycle.close()


def test_cube_client_rejects_ready_template_with_wrong_pinned_image() -> None:
    expected = "sha256:" + "a" * 64

    class Template:
        status = "READY"
        image_info = "http://registry.example/clawbox/runtime@sha256:" + "b" * 64

        @classmethod
        def get(cls, _reference: str):
            return cls

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    with pytest.raises(RuntimeError, match="digest mismatch"):
        client.validate_template_image("tpl-runtime", expected)


def test_cube_client_accepts_ready_template_with_pinned_image() -> None:
    digest = "sha256:" + "a" * 64

    class Template:
        status = "READY"
        image_info = "http://registry.example/clawbox/runtime@" + digest

        @classmethod
        def get(cls, _reference: str):
            return cls

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    result = client.validate_template_image("tpl-runtime", digest)
    assert result["image_digest"] == digest


def test_cube_client_uses_official_list_when_detail_omits_image_info() -> None:
    digest = "sha256:" + "a" * 64

    class Item:
        template_id = "tpl-runtime"
        image_info = "http://registry.example/clawbox/runtime@" + digest

    class Template:
        status = "READY"
        image_info = ""

        @classmethod
        def get(cls, _reference: str):
            return cls

        @classmethod
        def list(cls):
            return [Item()]

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    result = client.validate_template_image("tpl-runtime", digest)
    assert result["image_digest"] == digest


def test_cube_client_validates_many_templates_with_parallel_detail_reads() -> None:
    runtime_digest = "sha256:" + "a" * 64
    tool_digest = "sha256:" + "b" * 64

    class Item:
        def __init__(self, template_id: str, digest: str) -> None:
            self.template_id = template_id
            self.status = "READY"
            self.image_info = f"registry.example/{template_id}@{digest}"

    class Template:
        calls = []

        @classmethod
        def list(cls, **_kwargs):
            raise AssertionError("detail responses have complete image provenance")

        @classmethod
        def get(cls, reference: str, **kwargs):
            cls.calls.append((reference, kwargs))
            digest = runtime_digest if reference == "tpl-runtime" else tool_digest
            return Item(reference, digest)

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    result = client.validate_template_images({
        "tpl-runtime": runtime_digest,
        "tpl-tool": tool_digest,
    })

    assert sorted(Template.calls) == [
        ("tpl-runtime", {"limit": 1}),
        ("tpl-tool", {"limit": 1}),
    ]
    assert result["tpl-runtime"]["image_digest"] == runtime_digest
    assert result["tpl-tool"]["image_digest"] == tool_digest


def test_cube_client_bounds_a_stalled_command_stream() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox, command_stream_grace_s=0.01)
    sandbox = _Sandbox({"sandboxID": "stalled"})
    release = threading.Event()

    def stalled_run(_command: str, **_kwargs):
        release.wait()
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    sandbox.commands = SimpleNamespace(run=stalled_run)
    started = time.monotonic()
    try:
        client.run_command(sandbox, "true", timeout_s=0.02)
    except TimeoutError as exc:
        assert "command stream exceeded deadline" in str(exc)
    else:
        raise AssertionError("stalled command stream was not bounded")
    finally:
        release.set()
    assert time.monotonic() - started < 0.5


def test_cube_client_retries_only_preconnect_local_port_exhaustion(monkeypatch) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    sandbox = _Sandbox({"sandboxID": "port-retry"})
    calls = []

    def run(command: str, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            cause = OSError(99, "Cannot assign requested address")
            raise RuntimeError("connect failed") from cause
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    sandbox.commands = SimpleNamespace(run=run)
    result = client.run_command(sandbox, "true", timeout_s=1)
    assert result.stdout == "ok"
    assert calls == ["true", "true"]


def test_cube_client_does_not_retry_ambiguous_command_failure(monkeypatch) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    sandbox = _Sandbox({"sandboxID": "no-retry"})
    calls = []

    def run(command: str, **_kwargs):
        calls.append(command)
        raise RuntimeError("stream ended after request was sent")

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    sandbox.commands = SimpleNamespace(run=run)
    with pytest.raises(RuntimeError, match="stream ended"):
        client.run_command(sandbox, "touch /tmp/result", timeout_s=1)
    assert calls == ["touch /tmp/result"]


def test_incremental_pause_cannot_silently_use_legacy_api() -> None:
    sandbox = _Sandbox()
    with pytest.raises(ValueError, match="explicit snapshot tier"):
        CubeSandboxClient.pause_sandbox(sandbox)
    assert sandbox.state == "running"
    CubeSandboxClient.pause_sandbox(sandbox, snapshot_mechanism="full-copy")
    assert sandbox.state == "paused"


def test_pause_uses_synchronous_server_completion_and_validates_lineage(monkeypatch) -> None:
    from clawbox.cube import snapshot
    calls = []

    def pause(**kwargs):
        calls.append(kwargs)
        return {"tier": "warm", "generation": 1, "memory_snapshot_path": "/warm/vm/g1.mem"}

    def validate(path, *, require_lineage):
        assert path == "/warm/vm/g1.mem" and require_lineage
        raise RuntimeError("invalid native RAM lineage")

    monkeypatch.setattr(snapshot, "read_snapshot_metadata", validate)
    with pytest.raises(RuntimeError, match="invalid native RAM lineage"):
        CubeSandboxClient.pause_sandbox(
            SimpleNamespace(pause=pause), tier="warm",
            memory_snapshot_path="/warm/vm/g1.mem", generation=1,
        )
    assert calls[0]["wait"] is False



def test_missing_cold_destination_fails_before_pausing_vm() -> None:
    _Sandbox.items = {}
    lifecycle = CubeSandboxLifecycle(CubeSandboxClient(sandbox_class=_Sandbox),
                                    template="tpl", node_name="node-a", ownership=_owner())
    lifecycle.start()
    with pytest.raises(ValueError, match="cold_snapshot_root"):
        lifecycle.checkpoint_and_evict()
    assert lifecycle.resident and lifecycle.sandbox.state == "running"
    lifecycle.close()


def test_warm_only_never_falls_back_to_cold():
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(client, template="tpl", node_name="node-a", ownership=_owner(),
                                    warm_snapshot_root="/warm", cold_snapshot_root="/must-not-write",
                                    snapshot_storage="warm-only", snapshot_pool=WarmSnapshotPool(512),
                                    snapshot_reservation_bytes=1024)
    lifecycle.start()
    with pytest.raises(RuntimeError, match="disk fallback is disabled"):
        lifecycle.checkpoint_and_evict()
    assert lifecycle.resident and lifecycle.sandbox.state == "running"
    with pytest.raises(ValueError, match="COLD snapshots are disabled"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    lifecycle.close()


def test_lifecycle_preserves_id_across_pause_restore_and_executor(tmp_path) -> None:
    _Sandbox.items = {}
    calls=[]
    class Routed(_Sandbox):
        @classmethod
        def create(cls, **kwargs):
            calls.append('create')
            return super().create(**kwargs)
        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            calls.append('connect')
            return super().connect(sandbox_id, **kwargs)
    client = CubeSandboxClient(sandbox_class=Routed)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        cold_snapshot_root=str(tmp_path / "cold"),
        cgroup_token='a'*32, prepare_cgroup=lambda:calls.append('prepare'),
        verify_cgroup=lambda sandbox_id:calls.append(('verify',sandbox_id)),
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id
    assert Routed.create_kwargs['env_vars']['CLAWBOX_CGROUP_TOKEN']=='a'*32
    assert calls==['prepare','create',('verify',sandbox_id)]
    executor = CubeCommandExecutor(client, lambda: lifecycle.sandbox)
    assert executor.execute("printf ok", 5).stdout == "ok"
    lifecycle.checkpoint_and_evict()
    assert lifecycle.tier is SnapshotTier.COLD
    assert lifecycle.timings[-1]["snapshot_metrics"]["snapshot_mechanism"] == "incremental-cow"
    assert not lifecycle.resident
    assert lifecycle.checkpoint_and_evict() is None
    lifecycle.restore()
    assert calls[-3:]==['prepare','connect',('verify',sandbox_id)]
    assert lifecycle.sandbox_id == sandbox_id and lifecycle.resident
    lifecycle.close()
    assert sandbox_id not in _Sandbox.items


def test_incremental_lifecycle_events_expose_compact_cross_tier_lineage(tmp_path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client,
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=WarmSnapshotPool(65536), snapshot_reservation_bytes=8192,
    )
    lifecycle.start()

    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    first = lifecycle.timings[-1]["snapshot_metrics"]
    lifecycle.restore()
    pause = client.pause_sandbox

    def pause_and_retire_warm(sandbox, **kwargs):
        result = pause(sandbox, **kwargs)
        previous = Path(first["memory_snapshot_path"])
        previous.unlink()
        Path(str(previous) + ".lineage.json").unlink()
        shutil.rmtree(Path(str(previous) + ".layers"))
        return result

    # Native Cube retires the predecessor names after linking their page
    # closure into the successor. The minimal fake has no lineage catalog, so
    # reproduce that completed cleanup at the same post-pause boundary.
    client.pause_sandbox = pause_and_retire_warm
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    second = lifecycle.timings[-1]["snapshot_metrics"]

    assert first["lineage_snapshot_id"].endswith("/session/tool/g1")
    assert first["lineage_parent_snapshot_id"] is None
    assert first["lineage_layer_map"] == [{
        "layer_index": 0,
        "identity": first["lineage_layer_map"][0]["identity"],
        "allocated_bytes": first["lineage_layer_map"][0]["allocated_bytes"],
        "external": False,
        "managed": False,
        "managed_layer_id": None,
        "path_kind": "snapshot_owned",
        "range_count": 0,
        "range_bytes": 0,
        "tier": "warm",
    }]
    assert second["lineage_snapshot_id"].endswith("/session/tool/g2")
    assert second["lineage_parent_snapshot_id"] == first["lineage_snapshot_id"]
    assert second["lineage_layer_map"][0]["tier"] == "cold"
    lifecycle.restore()
    lifecycle.close()


@pytest.mark.parametrize('return_to_warm', [False, True])
def test_cold_lifecycle_retains_warm_ancestor_accounting(tmp_path, return_to_warm):
    from clawbox.cube.snapshot import read_snapshot_metadata, _allocated_bytes
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1024 * 1024)
    warm = tmp_path / 'warm'
    lifecycle = CubeSandboxLifecycle(
        client, template='tpl', node_name='node-a', ownership=_owner(),
        warm_snapshot_root=str(warm), cold_snapshot_root=str(tmp_path / 'cold'),
        snapshot_pool=pool, snapshot_reservation_bytes=65536,
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    first = Path(lifecycle.timings[-1]['snapshot_metrics']['memory_snapshot_path'])
    lifecycle.restore()
    managed = warm / 'layers' / 'ancestor' / '000000.mem'
    managed.parent.mkdir(parents=True)
    managed.hardlink_to(first)
    inherited_bytes = _allocated_bytes(managed)
    native_pause = client.pause_sandbox

    def publish_child(sandbox, **kwargs):
        result = dict(native_pause(sandbox, **kwargs))
        head = Path(kwargs['memory_snapshot_path'])
        directory = Path(str(head) + '.layers')
        (directory / '000000.mem').rename(directory / '000001.mem')
        sidecar = Path(str(head) + '.lineage.json')
        document = json.loads(sidecar.read_text())
        document.update(schema_version=2, full_base=False, layers=[
            {'name': '000000.mem', 'managed': True, 'layer_id': 'ancestor',
             'external_path': str(managed)},
            {'name': '000001.mem'},
        ])
        sidecar.write_text(json.dumps(document))
        if first.exists():
            first.unlink()
            Path(str(first) + '.lineage.json').unlink()
            shutil.rmtree(Path(str(first) + '.layers'))
        result.update(read_snapshot_metadata(str(head), require_lineage=True))
        return result

    client.pause_sandbox = publish_child
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    assert pool.committed_bytes == inherited_bytes
    assert not pool.snapshot()['manifests']
    assert lifecycle.timings[-1]['snapshot_metrics']['lineage_layer_map'][0]['tier'] == 'warm'
    lifecycle.restore()
    assert pool.committed_bytes == inherited_bytes
    if return_to_warm:
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
        own = lifecycle.timings[-1]['snapshot_metrics']['allocated_bytes']
        assert pool.committed_bytes == own + inherited_bytes
        lifecycle.restore()
    native_kill = client.kill_sandbox

    def kill_and_gc(sandbox):
        native_kill(sandbox)
        shutil.rmtree(warm / lifecycle.sandbox_id)
        managed.unlink()

    client.kill_sandbox = kill_and_gc
    lifecycle.close()
    assert pool.accounted_bytes == 0


def test_incremental_lineage_labels_template_backing_separately(tmp_path) -> None:
    template = tmp_path / "template.mem"
    template.write_bytes(b"template")
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox),
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
    )
    manifest = lifecycle._annotate_snapshot_lineage({
        "external_layer_allocations": [{"layer_index": 0, "path": str(template)}],
        "lineage_layer_map": [{
            "layer_index": 0, "external": True, "path_kind": "template_external",
        }],
    }, key=SnapshotKey("session", "tool", 1), parent=None, tier=SnapshotTier.WARM)

    assert manifest["lineage_layer_map"][0]["tier"] == "template"


def test_restore_fault_sampling_uses_current_verified_leaf_and_counts_its_time(tmp_path, monkeypatch):
    from clawbox.experiments import memory
    from clawbox.cube import lifecycle as lifecycle_module
    _Sandbox.items = {}
    now = [100.0]
    original_is_dir = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda p: True if p == Path('/proc') else original_is_dir(p))
    monkeypatch.setattr(lifecycle_module.time, 'monotonic', lambda: now[0])
    old_leaf, restored_leaf = tmp_path/'old-leaf', tmp_path/'restored-leaf'
    active_leaf = [old_leaf]
    seen = []
    class Sampler:
        def __init__(self, sandbox_id, *, cgroup=None):
            assert cgroup == restored_leaf  # Never fall back to a host-wide scan.
            seen.append(sandbox_id)
            now[0] += 2.0
    monkeypatch.setattr(memory, 'SandboxRSSSampler', Sampler)
    vm = CubeSandboxLifecycle(CubeSandboxClient(sandbox_class=_Sandbox),
        template='tpl', node_name='node-a', ownership=_owner(),
        cold_snapshot_root=str(tmp_path/'cold'),
        verify_cgroup=lambda _: {'cgroup': str(active_leaf[0])})
    vm.start()
    vm.checkpoint_and_evict()
    active_leaf[0] = restored_leaf
    assert vm.restore() == 2.0
    assert seen == [vm.sandbox_id]
    assert vm.timings[-1]['stage_seconds']['restore_fault_observation_s'] == 2.0
    assert vm._first_tool_restore['restore_ready_monotonic_s'] == now[0]
    vm.close()


def test_lifecycle_passes_verified_cgroup_to_numa_borrower(tmp_path) -> None:
    _Sandbox.items = {}
    calls = []
    class Borrower:
        def pin_local(self, sandbox_id, *, verified_cgroup=None):
            calls.append((sandbox_id, verified_cgroup))
        def release_destroyed(self, sandbox_id):
            pass
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    leaf = tmp_path/'leaf'
    lifecycle = CubeSandboxLifecycle(
        client, template='tpl', node_name='node-a', ownership=_owner(),
        cold_snapshot_root=str(tmp_path/'cold'), numa_borrower=Borrower(),
        verify_cgroup=lambda sandbox_id: {'sandbox_id': sandbox_id, 'cgroup': str(leaf)},
    )
    lifecycle.start()
    assert calls == [(lifecycle.sandbox_id, str(leaf))]
    lifecycle.close()


def test_checkpoint_returns_borrowed_tombstone_to_local_before_restore(tmp_path) -> None:
    _Sandbox.items = {}
    calls = []

    class Borrower:
        def pin_local(self, sandbox_id, *, verified_cgroup=None):
            calls.append(('pin', sandbox_id))
        def borrowed(self):
            return (lifecycle.sandbox_id,)
        def return_local(self, sandbox_id):
            calls.append(('return', sandbox_id))
        def release_destroyed(self, sandbox_id):
            calls.append(('destroy', sandbox_id))

    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template='tpl',
        node_name='node-a', ownership=_owner(),
        cold_snapshot_root=str(tmp_path / 'cold'), numa_borrower=Borrower(),
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id
    lifecycle.checkpoint_and_evict()
    assert ('return', sandbox_id) in calls
    assert ('destroy', sandbox_id) not in calls
    lifecycle.restore()
    lifecycle.close()
    assert ('destroy', sandbox_id) in calls


def test_checkpoint_manifest_failure_does_not_mark_paused_vm_running() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id

    def fail_after_pause(sandbox, **kwargs):
        sandbox.state = "paused"
        raise RuntimeError("lineage unreadable after remote pause")

    client.pause_sandbox = fail_after_pause
    with pytest.raises(RuntimeError, match="lineage unreadable"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    assert lifecycle.state.value == "unknown"
    assert not lifecycle.resident
    with pytest.raises(Exception, match="not resident"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    lifecycle.close()
    assert sandbox_id not in _Sandbox.items


def test_checkpoint_reconciles_lost_response_from_complete_native_snapshot(tmp_path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    pause = client.pause_sandbox

    def lose_response_after_pause(sandbox, **kwargs):
        pause(sandbox, **kwargs)
        raise RuntimeError("control response lost")

    client.pause_sandbox = lose_response_after_pause
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.state.value == "swapped"
    assert pool.reserved_bytes == 0
    assert pool.committed_bytes > 0
    timing = lifecycle.timings[-1]
    assert timing["status"] == "ok"
    assert timing["snapshot_metrics"]["control_response_reconciled"] is True
    lifecycle.restore()
    snapshot = Path(timing["snapshot_path"])
    (Path(str(snapshot) + ".layers") / "000000.mem").unlink()
    Path(str(snapshot) + ".layers").rmdir()
    Path(str(snapshot) + ".lineage.json").unlink()
    snapshot.unlink()
    lifecycle.close()


def test_checkpoint_reconciles_master_metadata_failure_after_completed_pause(tmp_path) -> None:
    class MasterMetadataFailure(RuntimeError):
        status_code = 130595

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    pause = client.pause_sandbox

    def fail_master_metadata_after_pause(sandbox, **kwargs):
        pause(sandbox, **kwargs)
        raise MasterMetadataFailure(
            "pause ok on cubelet but master meta failed: invalid connection"
        )

    client.pause_sandbox = fail_master_metadata_after_pause
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)

    assert lifecycle.state.value == "swapped"
    assert pool.reserved_bytes == 0
    assert pool.committed_bytes > 0
    timing = lifecycle.timings[-1]
    assert timing["snapshot_metrics"]["control_response_reconciled"] is True
    lifecycle.restore()
    snapshot = Path(timing["snapshot_path"])
    (Path(str(snapshot) + ".layers") / "000000.mem").unlink()
    Path(str(snapshot) + ".layers").rmdir()
    Path(str(snapshot) + ".lineage.json").unlink()
    snapshot.unlink()
    lifecycle.close()


@pytest.mark.parametrize("remote_state", ["running", None])
def test_checkpoint_does_not_reconcile_master_metadata_failure_without_paused_proof(
    tmp_path, remote_state,
) -> None:
    class MasterMetadataFailure(RuntimeError):
        status_code = 130595

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    client.get_sandbox_state = lambda _sandbox: remote_state

    def fail_master_metadata(_sandbox, **_kwargs):
        raise MasterMetadataFailure("master metadata failed")

    client.pause_sandbox = fail_master_metadata
    with pytest.raises(MasterMetadataFailure, match="metadata failed"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.state.value == (
        "running" if remote_state == "running" else "unknown"
    )
    assert pool.reserved_bytes == 8192


def test_checkpoint_does_not_reconcile_master_metadata_failure_without_snapshot_metadata(
    tmp_path,
) -> None:
    class MasterMetadataFailure(RuntimeError):
        status_code = 130595

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()

    def fail_without_snapshot(sandbox, **_kwargs):
        sandbox.state = "paused"
        raise MasterMetadataFailure("master metadata failed")

    client.pause_sandbox = fail_without_snapshot
    with pytest.raises(MasterMetadataFailure, match="metadata failed"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.state.value == "unknown"
    assert pool.reserved_bytes == 8192


def test_checkpoint_retries_begin_invalid_connection_once_when_still_running(tmp_path) -> None:
    class BeginFailure(RuntimeError):
        status_code = 130400

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=WarmSnapshotPool(65536), snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    pause = client.pause_sandbox
    calls = []

    def fail_once_then_pause(sandbox, **kwargs):
        calls.append((kwargs["generation"], kwargs["memory_snapshot_path"]))
        if len(calls) == 1:
            raise BeginFailure("begin pause snapshot: invalid connection")
        return pause(sandbox, **kwargs)

    client.pause_sandbox = fail_once_then_pause
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)

    assert lifecycle.state.value == "swapped"
    assert len(calls) == 2 and calls[0] == calls[1]
    timing = lifecycle.timings[-1]
    assert timing["stage_seconds"]["pause_begin_retry_count"] == 1.0
    lifecycle.restore()
    snapshot = Path(timing["snapshot_path"])
    (Path(str(snapshot) + ".layers") / "000000.mem").unlink()
    Path(str(snapshot) + ".layers").rmdir()
    Path(str(snapshot) + ".lineage.json").unlink()
    snapshot.unlink()
    lifecycle.close()


def test_checkpoint_does_not_retry_begin_failure_when_remote_state_is_not_running(tmp_path) -> None:
    class BeginFailure(RuntimeError):
        status_code = 130400

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=WarmSnapshotPool(65536), snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    calls = []
    client.get_sandbox_state = lambda _sandbox: "paused"

    def fail_begin(_sandbox, **_kwargs):
        calls.append(1)
        raise BeginFailure("begin pause snapshot: invalid connection")

    client.pause_sandbox = fail_begin
    with pytest.raises(BeginFailure):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert calls == [1]
    assert lifecycle.state.value == "unknown"


def test_checkpoint_releases_generation_after_repeated_begin_failure(tmp_path) -> None:
    class BeginFailure(RuntimeError):
        status_code = 130400

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    calls = []

    def always_fail(_sandbox, **kwargs):
        calls.append(kwargs["generation"])
        raise BeginFailure("begin pause snapshot: invalid connection")

    client.pause_sandbox = always_fail
    with pytest.raises(BeginFailure):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert calls == [1, 1]
    assert lifecycle.state.value == "running"
    assert pool.reserved_bytes == 0


def test_checkpoint_does_not_reconcile_server_timeout_as_completed_pause(tmp_path) -> None:
    class ServerTimeout(RuntimeError):
        status_code = 408

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(65536)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    pause = client.pause_sandbox

    def server_timeout_after_writing_snapshot(sandbox, **kwargs):
        pause(sandbox, **kwargs)
        raise ServerTimeout("HTTP 408")

    client.pause_sandbox = server_timeout_after_writing_snapshot
    with pytest.raises(ServerTimeout, match="HTTP 408"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.state.value == "unknown"
    assert pool.reserved_bytes == 8192
    with pytest.raises(Exception, match="snapshot files remain"):
        lifecycle.close()


def test_checkpoint_slot_timeout_aborts_unstarted_warm_reservation(tmp_path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(8192)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=4096,
    )
    lifecycle.start()

    @contextmanager
    def reject_transfer(_operation):
        raise TimeoutError("snapshot slot timed out")
        yield

    pool.transfer = reject_transfer
    with pytest.raises(TimeoutError, match="snapshot slot timed out"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.state.value == "running"
    assert pool.reserved_bytes == pool.committed_bytes == 0
    assert lifecycle.sandbox.state == "running"
    lifecycle.close()


def test_restore_failure_after_remote_resume_does_not_mark_vm_swapped(tmp_path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        cold_snapshot_root=str(tmp_path / "cold"),
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict()

    def fail_after_resume(sandbox_id, **kwargs):
        _Sandbox.items[sandbox_id].state = "running"
        raise RuntimeError("response lost after resume")

    client.connect_sandbox = fail_after_resume
    with pytest.raises(RuntimeError, match="response lost"):
        lifecycle.restore()
    assert lifecycle.state.value == "unknown"
    assert not lifecycle.resident
    lifecycle.close()
    assert not _Sandbox.items


def test_connect_republishes_assigned_capability_only_while_remote_is_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AssignedCapabilityError(RuntimeError):
        status_code = 130562

    class InterruptedResumeSandbox(_Sandbox):
        connect_calls = 0

        def get_info(self):
            return {"state": self.state}

        @classmethod
        def list_v2(cls, **kwargs):
            pytest.fail("resume reconciliation must query the exact sandbox identity")

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            cls.connect_calls += 1
            if cls.connect_calls == 1:
                raise RequestsConnectionError("first resume response lost")
            if cls.connect_calls == 2:
                raise AssignedCapabilityError("placement capability is already assigned")
            return super().connect(sandbox_id, **kwargs)

    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", lambda _seconds: None)
    paused = InterruptedResumeSandbox({
        "sandboxID": "sb-paused", "state": "paused",
    })
    InterruptedResumeSandbox.items = {"sb-paused": paused}
    refreshed = []
    client = CubeSandboxClient(sandbox_class=InterruptedResumeSandbox)
    client._handles["sb-paused"] = paused

    sandbox = client.connect_sandbox(
        "sb-paused", refresh_placement=lambda: refreshed.append(True),
    )

    assert sandbox.state == "running"
    assert InterruptedResumeSandbox.connect_calls == 3
    assert refreshed == [True]


def test_connect_never_republishes_capability_for_running_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AssignedCapabilityError(RuntimeError):
        status_code = 130562

    class ReconciledResumeSandbox(_Sandbox):
        connect_calls = 0

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            cls.connect_calls += 1
            item = cls.items[sandbox_id]
            if cls.connect_calls == 1:
                item.state = "running"
                raise AssignedCapabilityError("placement capability is already assigned")
            return item

    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", lambda _seconds: None)
    ReconciledResumeSandbox.items = {
        "sb-running": ReconciledResumeSandbox({
            "sandboxID": "sb-running", "state": "paused",
        }),
    }
    refreshed = []
    client = CubeSandboxClient(sandbox_class=ReconciledResumeSandbox)

    sandbox = client.connect_sandbox(
        "sb-running", refresh_placement=lambda: refreshed.append(True),
    )

    assert sandbox.state == "running"
    assert ReconciledResumeSandbox.connect_calls == 2
    assert refreshed == []


def test_failed_warm_manifest_retains_inflight_capacity_until_destroy():
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(2048)
    lifecycle = CubeSandboxLifecycle(client, template='tpl', node_name='node-a',
        ownership=_owner(), warm_snapshot_root='/warm', cold_snapshot_root='/cold',
        snapshot_pool=pool, snapshot_reservation_bytes=1024)
    lifecycle.start()
    def lost_manifest(sandbox, **kwargs):
        sandbox.state = 'paused'
        raise RuntimeError('manifest lost after writing')
    client.pause_sandbox = lost_manifest
    with pytest.raises(RuntimeError, match='manifest lost'):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert pool.reserved_bytes == 1024
    assert lifecycle.state.value == 'unknown'
    lifecycle.close()
    assert pool.reserved_bytes == pool.committed_bytes == 0


def test_failed_warm_cleanup_keeps_capacity_until_files_are_removed(tmp_path):
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(2048)
    lifecycle = CubeSandboxLifecycle(client, template='tpl', node_name='node-a',
        ownership=_owner(), warm_snapshot_root=str(tmp_path), cold_snapshot_root='/cold',
        snapshot_pool=pool, snapshot_reservation_bytes=1024)
    lifecycle.start()
    written = []
    def lost_manifest(sandbox, **kwargs):
        path = Path(kwargs['memory_snapshot_path'])
        path.parent.mkdir(parents=True)
        path.write_bytes(b'owned snapshot')
        written.append(path)
        sandbox.state = 'paused'
        raise RuntimeError('manifest lost after writing')
    client.pause_sandbox = lost_manifest
    with pytest.raises(RuntimeError, match='manifest lost'):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    with pytest.raises(Exception, match='snapshot files remain'):
        lifecycle.close()
    assert pool.reserved_bytes == 1024
    assert not _Sandbox.items
    written[0].unlink()
    lifecycle.close()
    assert pool.reserved_bytes == pool.committed_bytes == 0


def test_crash_rollback_is_repeatable_and_supports_later_cycles(tmp_path: Path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1 << 20)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=8192,
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id

    def lose_manifest_after_pause(sandbox, **kwargs):
        raise RuntimeError("publisher crashed")

    client.pause_sandbox = lose_manifest_after_pause
    with pytest.raises(RuntimeError, match="publisher crashed"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert pool.reserved_bytes == 8192

    rollback_calls = []

    def crash_rollback(current_id, **kwargs):
        rollback_calls.append(current_id)
        item = _Sandbox.items[current_id]
        item.state = "running"
        return item

    client.crash_rollback_sandbox = crash_rollback
    del client.pause_sandbox

    lifecycle.crash_rollback()
    lifecycle.crash_rollback()
    assert rollback_calls == [sandbox_id, sandbox_id]
    assert pool.reserved_bytes == pool.committed_bytes == 0
    assert lifecycle.state.value == "running"
    assert sandbox_id in _Sandbox.items

    for _ in range(2):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
        lifecycle.restore()
    assert sandbox_id in _Sandbox.items
    lifecycle.close()


def test_crash_rollback_client_retries_sdk_and_prepares_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []
    prepared = []

    class CrashRollbackSandbox:
        @classmethod
        def crash_rollback(cls, sandbox_id, **kwargs):
            attempts.append((sandbox_id, kwargs))
            if len(attempts) == 1:
                raise RequestsConnectionError("response lost")
            return SimpleNamespace(sandbox_id=sandbox_id)

    monkeypatch.setattr("clawbox.cube.api_retry.time.sleep", lambda _seconds: None)
    client = CubeSandboxClient(
        sandbox_class=CrashRollbackSandbox, request_timeout_s=5,
    )

    recovered = client.crash_rollback_sandbox(
        "sandbox-1", before_retry=lambda: prepared.append(True),
    )

    assert recovered.sandbox_id == "sandbox-1"
    assert len(attempts) == 2
    assert prepared == [True]
    assert client._handles["sandbox-1"] is recovered


def test_crash_rollback_retry_deadline_does_not_start_an_expired_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    attempts = []
    prepared = []

    class FailedCrashRollbackSandbox:
        @classmethod
        def crash_rollback(cls, sandbox_id, **kwargs):
            attempts.append((sandbox_id, kwargs))
            raise RequestsConnectionError("Cube API unavailable")

    monkeypatch.setattr(
        "clawbox.cube.api_retry.time.monotonic", lambda: now[0],
    )
    monkeypatch.setattr(
        "clawbox.cube.api_retry.time.sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    client = CubeSandboxClient(
        sandbox_class=FailedCrashRollbackSandbox, request_timeout_s=.1,
    )

    with pytest.raises(RuntimeError, match="crash rollback sandbox sandbox-1"):
        client.crash_rollback_sandbox(
            "sandbox-1", before_retry=lambda: prepared.append(True),
        )

    assert len(attempts) == 1
    assert prepared == []


def test_crash_rollback_preserves_retained_warm_generation_until_close(
    tmp_path: Path,
) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(4096)
    prepared = []
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", lazy_restore=True,
        prepare_cgroup=lambda: prepared.append(True),
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    lifecycle.restore()
    retained_key = lifecycle._snapshot_key
    assert retained_key is not None
    assert pool.manifest(retained_key).retained_by_vm

    def fail_successor(_sandbox, **_kwargs):
        raise RuntimeError("publisher crashed")

    client.pause_sandbox = fail_successor
    with pytest.raises(RuntimeError, match="publisher crashed"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    client.crash_rollback_sandbox = lambda sandbox_id, **_kwargs: _Sandbox.connect(sandbox_id)

    lifecycle.crash_rollback()

    assert prepared[-1] is True
    assert lifecycle._snapshot_key == retained_key
    assert lifecycle._snapshot_tier is SnapshotTier.WARM
    assert pool.committed_bytes == 1024
    assert pool.reserved_bytes == 0
    lifecycle.close()
    assert pool.committed_bytes == pool.reserved_bytes == 0


@pytest.mark.parametrize("capacity", [0, 512])
def test_disabled_or_undersized_warm_pool_checkpoints_to_ssd(capacity) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.tier is SnapshotTier.COLD
    assert pool.committed_bytes == pool.reserved_bytes == 0
    lifecycle.restore()
    assert lifecycle.resident
    lifecycle.close()


@pytest.mark.parametrize("capacity, expected_tier", [(1024, SnapshotTier.COLD), (2048, SnapshotTier.WARM)])
def test_lazy_restore_retains_warm_until_next_commit_or_close(capacity, expected_tier):
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024, lazy_restore=True,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    lifecycle.restore()
    assert lifecycle.tier is SnapshotTier.LOCAL
    assert pool.committed_bytes == 1024
    assert pool.snapshot()["manifests"][0]["retained_by_vm"]
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.tier is expected_tier
    assert pool.committed_bytes == (1024 if expected_tier is SnapshotTier.WARM else 0)
    lifecycle.restore()
    lifecycle.close()
    assert pool.committed_bytes == pool.reserved_bytes == 0


def test_compressed_restore_charges_expanded_raw_for_runtime(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    pool = WarmSnapshotPool(1200)
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox),
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", lazy_restore=True,
        snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
        storage_observation=lambda: {"unique_allocated_bytes": 1024},
    )

    def compress(_path, *, binary, native_snapshot_metrics=None):
        assert binary == "/bin/true"
        return {
            "baseline_allocated_bytes": 900,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 1024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        }

    def decompress(_path, *, binary):
        assert binary == "/bin/true"
        assert pool.expansion_reserved_bytes == 900
        return {"wall_seconds": 0.1}

    monkeypatch.setattr(compression_module, "compress_snapshot", compress)
    monkeypatch.setattr(compression_module, "decompress_snapshot", decompress)
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 1024},
    )

    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    manifest = pool.manifest(lifecycle._snapshot_key)
    assert manifest.allocated_bytes == 144
    assert manifest.compressed_allocated_bytes == 144
    assert manifest.raw_allocated_bytes == 1024
    assert manifest.expansion_allocated_bytes == 900
    assert not manifest.expanded

    lifecycle.restore()

    manifest = pool.manifest(lifecycle._snapshot_key)
    assert manifest.expanded and manifest.retained_by_vm
    assert manifest.allocated_bytes == manifest.raw_allocated_bytes == 1024
    assert manifest.compressed_allocated_bytes == 0
    assert pool.expansion_reserved_bytes == 0
    assert pool.accounted_bytes == 1024
    observations = lifecycle.timings[-1]["storage_observations"]
    assert observations["after_expansion_reserve"]["unique_allocated_bytes"] == 1024
    assert observations["after_raw_accounting"]["unique_allocated_bytes"] == 1024
    assert observations["after_runtime_retain"]["unique_allocated_bytes"] == 1024
    lifecycle.close()
    assert pool.accounted_bytes == 0


def test_compressed_restore_capacity_failure_precedes_decompression(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    pool = WarmSnapshotPool(1100)
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client,
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
    )
    calls = []
    connect_calls = 0
    original_connect = client.connect_sandbox

    def count_connect(*args, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        return original_connect(*args, **kwargs)

    client.connect_sandbox = count_connect
    client.get_sandbox_state = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("state query unavailable")
    )

    monkeypatch.setattr(
        compression_module, "compress_snapshot",
        lambda _path, **_kwargs: {
            "baseline_allocated_bytes": 5000,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 5024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        },
    )

    def decompress(*args, **kwargs):
        calls.append((args, kwargs))
        return {"wall_seconds": 0.1}

    monkeypatch.setattr(compression_module, "decompress_snapshot", decompress)
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 5024},
    )

    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    with pytest.raises((WarmSnapshotTooLarge, WarmCapacityError)):
        lifecycle.restore()
    assert calls == []
    assert connect_calls == 0
    assert lifecycle.state.value == "swapped"
    assert pool.expansion_reserved_bytes == 0
    assert pool.manifest(lifecycle._snapshot_key).compressed_allocated_bytes == 44
    lifecycle.close()


def test_compressed_restore_retry_reuses_expansion_reservation(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    pool = WarmSnapshotPool(1200)
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox),
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
    )
    monkeypatch.setattr(
        compression_module, "compress_snapshot",
        lambda _path, **_kwargs: {
            "baseline_allocated_bytes": 900,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 1024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        },
    )
    calls = 0

    def decompress(_path, *, binary):
        nonlocal calls
        calls += 1
        assert pool.expansion_reserved_bytes == 900
        if calls == 1:
            raise RuntimeError("partial expansion")
        return {"wall_seconds": 0.1}

    monkeypatch.setattr(compression_module, "decompress_snapshot", decompress)
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 1024},
    )

    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    with pytest.raises(RuntimeError, match="partial expansion"):
        lifecycle.restore()
    assert pool.expansion_reserved_bytes == 900
    assert lifecycle.state.value == "swapped"

    lifecycle.restore()
    assert calls == 2
    assert pool.expansion_reserved_bytes == 0
    assert pool.manifest(lifecycle._snapshot_key).allocated_bytes == 1024
    assert pool.manifest(lifecycle._snapshot_key).expansion_allocated_bytes == 0
    lifecycle.close()


def test_compressed_restore_failure_after_expansion_keeps_raw_charged(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1200)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
    )
    monkeypatch.setattr(
        compression_module, "compress_snapshot",
        lambda _path, **_kwargs: {
            "baseline_allocated_bytes": 900,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 1024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        },
    )
    monkeypatch.setattr(
        compression_module, "decompress_snapshot",
        lambda _path, **_kwargs: {"wall_seconds": 0.1},
    )
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 1024},
    )

    original_connect = client.connect_sandbox
    connect_calls = 0

    def fail_once_after_expansion(sandbox_id, **kwargs):
        nonlocal connect_calls
        connect_calls += 1
        _Sandbox.items[sandbox_id].state = "paused"
        if connect_calls == 1:
            raise RuntimeError("response lost after expansion")
        return original_connect(sandbox_id, **kwargs)

    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    client.connect_sandbox = fail_once_after_expansion
    with pytest.raises(RuntimeError, match="response lost"):
        lifecycle.restore()

    manifest = pool.manifest(lifecycle._snapshot_key)
    assert manifest.expanded
    assert manifest.allocated_bytes == 1024
    assert manifest.expansion_allocated_bytes == 0
    assert not manifest.retained_by_vm
    assert pool.expansion_reserved_bytes == 0
    assert pool.accounted_bytes == 1024
    assert lifecycle.state.value == "swapped"

    lifecycle.restore()
    assert connect_calls == 2
    assert pool.manifest(lifecycle._snapshot_key).retained_by_vm
    lifecycle.close()
    assert pool.accounted_bytes == 0


def test_compressed_spill_reserves_raw_then_retires_warm_manifest(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    pool = WarmSnapshotPool(1200)
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox),
        template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
    )

    def compression(_path, **kwargs):
        return {
            "baseline_allocated_bytes": 900,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 1024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        }

    def decompress(_path, *, binary):
        assert pool.expansion_reserved_bytes == 900
        return {"wall_seconds": 0.1}

    monkeypatch.setattr(compression_module, "compress_snapshot", compression)
    monkeypatch.setattr(compression_module, "decompress_snapshot", decompress)
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 1024},
    )

    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    spilled = pool.spill_for_admission(pool.capacity_bytes)

    assert [item.key for item in spilled] == [lifecycle._snapshot_key] or (
        len(spilled) == 1 and spilled[0].generation == 1
    )
    assert lifecycle.tier is SnapshotTier.COLD
    assert pool.committed_bytes == 0
    assert pool.expansion_reserved_bytes == 0
    event = next(item for item in lifecycle.timings if item["operation"] == "spill")
    assert event["snapshot_metrics"]["compression"]["baseline_allocated_bytes"] == 900
    lifecycle.close()


def test_compressed_spill_retry_reuses_already_expanded_raw(
    tmp_path, monkeypatch,
) -> None:
    import clawbox.cube.snapshot as snapshot_module
    import clawbox.cube.snapshot_compression as compression_module

    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1200)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy", snapshot_compression="zstd-page",
        snapshot_compression_binary="/bin/true",
    )
    monkeypatch.setattr(
        compression_module, "compress_snapshot",
        lambda _path, **_kwargs: {
            "baseline_allocated_bytes": 900,
            "compressed_allocated_bytes": 20,
            "raw_native_allocated_bytes": 1024,
            "compressed_container_allocated_bytes": 16,
            "compression_manifest_allocated_bytes": 4,
        },
    )
    decompress_calls = 0

    def decompress(_path, *, binary):
        nonlocal decompress_calls
        decompress_calls += 1
        assert pool.expansion_reserved_bytes == 900
        return {"wall_seconds": 0.1}

    monkeypatch.setattr(compression_module, "decompress_snapshot", decompress)
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_args, **_kwargs: {"allocated_bytes": 1024},
    )
    original_relocate = client.relocate_snapshot
    relocate_calls = 0

    def fail_relocate_once(*args, **kwargs):
        nonlocal relocate_calls
        relocate_calls += 1
        if relocate_calls == 1:
            raise RuntimeError("relocation response lost")
        return original_relocate(*args, **kwargs)

    client.relocate_snapshot = fail_relocate_once
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    with pytest.raises(RuntimeError, match="relocation response lost"):
        pool.spill_for_admission(pool.capacity_bytes)

    manifest = pool.manifest(lifecycle._snapshot_key)
    assert manifest.expanded
    assert manifest.allocated_bytes == 1024
    assert manifest.expansion_allocated_bytes == 0
    assert pool.expansion_reserved_bytes == 0
    assert lifecycle.state.value == "swapped"

    spilled = pool.spill_for_admission(pool.capacity_bytes)
    assert len(spilled) == 1
    assert decompress_calls == 1
    assert relocate_calls == 2
    assert lifecycle.tier is SnapshotTier.COLD
    assert pool.committed_bytes == 0
    lifecycle.close()


def test_warm_overflow_spills_authoritative_lru_to_cold() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1024, clock=lambda: 1.0)
    owners = [
        Ownership("run", "attempt", "task", "experiment", f"session-{index}", "policy")
        for index in (1, 2)
    ]
    lifecycles = [
        CubeSandboxLifecycle(
            client, template="tpl", node_name="node-a", ownership=owner,
            warm_snapshot_root="/warm", cold_snapshot_root="/cold",
            snapshot_pool=pool, snapshot_reservation_bytes=1024,
            snapshot_mechanism="full-copy",
        )
        for owner in owners
    ]
    for lifecycle in lifecycles:
        lifecycle.start()
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycles[0].tier is SnapshotTier.COLD
    assert lifecycles[1].tier is SnapshotTier.WARM
    assert pool.committed_bytes == 1024
    spill = next(item for item in lifecycles[0].timings if item["operation"] == "spill")
    assert spill["tier_from"] == "warm" and spill["tier_to"] == "cold"


@pytest.mark.parametrize("operation", ["restore", "close"])
def test_lifecycle_consumption_waits_for_selected_spill(operation: str) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1024)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    relocate = client.relocate_snapshot

    def delayed_relocate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return relocate(*args, **kwargs)

    client.relocate_snapshot = delayed_relocate

    def consume():
        getattr(lifecycle, operation)()
        finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        spilling = executor.submit(pool.spill_for_admission, 1024)
        assert entered.wait(5)
        consuming = executor.submit(consume)
        try:
            assert not finished.wait(0.1)
        finally:
            release.set()
        spilling.result(timeout=5)
        consuming.result(timeout=5)
    assert pool.committed_bytes == 0
    if operation == "restore":
        assert lifecycle.resident and lifecycle.tier is SnapshotTier.LOCAL
        lifecycle.close()
    assert not _Sandbox.items


@pytest.mark.parametrize("fail_runtime_create", [False, True])
def test_openclaw_snapshot_pauses_runtime_and_restores_it_before_model_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_runtime_create: bool,
) -> None:
    coordinators = []
    create_claims_at_cleanup = []
    real_coordinator = worker_module.PolicyCoordinator

    class ObservedCoordinator(real_coordinator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            coordinators.append(self)

        def blocked_demand_pressure(self):
            return True  # exercise supported wait-aware eviction under pressure

        def unregister(self, session_id):
            create_claims_at_cleanup.append((session_id, "create") in self.demand.claims)
            super().unregister(session_id)

    monkeypatch.setattr(worker_module, "PolicyCoordinator", ObservedCoordinator)
    monkeypatch.setattr(worker_module, "warm_runtime_ssh_route", lambda *_args, **_kwargs: 1)

    class SnapshotCommands(_Commands):
        def run(self, command: str, **kwargs):
            self.owner.command_calls.append((command, kwargs))
            if "kill -0 $pid" in command:
                return SimpleNamespace(exit_code=0, stdout="4242", stderr="")
            if 'printf %s "$TASK_ID"' in command:
                match = re.search(r"/state/openclaw/([^/]+)/ssh/id_ed25519", command)
                assert match is not None
                return SimpleNamespace(
                    exit_code=0, stdout=match.group(1), stderr="",
                )
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class SnapshotSandbox(_Sandbox):
        sequence = 0
        created: list["SnapshotSandbox"] = []

        def __init__(self, data=None, config=None) -> None:
            super().__init__(data, config)
            self.pause_calls = 0
            self.resume_calls = 0
            self.endpoint_calls = 0
            self.commands = SnapshotCommands(self)

        def get_tcp_endpoint(self, container_port):
            self.endpoint_calls += 1
            return super().get_tcp_endpoint(container_port)

        @classmethod
        def create(cls, **kwargs):
            if fail_runtime_create and kwargs["template"] == "runtime-tpl":
                raise RuntimeError("injected Runtime creation failure")
            cls.sequence += 1
            item = cls({
                "sandboxID": f"snapshot-{cls.sequence}",
                "templateID": kwargs["template"],
                "metadata": kwargs["metadata"],
            })
            cls.items[item.sandbox_id] = item
            cls.created.append(item)
            item.create_kwargs = kwargs
            return item

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            item = cls.items[sandbox_id]
            item.state = "running"
            item.resume_calls += 1
            return item

        def pause(self, wait=True, **kwargs):
            self.pause_calls += 1
            return super().pause(wait=wait, **kwargs)

        def kill(self):
            if agent_finished[0]:
                assert all(not controller.vms for controller in pressure_instances)
            super().kill()

    SnapshotSandbox.items = {}
    SnapshotSandbox.sequence = 0
    SnapshotSandbox.created = []
    agent_finished = [False]
    pressure_instances = []
    real_pressure_control = worker_module.PressureControl

    class ObservedPressureControl(real_pressure_control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            pressure_instances.append(self)

    monkeypatch.setattr(worker_module, "PressureControl", ObservedPressureControl)
    # This simulated lifecycle has no host microVM processes. Keep the real
    # sampler, but isolate its procfs discovery from the test host OS.
    from clawbox.experiments.memory import SandboxRSSSampler
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(worker_module, "SandboxRSSSampler",
                        lambda sandbox_id, **kw: SandboxRSSSampler(sandbox_id, proc_root=proc_root, **kw))
    # Emulate only cgroup I/O; exercise the real demand ledger and independent
    # OFF runtime controller through the complete worker lifecycle.
    cg = tmp_path/'cg'
    cg.mkdir()
    def files(path):
        path.mkdir(parents=True, exist_ok=True)
        for key, value in {'memory.max': str(100000*1024**2), 'memory.current': '0',
                           'cgroup.procs': '',
                           'memory.stat': 'anon 0\nfile 0\npgscan 0\npgsteal 0\n',
                           'memory.low': '0', 'memory.high': 'max', 'cpu.weight': '100',
                           'memory.events': 'high 0\noom_kill 0\n'}.items():
            (path/key).write_text(value)
    files(cg)
    from clawbox.experiments.priority import PriorityDomain
    class FakePriorityDomain(PriorityDomain):
        def start(self):
            for branch in (self.high, self.low):
                files(branch)
            for i in self.priorities:
                for role in ('tool', 'runtime'):
                    branch = self.high if self.priorities[i] == 'high' else self.low
                    leaf = branch/f'session-{i}'/role
                    files(leaf.parent); files(leaf)
                    self.leaves[i, role] = leaf
        def prepare(self, *args): pass
        def verify(self, index, role, sandbox_id):
            return {'priority': self.priorities[index], 'cgroup': str(self.leaves[index, role])}
        def close(self): pass
    monkeypatch.setattr(worker_module, 'PriorityDomain', FakePriorityDomain)
    def unexpected_process_scan(*args):
        raise AssertionError('Tool admission rescanned /proc despite a verified VM leaf')
    monkeypatch.setattr(worker_module, 'locate_cgroup', unexpected_process_scan)
    trace = tmp_path / "openclaw-snapshot.jsonl"
    write_spans(trace, llm_spans(
        [{"role": "user", "content": "hello"}],
        {"content": "", "tool_calls": [{"id": "call-1", "type": "function",
         "function": {"name": "exec", "arguments": '{"command":"true"}'}}]},
        duration_ms=500,
    ))

    def post_policy(policy_control, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            policy_control.url + path,
            data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {policy_control.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            pytest.fail(f"policy {path} returned {exc.code}: {exc.read().decode()}")

    def created_vm(template_id: str):
        return next(
            item for item in SnapshotSandbox.created
            if item.template_id == template_id
        )

    def fake_run_openclaw(*, prompt, session_id, configuration, ssh,
                          policy_control, runtime_executor, output_dir,
                          timeout_seconds, model_gateway, prediction_manifest=None,
                          resident_poll=None, checkpoint_relay=False, diagnostic_artifacts=False,
                          resident_file_nonempty=None,
                          startup_begin=None, startup_wait=None, startup_event=None,
                          cancelled=None):
        # Pair creation must release its own claim before the Agent runs,
        # even when demand-backed Guest RAM uses less than configured capacity.
        # Otherwise the unused footprint remains pending until session teardown.
        assert coordinators
        assert all((session_id, "create") not in c.demand.claims for c in coordinators)
        assert resident_poll is not None
        assert callable(resident_file_nonempty)
        assert checkpoint_relay is True
        assert callable(startup_wait)
        assert callable(startup_begin)
        assert callable(startup_event)
        assert callable(cancelled)
        startup_begin()
        startup_wait()
        assert model_gateway.gateway.timeout_s == 120
        assert (
            created_vm("runtime-tpl").create_kwargs["env_vars"]
            ["CLAWBOX_MODEL_GATEWAY_TOKEN"]
            == model_gateway.token
        )
        payload = {
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, _content_type, _body, request_id = model_gateway.gateway.complete_http(payload)
        assert status == 200
        # Response preparation restores only Runtime. Tool remains
        # swapped until the following Tool admission reserves memory for it.
        assert created_vm("tool-tpl").resume_calls == 0
        model_gateway.mark_delivery(request_id, delivered=True)
        command = "true"
        command_sha256 = hashlib.sha256(command.encode()).hexdigest()
        admission = post_policy(policy_control, "/v1/tool/admit", {
            "session_id": session_id, "execution_id": "exec-1",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": time.time(),
        })
        assert admission["decision"] == "ADMIT"
        started = time.time()
        post_policy(policy_control, "/v1/tool/complete", {
            "session_id": session_id, "execution_id": "exec-1",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": started,
            "execution_started_at": started,
            "ssh_reaped_at": started + 0.01,
            "execution_completed_at": started + 0.01,
            "exit_code": 0,
            "endpoint_sandbox_id": admission["sandbox_id"],
            "endpoint_epoch": admission["epoch"],
            "endpoint_host": admission["host"],
            "endpoint_port": admission["port"],
        })
        endpoint_calls_after_first_admission = created_vm("tool-tpl").endpoint_calls
        identity_checks_after_first_admission = sum(
            'printf %s "$TASK_ID"' in command
            for command, _kwargs in created_vm("runtime-tpl").command_calls
        )
        admission2 = post_policy(policy_control, "/v1/tool/admit", {
            "session_id": session_id, "execution_id": "exec-2",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": time.time(),
        })
        assert admission2["sandbox_id"] == admission["sandbox_id"]
        assert admission2["epoch"] == admission["epoch"]
        assert created_vm("tool-tpl").endpoint_calls == endpoint_calls_after_first_admission
        assert sum(
            'printf %s "$TASK_ID"' in command
            for command, _kwargs in created_vm("runtime-tpl").command_calls
        ) == identity_checks_after_first_admission
        started2 = time.time()
        post_policy(policy_control, "/v1/tool/complete", {
            "session_id": session_id, "execution_id": "exec-2",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": started2,
            "execution_started_at": started2,
            "ssh_reaped_at": started2 + 0.01,
            "execution_completed_at": started2 + 0.01,
            "exit_code": 0,
            "endpoint_sandbox_id": admission2["sandbox_id"],
            "endpoint_epoch": admission2["epoch"],
            "endpoint_host": admission2["host"],
            "endpoint_port": admission2["port"],
        })
        agent_finished[0] = True
        return {
            "agent_pid_file": f"/state/openclaw/{session_id}/agent.pid",
            "tool_latencies": [0.01, 0.01], "runtime_traces": [],
            "final_response_observed_unix_s": time.time(),
            "agent_replay_invalid": True, "agent_tool_failures": 0,
            "policy_control_records": policy_control.records(),
        }

    monkeypatch.setattr(worker_module, "run_openclaw", fake_run_openclaw)
    monkeypatch.setattr(
        worker_module, "collect_and_validate_native_tool_artifacts",
        lambda **_kwargs: SimpleNamespace(
            root=tmp_path / "native", validation={"ok": True},
            cgroup_artifacts={execution_id: {
                "memory_rss_peak_bytes": 2 * 1024 * 1024,
                "ts_start": 1.0, "ts_end": 1.01,
                "cpu_utilization_avg_cores": 0.25,
                "sampling_quality": "valid",
            } for execution_id in ("exec-1", "exec-2")},
        ),
    )
    monkeypatch.setenv("CLAWBOX_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAWBOX_MODEL_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAWBOX_POLICY_PORT", "0")
    monkeypatch.setenv("CLAWBOX_MODEL_GATEWAY_PORT", "0")
    spec = ExperimentSpec.model_validate({
        "schema_version": 3, "experiment_id": "openclaw-snapshot-test",
        "workload": {"source": "recorded_trace", "input": str(trace), "cases": [{
            "case_id": "snapshot-case", "source": "recorded_trace",
            "source_reference": str(trace), "replay_trace_reference": str(trace),
            "prompt": "hello",
        }]},
        "agent": {"driver": "openclaw"},
        "inference": {"backend": "replay", "configuration": {
            "model": "recorded-model",
        }},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tool-tpl", "memory_mib": 4096},
        "execution": {"concurrency_levels": [1], "randomized_order": False,
                       "arm_timeout_seconds": 10, "command_timeout_seconds": 5,
                       "stabilization_seconds": 0},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 100000,
                       "local_memory_cgroup": str(cg),
                       "emergency_free_memory_mib": 1, "checkpoint_restore_headroom_mib": 1,
                       "static_tool_memory_mib": 1,
                       "small_tool_admission_bypass_mib": 0,
                           "snapshot_compression": "none",
                       "cold_snapshot_root": str(tmp_path / "cold")},
        "policies": [{"name": "snapshot", "admission": "fixed",
                      "runtime_control": "off",
                      "reclamation": "snapshot_pause", "eviction": "wait_aware_pressure",
                      "restore": "reactive"}],
    })
    result = ExperimentWorker(
        spec, run_id="openclaw-snapshot", attempt_id="attempt", task_uid="task",
        output_root=tmp_path / "results",
        client=CubeSandboxClient(sandbox_class=SnapshotSandbox),
    ).run()[0]

    assert create_claims_at_cleanup and not any(create_claims_at_cleanup), result.correctness
    if fail_runtime_create:
        assert result.status.value == "failed"
        assert len(SnapshotSandbox.created) == 1
        assert SnapshotSandbox.items == {}
        assert all(not c.demand.claims for c in coordinators)
        return
    assert result.status.value == "succeeded", result.model_dump()
    assert result.provenance["evidence_class"] == "deterministic-managed-replay"
    assert result.provenance["configured_traces"] == [{
        "case_id": "snapshot-case", "reference": str(trace),
    }]
    observation = result.performance["tool_execution_observations"][0]
    assert observation["actual_measured_memory_mib"] == 2.0
    assert observation["admitted_reservation_mib"] == 1
    assert observation["telemetry_validity"] == "valid"
    # Worker creates the Tool before the Runtime, even though the Runtime is
    # the long-lived OpenClaw process whose PID is witnessed.
    tool, runtime = SnapshotSandbox.created
    assert runtime.pause_calls >= 1
    assert runtime.resume_calls >= 1
    assert tool.pause_calls >= 1
    assert SnapshotSandbox.items == {}
    event_path = next((tmp_path / "results" / "events").glob("*.jsonl"))
    event_rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    pid_phases = {
        row["phase"] for row in event_rows
        if row.get("event") == "openclaw_agent_pid_observed"
    }
    assert "before_runtime_pause" in pid_phases
    assert {
        "after_runtime_restore_scheduled", "after_runtime_restore_response",
    } & pid_phases
    pause_roles = {
        row.get("role") for row in event_rows if row.get("event") == "sandbox_paused"
    }
    restore_roles = {
        row.get("role") for row in event_rows if row.get("event") == "sandbox_restored"
    }
    assert {"runtime", "tool"} <= pause_roles
    assert {"runtime", "tool"} <= restore_roles
    gateway_path = next((tmp_path / "results" / "model-gateway").glob("*.json"))
    gateway_record = json.loads(gateway_path.read_text())[0]
    admission = gateway_record["admission"]
    assert admission["runtime_snapshot_enabled"] is True
    assert admission["tool_snapshot_performed"] is True
    assert admission["runtime_snapshot_performed"] is True
    assert admission["runtime_pause_started_at"] <= admission["runtime_pause_completed_at"]
    assert admission["runtime_restore_started_at"] <= admission["runtime_restore_completed_at"]
    assert (
        admission["runtime_pause_completed_at"]
        <= admission["runtime_restore_started_at"]
        <= gateway_record["response_released_unix_s"]
    )
    assert admission["runtime_agent_pid_before_pause"] == 4242
    assert admission["runtime_agent_pid_after_restore"] == 4242
    session_spans = result.performance["session_timelines"][0]["time_spans"]
    span_names = {span["name"] for span in session_spans}
    assert {
        "model.wait", "model.response_hold", "model.response_delivery",
        "tool.operation", "sandbox.tool.create", "sandbox.tool.checkpoint",
        "sandbox.tool.restore", "sandbox.tool.destroy",
        "sandbox.runtime.create", "sandbox.runtime.checkpoint",
        "sandbox.runtime.restore", "sandbox.runtime.destroy",
    } <= span_names
    for span in session_spans:
        assert span["end_unix_s"] >= span["start_unix_s"]
        assert span["duration_seconds"] >= 0
        if "start_monotonic_s" in span:
            assert span["end_monotonic_s"] >= span["start_monotonic_s"]


def test_lifecycle_records_failed_create_with_state_and_error_type() -> None:
    class FailingClient:
        def create_sandbox(self, **_kwargs):
            raise TimeoutError("create timed out")

    lifecycle = CubeSandboxLifecycle(
        FailingClient(), template="tpl", node_name="node-a", ownership=_owner(),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        lifecycle.start()
    assert lifecycle.state.value == "new"
    assert lifecycle.timings[-1]["operation"] == "create"
    assert lifecycle.timings[-1]["status"] == "error"
    assert lifecycle.timings[-1]["error_type"] == "TimeoutError"
    assert lifecycle.timings[-1]["state_before"] == "new"
    assert lifecycle.timings[-1]["state_after"] == "new"
    assert lifecycle.timings[-1]["stage_seconds"]["api_create_s"] >= 0
    assert lifecycle.timings[-1]["stage_seconds"]["physical_observation_after_s"] >= 0


def test_lifecycle_create_uses_the_shared_cube_operation_budget() -> None:
    pool = WarmSnapshotPool(0, transfer_concurrency=2)
    observed = []

    class Client:
        def create_sandbox(self, **_kwargs):
            observed.append(pool.snapshot()["active_transfers"])
            return SimpleNamespace(sandbox_id="sandbox-1")

        @staticmethod
        def sandbox_id(sandbox):
            return sandbox.sandbox_id

    lifecycle = CubeSandboxLifecycle(
        Client(), template="tpl", node_name="node-a", ownership=_owner(),
        snapshot_pool=pool,
    )

    lifecycle.start()

    assert observed == [{"create": 1}]
    assert pool.snapshot()["active_transfers"] == {"create": 0}


def test_lifecycle_records_create_stage_breakdown() -> None:
    _Sandbox.items = {}
    prepared = []
    verified = []
    pinned = []

    class Borrower:
        def pin_local(self, sandbox_id, *, verified_cgroup=None):
            pinned.append((sandbox_id, verified_cgroup))

    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template="tpl",
        node_name="node-a", ownership=_owner(),
        physical_observation=lambda: {"host_used_bytes": 1},
        prepare_cgroup=lambda: prepared.append(True),
        verify_cgroup=lambda sandbox_id: (
            verified.append(sandbox_id) or {"cgroup": "/sandbox/cgroup"}
        ),
        numa_borrower=Borrower(),
    )

    lifecycle.start()

    assert prepared == [True]
    assert len(verified) == 1
    assert pinned == [(verified[0], "/sandbox/cgroup")]
    stages = lifecycle.timings[-1]["stage_seconds"]
    assert set(stages) == {
        "physical_observation_before_s", "prepare_cgroup_s", "api_create_s",
        "verify_cgroup_s", "pin_local_s", "physical_observation_after_s",
        "transfer_slot_wait_s",
    }
    assert all(value >= 0 for value in stages.values())


def test_lifecycle_records_host_physical_memory_reclamation() -> None:
    _Sandbox.items = {}
    samples = iter([
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 100},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 120},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 120},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 70},
    ])
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template="tpl",
        node_name="node-a", ownership=_owner(),
        physical_observation=lambda: next(samples),
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict()
    checkpoint = lifecycle.timings[-1]
    assert checkpoint["host_memory_before"]["host_used_bytes"] == 120
    assert checkpoint["host_memory_after"]["host_used_bytes"] == 70
    assert checkpoint["host_observed_reclaimed_bytes"] == 50
    assert checkpoint["host_observed_growth_bytes"] == 0
    assert checkpoint["host_observed_net_change_bytes"] == -50
    assert checkpoint["host_reclamation_evidence"] == "observed_whole_host_reclamation"
    assert checkpoint["lifecycle_mechanism"] == "cubesandbox_pause_snapshot_destroy"
    assert checkpoint["host_memory_effect"]["host_used_bytes"] == {
        "before_bytes": 120, "after_bytes": 70, "net_change_bytes": -50,
        "reclaimed_bytes": 50, "growth_bytes": 0,
    }


def test_observed_command_preserves_execution_id_and_reads_cgroup_artifact() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
    )
    lifecycle.start()
    sandbox = lifecycle.sandbox
    execution_id = "call:123"
    cgroup = {
        "schema": "cgroup_resource_v1", "execution_id": execution_id,
        "source": "cgroup-v2", "memory_rss_peak_bytes": 4096,
    }
    sandbox.files.content[
        "/var/lib/clawtune/artifacts/tool-resource/cgroup-resource-call_123.json"
    ] = json.dumps(cgroup)
    pmu = {"schema": "pmu_profile_v1", "execution_id": execution_id}
    pmu_path = "/var/lib/clawtune/artifacts/tool-resource/pmu-profile-call_123.json"
    sandbox.files.content[pmu_path] = json.dumps(pmu)

    def run(command: str, **_kwargs):
        encoded = command.rsplit(" ", 1)[-1]
        envelope = base64.b64decode(encoded).decode()
        assert envelope.endswith("\nprintf observed")
        record = {
            "execution_id": execution_id, "exit_code": 0,
            "telemetry_state": "complete", "telemetry_artifact": "",
            "pmu_state": "reliable", "pmu_artifact": pmu_path,
        }
        return SimpleNamespace(
            exit_code=0, stdout="observed", stderr=(
                "diagnostic\nCLAWBOX_TELEMETRY_RECORD=" + json.dumps(record) + "\n"
            ),
        )

    sandbox.commands = SimpleNamespace(run=run)
    observed = CubeCommandExecutor(
        client, lambda: lifecycle.sandbox,
    ).execute_observed("printf observed", 5, execution_id=execution_id)
    assert observed.result.stdout == "observed"
    assert observed.result.stderr == "diagnostic"
    assert observed.bridge_record["execution_id"] == execution_id
    assert json.loads(observed.artifacts["cgroup_resource_v1"])[
        "memory_rss_peak_bytes"
    ] == 4096
    assert json.loads(observed.artifacts["pmu_profile_v1"])["execution_id"] == execution_id
    assert observed.telemetry_unavailable_reason is None
    lifecycle.close()


def test_cleanup_uses_journal_and_metadata_fallback(tmp_path: Path) -> None:
    _Sandbox.items = {}
    journal = OwnedSandboxJournal(tmp_path / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal, sandbox_class=_Sandbox)
    first = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    second = _Sandbox.create(template="tpl", metadata=_owner().metadata())
    client._handles.clear()
    client.kill_owned_sandboxes("task")
    assert first.sandbox_id not in _Sandbox.items
    assert second.sandbox_id not in _Sandbox.items


def test_cleanup_retries_transient_kill_and_removes_every_owned_sandbox() -> None:
    class PartiallyFailingSandbox(_Sandbox):
        failed = False
        constructed = 0
        closed = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            type(self).constructed += 1

        def close(self):
            type(self).closed += 1

        def kill(self):
            if not type(self).failed:
                type(self).failed = True
                raise RuntimeError("transient kill failure")
            return super().kill()

    PartiallyFailingSandbox.items = {}
    client = CubeSandboxClient(sandbox_class=PartiallyFailingSandbox)
    first = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    second = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    client.kill_owned_sandboxes("task")
    assert second.sandbox_id not in PartiallyFailingSandbox.items
    assert first.sandbox_id not in PartiallyFailingSandbox.items
    assert PartiallyFailingSandbox.items == {}
    assert PartiallyFailingSandbox.constructed == 3  # two creates and one retry
    assert PartiallyFailingSandbox.closed == 3


@pytest.mark.parametrize("bulk_cleanup", [False, True])
def test_cleanup_bounds_parallel_destroy_requests(bulk_cleanup) -> None:
    class SlowSandbox(_Sandbox):
        active = 0
        peak = 0
        lock = threading.Lock()

        def kill(self):
            with type(self).lock:
                type(self).active += 1
                type(self).peak = max(type(self).peak, type(self).active)
            try:
                time.sleep(0.02)
                return super().kill()
            finally:
                with type(self).lock:
                    type(self).active -= 1

    SlowSandbox.items = {}
    client = CubeSandboxClient(sandbox_class=SlowSandbox, cleanup_workers=2)
    sandboxes = [client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
                 for _ in range(6)]

    if bulk_cleanup:
        client.kill_owned_sandboxes("task")
    else:
        # Normal session close passes an ID; direct callers may pass handles.
        # Both share one client budget even without the bulk cleanup pool.
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(client.kill_sandbox, [
                item.sandbox_id if index % 2 else item
                for index, item in enumerate(sandboxes)
            ]))

    assert SlowSandbox.items == {}
    assert SlowSandbox.peak == 2


def test_failed_delete_releases_shared_slot_and_propagates_failure() -> None:
    client = CubeSandboxClient(sandbox_class=_Sandbox, cleanup_workers=1)
    closed = []

    def fail():
        raise RuntimeError("injected kill failure")

    broken = SimpleNamespace(sandbox_id="broken", kill=fail, close=lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="injected kill failure"):
        client.kill_sandbox(broken)
    assert closed == [True]
    finished = threading.Event()
    healthy = SimpleNamespace(sandbox_id="healthy", kill=finished.set)
    thread = threading.Thread(target=client.kill_sandbox, args=(healthy,), daemon=True)
    thread.start()
    assert finished.wait(1), "failed delete must return its shared slot"
    thread.join(1)


def test_cleanup_rejects_non_positive_worker_count() -> None:
    with pytest.raises(ValueError, match="cleanup_workers must be positive"):
        CubeSandboxClient(sandbox_class=_Sandbox, cleanup_workers=0)


@pytest.mark.parametrize("capacity,borrowed,expected", [(1536, 0, False), (2048, 1024, False), (2048, 0, True)])
def test_idle_pair_reserves_both_snapshots_before_either_pause(tmp_path, capacity, borrowed, expected):
    from clawbox.experiments.policy import PolicyCoordinator
    from clawbox.experiments.spec import PolicySpec
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity, borrow_capacity_bytes=capacity)
    if borrowed:
        pool.reserve_borrow("borrower", borrowed)
    vms = [CubeSandboxLifecycle(client, template="tpl", node_name="node-a", ownership=_owner(),
            role=role, snapshot_mechanism="full-copy", snapshot_storage="warm-only",
            warm_snapshot_root=str(tmp_path), snapshot_pool=pool, snapshot_reservation_bytes=1024)
           for role in ("tool", "runtime")]
    for vm in vms:
        vm.start()
    c = PolicyCoordinator(PolicySpec(name="wait", admission="off", reclamation="snapshot_pause",
        eviction="wait_aware_pressure", restore="reactive"), budget_mib=32,
        emergency_free_mib=0, operation_headroom_mib=0)
    c.register("session", vms[0]); c.register_runtime("session", vms[1])
    c.begin_model_wait("session", "request")
    try:
        assert c.evict_one_for_watermark() is expected
        assert all(vm.resident != expected for vm in vms)
        assert pool.reserved_bytes == 0
        assert c.pause_count == (2 if expected else 0)
        if not expected:
            assert c.checkpoint_capacity_skips == 1
            assert c.watermark_reclaim_candidate() is not None
        if borrowed:
            pool.release_borrow("borrower")
            assert c.evict_one_for_watermark()
            assert all(not vm.resident for vm in vms)
    finally:
        for vm in vms:
            vm.close()
    assert pool.reserved_bytes == pool.committed_bytes == 0


def test_reserved_checkpoint_failure_keeps_unknown_snapshot_charge(tmp_path):
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(2048)
    vm = CubeSandboxLifecycle(client, template="tpl", node_name="node-a", ownership=_owner(),
        snapshot_storage="warm-only", warm_snapshot_root=str(tmp_path), snapshot_pool=pool,
        snapshot_reservation_bytes=1024)
    vm.start()
    def fail_after_pause(sandbox, **kwargs):
        sandbox.state = "paused"
        raise RuntimeError("lost manifest")
    client.pause_sandbox = fail_after_pause
    with pytest.raises(RuntimeError, match="lost manifest"):
        with vm.reserve_checkpoint():
            vm.checkpoint_and_evict()
    assert pool.reserved_bytes == 1024
    assert vm.state.value == "unknown"
    vm.close()
    assert pool.reserved_bytes == 0


@pytest.mark.parametrize("pair", [False, True])
def test_public_snapshot_qualification_exercises_selected_scope(tmp_path, monkeypatch, pair):
    from clawbox.experiments import qualification as q
    from clawbox.experiments.spec import load_experiment, PolicySpec
    spec = load_experiment(Path("examples/experiments/openclaw-cube-replay-c60-overcommit.yaml"))
    resources = spec.resources.model_copy(update={
        "compute_nodes": (), "shared_memory_borrow_limit_mib": None,
        "snapshot_mechanism": "full-copy", "snapshot_storage": "warm-only",
            "snapshot_compression": "none",
        "warm_memory_capacity_mib": 16384, "warm_snapshot_root": str(tmp_path / "warm"),
        "snapshot_destination": "warm",
    })
    policy = PolicySpec(name="wait", admission="off", reclamation="snapshot_pause",
                        eviction="wait_aware_pressure", restore="reactive", runtime_snapshot=pair)
    spec = spec.model_copy(update={"schema_version": 3, "resources": resources, "policies": (policy,)})
    _Sandbox.items = {}
    def run(self, command, **kwargs):
        stdout = "qualification-before" if command.startswith("printf") else "4242"
        return SimpleNamespace(exit_code=0, stdout=stdout, stderr="")
    monkeypatch.setattr(_Commands, "run", run)
    monkeypatch.setattr(q, "CubeSandboxClient", lambda **kw: CubeSandboxClient(sandbox_class=_Sandbox, **kw))
    result = q.snapshot_roundtrip_test(spec, artifact_root=tmp_path / "probe", _template="tpl")
    assert result["passed"]
    assert result["pair_checkpoint_verified"] is pair
    runtime_operations = [t["operation"] for t in result["runtime_timings"]]
    assert ("checkpoint" in runtime_operations) is pair
    assert ("restore" in runtime_operations) is pair
    assert not _Sandbox.items


def _cold_next_use_lifecycle(tmp_path, monkeypatch, *, capacity=100, compressed=False):
    import clawbox.cube.snapshot as snapshot_module
    from clawbox.cube.lifecycle import SandboxState

    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity, next_use_aware=True)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root=str(tmp_path / "warm"),
        cold_snapshot_root=str(tmp_path / "cold"),
        snapshot_pool=pool, snapshot_reservation_bytes=80,
        snapshot_mechanism="full-copy",
        snapshot_compression="zstd-page" if compressed else "none",
        snapshot_compression_binary="/bin/true",
        next_use_residency=True,
    )
    key = SnapshotKey(lifecycle.ownership.session_id, lifecycle.role, 1)
    lifecycle.sandbox_id = "sandbox-promote"
    lifecycle.sandbox = SimpleNamespace(state="paused")
    lifecycle._state = SandboxState.SWAPPED
    lifecycle._generation = 1
    lifecycle._snapshot_key = key
    lifecycle._snapshot_tier = SnapshotTier.COLD
    lifecycle._tier = SnapshotTier.COLD
    lifecycle._replica_memory_path = str(
        tmp_path / "cold" / "sandbox-promote" / "tool-g1.mem"
    )
    lifecycle.set_snapshot_next_use(120, revision=1)
    monkeypatch.setattr(snapshot_module, "read_snapshot_metadata", lambda *_a, **_k: {
        "logical_bytes": 60,
        "allocated_bytes": 60,
        "transferred_bytes": 60,
    })
    return lifecycle, client, pool, key


def test_next_use_cold_restore_promotes_whole_snapshot_before_connect(tmp_path, monkeypatch):
    lifecycle, client, pool, key = _cold_next_use_lifecycle(tmp_path, monkeypatch)
    calls = []

    def relocate(_sandbox, **kwargs):
        calls.append(("relocate", kwargs["tier"], kwargs["memory_snapshot_path"]))
        return {
            "tier": kwargs["tier"], "generation": kwargs["generation"],
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
        }

    def connect(sandbox_id, **_kwargs):
        calls.append(("connect", sandbox_id))
        return SimpleNamespace(state="running")

    client.relocate_snapshot = relocate
    client.connect_sandbox = connect
    lifecycle.restore()

    assert [item[0] for item in calls] == ["relocate", "connect"]
    manifest = pool.manifest(key)
    assert manifest.tier is SnapshotTier.WARM
    assert manifest.retained_by_vm
    assert manifest.path.startswith(str(tmp_path / "warm"))
    assert lifecycle._snapshot_tier is SnapshotTier.WARM
    promotion = next(t for t in lifecycle.timings if t["operation"] == "promote")
    assert promotion["status"] == "ok"
    assert promotion["tier_from"] == "cold" and promotion["tier_to"] == "warm"


def test_next_use_cold_restore_does_not_spill_to_make_promotion_space(tmp_path, monkeypatch):
    lifecycle, client, pool, _key = _cold_next_use_lifecycle(tmp_path, monkeypatch)
    other = SnapshotKey("other", "tool", 1)
    pool.reserve(other, 50)
    pool.commit(
        other, path="/warm/other", logical_bytes=50,
        allocated_bytes=50, transferred_bytes=50,
    )
    calls = []
    client.relocate_snapshot = lambda *_a, **_k: calls.append("relocate")
    client.connect_sandbox = lambda sandbox_id, **_k: (
        calls.append("connect") or SimpleNamespace(state="running")
    )

    lifecycle.restore()

    assert calls == ["connect"]
    assert pool.manifest(other).path == "/warm/other"
    promotion = next(t for t in lifecycle.timings if t["operation"] == "promote")
    assert promotion["status"] == "skipped"
    assert promotion["snapshot_metrics"]["reason"] == "insufficient_free_warm_capacity"


def test_next_use_promotion_failure_aborts_capacity_reservation(tmp_path, monkeypatch):
    lifecycle, client, pool, _key = _cold_next_use_lifecycle(tmp_path, monkeypatch)
    client.relocate_snapshot = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("relocation failed")
    )
    with pytest.raises(RuntimeError, match="relocation failed"):
        lifecycle.restore()
    assert pool.reserved_bytes == 0
    assert lifecycle.state.value == "swapped"
    assert next(t for t in lifecycle.timings if t["operation"] == "promote")["status"] == "error"


def test_next_use_compressed_cold_snapshot_expands_before_promotion(tmp_path, monkeypatch):
    import clawbox.cube.snapshot_compression as compression_module
    import clawbox.cube.snapshot as snapshot_module
    lifecycle, client, pool, key = _cold_next_use_lifecycle(
        tmp_path, monkeypatch, capacity=130, compressed=True,
    )
    calls = []
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_a, **_k: ({
            "logical_bytes": 120,
            "allocated_bytes": 40,
            "raw_allocated_bytes": 120,
            "transferred_bytes": 120,
            "compression": {"baseline_allocated_bytes": 120},
        } if "decompress" not in calls else {
            "logical_bytes": 120,
            "allocated_bytes": 120,
            "transferred_bytes": 120,
        }),
    )
    monkeypatch.setattr(
        compression_module, "decompress_snapshot",
        lambda *_a, **_k: calls.append("decompress") or {"wall_seconds": .1},
    )

    def relocate(_sandbox, **kwargs):
        calls.append("relocate")
        return {
            "tier": kwargs["tier"], "generation": kwargs["generation"],
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
        }

    client.relocate_snapshot = relocate
    client.connect_sandbox = lambda *_a, **_k: (
        calls.append("connect") or SimpleNamespace(state="running")
    )
    lifecycle.restore()
    assert calls[:3] == ["decompress", "relocate", "connect"]
    assert pool.manifest(key).expanded
    promotion = next(t for t in lifecycle.timings if t["operation"] == "promote")
    assert promotion["snapshot_metrics"]["reservation_bytes"] == 120


def test_next_use_compressed_promotion_reserves_reused_parent_expansion(
    tmp_path, monkeypatch,
):
    import clawbox.cube.snapshot_compression as compression_module
    import clawbox.cube.snapshot as snapshot_module

    lifecycle, client, pool, key = _cold_next_use_lifecycle(
        tmp_path, monkeypatch, capacity=230, compressed=True,
    )
    calls = []
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_a, **_k: ({
            "logical_bytes": 220,
            "allocated_bytes": 40,
            "raw_allocated_bytes": 120,
            "transferred_bytes": 120,
            "compression": {
                "baseline_allocated_bytes": 120,
                "compressed_allocated_bytes": 40,
                "decompression_allocated_bytes": 220,
            },
        } if "decompress" not in calls else {
            "logical_bytes": 220,
            "allocated_bytes": 220,
            "transferred_bytes": 120,
        }),
    )
    monkeypatch.setattr(
        compression_module, "decompress_snapshot",
        lambda *_a, **_k: calls.append("decompress") or {"wall_seconds": .1},
    )
    client.relocate_snapshot = lambda _sandbox, **kwargs: (
        calls.append("relocate") or {
            "tier": kwargs["tier"],
            "generation": kwargs["generation"],
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
        }
    )
    client.connect_sandbox = lambda *_a, **_k: (
        calls.append("connect") or SimpleNamespace(state="running")
    )

    lifecycle.restore()

    assert calls == ["decompress", "relocate", "connect"]
    assert pool.manifest(key).allocated_bytes == 220
    promotion = next(t for t in lifecycle.timings if t["operation"] == "promote")
    assert promotion["snapshot_metrics"]["reservation_bytes"] == 220


def test_next_use_compressed_promotion_reserves_sparse_owned_materialization(
    tmp_path, monkeypatch,
):
    import clawbox.cube.snapshot_compression as compression_module
    import clawbox.cube.snapshot as snapshot_module

    decompressed = 747_794_432
    owned_logical = 1_812_430_848
    lifecycle, client, pool, key = _cold_next_use_lifecycle(
        tmp_path, monkeypatch, capacity=1_900_000_000, compressed=True,
    )
    calls = []
    monkeypatch.setattr(
        snapshot_module, "read_snapshot_metadata",
        lambda *_a, **_k: ({
            "logical_bytes": 2_500_000_000,
            "allocated_bytes": 200_000_000,
            "raw_allocated_bytes": 500_000_000,
            "owned_logical_bytes": owned_logical,
            "owned_allocated_bytes": decompressed,
            "transferred_bytes": 200_000_000,
            "compression": {
                "baseline_allocated_bytes": 500_000_000,
                "compressed_allocated_bytes": 200_000_000,
                "decompression_allocated_bytes": decompressed,
            },
        } if "decompress" not in calls else {
            "logical_bytes": 2_500_000_000,
            "allocated_bytes": owned_logical,
            "owned_logical_bytes": owned_logical,
            "owned_allocated_bytes": owned_logical,
            "transferred_bytes": owned_logical,
        }),
    )
    monkeypatch.setattr(
        compression_module, "decompress_snapshot",
        lambda *_a, **_k: calls.append("decompress") or {"wall_seconds": .1},
    )
    client.relocate_snapshot = lambda _sandbox, **kwargs: (
        calls.append("relocate") or {
            "tier": kwargs["tier"],
            "generation": kwargs["generation"],
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
        }
    )
    client.connect_sandbox = lambda *_a, **_k: (
        calls.append("connect") or SimpleNamespace(state="running")
    )

    lifecycle.restore()

    assert calls == ["decompress", "relocate", "connect"]
    assert pool.manifest(key).allocated_bytes == owned_logical
    promotion = next(t for t in lifecycle.timings if t["operation"] == "promote")
    assert promotion["snapshot_metrics"]["reservation_bytes"] == owned_logical


def test_owned_materialization_headroom_is_summed_per_overlapping_layer():
    manifest = {
        # Aggregate subtraction incorrectly returns zero because the fully
        # allocated first layer masks remaining holes in the second layer.
        "owned_logical_bytes": 100,
        "owned_allocated_bytes": 130,
        "lineage_layer_map": [
            {
                "identity": "1:1", "external": False,
                "range_bytes": 100, "allocated_bytes": 100,
            },
            {
                "identity": "1:2", "external": False,
                "range_bytes": 40, "allocated_bytes": 30,
            },
        ],
    }

    assert CubeSandboxLifecycle._owned_materialization_headroom(manifest) == 10
