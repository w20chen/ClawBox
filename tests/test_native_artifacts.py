from __future__ import annotations

import base64
import hashlib
import gzip
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from clawbox.experiments.native_artifacts import (
    _decode_framed_artifacts,
    _collect_large_artifact_stream,
    _runtime_spans,
    runtime_request_failures,
    collect_and_validate_native_tool_artifacts,
    validate_native_tool_join,
)


@pytest.mark.parametrize("name,details,content,reason", [
    ("exec", {"failureKind": "runtime-error", "status": "failed", "exitCode": None},
     'workdir "/workspace" is unavailable: command was not executed', "command_not_executed"),
    ("edit", {"status": "error", "error": "Could not edit file: /testbed/a. Error code: 125."},
     "", "ssh_transport_failed"),
    ("read", {"status": "error", "error": "ssh exited with code 125"}, "", "ssh_transport_failed"),
    ("exec", {"status": "error", "error": "exec preflight: complex interpreter invocation"},
     "", "exec_preflight_refused"),
])
def test_request_failure_evidence_survives_missing_native_id_and_ok_status(tmp_path, name, details, content, reason):
    record = {"record_type": "span_end", "kind": "tool", "name": name,
              "session_id": "s", "span_id": "call-1", "status": {"code": "ok"},
              "execution": {"execution_id": None},
              "output": {"exit_code": 0, "result": {"details": details,
                         "content": [{"type": "text", "text": content}]}}}
    path = tmp_path / "runtime.jsonl"
    path.write_text((json.dumps(record) + "\n") * 2, encoding="utf-8")
    assert _runtime_spans([str(path)]) == []
    failures = runtime_request_failures([str(path)], session_id="s")
    assert len(failures) == 1
    assert failures[0] == {"call_id": "call-1", "tool": name, "execution_id": None,
                           "reason": reason, "message": details.get("error") or content}
    assert runtime_request_failures([str(path)], session_id="another-session") == []


def test_expected_file_diagnostics_are_not_transport_rejections(tmp_path):
    record = {"record_type": "span_end", "kind": "tool", "name": "edit", "session_id": "s",
              "output": {"result": {"details": {"status": "error", "error": "old text was not found"}}}}
    path = tmp_path / "runtime.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert runtime_request_failures([str(path)], session_id="s") == []


def test_native_join_preserves_admission_failure_cause():
    with pytest.raises(ValueError, match="PredictionUnavailable: missing memory evidence"):
        validate_native_tool_join(
            bridge_records=[], cgroup_artifacts={}, clause_artifacts={},
            policy_records=[{"request": {"execution_id": "exec-1"},
                             "admission_error": {"type": "PredictionUnavailable", "message": "missing memory evidence"},
                             "completion": None}],
        )


@pytest.mark.skipif(sys.platform == "win32", reason="guest export uses POSIX shell tools")
def test_real_compressed_export_exceeds_4mib_and_roundtrips_in_chunks(tmp_path, monkeypatch):
    from clawbox.experiments import native_artifacts as native
    resources = tmp_path / "tool-resource"
    resources.mkdir()
    payload = os.urandom(5 * 1024 * 1024)
    (resources / "large.json").write_bytes(payload)
    (tmp_path / "tool-bridge.jsonl").write_bytes(b"{}\n")
    monkeypatch.setattr(native, "_TOOL_RESOURCE_ROOT", str(resources))
    monkeypatch.setattr(native, "_ARTIFACT_EXPORT", str(tmp_path / "export"))

    class Executor:
        def execute(self, command, timeout):
            completed = subprocess.run(shlex.split(command)[-1], shell=True,
                                       capture_output=True, text=True, timeout=timeout)
            return CommandResult(completed.returncode, completed.stdout[:4*1024*1024],
                                 completed.stderr, 0)

    ssh = NativeSSHConfig("executor@tool:2222", "private", "public")
    first = Executor().execute(native._direct_ssh_command(
        ssh, "/identity", "/known-hosts", native._collection_command()), 60)
    assert len(first.stdout) == 4 * 1024 * 1024
    assert native._ARTIFACT_END not in first.stdout
    result = _collect_large_artifact_stream(Executor(), ssh, "/identity", "/known-hosts")
    assert _decode_framed_artifacts(result.stdout)["large.json"] == payload


def test_large_export_is_frozen_once_and_read_below_stdout_limit():
    from clawbox.experiments.native_artifacts import _ARTIFACT_CHUNK_BYTES
    payload = "x" * (5 * _ARTIFACT_CHUNK_BYTES + 123)
    commands = []

    class Executor:
        def execute(self, command, timeout):
            commands.append(command)
            if "wc -c" in command:
                return CommandResult(0, str(len(payload)), "", 0)
            index = len(commands) - 2
            chunk = payload[index * _ARTIFACT_CHUNK_BYTES:(index + 1) * _ARTIFACT_CHUNK_BYTES]
            assert f"skip={index}" in command
            return CommandResult(0, chunk, "", 0)

    result = _collect_large_artifact_stream(
        Executor(), NativeSSHConfig("executor@tool:2222", "private", "public"),
        "/identity", "/known-hosts",
    )
    assert result.stdout == payload
    assert len(commands) == 7
    assert sum("tar -czf" in command for command in commands) == 1


def test_large_export_rejects_a_truncated_chunk():
    class Executor:
        def execute(self, command, timeout):
            return CommandResult(0, "100" if "wc -c" in command else "short", "", 0)

    with pytest.raises(RuntimeError, match="chunk at 0 is incomplete"):
        _collect_large_artifact_stream(
            Executor(), NativeSSHConfig("executor@tool:2222", "private", "public"),
            "/identity", "/known-hosts",
        )


def test_compressed_artifact_transfer_preserves_large_payload():
    payload = (b'{"clause":"example", "resource_bytes":12345}\n' * 120000)
    encoded = base64.b64encode(gzip.compress(payload)).decode()
    stream = "__CLAWBOX_ARTIFACT_GZIP_V1__clause.json\n" + encoded + "\n__CLAWBOX_ARTIFACT_END__\n"
    assert len(payload) > 4 * 1024 * 1024
    assert len(stream) < 4 * 1024 * 1024
    assert _decode_framed_artifacts(stream) == {"clause.json": payload}


def test_tar_export_accepts_legitimate_clause_telemetry_over_16_mib():
    payload = b"x" * (17 * 1024 * 1024)
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("tool-resource/clause-telemetry-exec-large.json")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    stream = (
        "__CLAWBOX_ARTIFACT_TAR_GZIP_V1__\n"
        + base64.b64encode(archive_buffer.getvalue()).decode()
        + "\n__CLAWBOX_ARTIFACT_END__\n"
    )
    assert _decode_framed_artifacts(stream)[
        "clause-telemetry-exec-large.json"
    ] == payload


def test_artifact_limit_is_256_mib_and_error_reports_actual_size(monkeypatch):
    from clawbox.experiments import native_artifacts as native

    assert native._MAX_ARTIFACT_BYTES == 256 * 1024 * 1024
    monkeypatch.setattr(native, "_MAX_ARTIFACT_BYTES", 4)
    payload = base64.b64encode(b"12345").decode()
    stream = (
        "__CLAWBOX_ARTIFACT_V1__large.json\n"
        + payload
        + "\n__CLAWBOX_ARTIFACT_END__\n"
    )
    with pytest.raises(ValueError, match=r"large\.json is 5 bytes; limit is 4 bytes"):
        _decode_framed_artifacts(stream)


from clawbox.experiments.worker import (
    enrich_tool_execution_observations,
    summarize_tool_execution_observations,
)
from clawbox.experiments.openclaw_driver import NativeSSHConfig
from clawbox.replay.lifecycle import CommandResult


def _policy(
    execution_id: str, digest: str, effective_digest: str | None = None,
) -> list[dict]:
    request = {
        "session_id": "session-a", "execution_id": execution_id,
        "command_sha256": digest, "operation": "exec",
    }
    if effective_digest is not None:
        request["effective_command_sha256"] = effective_digest
    return [{
        "request": request,
        "admission": {"decision": "ADMIT"},
        "completion": {"status": "COMPLETED"},
    }]


def _artifacts(execution_id: str, digest: str) -> tuple[list[dict], dict, dict]:
    bridge = [{
        "execution_source": "runtime-envelope", "execution_id": execution_id,
        "task_id": "session-a",
        "command_sha256": digest, "telemetry_state": "complete",
        "telemetry_artifact": (
            f"/var/lib/clawtune/artifacts/tool-resource/"
            f"clause-telemetry-{execution_id}.json"
        ),
    }]
    cgroup = {
        "schema": "cgroup_resource_v1", "execution_id": execution_id,
        "source": "cgroup-v2", "sampling_quality": "valid",
        "ts_start": 1.0, "ts_end": 2.0,
        "cpu_utilization_avg_cores": 0.1, "memory_rss_peak_bytes": 4096,
    }
    clause = {
        "version": 2, "collection_validity": "valid", "cleanup": "ok",
        "provenance": {"collector": "ebpf_ebpf"},
        "telemetry_loss_total": {"total": 0},
        "calls": [{"tool_call_id": execution_id, "eligible_for_kb": True}],
    }
    return bridge, cgroup, clause


def test_filesystem_call_allows_non_kb_clause_but_keeps_integrity_checks():
    execution_id, digest = "file-edit", "a" * 64
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    policy = _policy(execution_id, digest)
    policy[0]["request"].update(operation="filesystem", runtime_trace_expected=False)
    clause["collection_validity"] = "invalid"
    clause["calls"][0]["eligible_for_kb"] = False
    clause["calls"][0]["clauses"] = []
    clause["calls"][0]["invalid_reasons"] = [{
        "kind": "analysis_failure", "detail": "cannot identify tree: reason=no_exec_images",
    }]
    clause["collector"] = {"health": "healthy"}

    report = validate_native_tool_join(
        bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause}, policy_records=policy,
        expected_session_id="session-a",
    )
    assert report["non_kb_filesystem_execution_ids"] == [execution_id]

    clause["telemetry_loss_total"]["total"] = 1
    with pytest.raises(ValueError, match="reports event loss"):
        validate_native_tool_join(
            bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause}, policy_records=policy,
            expected_session_id="session-a",
        )

    clause["telemetry_loss_total"]["total"] = 0
    clause["calls"][0]["invalid_reasons"][0]["detail"] = "unexpected collector failure"
    with pytest.raises(ValueError, match="unexpected invalid state"):
        validate_native_tool_join(
            bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause}, policy_records=policy,
            expected_session_id="session-a",
        )


def test_backend_maintenance_allows_declared_ineligible_cgroup_fallback():
    execution_id, digest = "maintenance-1", "b" * 64
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    policy = _policy(execution_id, digest)
    policy[0]["request"].update(
        operation="ssh_backend_maintenance",
        execution_scope="backend-maintenance",
        runtime_trace_expected=False,
    )
    cgroup.update(
        source="process-tree",
        fallback_used=True,
        sampling_quality="degraded",
        cpu_utilization_avg_cores=0.0,
        memory_rss_peak_bytes=0,
        memory_eligible=False,
        memory_unavailable_reason="no_in_execution_memory_sample",
        cgroup_setup_error=None,
        cgroup_read_error=None,
        collector_errors=[],
    )

    report = validate_native_tool_join(
        bridge_records=bridge,
        cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause},
        policy_records=policy,
        expected_session_id="session-a",
    )

    assert report["backend_maintenance_cgroup_fallback_execution_ids"] == [
        execution_id
    ]
    assert report["backend_maintenance_cgroup_fallback_execution_count"] == 1


def test_backend_maintenance_allows_declared_eligible_process_tree_memory():
    execution_id, digest = "maintenance-eligible", "d" * 64
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    policy = _policy(execution_id, digest)
    policy[0]["request"].update(
        operation="ssh_backend_maintenance",
        execution_scope="backend-maintenance",
        runtime_trace_expected=False,
    )
    cgroup.update(
        source="process-tree",
        fallback_used=True,
        sampling_quality="degraded",
        memory_eligible=True,
        memory_measurement="guest_memtotal_minus_memavailable",
        memory_environment_id="cube:session-a:sandbox-a",
        memory_baseline_bytes=1024,
        memory_extra_peak_bytes=256,
        memory_total_peak_bytes=1280,
        memory_unavailable_reason=None,
        cgroup_setup_error=None,
        cgroup_read_error=None,
        collector_errors=[],
    )

    report = validate_native_tool_join(
        bridge_records=bridge,
        cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause},
        policy_records=policy,
        expected_session_id="session-a",
    )

    assert report["backend_maintenance_cgroup_fallback_execution_ids"] == [
        execution_id
    ]


def test_agent_tool_rejects_cgroup_process_tree_fallback():
    execution_id, digest = "exec-1", "c" * 64
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    cgroup.update(
        source="process-tree",
        fallback_used=True,
        sampling_quality="degraded",
        memory_eligible=False,
        memory_unavailable_reason="no_in_execution_memory_sample",
    )

    with pytest.raises(ValueError, match="not cgroup-v2"):
        validate_native_tool_join(
            bridge_records=bridge,
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest),
        )

def test_shell_exec_retains_partial_non_kb_clause():
    execution_id, digest = "exec-invalid", "b" * 64
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    clause["collection_validity"] = "invalid"
    clause["calls"][0]["eligible_for_kb"] = False
    validate_native_tool_join(
        bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause},
        policy_records=_policy(execution_id, digest),
        expected_session_id="session-a",
    )

def test_runtime_spans_collapse_only_agreeing_mirrored_writers(
    tmp_path: Path,
) -> None:
    execution_id = "exec-mirrored"
    base = {
        "record_type": "span_end", "kind": "tool", "name": "exec",
        "trace_id": "trace-a", "span_id": "call-a", "session_id": "session-a",
        "repo": "workspace",
        "status": {"code": "ok"}, "output": {"exit_code": 0},
        "execution": {"execution_id": execution_id, "mode": "launcher"},
    }
    rich = {
        **base,
        "execution": {
            "execution_id": execution_id, "mode": "marker",
            "requested_command": "printf ok", "effective_command": "envelope",
            "payload_command": "printf ok",
        },
    }
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(base) + "\n", encoding="utf-8")
    second.write_text(json.dumps(rich) + "\n", encoding="utf-8")

    spans = _runtime_spans([str(first), str(second)])

    assert len(spans) == 1
    assert spans[0]["execution"]["requested_command"] == "printf ok"
    assert spans[0]["repo"] == "workspace"

    distinct = {**base, "span_id": "call-b"}
    second.write_text(json.dumps(distinct) + "\n", encoding="utf-8")
    assert len(_runtime_spans([str(first), str(second)])) == 2


def test_runtime_spans_recovers_exact_clawtune_envelope_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    path.write_text(json.dumps({
        "record_type": "span_end", "kind": "tool", "name": "exec",
        "trace_id": "trace-a", "span_id": "call-a", "session_id": "session-a",
        "status": {"code": "ok"}, "output": {"exit_code": 0},
        "execution": {
            "execution_id": None,
            "effective_command": "__CBX_EXEC_1__exec-recovered\nprintf ok",
            "payload_command": "printf ok",
        },
    }) + "\n", encoding="utf-8")

    spans = _runtime_spans([str(path)])

    assert spans[0]["execution"]["execution_id"] == "exec-recovered"
    assert spans[0]["execution"]["execution_id_source"] == (
        "effective_command_envelope_recovery"
    )


def test_runtime_spans_excludes_tool_request_rejected_before_execution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    rejected = {
        "record_type": "span_end", "kind": "tool", "name": "exec",
        "trace_id": "trace-a", "span_id": "call-a", "session_id": "session-a",
        "status": {"code": "error"},
        "output": {"result": {
            "content": [{"type": "text", "text": (
                'workdir "/workspace" is unavailable: command was not executed.'
            )}],
            "details": {
                "failureKind": "runtime-error", "status": "failed",
                "exitCode": None,
            },
        }},
        "execution": {"execution_id": "exec-rejected", "mode": "launcher"},
    }
    path.write_text(json.dumps(rejected) + "\n", encoding="utf-8")

    assert _runtime_spans([str(path)]) == []

    rejected["output"]["result"]["content"][0]["text"] = "unknown runtime error"
    path.write_text(json.dumps(rejected) + "\n", encoding="utf-8")
    assert len(_runtime_spans([str(path)])) == 1


def test_runtime_spans_recovers_base64_clawtune_envelope_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    header = base64.urlsafe_b64encode(json.dumps({
        "v": 1,
        "execution_id": "exec-recovered-b64",
        "profile_command_b64": base64.urlsafe_b64encode(b"printf ok").decode(),
    }).encode()).decode().rstrip("=")
    path.write_text(json.dumps({
        "record_type": "span_end", "kind": "tool", "name": "exec",
        "trace_id": "trace-a", "span_id": "call-a", "session_id": "session-a",
        "status": {"code": "ok"}, "output": {"exit_code": 0},
        "execution": {
            "execution_id": None,
            "effective_command": f"__CBX_EXEC_1__b64:{header}\nprintf ok",
            "payload_command": "printf ok",
        },
    }) + "\n", encoding="utf-8")

    spans = _runtime_spans([str(path)])

    assert spans[0]["execution"]["execution_id"] == "exec-recovered-b64"
    assert spans[0]["execution"]["execution_id_source"] == (
        "effective_command_envelope_recovery"
    )


def test_runtime_spans_rejects_structured_envelope_identity_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.jsonl"
    path.write_text(json.dumps({
        "record_type": "span_end", "kind": "tool", "name": "exec",
        "execution": {
            "execution_id": "exec-structured",
            "effective_command": (
                '__CBX_EXEC_1__{"v":1,"execution_id":"exec-envelope"}'
                "\nprintf ok"
            ),
        },
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="structured/envelope"):
        _runtime_spans([str(path)])


def test_native_tool_join_requires_exact_bridge_and_artifact_identity() -> None:
    execution_id = "exec-1"
    digest = hashlib.sha256(b"printf ok").hexdigest()
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    verdict = validate_native_tool_join(
        bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause},
        policy_records=_policy(execution_id, digest),
    )
    assert verdict["valid"] is True
    assert verdict["exact_id_join_rate"] == 1.0

    with pytest.raises(ValueError, match="bridge command digests"):
        validate_native_tool_join(
            bridge_records=[{**bridge[0], "command_sha256": "b" * 64}],
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest),
        )

    effective_digest = hashlib.sha256(b"wrapper").hexdigest()
    with pytest.raises(ValueError, match="effective command digests"):
        validate_native_tool_join(
            bridge_records=[{
                **bridge[0], "effective_command_sha256": "b" * 64,
            }],
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest, effective_digest),
        )

    with pytest.raises(ValueError, match="wrong session"):
        validate_native_tool_join(
            bridge_records=[{**bridge[0], "task_id": "session-b"}],
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest),
            expected_session_id="session-a",
        )


def test_native_tool_join_rejects_non_ebpf_and_impossible_cpu_counters():
    execution_id = "exec-1"
    bridge, cgroup, clause = _artifacts(execution_id, "digest")

    def validate():
        return validate_native_tool_join(
            bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, "digest"),
        )

    clause["provenance"]["collector"] = "cgroup"
    with pytest.raises(ValueError, match="not from eBPF"):
        validate()
    clause["provenance"]["collector"] = "ebpf_ebpf"
    clause["calls"][0].update({
        "provenance": {"call_ended_monotonic_ns": 10_000_000_000, "quota_cores": 2},
        "clauses": [{"cpu_ns_cumulative": 2**64 - 1}],
    })
    with pytest.raises(ValueError, match="impossible eBPF CPU counter"):
        validate()
    clause["calls"][0]["clauses"][0]["cpu_ns_cumulative"] = 1_000_000_000
    assert validate()["valid"] is True


def test_native_tool_join_reports_preflight_rejections_without_inventing_telemetry():
    execution_id = "exec-1"
    bridge, cgroup, clause = _artifacts(execution_id, "digest")
    executed = {"session_id": "session-a", "execution": {"execution_id": execution_id}}
    rejected = {
        "name": "exec", "session_id": "session-a",
        "execution": {"execution_id": "exec-rejected"},
        "output": {"result": {"details": {
            "status": "error", "error": "exec preflight: complex interpreter invocation",
        }}},
    }

    def validate(spans):
        return validate_native_tool_join(
            bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, "digest"),
            runtime_span_records=spans, expected_session_id="session-a",
        )

    verdict = validate([executed, rejected, {**rejected, "status": {"code": "ok"}}])
    assert verdict["preflight_rejected_execution_ids"] == ["exec-rejected"]
    assert verdict["preflight_rejected_execution_count"] == 1
    assert verdict["runtime_trace_execution_count"] == 1
    assert verdict["exact_id_join_rate"] == 1
    for altered in (
        {**rejected, "output": {"result": {"details": {"status": "error", "error": "SSH failed"}}}},
        {**rejected, "execution": {"execution_id": "exec-rejected", "payload_pid": 123}},
    ):
        with pytest.raises(ValueError, match="Runtime trace identity mismatch"):
            validate([executed, altered])


def test_native_tool_join_allows_explicit_runtime_trace_exemption() -> None:
    execution_id = "exec-fs"
    digest = hashlib.sha256(b"read file").hexdigest()
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    policy = _policy(execution_id, digest)
    policy[0]["request"].update({
        "operation": "filesystem", "runtime_trace_expected": False,
    })
    verdict = validate_native_tool_join(
        bridge_records=bridge, cgroup_artifacts={execution_id: cgroup},
        clause_artifacts={execution_id: clause}, policy_records=policy,
        runtime_span_records=[], expected_session_id="session-a",
    )
    assert verdict["exact_id_join_rate"] == 1.0
    assert verdict["runtime_trace_expected_execution_count"] == 0
    assert verdict["runtime_trace_exempt_execution_count"] == 1


class _RuntimeExecutor:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.commands: list[str] = []

    def execute(self, command: str, _timeout: float) -> CommandResult:
        self.commands.append(command)
        return CommandResult(0, self.stdout, "", 0.1)


class _TransientRuntimeExecutor(_RuntimeExecutor):
    def __init__(self, stdout: str) -> None:
        super().__init__(stdout)
        self.attempts = 0

    def execute(self, command: str, _timeout: float) -> CommandResult:
        self.attempts += 1
        if self.attempts == 1:
            raise ConnectionError("incomplete chunked read")
        return super().execute(command, _timeout)


@pytest.mark.parametrize("broken_pmu", [None, b'{"schema":', b'{"schema":"pmu_profile_v1"}'])
def test_native_tool_artifact_collection_copies_raw_files_and_validates(tmp_path: Path, broken_pmu) -> None:
    execution_id = "exec-1"
    digest = hashlib.sha256(b"printf ok").hexdigest()
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    files = {
        "tool-bridge.jsonl": (json.dumps(bridge[0]) + "\n").encode(),
        "cgroup-resource-exec-1.json": (json.dumps(cgroup) + "\n").encode(),
        "clause-telemetry-exec-1.json": (json.dumps(clause) + "\n").encode(),
    }
    if broken_pmu is not None:
        files["pmu-profile-exec-1.json"] = broken_pmu
    stdout = "".join(
        "__CLAWBOX_ARTIFACT_V1__" + name + "\n"
        + base64.b64encode(raw).decode() + "\n"
        for name, raw in files.items()
    ) + "__CLAWBOX_ARTIFACT_END__\n"
    executor = _RuntimeExecutor(stdout)
    runtime_trace = tmp_path / "runtime.jsonl"
    runtime_trace.write_text(json.dumps({
        "record_type": "span_end", "kind": "tool", "session_id": "session-a",
        "execution": {"execution_id": execution_id, "command_digest": digest},
    }) + "\n", encoding="utf-8")
    collection = collect_and_validate_native_tool_artifacts(
        runtime_executor=executor,
        ssh=NativeSSHConfig("executor@tool.example:2222", "private", "public"),
        session_id="session-a", output_dir=tmp_path,
        policy_records=_policy(execution_id, digest),
        runtime_trace_paths=[str(runtime_trace)],
    )
    assert collection.validation["exact_id_join_rate"] == 1.0
    assert collection.validation["artifact_collection_attempts"] == 1
    assert collection.validation["pmu"]["invalid_artifacts"] == (
        ["pmu-profile-exec-1.json"] if broken_pmu is not None else []
    )
    assert collection.validation["runtime_trace_execution_count"] == 1
    assert (collection.root / "tool-bridge.jsonl").read_bytes() == files["tool-bridge.jsonl"]
    assert (collection.root / "validation.json").is_file()
    assert executor.commands[0].startswith(
        "/state/openclaw/session-a/bin/ssh.real "
    )
    assert "-p 2222" in executor.commands[0]
    assert "__CBX_EXEC_1__" not in executor.commands[0]


def test_native_tool_artifact_collection_retries_transport_only(tmp_path: Path) -> None:
    execution_id = "exec-1"
    digest = hashlib.sha256(b"printf ok").hexdigest()
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    files = {
        "tool-bridge.jsonl": (json.dumps(bridge[0]) + "\n").encode(),
        "cgroup-resource-exec-1.json": (json.dumps(cgroup) + "\n").encode(),
        "clause-telemetry-exec-1.json": (json.dumps(clause) + "\n").encode(),
    }
    stdout = "".join(
        "__CLAWBOX_ARTIFACT_V1__" + name + "\n"
        + base64.b64encode(raw).decode() + "\n"
        for name, raw in files.items()
    ) + "__CLAWBOX_ARTIFACT_END__\n"
    executor = _TransientRuntimeExecutor(stdout)
    collection = collect_and_validate_native_tool_artifacts(
        runtime_executor=executor,
        ssh=NativeSSHConfig(
            target="clawbox@127.0.0.1:2222", identity_private_key="private",
            host_public_key="ssh-ed25519 public", host_key_alias="tool-a",
        ),
        session_id="session-a", output_dir=tmp_path,
        policy_records=_policy(execution_id, digest),
    )

    assert executor.attempts == 2
    assert collection.validation["artifact_collection_attempts"] == 2


def test_native_tool_artifact_collection_fails_closed_on_missing_cgroup(tmp_path: Path) -> None:
    execution_id = "exec-1"
    digest = hashlib.sha256(b"printf ok").hexdigest()
    bridge, _cgroup, clause = _artifacts(execution_id, digest)
    files = {
        "tool-bridge.jsonl": (json.dumps(bridge[0]) + "\n").encode(),
        "clause-telemetry-exec-1.json": (json.dumps(clause) + "\n").encode(),
    }
    stdout = "".join(
        "__CLAWBOX_ARTIFACT_V1__" + name + "\n"
        + base64.b64encode(raw).decode() + "\n"
        for name, raw in files.items()
    ) + "__CLAWBOX_ARTIFACT_END__\n"
    with pytest.raises(ValueError, match="cgroup"):
        collect_and_validate_native_tool_artifacts(
            runtime_executor=_RuntimeExecutor(stdout),
            ssh=NativeSSHConfig("executor@tool.example:2222", "private", "public"),
            session_id="session-a", output_dir=tmp_path,
            policy_records=_policy(execution_id, digest),
        )
    failure_root = tmp_path / "tool-artifacts" / "session-a"
    assert (failure_root / "tool-bridge.jsonl").read_bytes() == files["tool-bridge.jsonl"]
    validation = json.loads((failure_root / "validation.json").read_text())
    assert validation["valid"] is False
    assert "cgroup" in validation["error"]


def test_native_tool_join_rejects_non_finite_cgroup_measurements() -> None:
    execution_id = "exec-1"
    digest = hashlib.sha256(b"printf ok").hexdigest()
    bridge, cgroup, clause = _artifacts(execution_id, digest)
    cgroup["cpu_utilization_avg_cores"] = float("nan")
    with pytest.raises(ValueError, match="cpu_utilization_avg_cores"):
        validate_native_tool_join(
            bridge_records=bridge,
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest),
        )


def test_native_measurements_enrich_admission_and_prediction_evidence() -> None:
    records = [{
        "session_id": "session-a", "execution_id": "exec-1",
        "canonical_prediction_key": "printf ok",
        "prediction_source": "runtime_clawtune_immutable_kb",
        "fallback_level": "exact_command",
        "predicted_guest_memory_p90_mib": 3.0,
        "admission_prediction_target": "host_cgroup_memory_current_increment",
        "predicted_incremental_memory_mib": 4.0,
        "admitted_reservation_mib": 4,
        "admission_blocked_seconds": 0.25,
        "actual_host_execution_increment_mib": 3.0,
    }]
    _bridge, cgroup, _clause = _artifacts(
        "exec-1", hashlib.sha256(b"printf ok").hexdigest()
    )
    cgroup["memory_rss_peak_bytes"] = 2 * 1024 * 1024
    cgroup["ts_end"] = 2.5

    enriched = enrich_tool_execution_observations(records, {"exec-1": cgroup})
    assert enriched[0]["actual_measured_memory_mib"] == 2.0
    assert enriched[0]["actual_execution_duration_seconds"] == 1.5
    assert enriched[0]["prediction_error_mib"] == 1.0
    assert enriched[0]["prediction_underestimate_mib"] == 0.0
    assert enriched[0]["telemetry_eligible_for_kb"] is None
    enriched.append({
        **enriched[0], "execution_id": "maintenance-1",
        "execution_scope": "backend-maintenance",
        "prediction_source": "backend_maintenance_static",
        "fallback_level": "not_applicable",
    })
    summary = summarize_tool_execution_observations(enriched)
    assert summary["tool_execution_observation_count"] == 1
    assert summary["prediction_fallback_rate"] == 0.0
    assert summary["prediction_error_p90_mib"] == 1.0
    assert summary["prediction_coverage_fraction"] == 1.0
    assert summary["prediction_exceedance_rate"] == 0.0
    assert summary["prediction_relative_absolute_error_mean"] == 0.5
    assert summary["reservation_over_actual_mean_ratio"] == pytest.approx(4 / 3)
    assert summary["prediction_p90_pinball_loss_mean_mib"] == pytest.approx(0.1)
    assert summary["host_increment_prediction_coverage_fraction"] == 1.0
    assert summary["host_increment_prediction_exceedance_rate"] == 0.0
    assert summary["prediction_source_distribution"] == {
        "runtime_clawtune_immutable_kb": 1,
    }
    assert summary["prediction_fallback_level_distribution"] == {"exact_command": 1}
    assert summary["telemetry_invalid_count"] == 0


def test_native_measurement_enrichment_requires_exact_execution_set() -> None:
    with pytest.raises(RuntimeError, match="identity mismatch"):
        enrich_tool_execution_observations(
            [{"execution_id": "exec-1"}],
            {"exec-2": {
                "memory_rss_peak_bytes": 1, "ts_start": 0, "ts_end": 1,
                "cpu_utilization_avg_cores": 1,
            }},
        )

def test_p50_errors_compare_guest_increment_not_rss_or_host():
    records = [{'session_id': 's', 'execution_id': 'exec-1', 'prediction_quantile': .5, 'predicted_guest_extra_memory_mib': 3,
        'admission_prediction_target': 'environment_memory_peak_minus_baseline',
        'predicted_incremental_memory_mib': 3, 'actual_host_execution_increment_mib': 99}]
    _, cgroup, _ = _artifacts('exec-1', hashlib.sha256(b'true').hexdigest())
    cgroup.update(memory_measurement='guest_memtotal_minus_memavailable', memory_eligible=True,
                  memory_extra_peak_bytes=2*1024**2, memory_rss_peak_bytes=50*1024**2)
    result = enrich_tool_execution_observations(records, {'exec-1': cgroup})[0]
    assert result['actual_extra_memory_mib'] == 2
    assert result['p50_extra_memory_error_mib'] == 1
    assert 'host_increment_prediction_error_mib' not in result
    assert summarize_tool_execution_observations(records)['p50_extra_memory_mae_mib'] == 1
