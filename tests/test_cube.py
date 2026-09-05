from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

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
from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.worker import ExperimentWorker


class _Files:
    def __init__(self) -> None:
        self.content: dict[str, str] = {}

    def read(self, path: str) -> str:
        return self.content[path]

    def write(self, path: str, data: str) -> None:
        self.content[path] = data


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

    def pause(self, wait=True):
        self.state = "paused"

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
    sandbox = _Sandbox({"sandboxID": "sb-7"})
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    endpoint = client.get_tcp_endpoint(sandbox)
    assert endpoint == CubeSandboxTcpEndpoint(
        sandbox_id="sb-7", container_port=2222, address="192.0.2.7:20007",
    )

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


def test_lifecycle_preserves_id_across_pause_restore_and_executor() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id
    executor = CubeCommandExecutor(client, lambda: lifecycle.sandbox)
    assert executor.execute("printf ok", 5).stdout == "ok"
    lifecycle.checkpoint_and_evict()
    assert not lifecycle.resident
    lifecycle.restore()
    assert lifecycle.sandbox_id == sandbox_id and lifecycle.resident
    lifecycle.close()
    assert sandbox_id not in _Sandbox.items


def test_openclaw_snapshot_pauses_runtime_and_restores_it_before_model_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SnapshotCommands(_Commands):
        def run(self, command: str, **kwargs):
            self.owner.command_calls.append((command, kwargs))
            if "kill -0 $pid" in command:
                return SimpleNamespace(exit_code=0, stdout="4242", stderr="")
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class SnapshotSandbox(_Sandbox):
        sequence = 0
        created: list["SnapshotSandbox"] = []

        def __init__(self, data=None, config=None) -> None:
            super().__init__(data, config)
            self.pause_calls = 0
            self.resume_calls = 0
            self.commands = SnapshotCommands(self)

        @classmethod
        def create(cls, **kwargs):
            cls.sequence += 1
            item = cls({
                "sandboxID": f"snapshot-{cls.sequence}",
                "templateID": kwargs["template"],
                "metadata": kwargs["metadata"],
            })
            cls.items[item.sandbox_id] = item
            cls.created.append(item)
            return item

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            item = cls.items[sandbox_id]
            item.state = "running"
            item.resume_calls += 1
            return item

        def pause(self, wait=True):
            self.pause_calls += 1
            super().pause(wait=wait)

    SnapshotSandbox.items = {}
    SnapshotSandbox.sequence = 0
    SnapshotSandbox.created = []
    trace = tmp_path / "openclaw-snapshot.jsonl"
    trace.write_text(json.dumps({
        "type": "action", "action_type": "llm_call", "action_id": "llm-1",
        "iteration": 0, "ts_start": 0, "ts_end": 0.5,
        "data": {
            "model": "recorded-model",
            "raw_request": {"messages": [{"role": "user", "content": "hello"}]},
            "raw_response": {
                "content": None,
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "exec", "arguments": '{"command":"true"}'},
                }],
            },
            "llm_latency_ms": 500,
        },
    }) + "\n", encoding="utf-8")

    def post_policy(policy_control, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            policy_control.url + path,
            data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {policy_control.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def fake_run_openclaw(*, prompt, session_id, configuration, ssh,
                          policy_control, runtime_executor, output_dir,
                          timeout_seconds, model_gateway, prediction_manifest=None):
        payload = {
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, _content_type, _body, request_id = model_gateway.gateway.complete_http(payload)
        assert status == 200
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
        return {
            "agent_pid_file": f"/state/openclaw/{session_id}/agent.pid",
            "tool_latencies": [0.01], "runtime_traces": [],
            "policy_control_records": policy_control.records(),
        }

    monkeypatch.setattr(worker_module, "run_openclaw", fake_run_openclaw)
    monkeypatch.setattr(
        worker_module, "collect_and_validate_native_tool_artifacts",
        lambda **_kwargs: SimpleNamespace(
            root=tmp_path / "native", validation={"ok": True},
            cgroup_artifacts={"exec-1": {
                "memory_rss_peak_bytes": 2 * 1024 * 1024,
                "ts_start": 1.0, "ts_end": 1.01,
                "cpu_utilization_avg_cores": 0.25,
            }},
        ),
    )
    monkeypatch.setenv("CLAWBOX_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAWBOX_MODEL_GATEWAY_HOST", "127.0.0.1")
    spec = ExperimentSpec.model_validate({
        "schema_version": 2, "experiment_id": "openclaw-snapshot-test",
        "workload": {"source": "recorded_trace", "input": str(trace), "cases": [{
            "case_id": "snapshot-case", "source": "recorded_trace",
            "source_reference": str(trace), "replay_trace_reference": str(trace),
            "prompt": "hello",
        }]},
        "agent": {"driver": "openclaw"},
        "inference": {"backend": "replay", "configuration": {"model": "recorded-model"}},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tool-tpl", "memory_mib": 4096},
        "execution": {"concurrency_levels": [1], "randomized_order": False,
                       "arm_timeout_seconds": 10, "command_timeout_seconds": 5,
                       "stabilization_seconds": 0},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 100000,
                       "emergency_free_memory_mib": 1, "checkpoint_restore_headroom_mib": 1,
                       "static_tool_memory_mib": 1},
        "policies": [{"name": "snapshot", "admission": "tool_static",
                      "reclamation": "snapshot_pause", "eviction": "eager",
                      "restore": "reactive"}],
    })
    result = ExperimentWorker(
        spec, run_id="openclaw-snapshot", attempt_id="attempt", task_uid="task",
        output_root=tmp_path / "results",
        client=CubeSandboxClient(sandbox_class=SnapshotSandbox),
    ).run()[0]

    assert result.status.value == "succeeded"
    assert result.provenance["evidence_class"] == "deterministic-managed-replay"
    assert result.provenance["configured_trace_sha256"] == hashlib.sha256(
        trace.read_bytes()
    ).hexdigest()
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
    assert {"before_runtime_pause", "after_runtime_restore_response"} <= pid_phases
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
    assert admission["runtime_pause_started_at"] <= admission["runtime_pause_completed_at"]
    assert admission["runtime_restore_started_at"] <= admission["runtime_restore_completed_at"]
    assert (
        admission["runtime_pause_completed_at"]
        <= admission["runtime_restore_started_at"]
        <= gateway_record["response_released_unix_s"]
    )
    assert admission["runtime_agent_pid_before_pause"] == 4242
    assert admission["runtime_agent_pid_after_restore"] == 4242
    session_spans = result.performance["session_time_spans"][0]
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
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict()
    checkpoint = lifecycle.timings[-1]
    assert checkpoint["host_memory_before"]["host_used_bytes"] == 120
    assert checkpoint["host_memory_after"]["host_used_bytes"] == 70
    assert checkpoint["host_observed_reclaimed_bytes"] == 50
    assert checkpoint["host_observed_growth_bytes"] == 0


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

    def run(command: str, **_kwargs):
        encoded = command.rsplit(" ", 1)[-1]
        envelope = base64.b64decode(encoded).decode()
        assert envelope.endswith("\nprintf observed")
        record = {
            "execution_id": execution_id, "exit_code": 0,
            "telemetry_state": "complete", "telemetry_artifact": "",
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


def test_cleanup_attempts_every_owned_sandbox_after_one_kill_fails() -> None:
    class PartiallyFailingSandbox(_Sandbox):
        failed = False

        def kill(self):
            if not type(self).failed:
                type(self).failed = True
                raise RuntimeError("transient kill failure")
            return super().kill()

    PartiallyFailingSandbox.items = {}
    client = CubeSandboxClient(sandbox_class=PartiallyFailingSandbox)
    first = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    second = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    with pytest.raises(RuntimeError, match="kill errors="):
        client.kill_owned_sandboxes("task")
    # The first failure is reported, but the second owned sandbox was still
    # attempted and removed.  A retry can reclaim the failed first handle.
    assert second.sandbox_id not in PartiallyFailingSandbox.items
    assert first.sandbox_id in PartiallyFailingSandbox.items
    client.kill_owned_sandboxes("task")
    assert PartiallyFailingSandbox.items == {}


def test_failed_lifetime_admission_does_not_underflow_cleanup_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"action","action_type":"tool_exec","action_id":"t",'
        '"ts_start":0,"ts_end":0,"data":{"tool_name":"exec",'
        '"args":{"command":"true"},"exit_code":0}}\n',
        encoding="utf-8",
    )
    spec = ExperimentSpec.model_validate({
        "schema_version": 2, "experiment_id": "failed-admission-cleanup",
        "workload": {"source": "recorded_trace", "input": str(trace)},
        "agent": {"driver": "replay_engine"}, "inference": {"backend": "replay"},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tool-tpl", "memory_mib": 4096},
        "execution": {"concurrency_levels": [1], "randomized_order": False,
                       "arm_timeout_seconds": 1, "stabilization_seconds": 0},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 100000,
                       "emergency_free_memory_mib": 1},
        "policies": [{"name": "naive", "admission": "lifetime_full",
                      "reclamation": "resident", "eviction": "none", "restore": "none"}],
    })
    unregistered: list[str] = []
    original_unregister = PolicyCoordinator.unregister

    def fail_acquire(self, session_id: str, amount_mib: int, timeout_s: float) -> float:
        raise RuntimeError("admission deliberately failed")

    def record_unregister(self, session_id: str) -> None:
        unregistered.append(session_id)
        original_unregister(self, session_id)

    monkeypatch.setattr(PolicyCoordinator, "acquire", fail_acquire)
    monkeypatch.setattr(PolicyCoordinator, "unregister", record_unregister)
    result = ExperimentWorker(
        spec, run_id="run", attempt_id="attempt", task_uid="task",
        output_root=tmp_path / "results", client=CubeSandboxClient(sandbox_class=_Sandbox),
    ).run()[0]
    assert result.status.value == "failed"
    assert "reservation underflow" not in str(result.correctness["failure"])
    assert len(unregistered) == 1
    assert unregistered[0].endswith("-0000")


def test_worker_runs_atomic_arm_and_skips_matching_completion(tmp_path: Path) -> None:
    _Sandbox.items = {}
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"action","action_type":"llm_call","action_id":"l","ts_start":0,"ts_end":0,'
        '"data":{"llm_latency_ms":0}}\n'
        '{"type":"action","action_type":"tool_exec","action_id":"t","ts_start":0,"ts_end":0,'
        '"data":{"tool_name":"exec","args":{"command":"true"},"exit_code":0}}\n',
        encoding="utf-8",
    )
    spec = ExperimentSpec.model_validate({
        "schema_version": 2, "experiment_id": "worker-test",
        "workload": {"source": "recorded_trace", "input": str(trace)},
        "agent": {"driver": "replay_engine"}, "inference": {"backend": "replay"},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tpl"},
        "execution": {"concurrency_levels": [1], "randomized_order": False,
                      "stabilization_seconds": 0},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 100000,
                      "emergency_free_memory_mib": 1},
        "policies": [{"name": "naive", "admission": "lifetime_full",
                      "reclamation": "resident", "eviction": "none", "restore": "none"}],
    })
    output = tmp_path / "results"
    first_client = CubeSandboxClient(sandbox_class=_Sandbox)
    first = ExperimentWorker(spec, run_id="run", attempt_id="attempt", task_uid="task",
                             output_root=output, client=first_client).run()
    assert first[0].status == "succeeded"
    marker = next((output / "arms").glob("*.complete"))
    assert marker.read_text().strip() == first[0].arm.spec_digest
    _Sandbox.create_kwargs = {}
    second = ExperimentWorker(spec, run_id="run", attempt_id="attempt", task_uid="task",
                              output_root=output,
                              client=CubeSandboxClient(sandbox_class=_Sandbox)).run()
    assert second[0].arm.arm_id == first[0].arm.arm_id
    assert _Sandbox.create_kwargs == {}


def test_worker_runs_every_current_baseline_at_c40_with_complete_spans(
    tmp_path: Path,
) -> None:
    class ConcurrentSandbox(_Sandbox):
        create_lock = threading.Lock()
        create_sequence = 0

        @classmethod
        def create(cls, **kwargs):
            with cls.create_lock:
                cls.create_sequence += 1
                sandbox_id = f"c40-{cls.create_sequence}"
            item = cls({
                "sandboxID": sandbox_id,
                "templateID": kwargs["template"],
                "metadata": kwargs["metadata"],
            })
            cls.items[sandbox_id] = item
            return item

    ConcurrentSandbox.items = {}
    trace = tmp_path / "c40-trace.jsonl"
    trace.write_text(
        '{"type":"action","action_type":"llm_call","action_id":"llm-1",'
        '"ts_start":0,"ts_end":0.01,"data":{"llm_latency_ms":10}}\n'
        '{"type":"action","action_type":"tool_exec","action_id":"tool-1",'
        '"ts_start":0.01,"ts_end":0.02,"data":{"tool_name":"exec",'
        '"args":{"command":"true"},"exit_code":0}}\n',
        encoding="utf-8",
    )
    # Exercise every catalog entry at c40. Compatibility aliases are not a
    # second backend; they materialize the same supported schema-v2 policies
    # under their retained historical names.
    policy_data = [baseline.as_policy().model_dump(mode="json")
                   for baseline in BASELINES.values()]
    assert [item["name"] for item in policy_data] == list(BASELINES)
    spec = ExperimentSpec.model_validate({
        "schema_version": 2, "experiment_id": "baseline-c40-test",
        "workload": {"source": "recorded_trace", "input": str(trace)},
        "agent": {"driver": "replay_engine"}, "inference": {"backend": "replay"},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tool-tpl", "memory_mib": 4096},
        "execution": {"concurrency_levels": [40], "randomized_order": False,
                       "stabilization_seconds": 0},
        "resources": {
            "target_node": "node-a", "pool_memory_budget_mib": 1000000,
            "emergency_free_memory_mib": 1, "checkpoint_restore_headroom_mib": 8192,
            "static_tool_memory_mib": 256, "full_tool_memory_mib": 4096,
            "p90_predictions": "examples/predictions/smoke-p90.json",
            "oracle_measurements": "examples/predictions/smoke-oracle.json",
        },
        "policies": policy_data,
        "output": {"directory": str(tmp_path / "output")},
    })
    results = ExperimentWorker(
        spec, run_id="c40-run", attempt_id="c40-attempt", task_uid="c40-task",
        output_root=tmp_path / "results",
        client=CubeSandboxClient(sandbox_class=ConcurrentSandbox),
    ).run()
    assert len(results) == len(policy_data)
    assert all(result.status.value == "succeeded" for result in results)
    assert all(result.arm.concurrency == 40 for result in results)
    assert ConcurrentSandbox.items == {}
    for result in results:
        spans = {
            span["name"]
            for session in result.performance["session_time_spans"]
            for span in session
        }
        assert {"session", "sandbox.create", "agent", "sandbox.cleanup"} <= spans
