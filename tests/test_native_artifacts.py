from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from clawbox.experiments.native_artifacts import (
    collect_and_validate_native_tool_artifacts,
    validate_native_tool_join,
)
from clawbox.experiments.worker import (
    enrich_tool_execution_observations,
    summarize_tool_execution_observations,
)
from clawbox.experiments.openclaw_driver import NativeSSHConfig
from clawbox.replay.lifecycle import CommandResult


def _policy(execution_id: str, digest: str) -> list[dict]:
    return [{
        "request": {
            "session_id": "session-a", "execution_id": execution_id,
            "command_sha256": digest, "operation": "exec",
        },
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
        "telemetry_loss_total": {"total": 0},
        "calls": [{"tool_call_id": execution_id, "eligible_for_kb": True}],
    }
    return bridge, cgroup, clause


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

    with pytest.raises(ValueError, match="wrong session"):
        validate_native_tool_join(
            bridge_records=[{**bridge[0], "task_id": "session-b"}],
            cgroup_artifacts={execution_id: cgroup},
            clause_artifacts={execution_id: clause},
            policy_records=_policy(execution_id, digest),
            expected_session_id="session-a",
        )


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


def test_native_tool_artifact_collection_copies_raw_files_and_validates(tmp_path: Path) -> None:
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
    assert collection.validation["runtime_trace_execution_count"] == 1
    assert (collection.root / "tool-bridge.jsonl").read_bytes() == files["tool-bridge.jsonl"]
    assert (collection.root / "validation.json").is_file()
    assert "-p 2222" in executor.commands[0]
    assert "__CBX_EXEC_1__" not in executor.commands[0]


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
        "predicted_incremental_memory_mib": 3.0,
        "admitted_reservation_mib": 3,
        "admission_blocked_seconds": 0.25,
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
    assert enriched[0]["telemetry_eligible_for_kb"] is True
    summary = summarize_tool_execution_observations(enriched)
    assert summary["prediction_fallback_rate"] == 0.0
    assert summary["prediction_error_p90_mib"] == 1.0
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
