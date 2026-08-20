"""Signed native Tool-VM telemetry ingestion using pinned ClawTune code."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NATIVE_MANIFEST_SCHEMA = "clawbox.native_telemetry_manifest_v1"
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class NativeArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clause_telemetry_v2", "cgroup_resource_v1"]
    execution_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=255)
    sha256: str
    content_b64: str

    @field_validator("filename")
    @classmethod
    def _safe_filename(cls, value: str) -> str:
        if Path(value).name != value or value in {".", ".."}:
            raise ValueError("artifact filename must be a basename")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("artifact sha256 must be lowercase hex")
        return value

    def raw_bytes(self) -> bytes:
        try:
            raw = base64.b64decode(self.content_b64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError(f"{self.filename}: invalid base64") from exc
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"{self.filename}: artifact exceeds size limit")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError(f"{self.filename}: artifact digest mismatch")
        return raw

    def json_payload(self) -> dict[str, Any]:
        try:
            value = json.loads(self.raw_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{self.filename}: artifact is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{self.filename}: artifact must be a JSON object")
        return value


class NativeTelemetryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_name: Literal["clawbox.native_telemetry_manifest_v1"] = Field(
        default=NATIVE_MANIFEST_SCHEMA, alias="schema"
    )
    tenant_id: str = Field(min_length=1, max_length=128)
    repo_fingerprint: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    cell_id: str = Field(min_length=1, max_length=128)
    collector_version: str = Field(min_length=1, max_length=128)
    clawtune_revision: str
    artifacts: list[NativeArtifact] = Field(min_length=2, max_length=4096)

    @field_validator("clawtune_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        if not _REVISION.fullmatch(value):
            raise ValueError("clawtune_revision must be an exact commit")
        return value

    @model_validator(mode="after")
    def _unique_artifacts(self) -> "NativeTelemetryManifest":
        keys = [(item.kind, item.execution_id) for item in self.artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate artifact kind/execution identity")
        digests = [item.sha256 for item in self.artifacts]
        if len(digests) != len(set(digests)):
            raise ValueError("duplicate artifact digest")
        return self


def canonical_manifest(manifest: NativeTelemetryManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def manifest_digest(manifest: NativeTelemetryManifest) -> str:
    return hashlib.sha256(canonical_manifest(manifest)).hexdigest()


def sign_native_manifest(manifest: NativeTelemetryManifest, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), canonical_manifest(manifest), hashlib.sha256
    ).hexdigest()


def verify_native_manifest(
    manifest: NativeTelemetryManifest, secret: str, signature: str
) -> bool:
    return hmac.compare_digest(sign_native_manifest(manifest, secret), signature)


@dataclass(frozen=True)
class NativeProjection:
    clause_snapshot: dict[str, Any]
    runtime_snapshot: dict[str, Any]
    source_digest: str
    artifact_count: int
    execution_ids: tuple[str, ...]
    evidence: dict[str, Any]


def _clawtune_api():
    candidates = [
        os.getenv("CLAWTUNE_SIDECAR_SRC"),
        str(Path(__file__).resolve().parents[3] / "ClawTune" / "services" / "sidecar" / "src"),
        "/opt/clawtune/services/sidecar/src",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir() and candidate not in sys.path:
            sys.path.insert(0, candidate)
    try:
        from tool_resource.runtime_kb import (  # type: ignore[import-not-found]
            ClauseResourceKB,
            CompletedCall,
            RuntimeToolResourceKB,
            ToolCallQuery,
        )
        from tool_resource.sdk import (  # type: ignore[import-not-found]
            _observations_from_call,
            _validate_artifact,
        )
    except ImportError as exc:  # pragma: no cover - production image gate
        raise RuntimeError("pinned ClawTune package is unavailable") from exc
    return (
        ClauseResourceKB,
        CompletedCall,
        RuntimeToolResourceKB,
        ToolCallQuery,
        _observations_from_call,
        _validate_artifact,
    )


def _finite_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"cgroup artifact missing {name}")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"cgroup artifact has invalid {name}")
    return result


def _positive_number(value: Any, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise ValueError(f"cgroup artifact has non-positive {name}")
    return result


def _validate_cgroup(payload: Mapping[str, Any], execution_id: str) -> None:
    if payload.get("schema") != "cgroup_resource_v1":
        raise ValueError("unsupported cgroup artifact schema")
    if payload.get("execution_id") != execution_id:
        raise ValueError("cgroup artifact execution identity mismatch")
    if payload.get("source") != "cgroup-v2":
        raise ValueError("native training requires cgroup-v2 resource scope")
    if payload.get("sampling_quality") != "valid":
        raise ValueError("cgroup artifact sampling is not valid")
    if payload.get("cgroup_setup_error") or payload.get("cgroup_read_error"):
        raise ValueError("cgroup artifact reports setup/read failure")
    if payload.get("collector_errors"):
        raise ValueError("cgroup artifact reports collector errors")
    _finite_number(payload.get("ts_start"), "ts_start")
    _finite_number(payload.get("ts_end"), "ts_end")
    _positive_number(payload.get("cpu_utilization_avg_cores"), "cpu utilization")
    _positive_number(payload.get("memory_rss_peak_bytes"), "peak RSS")


def project_native_manifest(manifest: NativeTelemetryManifest) -> NativeProjection:
    return project_native_manifests([manifest])


def project_native_manifests(
    manifests: list[NativeTelemetryManifest],
) -> NativeProjection:
    """Validate an immutable artifact set and build both native snapshots."""

    if not manifests:
        raise ValueError("native projection requires at least one manifest")
    identity = {
        (item.tenant_id, item.repo_fingerprint, item.clawtune_revision)
        for item in manifests
    }
    if len(identity) != 1:
        raise ValueError("native projection manifests cross an identity boundary")
    manifest = manifests[0]

    (
        ClauseResourceKB,
        CompletedCall,
        RuntimeToolResourceKB,
        ToolCallQuery,
        observations_from_call,
        validate_artifact,
    ) = _clawtune_api()
    clause_payloads: dict[str, dict[str, Any]] = {}
    cgroup_payloads: dict[str, dict[str, Any]] = {}
    digests: list[str] = []
    owners: dict[str, NativeTelemetryManifest] = {}
    seen_keys: set[tuple[str, str]] = set()
    for owner in manifests:
        for item in owner.artifacts:
            key = (item.kind, item.execution_id)
            if key in seen_keys:
                raise ValueError("duplicate artifact identity in immutable set")
            seen_keys.add(key)
            payload = item.json_payload()
            digests.append(item.sha256)
            if item.kind == "clause_telemetry_v2":
                validate_artifact(
                    Path(item.filename), payload,
                    expected_repo=manifest.repo_fingerprint,
                )
                if payload.get("version") != 2:
                    raise ValueError("unsupported native clause artifact version")
                loss = payload.get("telemetry_loss_total")
                if not isinstance(loss, Mapping) or loss.get("total") != 0:
                    raise ValueError("native clause artifact reports event loss")
                calls = payload.get("calls") or []
                if len(calls) != 1 or calls[0].get("tool_call_id") != item.execution_id:
                    raise ValueError("native clause artifact execution identity mismatch")
                if calls[0].get("eligible_for_kb") is not True:
                    raise ValueError("native clause call is not eligible for KB")
                clause_payloads[item.execution_id] = payload
                owners[item.execution_id] = owner
            else:
                _validate_cgroup(payload, item.execution_id)
                cgroup_payloads[item.execution_id] = payload

    if set(clause_payloads) != set(cgroup_payloads):
        raise ValueError("native clause and cgroup artifact identities are not paired")
    if not clause_payloads:
        raise ValueError("native batch contains no paired executions")

    clause_observations: list[Any] = []
    completed_calls: list[Any] = []
    evidence_rows: list[dict[str, Any]] = []
    for execution_id in sorted(clause_payloads):
        artifact = clause_payloads[execution_id]
        call = artifact["calls"][0]
        observations = observations_from_call(
            manifest.repo_fingerprint, call, require_timestamps=True
        )
        if not observations:
            raise ValueError(f"{execution_id}: native artifact has no eligible clauses")
        clause_observations.extend(observations)
        cgroup = cgroup_payloads[execution_id]
        ts_start = _finite_number(cgroup.get("ts_start"), "ts_start")
        ts_end = _finite_number(cgroup.get("ts_end"), "ts_end")
        if ts_end < ts_start:
            raise ValueError("cgroup artifact end precedes start")
        fidelity = (call.get("provenance") or {}).get(
            "source_replay_control_flow_fidelity"
        ) or {}
        exit_code = fidelity.get("replay_exit_code")
        completed_calls.append(
            CompletedCall(
                repo=manifest.repo_fingerprint,
                tool_name=str(cgroup.get("tool_name") or "exec"),
                command=str(call.get("command") or "") or None,
                ts_start=ts_start,
                ts_end=ts_end,
                censored=exit_code not in (None, 0),
                peak_cpu_cores=_positive_number(
                    cgroup.get("cpu_utilization_avg_cores"), "cpu utilization"
                ),
                peak_cpu_cores_eligible=True,
                peak_memory_mb=_positive_number(
                    cgroup.get("memory_rss_peak_bytes"), "peak RSS"
                )
                / (1024.0 * 1024.0),
                peak_memory_mb_eligible=True,
                ambient_before_mb=0.0,
            )
        )
        evidence_rows.append(
            {
                "execution_id": execution_id,
                "run_id": owners[execution_id].run_id,
                "attempt_id": owners[execution_id].attempt_id,
                "clause_count": len(observations),
            }
        )

    runtime_kb = RuntimeToolResourceKB.fit_public(completed_calls)
    for call in completed_calls:
        runtime_kb.observe_completed_call(call)
    advance_ts = max(call.ts_end for call in completed_calls) + 1e-6
    first = completed_calls[0]
    runtime_kb.query(
        ToolCallQuery(
            repo=manifest.repo_fingerprint,
            tool_name=first.tool_name,
            command=first.command,
            ts_start=advance_ts,
            ambient_before_mb=0.0,
        )
    )

    clause_kb = ClauseResourceKB.fit_public(clause_observations)
    for observation in clause_observations:
        clause_kb.observe_completed_clause(observation)
    clause_kb._advance(max(obs.ts_end for obs in clause_observations) + 1e-6)

    runtime_snapshot = runtime_kb.to_json_obj()
    clause_snapshot = clause_kb.to_json_obj()
    # The pinned native readers are the compatibility gate, not shape checks.
    RuntimeToolResourceKB.from_json_obj(runtime_snapshot)
    ClauseResourceKB.from_json_obj(clause_snapshot)
    source_digest = hashlib.sha256("\n".join(sorted(digests)).encode()).hexdigest()
    return NativeProjection(
        clause_snapshot=clause_snapshot,
        runtime_snapshot=runtime_snapshot,
        source_digest=source_digest,
        artifact_count=len(digests),
        execution_ids=tuple(sorted(clause_payloads)),
        evidence={
            "runs": sorted({item.run_id for item in manifests}),
            "executions": evidence_rows,
        },
    )
