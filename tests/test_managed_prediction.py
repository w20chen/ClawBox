from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import pytest

from clawbox.experiments.prediction import CommandPredictionProvider, PredictionUnavailable


def _trainer_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "train-p90-from-runs.py"
    spec = importlib.util.spec_from_file_location("train_p90_from_runs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prediction_provider_uses_exact_runtime_command_metadata_and_freezes_hash(tmp_path: Path) -> None:
    command = "pytest -q tests/test_one.py"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({
        "generation": 7,
        "per_tool_memory": {"workloads": {"case": {"tool_invocations": [{
            "command": command,
            "command_sha256": __import__("hashlib").sha256(command.encode()).hexdigest(),
            "predicted_command_memory_p90_mib": 42.5,
            "predicted_host_execution_increment_mib": 51.0,
            "host_mapping_source": "recording-set-a",
            "host_mapping_ratio_p90": 1.2,
            "key_kind": "exact_command",
            "fallback_path": ["repo:exact_command"],
            "evidence_count": 12,
        }]}}},
    }), encoding="utf-8")
    provider = CommandPredictionProvider(path, repository="repo-a")
    metadata = provider.manifest[provider.manifest.keys().__iter__().__next__()]
    resolved = provider.resolve(command, metadata)
    assert resolved["canonical_prediction_key"] == "pytest -q tests/test_one.py"
    assert resolved["predicted_guest_memory_p90_mib"] == 42.5
    assert resolved["predicted_incremental_memory_mib"] == 51.0
    assert resolved["admission_prediction_target"] == "host_vm_rss_execution_increment"
    assert resolved["fallback_level"] == "exact_command"
    assert provider.provenance()["sha256"] == __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def test_prediction_provider_fails_closed_for_missing_or_cross_command_metadata(tmp_path: Path) -> None:
    command = "true"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": command, "predicted_command_memory_p90_mib": 1,
        "predicted_host_execution_increment_mib": 1,
    }]}), encoding="utf-8")
    provider = CommandPredictionProvider(path)
    with pytest.raises(PredictionUnavailable):
        provider.resolve("false", provider.manifest[next(iter(provider.manifest))])
    with pytest.raises(PredictionUnavailable):
        provider.resolve(command, None)


def test_prediction_provider_rejects_uncalibrated_guest_memory(tmp_path: Path) -> None:
    path = tmp_path / "uncalibrated.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": "true", "predicted_command_memory_p90_mib": 1,
    }]}), encoding="utf-8")
    with pytest.raises(ValueError, match="calibrated positive host increment"):
        CommandPredictionProvider(path)


def test_prediction_provider_can_freeze_heldout_oracle_source(tmp_path: Path) -> None:
    path = tmp_path / "oracle.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": "true",
        "predicted_command_memory_p90_mib": 2,
        "predicted_host_execution_increment_mib": 3,
    }]}), encoding="utf-8")
    provider = CommandPredictionProvider(
        path, prediction_source="runtime_tool_oracle_heldout"
    )
    metadata = provider.manifest[next(iter(provider.manifest))]

    assert metadata["prediction_source"] == "runtime_tool_oracle_heldout"
    assert provider.resolve("true", metadata)["predicted_incremental_memory_mib"] == 3


def test_host_calibration_excludes_backend_maintenance(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"performance": {"tool_execution_observations": [
        {"execution_scope": "backend-maintenance",
         "actual_measured_memory_mib": 1, "actual_host_execution_increment_mib": 1000},
        *[
            {"execution_scope": "agent-tool", "actual_measured_memory_mib": 2,
             "actual_host_execution_increment_mib": value}
            for value in (2, 4, 6, 8, 10)
        ],
    ]}}), encoding="utf-8")

    calibration = _trainer_module()._host_increment_calibration([path])

    assert calibration["pair_count"] == 5
    assert calibration["ratio_max"] == 5
