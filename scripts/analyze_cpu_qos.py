#!/usr/bin/env python3
"""Analyze one frozen CPU QoS arm; this does not execute experiments.

Cohorts JSON is {"0": "ls", "1": "be", ...}, covering every offered session.
Without it, use session-result qos_class or frozen cases while an arm is running.
Use the JSON produced for a no-BE arm as --baseline-json. A multiplier is
required explicitly; e.g. --slo-multiplier 1.2 permits 20% p95 inflation.
All durations and window options are seconds. Windows are [start, end].
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def read_jsonl(path, *, growing=False):
    if path is None:
        return []
    with path.open(encoding="utf-8-sig") as stream:
        rows = []
        for line in stream:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if growing and not line.endswith("\n"):
                    break  # Only an unfinished append at EOF may be ignored.
                raise
        return rows


def percentile(values, q=.95):
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * q
    lower = int(at)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered)-1)] - ordered[lower]) * (at-lower)


def distribution(values):
    return {"count": len(values), "mean_seconds": statistics.fmean(values) if values else None,
            "p50_seconds": percentile(values, .5), "p95_seconds": percentile(values)}


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def event_time(event):
    if event.get("wall_time_ns") is not None:
        return int(event["wall_time_ns"]) / 1e9
    return timestamp(event["wall_time"]) if event.get("wall_time") else None


def frozen_arm(root, arm_id):
    # Read the supported frozen specification; never create a second runner.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from clawbox.experiments.spec import expand_matrix, load_experiment
    arms = [a for a in expand_matrix(load_experiment(root / "experiment.yaml"))
            if a.arm_id == arm_id]
    if len(arms) != 1:
        raise ValueError(f"arm {arm_id} is not uniquely present in frozen experiment.yaml")
    return arms[0].model_dump(mode="json")


def running_context(root, arm_id):
    state = read_json(root / "run-state.json")
    arm_id = arm_id or state.get("current_arm_id")
    if not arm_id and len(state["arms"]) == 1:
        arm_id = next(iter(state["arms"]))
    if arm_id not in state["arms"]:
        raise ValueError("select the running arm with --arm-id")
    progress = state["arms"][arm_id]
    started = progress.get("started_unix_s")
    if started is None:
        raise ValueError("selected arm has not started; analyze after its first events")
    arm = frozen_arm(root, arm_id)
    cases = arm.get("session_cases") or [arm["case"]]
    cohorts = {str(i): cases[i % len(cases)].get("qos_class") for i in range(arm["concurrency"])}
    return {"arm": arm, "run_id": state["run_id"], "attempt_id": progress["attempt_id"],
            "status": "in_progress", "source_arm_status": progress["status"],
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "correctness": {"uninterrupted_timing": not bool(state.get("resumed_unix_s"))}}, cohorts


def throughput_series(rows, calls, start, end, bin_seconds, *, incomplete):
    """One common window for LS, BE and their sum; task validation is separate."""
    size = math.ceil((end-start) / bin_seconds)
    series = []
    for index in range(size):
        left, right = start + index*bin_seconds, min(end, start + (index+1)*bin_seconds)
        for cohort in ("ls", "be", "all"):
            series.append({"bin": index, "qos_class": cohort, "start_unix_s": left,
                           "end_unix_s": right, "duration_seconds": right-left,
                           "partial_bin": right-left < bin_seconds, "incomplete": incomplete,
                           "observed_completed_tool_steps": 0, "tool_steps": 0,
                           "validated_session_tool_steps": 0, "completed_tasks": 0, "failed_tasks": 0})
    def destinations(at, cohort):
        if at is None or not start <= at <= end:
            return ()
        index = min(int((at-start) / bin_seconds), size-1)
        return (series[index*3 + (0 if cohort == "ls" else 1)], series[index*3+2])
    for call in calls:
        for row in destinations(call["completed_unix_s"], call["qos_class"]):
            row["observed_completed_tool_steps"] += int(call["completion_observed"])
            row["tool_steps"] += int(call["expected_outcome"])
            row["validated_session_tool_steps"] += int(call["comparable"])
    for task in rows:
        for name, at in (("completed_tasks", task["completed_unix_s"] if task["valid_completed"] else None),
                         ("failed_tasks", task["failed_unix_s"] if task["task_state"] == "failed" else None)):
            for row in destinations(at, task["qos_class"]):
                row[name] += 1
    for cohort in ("ls", "be", "all"):
        selected = [r for r in rows if cohort == "all" or r["qos_class"] == cohort]
        complete = sum(r["valid_completed"] and r["completed_unix_s"] is not None
                       and r["completed_unix_s"] < start for r in selected)
        failed = sum(r["task_state"] == "failed" and r["failed_unix_s"] is not None
                     and r["failed_unix_s"] < start for r in selected)
        for row in (r for r in series if r["qos_class"] == cohort):
            complete += row["completed_tasks"]
            failed += row["failed_tasks"]
            row.update(cumulative_completed_tasks=complete, cumulative_failed_tasks=failed,
                       remaining_offered_tasks=len(selected)-complete-failed,
                       tool_steps_per_second=row["tool_steps"]/row["duration_seconds"],
                       validated_session_tool_steps_per_second=row["validated_session_tool_steps"]/row["duration_seconds"])
    return series


def positive_interval(start, end):
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) for v in (start, end)) or end < start:
        return None
    return end - start


def unique_file(paths, description, required=False):
    paths = sorted(set(p.resolve() for p in paths if p.is_file()))
    if len(paths) > 1:
        raise ValueError(f"ambiguous {description}: {paths}; select an arm/attempt directory")
    if not paths and required:
        raise ValueError(f"missing {description}")
    return paths[0] if paths else None


def artifact_file(root, result, key, pattern):
    raw = (result.get("artifacts") or {}).get(key)
    if raw and Path(raw).is_file():
        return Path(raw)
    matches = list(root.glob(pattern))
    attempt = result.get("attempt_id")
    matching_attempt = [p for p in matches if attempt and attempt in p.parts]
    return unique_file(matching_attempt or matches, key)


def compare_ls_inputs(current, baseline):
    """Pair unique workload identities; removing BE renumbers session indices."""
    def identities(rows):
        mapped = {}
        for row in rows:
            key = (row["case_id"], row["trace_reference"])
            if not all(key) or key in mapped:
                return None
            mapped[key] = row
        return mapped
    left, right = identities(current), identities(baseline)
    if left is None or right is None:
        return False, None
    match = (left.keys() == right.keys() and all(
        left[key]["command_sha256"] == right[key]["command_sha256"] for key in left))
    offsets_known = left.keys() == right.keys() and all(
        row.get("arrival_offset_seconds") is not None for row in [*left.values(), *right.values()])
    arrival_match = all(left[key]["arrival_offset_seconds"] == right[key]["arrival_offset_seconds"]
                        for key in left) if offsets_known else None
    return match, arrival_match


def analyze(root, *, arm_id=None, cohorts=None, baseline=None, slo_multiplier=None,
            slo_metric="execution", window_origin="arrival", warmup_seconds=0.,
            window_seconds=None, timeseries_bin_seconds=10., observation_end_unix_s=None,
            observation_start_unix_s=None):
    root = Path(root)
    candidates = list((root / "arms").glob(f"{arm_id or '*'}.json"))
    if not candidates:
        candidates = list(root.glob(f"**/arms/{arm_id or '*'}.json"))
    result_path = unique_file(candidates, "arm result")
    provisional = result_path is None
    if provisional:
        result, frozen_cohorts = running_context(root, arm_id)
        cohorts = cohorts if cohorts is not None else frozen_cohorts
    else:
        result = read_json(result_path)
    arm = result["arm"]
    arm_id = arm["arm_id"]
    count = int(arm["concurrency"])
    events_path = artifact_file(root, result, "events", f"**/events/{arm_id}.jsonl")
    events = read_jsonl(events_path, growing=provisional)
    session_events = defaultdict(list)
    for event in events:
        if event.get("session_id"):
            session_events[event["session_id"]].append(event)
    progress_paths = defaultdict(list)
    for path in root.glob(f"**/session-progress/{arm_id}-*.json"):
        progress_paths[path.stem].append(path)
    sessions = {}
    for index in range(count):
        sid = f"{arm_id}-{index:04d}"
        paths = progress_paths[sid]
        attempt = result.get("attempt_id")
        matching_attempt = [p for p in paths if attempt and attempt in p.parts]
        path = unique_file(matching_attempt or paths, f"session progress {sid}")
        record = read_json(path) if path else {}
        sessions[index] = (sid, record, record.get("result") or {})
    if cohorts is None:
        cohorts = {str(i): row.get("qos_class") for i, (_, _, row) in sessions.items()}
    if set(cohorts) != {str(i) for i in range(count)} or any(v not in {"ls", "be"} for v in cohorts.values()):
        raise ValueError("cohorts must assign ls/be to every offered session index, including missing/failed sessions")
    if not any(v == "ls" for v in cohorts.values()):
        raise ValueError("cohorts contain no LS sessions")
    rows, calls, ls_inputs = [], [], []
    for index, (sid, record, session) in sessions.items():
        cohort = cohorts[str(index)]
        stream = session_events[sid]
        operations = {}
        for event in stream:
            eid = event.get("execution_id")
            if eid:
                operations.setdefault(eid, {})[event["event"]] = event
        spans = session.get("timeline", {}).get("time_spans", [])
        for event in stream:
            if event["event"] == "session_timing":
                spans = event["time_spans"]
        for span in spans:
            if span.get("name") == "tool.operation" and span.get("execution_scope") == "agent-tool":
                operations.setdefault(span["execution_id"], {})["span"] = span
        policy_path = artifact_file(root, result, f"policy_control:{sid}", f"**/policy-control/{sid}.json")
        for policy in read_json(policy_path) if policy_path else []:
            request = policy.get("request") or {}
            if request.get("execution_scope", "agent-tool") == "agent-tool":
                operations.setdefault(request["execution_id"], {})["policy"] = policy
        # Expected nonzero outcomes use exact call_id -> execution_id evidence.
        expected_calls = {str(match["call_id"]): match["exit_code"] for event in stream
                          if event["event"] == "recorded_tool_failures_matched" for match in event["matches"]}
        expected_exits = {}
        if expected_calls:
            for path in root.glob(f"**/runtime-traces/{sid}/**/*.jsonl"):
                if "tool-resource" in path.parts:
                    continue
                for span in read_jsonl(path):
                    if span.get("record_type") == "span_end" and span.get("kind") == "tool":
                        eid = (span.get("execution") or {}).get("execution_id")
                        if eid and str(span.get("span_id")) in expected_calls:
                            expected_exits[eid] = expected_calls[str(span["span_id"])]
        session_calls = []
        for eid, op in operations.items():
            queued = op.get("tool_admission_queued", {})
            admitted = op.get("tool_admitted", {})
            policy = op.get("policy", {})
            request = policy.get("request") or {}
            span = op.get("span", {})
            scope = request.get("execution_scope") or queued.get("execution_scope") or admitted.get("execution_scope") or span.get("execution_scope")
            if scope != "agent-tool":
                continue
            completion = policy.get("completion") or op.get("tool_completed") or {}
            host_completed = op.get("tool_completed") or {}
            completed_at = event_time(host_completed)
            completion_source = "host_tool_completed_event"
            if completed_at is None:
                completed_at = completion.get("execution_completed_at", span.get("end_unix_s"))
                completion_source = "runtime_wall_clock_requires_synchronization" if completed_at is not None else None
            exit_code = completion.get("exit_code")
            expected = type(exit_code) is int and (exit_code == 0 or expected_exits.get(eid) == exit_code)
            execution = positive_interval(completion.get("execution_started_at"), completion.get("execution_completed_at"))
            execution_source = 'completion_timestamps' if execution is not None else None
            if execution is None:
                execution = positive_interval(span.get("start_unix_s"), span.get("end_unix_s"))
                execution_source = 'session_tool_operation_span' if execution is not None else None
            # Both existing control events receive the same validated
            # execution_completed_at - execution_started_at duration. They
            # can preserve timings even when a bounded run stops before the
            # owning session writes its final policy-control file.
            for event_name, field in (
                ('pressure_execution_release', 'duration_seconds'),
                ('first_tool_after_restore', 'first_tool_after_restore_seconds'),
            ):
                value = op.get(event_name, {}).get(field)
                if (execution is None and bool(completion) and type(value) in (int, float)
                        and math.isfinite(value) and value > 0):
                    execution = value
                    execution_source = event_name
            timing = policy.get("timing") or {}
            critical = positive_interval(timing.get("admission_started_monotonic_s"), timing.get("completion_completed_monotonic_s"))
            queued_to_complete = positive_interval(
                int(queued["monotonic_time_ns"])/1e9 if queued.get("monotonic_time_ns") else None,
                int(op["tool_completed"]["monotonic_time_ns"])/1e9 if op.get("tool_completed", {}).get("monotonic_time_ns") else None)
            call = {"session_index": index, "qos_class": cohort, "execution_id": eid,
                    "command_sha256": request.get("command_sha256") or admitted.get("command_sha256"),
                    "expected_outcome": expected, "exit_code": exit_code,
                    "completion_observed": bool(completion), "completed_unix_s": completed_at,
                    "completion_time_source": completion_source,
                    "execution_seconds": execution, "critical_path_seconds": critical,
                    "execution_timing_source": execution_source,
                    "queued_to_completion_seconds": queued_to_complete}
            session_calls.append(call)
        unexpected = sum(not call["expected_outcome"] for call in session_calls)
        harmful = any(e["event"] == "harmful_replay_tool_divergence" for e in stream)
        valid = record.get("status") == "completed" and session.get("valid") is True
        useful = valid and bool(session_calls) and not unexpected and not harmful
        timeline = session.get("timeline") or {}
        failed_events = [e for e in stream if e["event"] == "session_failed"]
        task_state = ("completed" if valid else "failed" if record.get("error") or failed_events
                      or record.get("status") == "completed" else "incomplete")
        failed_at = event_time(failed_events[-1]) if failed_events else (
            timeline.get("validation_end") if record.get("status") == "completed" and not valid else None)
        rows.append({"session_index": index, "qos_class": cohort,
                     "valid_completed": valid, "useful_completed": useful,
                     "task_state": task_state, "failed_unix_s": failed_at,
                     "unexpected_or_unverified_calls": unexpected, "harmful_divergence": harmful,
                     "offered_unix_s": timeline.get("offered_start_unix_s"),
                     "agent_start_unix_s": timeline.get("agent_execution_start"),
                     # A successful Agent response is not a completed task until
                     # the host-side validation has finished.  This distinction
                     # matters when an observation cutoff falls between the two.
                     "completed_unix_s": timeline.get("validation_end"),
                     "recovered": session.get("recovered_from_previous_invocation", False)})
        for call in session_calls:
            call["valid_session"] = valid
            call["comparable"] = valid and call["expected_outcome"] and not harmful
        calls.extend(session_calls)
        if cohort == "ls":
            ls_inputs.append({"session_index": index, "case_id": session.get("case_id"),
                              "trace_reference": session.get("trace_reference"),
                              "arrival_offset_seconds": timeline.get("offered_start_offset_seconds"),
                              "command_sha256": sorted(call["command_sha256"] or "<missing>" for call in session_calls)})
    if provisional:
        observed_times = [at for event in events if (at := event_time(event)) is not None]
        observed_times += [r["completed_unix_s"] for r in rows if r["completed_unix_s"] is not None]
        if not observed_times:
            raise ValueError("running arm has no time-stamped observations yet; retry after its first events")
        result["completed_at"] = datetime.fromtimestamp(max(observed_times), timezone.utc).isoformat()
    if observation_end_unix_s is not None:
        if not math.isfinite(observation_end_unix_s):
            raise ValueError("observation cutoff must be finite")
        # Analysis-only censoring: preserve raw events and exclude cleanup or
        # work finishing after the predeclared observation deadline.
        cutoff = min(observation_end_unix_s, timestamp(result["completed_at"]))
        result["completed_at"] = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        for row in rows:
            if row["completed_unix_s"] is not None and row["completed_unix_s"] > cutoff:
                row.update(valid_completed=False, useful_completed=False,
                           task_state="incomplete", completed_unix_s=None)
            if row["failed_unix_s"] is not None and row["failed_unix_s"] > cutoff:
                row.update(task_state="incomplete", failed_unix_s=None)
        valid_at_cutoff = {r["session_index"]: r["valid_completed"] for r in rows}
        for call in calls:
            if call["completed_unix_s"] is not None and call["completed_unix_s"] > cutoff:
                call.update(completion_observed=False, expected_outcome=False,
                            completed_unix_s=None, execution_seconds=None,
                            critical_path_seconds=None, queued_to_completion_seconds=None)
            call["comparable"] = call["comparable"] and valid_at_cutoff[call["session_index"]] and call["completion_observed"]
    origin_field = {"arrival": "offered_unix_s", "agent": "agent_start_unix_s"}.get(window_origin)
    starts = [r[origin_field] for r in rows if origin_field and r[origin_field] is not None]
    if window_origin == "arrival":
        starts += [event_time(e) - e.get("dispatch_lateness_seconds", 0.) for e in events
                   if e["event"] == "session_dispatched" and event_time(e) is not None]
    if window_origin == "arm":
        origin = timestamp(result["started_at"])
    elif starts:
        origin = min(starts)
    elif provisional and window_origin == "arrival":
        dispatched = [event_time(e) - e.get("dispatch_lateness_seconds", 0.)
                      - e.get("scheduled_offset_seconds", 0.) for e in events
                      if e["event"] == "session_dispatched" and event_time(e) is not None]
        if not dispatched:
            raise ValueError("no offered arrival observed yet; use --window-origin arm")
        origin = min(dispatched)
    else:
        raise ValueError(f"no {window_origin} timestamp available; use --window-origin arm")
    start = observation_start_unix_s if observation_start_unix_s is not None else origin + warmup_seconds
    ends = [r["completed_unix_s"] for r in rows if r["completed_unix_s"] is not None]
    # A failed batch's elapsed observation does not disappear from the denominator.
    observed_end = timestamp(result["completed_at"])
    end = start + window_seconds if window_seconds is not None else (
        max(ends) if len(ends) == count and not provisional else observed_end)
    if provisional or observation_end_unix_s is not None:
        end = min(end, observed_end)
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("measurement window must have positive finite duration")
    if end > timestamp(result["completed_at"]):
        raise ValueError("measurement window extends beyond the completed observation")
    classes = {}
    for cohort in ("ls", "be", "all"):
        selected = [r for r in rows if cohort == "all" or r["qos_class"] == cohort]
        samples = [c for c in calls if cohort == "all" or c["qos_class"] == cohort]
        timed = [c for c in samples if c["completed_unix_s"] is not None and start <= c["completed_unix_s"] <= end]
        complete_in_window = sum(r["useful_completed"] and r["completed_unix_s"] is not None
                                 and start <= r["completed_unix_s"] <= end for r in selected)
        classes[cohort] = {"offered": len(selected),
                           "completed_tasks": sum(r["valid_completed"] for r in selected),
                           "failed_tasks": sum(r["task_state"] == "failed" for r in selected),
                           "failed_tasks_without_timestamps": sum(r["task_state"] == "failed" and r["failed_unix_s"] is None for r in selected),
                           "incomplete_tasks": sum(r["task_state"] == "incomplete" for r in selected),
                           "observed_completed_tool_steps": sum(c["completion_observed"] for c in samples),
                           "tool_steps": sum(c["expected_outcome"] for c in samples),
                           "tool_steps_in_window": sum(c["expected_outcome"] for c in timed),
                           "tool_steps_per_second": sum(c["expected_outcome"] for c in timed)/(end-start),
                           "observed_completed_tool_steps_in_window": sum(c["completion_observed"] for c in timed),
                           "observed_tool_steps_per_second": sum(c["completion_observed"] for c in timed)/(end-start),
                           "nonzero_exit_tool_steps_in_window": sum(c["completion_observed"] and c["exit_code"] not in (None, 0) for c in timed),
                           "completed_tasks_in_window": sum(r["valid_completed"] and r["completed_unix_s"] is not None and start <= r["completed_unix_s"] <= end for r in selected),
                           "validated_session_tool_steps_in_window": sum(c["comparable"] for c in timed),
                           "validated_session_tool_steps_per_second": sum(c["comparable"] for c in timed)/(end-start),
                           "completed_steps_without_timestamps": sum(c["completion_observed"] and c["completed_unix_s"] is None for c in samples),
                           "valid_completed": sum(r["valid_completed"] for r in selected),
                           "useful_completed": sum(r["useful_completed"] for r in selected),
                           "failed_or_incomplete": sum(not r["valid_completed"] for r in selected),
                           "unusable_or_incomplete": sum(not r["useful_completed"] for r in selected),
                           "valid_completion_rate": sum(r["valid_completed"] for r in selected)/len(selected) if selected else None,
                           "unexpected_or_unverified_calls": sum(not c["expected_outcome"] for c in samples),
                           "observed_calls": len(samples),
                           "comparable_calls": sum(c["comparable"] for c in samples),
                           "useful_completions_in_window": complete_in_window,
                           "valid_batch_throughput_sessions_per_second": complete_in_window/(end-start)}
        for metric in ("execution", "critical_path", "queued_to_completion"):
            classes[cohort][metric] = distribution([c[f"{metric}_seconds"] for c in samples
                if c["comparable"] and c[f"{metric}_seconds"] is not None])
            classes[cohort]['observed_window_'+metric] = distribution([
                c[f"{metric}_seconds"] for c in timed
                if c["completion_observed"] and c[f"{metric}_seconds"] is not None])
    output = {"schema_version": 1, "run_id": result.get("run_id"), "arm_id": arm_id,
              "attempt_id": result.get("attempt_id"), "arm_status": result["status"],
              "source_arm_status": result.get("source_arm_status", result["status"]),
              "source_result": str(result_path) if result_path else None, "source_events": str(events_path) if events_path else None,
              "incomplete": provisional or result["status"] != "succeeded" or any(r["task_state"] == "incomplete" for r in rows),
              "observed_until_unix_s": observed_end,
              "claim_scope": "finite_batch_pilot_not_steady_state_capacity",
              "window": {"origin": window_origin, "warmup_seconds": warmup_seconds,
                         "start_unix_s": start, "end_unix_s": end, "duration_seconds": end-start,
                         "completion_counting": "useful sessions finishing within inclusive window; may start before window"},
              "metrics_scope": "latencies use the entire arm; throughput alone uses the selected window",
              "observed_window_metrics_scope": "all completed agent-tool calls in the selected window, including nonzero exits; unfinished calls are censored, not zero latency; not a task correctness or whole-trace SLO guarantee",
              "observation_end_unix_s": observation_end_unix_s,
              "tool_steps_scope": "completed agent-tool calls with successful or exactly matched expected outcomes; task completion requires final validation",
              "critical_path_scope": "host admission callback entry through host completion callback return; excludes client pre-request and post-response transport",
              "classes": classes, "ls_inputs": ls_inputs, "sessions": rows, "calls": calls,
              "overall_cpu_summary": result.get("performance", {}).get("experiment_cpu"),
              "observed_agent_tool_concurrency": result.get("performance", {}).get("observed_agent_tool_concurrency"),
              "uncertainty": "one arm; no confidence interval or steady-state capacity estimate",
              "uninterrupted": result.get("correctness", {}).get("uninterrupted_timing", True)
                               and not any(r["recovered"] for r in rows)}
    if not math.isfinite(timeseries_bin_seconds) or timeseries_bin_seconds <= 0:
        raise ValueError("timeseries bin duration must be finite and positive")
    output["timeseries_bin_seconds"] = timeseries_bin_seconds
    output["timeseries"] = throughput_series(rows, calls, start, end, timeseries_bin_seconds,
                                              incomplete=output["incomplete"])
    if baseline is not None:
        if slo_multiplier is None or not math.isfinite(slo_multiplier) or slo_multiplier <= 0:
            raise ValueError("--baseline-json requires an explicit positive --slo-multiplier")
        if baseline["classes"]["be"]["offered"] != 0:
            raise ValueError("SLO baseline must contain zero offered BE sessions")
        base_p95 = baseline["classes"]["ls"][slo_metric]["p95_seconds"]
        if (base_p95 is None or not baseline.get("uninterrupted")
                or baseline["classes"]["ls"]["useful_completed"] != baseline["classes"]["ls"]["offered"]
                or baseline["classes"]["ls"][slo_metric]["count"] != baseline["classes"]["ls"]["observed_calls"]):
            raise ValueError("SLO baseline requires an uninterrupted, fully useful LS run with measured p95")
        threshold = base_p95 * slo_multiplier
        metric_key = f"{slo_metric}_seconds"
        ls_calls = [c for c in calls if c["qos_class"] == "ls"]
        violations = sum(not c["comparable"] or c[metric_key] is None or c[metric_key] > threshold for c in ls_calls)
        missing_sessions = sum(not r["valid_completed"] for r in rows if r["qos_class"] == "ls")
        inputs_match, arrivals_match = compare_ls_inputs(ls_inputs, baseline["ls_inputs"])
        p95 = classes["ls"][slo_metric]["p95_seconds"]
        all_calls_measured = bool(ls_calls) and all(c[metric_key] is not None for c in ls_calls)
        passes = (p95 is not None and p95 <= threshold and inputs_match and output["uninterrupted"]
                  and not output["incomplete"]
                  and arrivals_match is not False
                  and classes["ls"]["useful_completed"] == classes["ls"]["offered"] and all_calls_measured)
        output["slo"] = {"metric": slo_metric, "baseline_arm_id": baseline["arm_id"],
                         "baseline_p95_seconds": base_p95, "multiplier": slo_multiplier,
                         "threshold_seconds": threshold, "ls_inputs_match": inputs_match,
                         "ls_arrival_offsets_match": arrivals_match,
                         "ls_matching_scope": "unique case_id and trace_reference plus command digests; session indices may differ",
                         "observed_call_violation_count": violations,
                         "observed_call_denominator": len(ls_calls),
                         "failed_or_missing_ls_sessions": missing_sessions,
                         "missing_future_calls": "unknown; incomplete sessions cannot be treated as zero violations",
                         "passes_p95_and_completeness": passes,
                         "be_tool_steps_per_second_when_slo_met": classes["be"]["tool_steps_per_second"] if passes else None,
                         "be_throughput_when_slo_met": classes["be"]["valid_batch_throughput_sessions_per_second"] if passes else None}
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--arm-id", help="Required when the root contains multiple arm results")
    parser.add_argument("--cohorts", type=Path, help="Frozen JSON mapping every session index to ls/be")
    parser.add_argument("--baseline-json", type=Path, help="This script's output for the same LS workload with no BE")
    parser.add_argument("--slo-multiplier", type=float, help="Explicit allowed p95 factor relative to baseline")
    parser.add_argument("--slo-metric", choices=("execution", "critical_path", "queued_to_completion"), default="execution",
                        help="LS SLO metric; default execution is measured Tool execution time")
    parser.add_argument("--window-origin", choices=("arrival", "agent", "arm"), default="arrival",
                        help="Throughput starts at earliest offered arrival (default), earliest agent start, or arm start")
    parser.add_argument("--warmup-seconds", type=float, default=0., help="Offset throughput window start; default 0")
    parser.add_argument("--window-seconds", type=float, help="Fixed throughput window length; default through final completion (failed batches through arm end)")
    parser.add_argument("--observation-end-unix-s", type=float,
                        help="Analysis-only wall-clock cutoff; excludes later completions and cleanup, never changes replay timing")
    parser.add_argument("--observation-start-unix-s", type=float,
                        help="Explicit analysis window start, e.g. the final session submission event")
    parser.add_argument("--timeseries-bin-seconds", type=float, default=10., help="Fixed timeseries bin length in seconds; default 10, final bin may be shorter")
    parser.add_argument("--timeseries-csv", type=Path, help="Also export LS/BE/all interval step rates and cumulative task counts as CSV")
    parser.add_argument("--output", type=Path, help="Output JSON path; omitted writes stdout")
    args = parser.parse_args(argv)
    if not math.isfinite(args.warmup_seconds) or args.warmup_seconds < 0:
        parser.error("--warmup-seconds must be finite and nonnegative")
    if args.window_seconds is not None and (not math.isfinite(args.window_seconds) or args.window_seconds <= 0):
        parser.error("--window-seconds must be finite and positive")
    try:
        output = analyze(args.run_root, arm_id=args.arm_id,
                         cohorts=read_json(args.cohorts) if args.cohorts else None,
                         baseline=read_json(args.baseline_json) if args.baseline_json else None,
                         slo_multiplier=args.slo_multiplier, slo_metric=args.slo_metric,
                         window_origin=args.window_origin, warmup_seconds=args.warmup_seconds,
                         window_seconds=args.window_seconds, timeseries_bin_seconds=args.timeseries_bin_seconds,
                         observation_end_unix_s=args.observation_end_unix_s,
                         observation_start_unix_s=args.observation_start_unix_s)
        rendered = json.dumps(output, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        if args.timeseries_csv:
            with args.timeseries_csv.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(output["timeseries"][0]))
                writer.writeheader()
                writer.writerows(output["timeseries"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"CPU QoS analysis failed: {exc}\n")


if __name__ == "__main__":
    main()
