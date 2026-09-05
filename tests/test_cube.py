from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from clawbox.cube import (
    CubeCommandExecutor,
    CubeSandboxClient,
    CubeSandboxLifecycle,
    CubeSandboxTcpEndpoint,
    OwnedSandboxJournal,
    Ownership,
)
from clawbox.experiments import ExperimentSpec
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
