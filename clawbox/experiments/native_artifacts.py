"""Fail-closed collection and joining of native Tool-VM artifacts.

The Runtime owns the Agent process, but the Tool VM owns the authoritative
bridge, cgroup, and eBPF records.  This module copies those records over the
same native SSH data path in an explicit non-Agent phase and validates the
exact execution-ID join before a managed result can be accepted.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import json
import math
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clawbox.replay.lifecycle import CommandResult
from clawbox.tuning.schema import PmuProfile

from .openclaw_driver import NativeSSHConfig, split_native_ssh_target


_ARTIFACT_MARKER = "__CLAWBOX_ARTIFACT_V1__"
_ARTIFACT_GZIP_MARKER = "__CLAWBOX_ARTIFACT_GZIP_V1__"
_ARTIFACT_END = "__CLAWBOX_ARTIFACT_END__"
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_SAFE_EXECUTION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_EXECUTION_ENVELOPE_PREFIX = "__CBX_EXEC_1__"
_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_TOOL_RESOURCE_ROOT = "/var/lib/clawtune/artifacts/tool-resource"
_ARTIFACT_EXPORT = "/run/clawbox-ssh/artifact-export"
_ARTIFACT_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class NativeToolArtifactCollection:
    """Copied Tool records and the strict validation verdict for one session."""

    root: Path
    bridge_records: tuple[dict[str, Any], ...]
    cgroup_artifacts: dict[str, dict[str, Any]]
    clause_artifacts: dict[str, dict[str, Any]]
    validation: dict[str, Any]
    pmu_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)


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
        f"printf '%s%s\\n' {_ARTIFACT_GZIP_MARKER} \"$name\"; "
        "gzip -c \"$path\" | base64 | tr -d '\\n'; printf '\\n'; "
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
        "-o", f"HostKeyAlias={ssh.host_key_alias}",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        "-p", str(port), f"{user}@{host_argument}", remote_command,
    ]
    return shlex.join(args)


def _collect_large_artifact_stream(runtime_executor: Any, ssh: NativeSSHConfig,
                                   identity_file: str, known_hosts_file: str) -> CommandResult:
    """Read a fixed export in chunks below the Tool bridge's stdout limit."""
    def execute(remote: str) -> CommandResult:
        result = runtime_executor.execute(
            _direct_ssh_command(ssh, identity_file, known_hosts_file, remote), 60,
        )
        if result.exit_code:
            raise RuntimeError(f"Tool artifact export failed: {result.stderr[-2000:]}")
        return result

    # Capture once. Reading chunks itself creates maintenance telemetry, which
    # must not change the file being transferred between successive reads.
    size_result = execute(
        f"( {_collection_command()} ) > {_ARTIFACT_EXPORT} && "
        f"wc -c < {_ARTIFACT_EXPORT}"
    )
    size = int(size_result.stdout.strip())
    chunks = []
    for offset in range(0, size, _ARTIFACT_CHUNK_BYTES):
        result = execute(
            f"dd if={_ARTIFACT_EXPORT} bs={_ARTIFACT_CHUNK_BYTES} "
            f"skip={offset // _ARTIFACT_CHUNK_BYTES} count=1 2>/dev/null"
        )
        expected_size = min(_ARTIFACT_CHUNK_BYTES, size - offset)
        if len(result.stdout.encode("ascii")) != expected_size:
            raise RuntimeError(f"Tool artifact export chunk at {offset} is incomplete")
        chunks.append(result.stdout)
    return CommandResult(0, "".join(chunks), "", 0.0)


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
        compressed = line.startswith(_ARTIFACT_GZIP_MARKER)
        marker = _ARTIFACT_GZIP_MARKER if compressed else _ARTIFACT_MARKER
        if not line.startswith(marker):
            raise ValueError("Tool artifact stream has an unexpected output line")
        name = line.removeprefix(marker)
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
            if compressed:
                payload = gzip.decompress(payload)
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
    """Load Runtime ClawTune tool span ends from the copied trace files.

    ClawTune can persist the same logical span through its direct writer and
    its sidecar-normalized writer.  Collapse only those mirrored records that
    have the same trace/span/execution identity and agree on outcome; distinct
    spans reusing one execution ID remain visible to the strict join check.
    """
    spans: list[dict[str, Any]] = []
    for rendered_path in paths:
        path = Path(rendered_path)
        if "tool-resource" in path.parts or path.suffix != ".jsonl":
            continue
        if not path.is_file():
            raise ValueError(f"Runtime ClawTune trace is missing: {path}")
        starts: dict[tuple, dict[str, Any]] = {}
        for record in _jsonl(path.read_bytes(), str(path)):
            key = tuple(record.get(field) for field in (
                "gateway_id", "runtime_id", "trace_id", "span_id", "session_id",
            ))
            if record.get("record_type") == "span_start" and record.get("kind") == "tool":
                starts[key] = record
                continue
            if record.get("record_type") != "span_end" or record.get("kind") != "tool":
                continue
            execution = record.get("execution")
            if not isinstance(execution, dict):
                continue
            start = starts.get(key, {})
            args = (start.get("input") or {}).get("requested_args")
            if isinstance(args, dict) and isinstance(args.get("command"), str):
                execution = {"requested_command": args["command"], **execution}
                record = {**record, "execution": execution}
            explicit_id = str(execution.get("execution_id") or "")
            envelope_id = _runtime_envelope_execution_id(
                execution.get("effective_command")
            )
            if explicit_id and envelope_id and explicit_id != envelope_id:
                raise ValueError(
                    "Runtime ClawTune structured/envelope execution identity mismatch"
                )
            if not explicit_id and envelope_id:
                execution = {
                    **execution,
                    "execution_id": envelope_id,
                    "execution_id_source": "effective_command_envelope_recovery",
                }
                record = {**record, "execution": execution}
                explicit_id = envelope_id
            if not explicit_id:
                continue
            spans.append(record)
    by_execution: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        execution_id = str(span["execution"]["execution_id"])
        candidates = by_execution.setdefault(execution_id, [])
        matching = next((item for item in candidates if (
            item.get("trace_id"), item.get("span_id"), item.get("session_id"),
            item.get("name"), (item.get("status") or {}).get("code"),
            (item.get("output") or {}).get("exit_code"),
        ) == (
            span.get("trace_id"), span.get("span_id"), span.get("session_id"),
            span.get("name"), (span.get("status") or {}).get("code"),
            (span.get("output") or {}).get("exit_code"),
        )), None)
        if matching is None:
            candidates.append(span)
            continue
        current_execution = matching.get("execution") or {}
        incoming_execution = span.get("execution") or {}
        current_detail = sum(current_execution.get(key) is not None for key in (
            "requested_command", "effective_command", "payload_command",
        ))
        incoming_detail = sum(incoming_execution.get(key) is not None for key in (
            "requested_command", "effective_command", "payload_command",
        ))
        if incoming_detail > current_detail:
            preferred, fallback = span, matching
        else:
            preferred, fallback = matching, span
        merged = dict(preferred)
        for key, value in fallback.items():
            if merged.get(key) is None and value is not None:
                merged[key] = value
        merged_execution = dict(preferred.get("execution") or {})
        for key, value in (fallback.get("execution") or {}).items():
            if merged_execution.get(key) is None and value is not None:
                merged_execution[key] = value
        merged["execution"] = merged_execution
        candidates[candidates.index(matching)] = merged
    return [span for candidates in by_execution.values() for span in candidates]


def _runtime_envelope_execution_id(effective_command: Any) -> str | None:
    """Recover an exact ID from ClawTune's own first-line exec envelope.

    A Runtime span can retain the wrapped command while its structured
    correlation field is null (observed under high scheduling contention).
    This is not a fuzzy command join: only the versioned ClawTune envelope at
    byte zero is accepted, and malformed or unsafe identities remain absent so
    the normal exact-ID validation fails closed.
    """
    if not isinstance(effective_command, str):
        return None
    header, separator, _payload = effective_command.partition("\n")
    if not separator or not header.startswith(_EXECUTION_ENVELOPE_PREFIX):
        return None
    encoded = header.removeprefix(_EXECUTION_ENVELOPE_PREFIX)
    if encoded.startswith("b64:"):
        value = encoded.removeprefix("b64:")
        try:
            metadata = json.loads(base64.urlsafe_b64decode(
                value + "=" * (-len(value) % 4)
            ))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict) or metadata.get("v") != 1:
            return None
        execution_id = str(metadata.get("execution_id") or "")
    elif encoded.startswith("{"):
        try:
            metadata = json.loads(encoded)
        except json.JSONDecodeError:
            return None
        if not isinstance(metadata, dict) or metadata.get("v") != 1:
            return None
        execution_id = str(metadata.get("execution_id") or "")
    else:
        execution_id = encoded.strip()
    return execution_id if _SAFE_EXECUTION_ID.fullmatch(execution_id) else None


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
    if (payload.get("provenance") or {}).get("collector") != "ebpf_ebpf":
        raise ValueError(f"{execution_id}: clause telemetry is not from eBPF")
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
    # A task cannot accumulate more CPU than all guest CPUs since boot.
    # This catches incompatible BCC kernel layouts that otherwise look like
    # healthy, loss-free collections while reading pointers as CPU counters.
    provenance = call.get("provenance") or {}
    uptime_ns = provenance.get("call_ended_monotonic_ns")
    cores = provenance.get("quota_cores")
    if isinstance(uptime_ns, (int, float)) and isinstance(cores, (int, float)):
        for clause in call.get("clauses") or []:
            cpu_ns = clause.get("cpu_ns_cumulative")
            if isinstance(cpu_ns, (int, float)) and not 0 <= cpu_ns <= uptime_ns * cores:
                raise ValueError(
                    f"{execution_id}: impossible eBPF CPU counter; check guest kernel headers"
                )


def _is_preflight_rejection(span: dict[str, Any]) -> bool:
    details = ((span.get("output") or {}).get("result") or {}).get("details")
    execution = span.get("execution") or {}
    return (
        span.get("name") == "exec"
        and isinstance(details, dict)
        and details.get("status") == "error"
        and str(details.get("error") or "").startswith("exec preflight:")
        and not execution.get("payload_pid")
        and not execution.get("cgroup_path")
    )


def validate_native_tool_join(
    *, bridge_records: list[dict[str, Any]],
    cgroup_artifacts: dict[str, dict[str, Any]],
    clause_artifacts: dict[str, dict[str, Any]],
    policy_records: list[dict[str, Any]],
    runtime_span_records: list[dict[str, Any]] | None = None,
    expected_session_id: str | None = None,
) -> dict[str, Any]:
    """Validate the policy -> bridge -> cgroup/eBPF exact-ID join."""
    expected: dict[str, dict[str, Any]] = {}
    for item in policy_records:
        request = item.get("request")
        if not isinstance(request, dict):
            raise ValueError("policy record is missing request metadata")
        if expected_session_id is not None and request.get("session_id") != expected_session_id:
            raise ValueError(
                f"policy record is routed to the wrong session: "
                f"expected={expected_session_id!r} got={request.get('session_id')!r}"
            )
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
    preflight_rejected: list[str] = []
    if runtime_span_records is not None:
        for span in runtime_span_records:
            if (expected_session_id is not None
                    and span.get("session_id") != expected_session_id):
                raise ValueError(
                    f"Runtime trace is routed to the wrong session: "
                    f"expected={expected_session_id!r} got={span.get('session_id')!r}"
                )
            execution = span.get("execution")
            execution_id = str(
                execution.get("execution_id") if isinstance(execution, dict) else ""
            )
            runtime_by_id.setdefault(execution_id, []).append(span)
        # OpenClaw assigns an ID before its exec preflight. A rejected call
        # never reaches SSH admission or the Tool VM, so it has no eBPF span.
        # Keep the native records and report these attempts separately.
        for execution_id, spans in list(runtime_by_id.items()):
            if execution_id in expected:
                continue
            if all(_is_preflight_rejection(span) for span in spans):
                preflight_rejected.append(execution_id)
                del runtime_by_id[execution_id]
        trace_expected = {
            execution_id for execution_id, request in expected.items()
            if request.get("runtime_trace_expected", True) is not False
        }
        if set(runtime_by_id) != trace_expected:
            missing = sorted(trace_expected - set(runtime_by_id))
            extra = sorted(set(runtime_by_id) - trace_expected)
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
        if (expected_session_id is not None
                and bridge.get("task_id") != expected_session_id):
            raise ValueError(
                f"{execution_id}: Tool bridge is routed to the wrong session: "
                f"expected={expected_session_id!r} got={bridge.get('task_id')!r}"
            )
        if bridge.get("command_sha256") != request.get("command_sha256"):
            raise ValueError(f"{execution_id}: policy and bridge command digests differ")
        effective_digest = request.get("effective_command_sha256")
        if (effective_digest is not None
                and bridge.get("effective_command_sha256") != effective_digest):
            raise ValueError(
                f"{execution_id}: policy and bridge effective command digests differ"
            )
        if (runtime_span_records is not None
                and request.get("runtime_trace_expected", True) is not False):
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
        "preflight_rejected_execution_ids": sorted(preflight_rejected),
        "preflight_rejected_execution_count": len(preflight_rejected),
        "runtime_envelope_execution_count": len(runtime_records),
        "runtime_trace_execution_count": (
            len(runtime_by_id) if runtime_span_records is not None else None
        ),
        "runtime_trace_recovered_execution_count": (
            sum(
                (span.get("execution") or {}).get("execution_id_source")
                == "effective_command_envelope_recovery"
                for spans in runtime_by_id.values() for span in spans
            ) if runtime_span_records is not None else None
        ),
        "runtime_trace_expected_execution_count": (
            sum(request.get("runtime_trace_expected", True) is not False
                for request in expected.values())
            if runtime_span_records is not None else None
        ),
        "runtime_trace_exempt_execution_count": (
            sum(request.get("runtime_trace_expected", True) is False
                for request in expected.values())
            if runtime_span_records is not None else None
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
    result: CommandResult | None = None
    collection_attempt = 0
    max_collection_attempts = 8
    for collection_attempt in range(1, max_collection_attempts + 1):
        try:
            candidate: CommandResult = runtime_executor.execute(command, 60)
            if (candidate.exit_code == 0 and _ARTIFACT_END not in candidate.stdout
                    and len(candidate.stdout) >= _ARTIFACT_CHUNK_BYTES):
                candidate = _collect_large_artifact_stream(
                    runtime_executor, ssh, identity_file, known_hosts_file,
                )
        except Exception as exc:
            if collection_attempt == max_collection_attempts:
                raise RuntimeError(
                    "Tool artifact collection transport failed after "
                    f"{max_collection_attempts} attempts"
                ) from exc
            time.sleep(min(0.1 * (2 ** (collection_attempt - 1)), 1.0))
            continue
        if candidate.exit_code != 0:
            raise RuntimeError(
                f"Tool artifact collection failed with exit {candidate.exit_code}: "
                f"{candidate.stderr[-2000:]}"
            )
        if "__CLAWBOX_ARTIFACT_END__" not in candidate.stdout:
            diagnostics = output_dir / "tool-artifacts" / session_id
            diagnostics.mkdir(parents=True, exist_ok=True)
            (diagnostics / "collection-incomplete.stdout").write_text(candidate.stdout)
            (diagnostics / "collection-incomplete.stderr").write_text(candidate.stderr)
            if collection_attempt == max_collection_attempts:
                raise RuntimeError(
                    "Tool artifact collection produced no complete framed stream "
                    f"after {max_collection_attempts} attempts"
                )
            time.sleep(min(0.1 * (2 ** (collection_attempt - 1)), 1.0))
            continue
        result = candidate
        break
    if result is None:  # Defensive: the final retry branch above always raises.
        raise RuntimeError("Tool artifact collection produced no result")
    raw_files = _decode_framed_artifacts(result.stdout)
    root = output_dir / "tool-artifacts" / session_id
    root.mkdir(parents=True, exist_ok=True)
    for name, raw in raw_files.items():
        target = root / name
        temporary = target.with_name(target.name + ".next")
        temporary.write_bytes(raw)
        temporary.replace(target)
    bridge_raw = raw_files.pop("tool-bridge.jsonl", None)
    if bridge_raw is None:
        raise ValueError("Tool artifact collection is missing tool-bridge.jsonl")
    bridge_records = _jsonl(bridge_raw, "tool-bridge.jsonl")
    cgroup_artifacts: dict[str, dict[str, Any]] = {}
    clause_artifacts: dict[str, dict[str, Any]] = {}
    pmu_artifacts: dict[str, dict[str, Any]] = {}
    pmu_artifact_errors: list[str] = []
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
        elif name.startswith("pmu-profile-") and name.endswith(".json"):
            try:
                payload = _json_object(raw, name)
                execution_id = str(payload.get("execution_id") or "")
                PmuProfile.model_validate(payload)
            except (TypeError, ValueError):
                pmu_artifact_errors.append(name)
                continue
            if execution_id in pmu_artifacts:
                raise ValueError(f"duplicate PMU artifact identity: {execution_id}")
            pmu_artifacts[execution_id] = payload

    runtime_span_records = (
        _runtime_spans(runtime_trace_paths)
        if runtime_trace_paths is not None else None
    )
    # The bridge embeds PMU in new cgroup artifacts; accept the standalone
    # artifact as an exact-ID compatibility path for partially rolled images.
    for execution_id, pmu in pmu_artifacts.items():
        cgroup = cgroup_artifacts.get(execution_id)
        if cgroup is not None and not isinstance(cgroup.get("pmu"), dict):
            cgroup["pmu"] = pmu
    try:
        validation = validate_native_tool_join(
            bridge_records=bridge_records, cgroup_artifacts=cgroup_artifacts,
            clause_artifacts=clause_artifacts, policy_records=policy_records,
            runtime_span_records=runtime_span_records,
            expected_session_id=session_id,
        )
    except Exception as exc:
        (root / "validation.json").write_text(json.dumps({
            "valid": False,
            "artifact_collection_attempts": collection_attempt,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        raise
    validation["artifact_collection_attempts"] = collection_attempt
    validation["pmu"] = {
        "valid_artifacts": len(pmu_artifacts),
        "invalid_artifacts": pmu_artifact_errors,
    }
    validation_path = root / "validation.json"
    validation_path.write_text(
        json.dumps(validation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return NativeToolArtifactCollection(
        root=root, bridge_records=tuple(bridge_records),
        cgroup_artifacts=cgroup_artifacts, clause_artifacts=clause_artifacts,
        validation=validation,
        pmu_artifacts=pmu_artifacts,
    )
