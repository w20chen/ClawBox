from __future__ import annotations

import hashlib
import json
import pytest

from clawbox.experiments.placement import PLACEMENTS, _pmu_rate, _predict_history, _pressure, report


def test_pmu_rate_requires_matching_unmultiplexed_inherited_counter() -> None:
    pmu = {
        "execution_id": "one", "source": "perf_event_open", "mode": "counting",
        "scope": "task-inherit-enable-on-exec",
        "llc_semantics_confirmed": True, "collector_errors": [],
        "coverage": {"status": "reliable", "multiplexed": False,
                     "root_and_future_descendants": True, "kernel_included": True},
        "events": {"llc_read_accesses": {
            "semantics": "PERF_COUNT_HW_CACHE_LL:READ:ACCESS",
            "raw_count": 4_000_000, "time_running_ns": 2_000_000_000,
            "time_enabled_ns": 2_000_000_000, "running_ratio": 1.0}},
        "derived": {"llc_read_accesses_per_cpu_second": 2_000_000},
    }
    assert _pmu_rate(pmu, "one") == (2_000_000, None)
    assert _pmu_rate(pmu, "other")[0] is None
    pmu["events"]["llc_read_accesses"]["time_enabled_ns"] *= 2
    assert _pmu_rate(pmu, "one")[1] == "pmu_counter_time_invalid"


def test_history_excludes_same_task_and_future_observations() -> None:
    base = {"repo": "r", "command": "python -m pytest tests/a.py",
            "command_prefix": "python -m pytest tests/a.py", "executable": "python",
            "tool_category": "python -m pytest tests/a.py", "stack": ("arm", "launcher", "swe")}
    candidate = {**base, "task_id": "test", "time_ns": 10}
    training = [{**base, "task_id": f"train-{i}", "execution_id": f"exec-{i}",
                 "time_ns": i, "rate": float(i)}
                for i in (1, 2, 3)]
    training += [{**base, "task_id": "test", "execution_id": "same-task", "time_ns": 4, "rate": 1000.0},
                 {**base, "task_id": "future", "execution_id": "future", "time_ns": 11, "rate": 1000.0}]
    prediction = _predict_history(training, candidate)
    assert prediction["value"] == 2.0
    assert prediction["evidence_count"] == 3


def test_three_placements_keep_equal_pair_sizes() -> None:
    rates = {"A": 8., "B": 7., "C": 2., "D": 1.}
    assert all(sorted(sum((list(group) for group in placement), [])) == list("ABCD")
               for placement in PLACEMENTS.values())
    assert _pressure("P1", rates) == 15.
    assert _pressure("P2", rates) == 10.


def test_report_keeps_frozen_selection_even_when_other_placement_wins(tmp_path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"schema": "placement_plan_v1", "groups": [{
        "id": "G1", "selected": "P1", "round_robin": "P2", "name_selected": "P3"}]}))
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    topology = tmp_path / "topology.json"
    topology.write_text(json.dumps({"selection": {"cores": [[0, 2], [8, 10]],
                                                   "pool": [0, 2, 8, 10]}}))
    rounds = []
    for turn in range(5):
        for placement, duration in (("P1", 10.), ("P2", 8.), ("P3", 9.), ("linux", 11.)):
            mapping = (dict(zip(PLACEMENTS[placement][0], (0, 2))) |
                       dict(zip(PLACEMENTS[placement][1], (8, 10)))) if placement != "linux" else {}
            rounds.append({"plan_sha256": digest, "group_id": "G1", "round": turn,
                           "placement": placement, "status": "verified",
                           "tools": {letter: {"started_s": 0., "ended_s": duration,
                                               "checkpoint_verified": True,
                                               "mapping_verified": True,
                                               "allowed_host_cpus": [mapping[letter]] if mapping else [0, 2, 8, 10],
                                               "effective_mems": [0]} for letter in "ABCD"}})
    raw = tmp_path / "rounds.json"; raw.write_text(json.dumps(rounds))
    result = report(plan, raw, topology, tmp_path / "report.json")["groups"][0]
    assert result["selected"] == "P1"
    assert result["posthoc_fastest_reference"] == "P2"
    assert result["placements"]["P1"]["throughput_calls_per_s"] == .4
    rounds[0]["tools"]["A"]["allowed_host_cpus"] = [4]
    raw.write_text(json.dumps(rounds))
    with pytest.raises(ValueError, match="physical cluster"):
        report(plan, raw, topology, tmp_path / "bad-report.json")
