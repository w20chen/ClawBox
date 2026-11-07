"""Metric tests use synthetic artifacts, not VM performance evidence."""
import importlib.util
import json
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "analyze_cpu_qos", Path(__file__).parents[1] / "scripts" / "analyze_cpu_qos.py")
analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analysis)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def fixture(root, outcomes=(0, 0), missing=(), expected=()):
    arm = "arm"
    write(root / "arms/arm.json", {
        "arm": {"arm_id": arm, "concurrency": len(outcomes)},
        "run_id": "test", "attempt_id": "attempt", "status": "succeeded",
        "started_at": "1970-01-01T00:01:40+00:00",
        "completed_at": "1970-01-01T00:02:10+00:00",
        "correctness": {"uninterrupted_timing": True},
    })
    events = []
    for i, code in enumerate(outcomes):
        sid, eid = f"arm-{i:04d}", f"exec-{i}"
        if i in missing:
            write(root / f"session-progress/{sid}.json", {"session_id": sid, "status": "incomplete"})
            events.append({"event": "tool_admission_queued", "session_id": sid,
                           "execution_id": eid, "execution_scope": "agent-tool",
                           "monotonic_time_ns": "1000000000"})
            continue
        write(root / f"session-progress/{sid}.json", {
            "status": "completed", "session_id": sid,
            "result": {"valid": True, "case_id": "case", "trace_reference": "frozen-trace",
                       "qos_class": "ls" if i == 0 else "be",
                       "timeline": {"offered_start_unix_s": 100,
                                    "offered_start_offset_seconds": i,
                                    "agent_execution_start": 110,
                                    "final_agent_completion": 120 + i,
                                    "validation_end": 120 + i}}})
        write(root / f"policy-control/{sid}.json", [{
            "request": {"execution_id": eid, "execution_scope": "agent-tool", "command_sha256": "fixed"},
            "completion": {"exit_code": code, "execution_started_at": 112, "execution_completed_at": 114},
            "timing": {"admission_started_monotonic_s": 1, "completion_completed_monotonic_s": 5}}])
        events.extend([
            {"event": "tool_admission_queued", "session_id": sid, "execution_id": eid,
             "execution_scope": "agent-tool", "monotonic_time_ns": "2000000000"},
            {"event": "tool_completed", "session_id": sid, "execution_id": eid,
             "exit_code": code, "monotonic_time_ns": "4500000000",
             "wall_time_ns": str((114+i)*10**9)},
        ])
        if i in expected:
            events.append({"event": "recorded_tool_failures_matched", "session_id": sid,
                           "matches": [{"call_id": "call-1", "exit_code": code}]})
            path = root / f"runtime-traces/{sid}/trace.jsonl"
            write(path, {"record_type": "span_end", "kind": "tool", "span_id": "call-1",
                         "execution": {"execution_id": eid}})
    path = root / "events/arm.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    return root


def test_expected_nonzero_counts_but_unexpected_failure_is_not_goodput(tmp_path):
    root = fixture(tmp_path, (0, 1, 1), expected=(1,))
    report = analysis.analyze(root)
    assert report["classes"]["be"]["valid_completed"] == 2
    assert report["classes"]["be"]["useful_completed"] == 1
    assert report["classes"]["be"]["unexpected_or_unverified_calls"] == 1
    assert report["classes"]["be"]["execution"]["count"] == 1
    assert report["classes"]["be"]["valid_batch_throughput_sessions_per_second"] == pytest.approx(1/22)


def test_fixed_observation_counts_nonzero_calls_but_censors_late_completions(tmp_path):
    root = fixture(tmp_path, (0, 1, 0))
    report = analysis.analyze(root, window_seconds=30, observation_end_unix_s=115)
    total = report['classes']['all']
    assert report['window']['duration_seconds'] == 15
    assert total['observed_completed_tool_steps_in_window'] == 2
    assert total['nonzero_exit_tool_steps_in_window'] == 1
    assert total['observed_tool_steps_per_second'] == pytest.approx(2/15)
    assert total['observed_window_execution']['count'] == 2
    assert total['observed_window_execution']['p95_seconds'] == 2
    assert total['completed_tasks'] == 0
    assert total['incomplete_tasks'] == 3


def test_observation_cutoff_does_not_complete_task_before_validation(tmp_path):
    root = fixture(tmp_path, (0, 0))
    path = root / "session-progress/arm-0000.json"
    record = analysis.read_json(path)
    record["result"]["timeline"]["validation_end"] = 125
    write(path, record)
    report = analysis.analyze(root, observation_end_unix_s=122)
    assert report["sessions"][0]["task_state"] == "incomplete"
    assert report["classes"]["ls"]["completed_tasks_in_window"] == 0
    assert report["classes"]["ls"]["completed_tasks"] == 0


def test_missing_sessions_and_calls_remain_in_failure_denominators(tmp_path):
    root = fixture(tmp_path, (0, 0), missing=(1,))
    report = analysis.analyze(root, cohorts={"0": "ls", "1": "be"})
    assert report["classes"]["be"]["offered"] == 1
    assert report["classes"]["be"]["failed_or_incomplete"] == 1
    assert report["classes"]["be"]["unexpected_or_unverified_calls"] == 1
    assert report["classes"]["be"]["execution"]["p95_seconds"] is None
    assert report["window"]["duration_seconds"] == 30
    with pytest.raises(ValueError, match="every offered"):
        analysis.analyze(root)


def test_slo_checks_completeness_and_separates_host_critical_path(tmp_path):
    baseline = analysis.analyze(fixture(tmp_path / "base", (0,)), cohorts={"0": "ls"})
    root = fixture(tmp_path / "mixed")
    report = analysis.analyze(root, baseline=baseline, slo_multiplier=1.2)
    assert report["classes"]["ls"]["execution"]["p95_seconds"] == 2
    assert report["classes"]["ls"]["critical_path"]["p95_seconds"] == 4
    assert report["classes"]["ls"]["queued_to_completion"]["p95_seconds"] == 2.5
    assert report["slo"]["metric"] == "execution"
    assert report["slo"]["threshold_seconds"] == 2.4
    assert report["slo"]["passes_p95_and_completeness"]
    failed = analysis.analyze(fixture(tmp_path / "failed", (1, 0)),
                              baseline=baseline, slo_multiplier=1.2)
    assert failed["slo"]["observed_call_denominator"] == 1
    assert failed["slo"]["observed_call_violation_count"] == 1
    assert not failed["slo"]["passes_p95_and_completeness"]
    assert failed["slo"]["be_throughput_when_slo_met"] is None


def test_window_options_do_not_change_latency_samples(tmp_path):
    root = fixture(tmp_path)
    report = analysis.analyze(root, window_origin="agent", warmup_seconds=5, window_seconds=5)
    assert report["window"]["start_unix_s"] == 115
    assert report["classes"]["be"]["useful_completions_in_window"] == 0
    assert report["classes"]["be"]["execution"]["count"] == 1
    with pytest.raises(ValueError, match="beyond"):
        analysis.analyze(root, window_seconds=100)


def test_missing_timing_is_unavailable_and_cannot_pass_slo(tmp_path):
    baseline = analysis.analyze(fixture(tmp_path / "base", (0,)), cohorts={"0": "ls"})
    root = fixture(tmp_path / "mixed")
    path = root / "policy-control/arm-0000.json"
    record = analysis.read_json(path)
    record[0]["timing"] = {}
    write(path, record)
    report = analysis.analyze(root, baseline=baseline, slo_multiplier=1.2, slo_metric="critical_path")
    assert report["classes"]["ls"]["critical_path"]["p95_seconds"] is None
    assert report["slo"]["observed_call_violation_count"] == 1
    assert not report["slo"]["passes_p95_and_completeness"]


def test_unique_ls_workloads_match_after_be_removal_renumbers_sessions(tmp_path):
    base_root = fixture(tmp_path / "base", (0, 0))
    mixed_root = fixture(tmp_path / "mixed", (0, 0, 0))
    for root, index in ((base_root, 1), (mixed_root, 2)):
        path = root / f"session-progress/arm-{index:04d}.json"
        record = analysis.read_json(path)
        record["result"]["case_id"] = "second-unique-ls"
        record["result"]["timeline"]["offered_start_offset_seconds"] = 2.
        write(path, record)
    baseline = analysis.analyze(base_root, cohorts={"0": "ls", "1": "ls"})
    report = analysis.analyze(mixed_root, cohorts={"0": "ls", "1": "be", "2": "ls"},
                              baseline=baseline, slo_multiplier=1.2)
    assert report["slo"]["ls_inputs_match"]
    assert report["slo"]["ls_arrival_offsets_match"]
    assert report["slo"]["passes_p95_and_completeness"]
    baseline["ls_inputs"][1]["arrival_offset_seconds"] = 1.
    changed = analysis.analyze(mixed_root, cohorts={"0": "ls", "1": "be", "2": "ls"},
                               baseline=baseline, slo_multiplier=1.2)
    assert changed["slo"]["ls_inputs_match"]
    assert not changed["slo"]["ls_arrival_offsets_match"]
    assert not changed["slo"]["passes_p95_and_completeness"]


def test_three_class_steps_and_task_counts_share_window_and_sum(tmp_path):
    root = fixture(tmp_path, (0, 0, 1))
    report = analysis.analyze(root, timeseries_bin_seconds=10)
    ls, be, total = (report["classes"][key] for key in ("ls", "be", "all"))
    assert total["tool_steps_per_second"] == pytest.approx(
        ls["tool_steps_per_second"] + be["tool_steps_per_second"])
    assert total["tool_steps_in_window"] == 2
    assert total["observed_completed_tool_steps"] == 3
    assert total["completed_tasks"] == 3  # Final task validation is distinct from Tool outcome.
    assert total["useful_completed"] == 2
    assert total["failed_tasks"] == total["incomplete_tasks"] == 0
    bins = report["timeseries"]
    assert sum(row["tool_steps"] for row in bins if row["qos_class"] == "all") == 2
    assert bins[-1]["cumulative_completed_tasks"] == 3
    assert bins[-1]["partial_bin"]
    for index in {row["bin"] for row in bins}:
        group = [row for row in bins if row["bin"] == index]
        assert group[2]["tool_steps"] == group[0]["tool_steps"] + group[1]["tool_steps"]


def test_running_arm_counts_completed_steps_without_claiming_completed_tasks(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    (root / "arms/arm.json").unlink()
    for index in (0, 1):
        write(root / f"session-progress/arm-{index:04d}.json", {
            "session_id": f"arm-{index:04d}", "status": "started"})
        (root / f"policy-control/arm-{index:04d}.json").unlink()
    write(root / "run-state.json", {"run_id": "test", "current_arm_id": "arm",
          "arms": {"arm": {"status": "running", "attempt_id": "attempt", "started_unix_s": 100}}})
    monkeypatch.setattr(analysis, "frozen_arm", lambda *_: {
        "arm_id": "arm", "concurrency": 2, "session_cases": [{"qos_class": "ls"}, {"qos_class": "be"}]})
    path = root / "events/arm.jsonl"
    events = analysis.read_jsonl(path)
    for event in events:
        if event['event'] == 'tool_completed':
            event.update(execution_started_at=112, execution_completed_at=114)
    path.write_text('\n'.join(json.dumps(e) for e in events), encoding='utf-8')
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\n" + json.dumps({"event": "memory_sample", "wall_time_ns": str(125*10**9)}) + "\n")
        stream.write('{"event":')  # Concurrent, unfinished append is not a completed event.
    report = analysis.analyze(root, window_origin="arm", window_seconds=60)
    assert report["arm_status"] == "in_progress" and report["incomplete"]
    assert report["window"]["end_unix_s"] == 125
    assert report["classes"]["all"]["tool_steps_per_second"] == pytest.approx(2/25)
    assert report["classes"]["all"]["validated_session_tool_steps_per_second"] == 0
    assert report["classes"]["all"]["completed_tasks"] == 0
    assert report["classes"]["all"]["incomplete_tasks"] == 2
    assert report['classes']['all']['observed_window_execution']['count'] == 2
    assert report['classes']['all']['observed_window_execution']['p95_seconds'] == 2
    assert report["timeseries"][-1]["cumulative_completed_tasks"] == 0


def test_unknown_exit_is_observed_only_and_failures_remain_separate(tmp_path):
    root = fixture(tmp_path)
    write(root / "session-progress/arm-0001.json", {
        "session_id": "arm-0001", "status": "incomplete", "error": "tool failed"})
    (root / "policy-control/arm-0001.json").unlink()
    path = root / "events/arm.jsonl"
    events = analysis.read_jsonl(path)
    for row in events:
        if row["event"] == "tool_completed" and row["session_id"] == "arm-0001":
            row.pop("exit_code")
    events.append({"event": "session_failed", "session_id": "arm-0001", "wall_time_ns": str(122*10**9)})
    path.write_text("\n".join(json.dumps(row) for row in events), encoding="utf-8")
    report = analysis.analyze(root, cohorts={"0": "ls", "1": "be"})
    assert report["classes"]["be"]["observed_completed_tool_steps"] == 1
    assert report["classes"]["be"]["tool_steps"] == 0
    assert report["classes"]["be"]["failed_tasks"] == 1
    assert report["timeseries"][-1]["cumulative_failed_tasks"] == 1


def test_existing_control_events_preserve_exact_execution_time_without_finalization(tmp_path):
    root = fixture(tmp_path)
    (root/'policy-control/arm-0001.json').unlink()
    write(root/'session-progress/arm-0001.json', {'status':'started'})
    events=analysis.read_jsonl(root/'events/arm.jsonl')
    events.append({'event':'pressure_execution_release', 'session_id':'arm-0001',
                   'execution_id':'exec-1', 'duration_seconds':3.25})
    (root/'events/arm.jsonl').write_text('\n'.join(json.dumps(e) for e in events))
    report=analysis.analyze(root,cohorts={'0':'ls','1':'be'})
    assert report['classes']['be']['observed_window_execution']['p95_seconds']==3.25
    assert report['classes']['be']['completed_tasks']==0
    assert report['calls'][1]['execution_timing_source']=='pressure_execution_release'


def test_timeseries_csv_export_preserves_class_rows(tmp_path):
    root = fixture(tmp_path / "run")
    output, csv_path = tmp_path / "analysis.json", tmp_path / "rates.csv"
    analysis.main([str(root), "--output", str(output), "--timeseries-csv", str(csv_path),
                   "--timeseries-bin-seconds", "5"])
    import csv
    with csv_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["qos_class"] for row in rows} == {"ls", "be", "all"}
    assert sum(int(row["tool_steps"]) for row in rows if row["qos_class"] == "all") == 2


def test_validation_failure_has_failure_time_without_exception_event(tmp_path):
    root = fixture(tmp_path)
    path = root / "session-progress/arm-0001.json"
    record = analysis.read_json(path)
    record["result"]["valid"] = False
    record["result"]["timeline"]["validation_end"] = 121
    write(path, record)
    report = analysis.analyze(root)
    assert report["classes"]["all"]["completed_tasks"] == 1
    assert report["classes"]["all"]["failed_tasks"] == 1
    assert report["classes"]["all"]["failed_tasks_without_timestamps"] == 0
    assert report["timeseries"][-1]["cumulative_failed_tasks"] == 1
