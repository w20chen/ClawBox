"""Fail-closed collection and joining of native Tool-VM artifacts.

The Runtime owns the Agent process, but the Tool VM owns the authoritative
bridge, cgroup, and eBPF records.  This module copies those records over the
same native SSH data path in an explicit non-Agent phase and validates the
exact execution-ID join before a managed result can be accepted.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clawbox.replay.lifecycle import CommandResult

from .openclaw_driver import NativeSSHConfig, split_native_ssh_target


_ARTIFACT_MARKER = "__CLAWBOX_ARTIFACT_V1__"
_ARTIFACT_END = "__CLAWBOX_ARTIFACT_END__"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_TOOL_RESOURCE_ROOT = "/var/lib/clawtune/artifacts/tool-resource"


@dataclass(frozen=True, slots=True)
class NativeToolArtifactCollection:
    """Copied Tool records and the strict validation verdict for one session."""

    root: Path
    bridge_records: tuple[dict[str, Any], ...]
    cgroup_artifacts: dict[str, dict[str, Any]]
    clause_artifacts: dict[str, dict[str, Any]]
    validation: dict[str, Any]


def _collection_command() -> str:
    """Emit a framed, base64-only stream of Tool artifacts.

    The command is deliberately setup/validation-only: it uses direct
    ``/usr/bin/ssh`` from the Runtime and is not an Agent operation.  The
    framing prevents JSONL contents from being confused with command output.
    """
    return (
        "set -eu; "
        "for path in "
        f"{_TOOL_RESOURCE_ROOT}/../tool-bridge.jsonl "
        f"{_TOOL_RESOURCE_ROOT}/*.json; do "
        "[ -f \"$path\" ] || continue; "
        "name=${path##*/}; "
        f"printf '%s%s\\n' {_ARTIFACT_MARKER} \"$name\"; "
        "base64 \"$path\" | tr -d '\\n'; printf '\\n'; "
        f"done; printf '%s\\n' {_ARTIFACT_END}"
    )


def _direct_ssh_command(ssh: NativeSSHConfig, identity_file: str,
                        known_hosts_file: str, remote_command: str) -> str:
    user, host, port = split_native_ssh_target(ssh.target)
    host_argument = f"[{host}]" if ":" in host else host
    args = [
        "/usr/bin/ssh", "-i", identity_file,
        "-o", f"UserKnownHostsFile={known_hosts_file}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostkeys=no",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-p", str(port), f"{user}@{host_argument}", remote_command,
    ]
    return shlex.join(args)


def _decode_framed_artifacts(stdout: str) -> dict[str, bytes]:
    lines = stdout.splitlines()
    result: dict[str, bytes] = {}
    index = 0
    ended = False
    while index < len(lines):
        line = lines[index]
        if line == _ARTIFACT_END:
            ended = True
            if any(item.strip() for item in lines[index + 1:]):
                raise ValueError("Tool artifact stream has data after its end marker")
            break
        if not line.startswith(_ARTIFACT_MARKER):
            raise ValueError("Tool artifact stream has an unexpected output line")
        name = line.removeprefix(_ARTIFACT_MARKER)
        if not _SAFE_FILENAME.fullmatch(name):
            raise ValueError(f"unsafe Tool artifact filename: {name!r}")
        if name in result:
            raise ValueError(f"duplicate Tool artifact: {name}")
        index += 1
        if index >= len(lines):
            raise ValueError(f"Tool artifact {name} has no payload")
        encoded = lines[index].strip()
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"Tool artifact {name} is not valid base64") from exc
        if len(payload) > _MAX_ARTIFACT_BYTES:
            raise ValueError(f"Tool artifact {name} exceeds the size limit")
        result[name] = payload
        index += 1
    if not ended:
        raise ValueError("Tool artifact stream is missing its end marker")
    return result


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _jsonl(raw: bytes, label: str) -> list[dict[str, Any]]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8 JSONL") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        record = _json_object(line.encode(), f"{label}:{line_number}")
        records.append(record)
    return records


def _runtime_spans(paths: list[str]) -> list[dict[str, Any]]:
    """Load Runtime ClawTune tool span ends from the copied trace files."""
    spans: list[dict[str, Any]] = []
    for rendered_path in paths:
        path = Path(rendered_path)
        if "tool-resource" in path.parts or path.suffix != ".jsonl":
            continue
        if not path.is_file():
            raise ValueError(f"Runtime ClawTune trace is missing: {path}")
        for record in _jsonl(path.read_bytes(), str(path)):
            if record.get("record_type") != "span_end" or record.get("kind") != "tool":
                continue
            execution = record.get("execution")
            if not isinstance(execution, dict) or not execution.get("execution_id"):
                continue
            spans.append(record)
    return spans


def _validate_cgroup(payload: dict[str, Any], execution_id: str) -> None:
    if payload.get("schema") != "cgroup_resource_v1":
        raise ValueError(f"{execution_id}: unsupported cgroup artifact schema")
    if payload.get("execution_id") != execution_id:
        raise ValueError(f"{execution_id}: cgroup artifact identity mismatch")
    if payload.get("source") != "cgroup-v2":
        raise ValueError(f"{execution_id}: cgroup artifact is not cgroup-v2")
    if payload.get("sampling_quality") != "valid":
        raise ValueError(f"{execution_id}: cgroup sampling is not valid")
    if payload.get("cgroup_setup_error") or payload.get("cgroup_read_error"):
        raise ValueError(f"{execution_id}: cgroup artifact reports a read/setup error")
    if payload.get("collector_errors"):
        raise ValueError(f"{execution_id}: cgroup artifact reports collector errors")
    for key in ("ts_start", "ts_end", "cpu_utilization_avg_cores", "memory_rss_peak_bytes"):
        value = payload.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value < 0):
            raise ValueError(f"{execution_id}: cgroup artifact has invalid {key}")
    if payload["ts_end"] < payload["ts_start"]:
        raise ValueError(f"{execution_id}: cgroup artifact end precedes start")
    if payload["cpu_utilization_avg_cores"] <= 0:
        raise ValueError(f"{execution_id}: cgroup CPU utilization is not positive")
    if payload["memory_rss_peak_bytes"] <= 0:
        raise ValueError(f"{execution_id}: cgroup peak RSS is not positive")


def _validate_clause(payload: dict[str, Any], execution_id: str) -> None:
    if payload.get("version") != 2:
        raise ValueError(f"{execution_id}: unsupported clause telemetry version")
    if payload.get("collection_validity") != "valid":
        raise ValueError(f"{execution_id}: clause collection is not valid")
    if payload.get("cleanup") != "ok":
        raise ValueError(f"{execution_id}: clause collector cleanup is not ok")
    loss = payload.get("telemetry_loss_total")
    if not isinstance(loss, dict) or loss.get("total") != 0:
        raise ValueError(f"{execution_id}: clause telemetry reports event loss")
    calls = payload.get("calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError(f"{execution_id}: clause telemetry call count is not exactly one")
    call = calls[0]
    if not isinstance(call, dict) or call.get("tool_call_id") != execution_id:
        raise ValueError(f"{execution_id}: clause telemetry identity mismatch")
    if call.get("eligible_for_kb") is not True:
        raise ValueError(f"{execution_id}: clause telemetry is not eligible for KB")


def validate_native_tool_join(
    *, bridge_records: list[dict[str, Any]],
    cgroup_artifacts: dict[str, dict[str, Any]],
    clause_artifacts: dict[str, dict[str, Any]],
    policy_records: list[dict[str, Any]],
    runtime_span_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate the policy -> bridge -> cgroup/eBPF exact-ID join."""
    expected: dict[str, dict[str, Any]] = {}
    for item in policy_records:
        request = item.get("request")
        if not isinstance(request, dict):
            raise ValueError("policy record is missing request metadata")
        execution_id = str(request.get("execution_id") or "")
        if not _SAFE_EXECUTION_ID.fullmatch(execution_id):
            raise ValueError("policy record has an invalid execution identity")
        if item.get("completion") is None:
            raise ValueError(f"policy execution is incomplete: {execution_id}")
        if (item.get("admission") or {}).get("decision") != "ADMIT":
            raise ValueError(f"policy execution was not admitted: {execution_id}")
        if (item.get("completion") or {}).get("status") != "COMPLETED":
            raise ValueError(f"policy execution did not complete: {execution_id}")
        if execution_id in expected:
            raise ValueError(f"duplicate policy execution identity: {execution_id}")
        expected[execution_id] = request
    if not expected:
        raise ValueError("native Tool join contains no completed policy executions")

    runtime_by_id: dict[str, list[dict[str, Any]]] = {}
    if runtime_span_records is not None:
        for span in runtime_span_records:
            execution = span.get("execution")
            execution_id = str(
                execution.get("execution_id") if isinstance(execution, dict) else ""
            )
            runtime_by_id.setdefault(execution_id, []).append(span)
        if set(runtime_by_id) != set(expected):
            missing = sorted(set(expected) - set(runtime_by_id))
            extra = sorted(set(runtime_by_id) - set(expected))
            raise ValueError(
                f"native Runtime trace identity mismatch; missing={missing}, extra={extra}"
            )
        if any(len(items) != 1 for items in runtime_by_id.values()):
            raise ValueError("duplicate Runtime ClawTune execution identities")

    runtime_records = [
        item for item in bridge_records
        if item.get("execution_source") == "runtime-envelope"
    ]
    by_id: dict[str, list[dict[str, Any]]] = {}
    for item in runtime_records:
        execution_id = str(item.get("execution_id") or "")
        by_id.setdefault(execution_id, []).append(item)
    if set(by_id) != set(expected):
        missing = sorted(set(expected) - set(by_id))
        extra = sorted(set(by_id) - set(expected))
        raise ValueError(f"native Tool bridge identity mismatch; missing={missing}, extra={extra}")

    for execution_id, request in expected.items():
        records = by_id[execution_id]
        if len(records) != 1:
            raise ValueError(f"{execution_id}: duplicate Tool bridge executions")
        bridge = records[0]
        if bridge.get("command_sha256") != request.get("command_sha256"):
            raise ValueError(f"{execution_id}: policy and bridge command digests differ")
        if runtime_span_records is not None:
            span_execution = runtime_by_id[execution_id][0]["execution"]
            span_digest = span_execution.get("command_digest")
            if span_digest is not None and span_digest != request.get("command_sha256"):
                raise ValueError(f"{execution_id}: policy and Runtime command digests differ")
        if bridge.get("telemetry_state") != "complete":
            raise ValueError(f"{execution_id}: Tool telemetry is not complete")
        _validate_cgroup(cgroup_artifacts.get(execution_id, {}), execution_id)
        _validate_clause(clause_artifacts.get(execution_id, {}), execution_id)

    return {
        "valid": True,
        "expected_execution_ids": sorted(expected),
        "policy_execution_count": len(expected),
        "runtime_envelope_execution_count": len(runtime_records),
        "runtime_trace_execution_count": (
            len(runtime_by_id) if runtime_span_records is not None else None
        ),
        "exact_id_join_rate": 1.0,
        "duplicate_tool_execution_count": 0,
        "telemetry_loss_total": 0,
        "wrong_session_routing": 0,
    }


def collect_and_validate_native_tool_artifacts(
    *, runtime_executor: Any, ssh: NativeSSHConfig, session_id: str,
    output_dir: Path, policy_records: list[dict[str, Any]],
    runtime_trace_paths: list[str] | None = None,
) -> NativeToolArtifactCollection:
    """Collect Tool artifacts over direct SSH and fail closed on any gap."""
    identity_file = f"/state/openclaw/{session_id}/ssh/id_ed25519"
    known_hosts_file = f"/state/openclaw/{session_id}/ssh/known_hosts"
    command = _direct_ssh_command(
        ssh, identity_file, known_hosts_file, _collection_command(),
    )
    result: CommandResult = runtime_executor.execute(command, 60)
    if result.exit_code != 0:
        raise RuntimeError(
            f"Tool artifact collection failed with exit {result.exit_code}: "
            f"{result.stderr[-2000:]}"
        )
    if "__CLAWBOX_ARTIFACT_END__" not in result.stdout:
        raise RuntimeError("Tool artifact collection produced no complete framed stream")
    raw_files = _decode_framed_artifacts(result.stdout)
    bridge_raw = raw_files.pop("tool-bridge.jsonl", None)
    if bridge_raw is None:
        raise ValueError("Tool artifact collection is missing tool-bridge.jsonl")
    bridge_records = _jsonl(bridge_raw, "tool-bridge.jsonl")
    cgroup_artifacts: dict[str, dict[str, Any]] = {}
    clause_artifacts: dict[str, dict[str, Any]] = {}
    for name, raw in raw_files.items():
        if name.startswith("cgroup-resource-") and name.endswith(".json"):
            payload = _json_object(raw, name)
            execution_id = str(payload.get("execution_id") or "")
            if execution_id in cgroup_artifacts:
                raise ValueError(f"duplicate cgroup artifact identity: {execution_id}")
            cgroup_artifacts[execution_id] = payload
        elif name.startswith("clause-telemetry-") and name.endswith(".json"):
            payload = _json_object(raw, name)
            calls = payload.get("calls") or []
            first_call = calls[0] if isinstance(calls, list) and calls else {}
            execution_id = str(
                first_call.get("tool_call_id") if isinstance(first_call, dict) else ""
            )
            if execution_id in clause_artifacts:
                raise ValueError(f"duplicate clause artifact identity: {execution_id}")
            clause_artifacts[execution_id] = payload

    runtime_span_records = (
        _runtime_spans(runtime_trace_paths)
        if runtime_trace_paths is not None else None
    )
    validation = validate_native_tool_join(
        bridge_records=bridge_records, cgroup_artifacts=cgroup_artifacts,
        clause_artifacts=clause_artifacts, policy_records=policy_records,
        runtime_span_records=runtime_span_records,
    )
    root = output_dir / "tool-artifacts" / session_id
    root.mkdir(parents=True, exist_ok=True)
    files = {"tool-bridge.jsonl": bridge_raw, **raw_files}
    for name, raw in files.items():
        target = root / name
        temporary = target.with_name(target.name + ".next")
        temporary.write_bytes(raw)
        temporary.replace(target)
    validation_path = root / "validation.json"
    validation_path.write_text(
        json.dumps(validation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return NativeToolArtifactCollection(
        root=root, bridge_records=tuple(bridge_records),
        cgroup_artifacts=cgroup_artifacts, clause_artifacts=clause_artifacts,
        validation=validation,
    )
