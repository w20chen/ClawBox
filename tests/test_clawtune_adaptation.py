from datetime import datetime, timedelta, timezone

import pytest

from clawbox.tuning.clawtune import observation_to_completed_call
from clawbox.tuning.schema import ToolObservation
from clawbox.tuning.validate import ObservationValidator


@pytest.mark.parametrize("status,exit_code,partial", [("error", 1, False), ("timeout", 124, True)])
def test_partial_and_failed_calls_keep_available_labels(status, exit_code, partial):
    from tool_resource.runtime_kb import _target_values, training_target_provenance

    start = datetime(2026, 10, 25, tzinfo=timezone.utc)
    row = ToolObservation(execution_id="test", tool_name="exec", command="true",
        start_time=start, end_time=start + timedelta(milliseconds=10),
        duration_sec=.01, complete=True, exit_code=exit_code,
        status_code=status, collection_quality="degraded", cpu_time_sec=.002,
        memory_measurement="guest_memtotal_minus_memavailable",
        confirmed_timeout=partial, timeout_duration_lower_bound_ms=10 if partial else None)
    assert ObservationValidator().validate(row).valid
    call = observation_to_completed_call(row, "test/repo")
    assert call.censored is partial
    assert _target_values(call)["cpu_time_seconds"] == .002
    assert _target_values(call)["memory_extra_peak_bytes"] == 0
    assert training_target_provenance(call)["memory_extra_peak_bytes"] == "imputed_zero"
    if partial:
        assert training_target_provenance(call)["workload_latency_ms"] == "timeout_lower_bound"


def test_actual_bootstrap_v2_predicts_unseen_commands(clawtune_shell_parser):
    from clawbox.clawtune_integration import seed_directory
    from clawbox.experiments.prediction import ColdStartPredictionProvider, command_sha256

    seed = seed_directory()
    assert seed.name == "bootstrap-v2"
    provider = ColdStartPredictionProvider(seed, repository="clawbox/unseen-adaptation-test")
    for command in ("true", "python -m pytest -q", "unseen_binary --unseen-option"):
        result = provider.resolve(command, {"raw_command_sha256": command_sha256(command)})
        assert result["prediction_quantile"] == .5
        assert result["predicted_incremental_memory_mib"] >= 0
        assert result["predicted_guest_extra_memory_mib"] is None
        assert result["call_prediction"]["memory_measurement"] == "cgroup_v2_environment_union_v1"
        assert result["host_calibration"]["method"] == "identity_estimate"


def test_seed_environment_selects_same_state_and_host_prior(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from clawbox.clawtune_integration import seed_directory

    seed = seed_directory()
    monkeypatch.setenv("CLAWTUNE_KB_SEED", str(seed))
    path = Path(__file__).resolve().parents[1] / "scripts/initialize-clawtune-state.py"
    spec = importlib.util.spec_from_file_location("state_init", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.initialize(tmp_path / "state", "test")
    module.initialize(tmp_path / "state", "test")
    assert (tmp_path / "state/runtime-tool-resource-kb.json").read_bytes() == (
        seed / "runtime-tool-resource-kb.json").read_bytes()
