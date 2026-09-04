from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawbox.experiments.prediction import CommandPredictionProvider, PredictionUnavailable


def test_prediction_provider_uses_exact_runtime_command_metadata_and_freezes_hash(tmp_path: Path) -> None:
    command = "pytest -q tests/test_one.py"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({
        "generation": 7,
        "per_tool_memory": {"workloads": {"case": {"tool_invocations": [{
            "command": command,
            "command_sha256": __import__("hashlib").sha256(command.encode()).hexdigest(),
            "predicted_command_memory_p90_mib": 42.5,
            "key_kind": "exact_command",
            "fallback_path": ["repo:exact_command"],
            "evidence_count": 12,
        }]}}},
    }), encoding="utf-8")
    provider = CommandPredictionProvider(path, repository="repo-a")
    metadata = provider.manifest[provider.manifest.keys().__iter__().__next__()]
    resolved = provider.resolve(command, metadata)
    assert resolved["canonical_prediction_key"] == "pytest -q tests/test_one.py"
    assert resolved["predicted_incremental_memory_mib"] == 42.5
    assert resolved["fallback_level"] == "exact_command"
    assert provider.provenance()["sha256"] == __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def test_prediction_provider_fails_closed_for_missing_or_cross_command_metadata(tmp_path: Path) -> None:
    command = "true"
    path = tmp_path / "p90.json"
    path.write_text(json.dumps({"tool_invocations": [{
        "command": command, "predicted_command_memory_p90_mib": 1,
    }]}), encoding="utf-8")
    provider = CommandPredictionProvider(path)
    with pytest.raises(PredictionUnavailable):
        provider.resolve("false", provider.manifest[next(iter(provider.manifest))])
    with pytest.raises(PredictionUnavailable):
        provider.resolve(command, None)
