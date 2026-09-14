"""Fail-closed, offline planning for four-call placement experiments.

The trace copy is evidence, not a runnable checkpoint.  This module never
executes a trace command or imports a final KB snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import shlex
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from clawbox.tuning.clawtune import shell_command_heads, shell_command_prefix_tokens


PLACEMENTS = {
    "P1": (("A", "B"), ("C", "D")),
    "P2": (("A", "C"), ("B", "D")),
    "P3": (("A", "D"), ("B", "C")),
}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _read_spans(root: Path) -> list[dict[str, Any]]:
    calls = []
    for suite in ("terminal", "swe"):
        for path in sorted((root / suite / "traces").rglob("trace.jsonl")):
            starts: dict[tuple[str, str], dict[str, Any]] = {}
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("kind") != "tool":
                    continue
                key = (str(row.get("trace_id")), str(row.get("span_id")))
                if row.get("record_type") == "span_start":
                    if key in starts:
                        raise ValueError(f"duplicate span start: {path}:{line_number}")
                    starts[key] = row
                elif row.get("record_type") == "span_end":
                    start = starts.pop(key, None)
                    if start is None:
                        raise ValueError(f"unmatched span end: {path}:{line_number}")
                    a = (start.get("execution") or {}).get("execution_id")
                    b = (row.get("execution") or {}).get("execution_id")
                    if a != b:
                        raise ValueError(f"execution identity mismatch: {path}:{line_number}")
                    calls.append({"suite": suite, "task_id": path.parent.name,
                                  "trace_path": str(path), "start": start, "end": row})
            if starts:
                raise ValueError(f"{path}: {len(starts)} unmatched tool starts")
    if not calls:
        raise ValueError(f"no tool spans below {root}")
    ids = [c["start"]["execution"]["execution_id"] for c in calls
           if c["start"]["execution"].get("execution_id")]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate execution_id across source calls")
    return calls


def _pmu_rate(pmu: dict[str, Any], execution_id: str) -> tuple[float | None, str | None]:
    if not pmu:
        return None, "pmu_missing"
    if pmu.get("execution_id") != execution_id:
        return None, "pmu_execution_id_mismatch"
    coverage = pmu.get("coverage") or {}
    if (pmu.get("source") != "perf_event_open" or pmu.get("mode") != "counting"
            or coverage.get("status") != "reliable" or coverage.get("multiplexed") is not False
            or coverage.get("root_and_future_descendants") is not True
            or coverage.get("kernel_included") is not True
            or pmu.get("llc_semantics_confirmed") is not True
            or pmu.get("scope") != "task-inherit-enable-on-exec"
            or pmu.get("collector_errors")):
        return None, "pmu_coverage_unreliable"
    events = pmu.get("events") or {}
    event = events.get("llc_read_accesses") or {}
    if event.get("semantics") != "PERF_COUNT_HW_CACHE_LL:READ:ACCESS":
        return None, "pmu_event_semantics_unknown"
    raw = _number(event.get("raw_count"))
    running = _number(event.get("time_running_ns"))
    enabled = _number(event.get("time_enabled_ns"))
    if (raw is None or raw < 0 or running is None or running <= 0
            or running != enabled or event.get("running_ratio") != 1.0):
        return None, "pmu_counter_time_invalid"
    rate = raw / (running / 1e9)
    derived = _number((pmu.get("derived") or {}).get("llc_read_accesses_per_cpu_second"))
    if derived is None or not math.isclose(rate, derived, rel_tol=1e-6, abs_tol=1e-5):
        return None, "pmu_derived_denominator_mismatch"
    return rate, None


def _predict_history(training: list[dict[str, Any]], candidate: dict[str, Any],
                     *, field: str = "rate", name_only: bool = False) -> dict[str, Any]:
    command = candidate["command"]
    prefix = candidate["command_prefix"]
    scopes = [("executable", candidate["executable"])] if name_only else [
        ("repo_command", (candidate["repo"], command)),
        ("repo_prefix", (candidate["repo"], prefix)),
        ("tool_category", candidate["tool_category"]),
    ]
    for level, key in scopes:
        evidence = [x for x in training if x[field] is not None
                    and x["stack"] == candidate["stack"]
                    and x["time_ns"] < candidate["time_ns"]
                    and x["task_id"] != candidate["task_id"]
                    and (x["executable"] if level == "executable" else
                         x["tool_category"] if level == "tool_category" else
                         (x["repo"], x["command_prefix"]) if level == "repo_prefix" else
                         (x["repo"], x["command"])) == key]
        if len(evidence) >= 3:
            return {"value": statistics.median(x[field] for x in evidence),
                    "match_level": level, "evidence_count": len(evidence),
                    "evidence_execution_ids": [x["execution_id"] for x in evidence],
                    "confidence": "history_at_least_3"}
    return {"value": None, "match_level": None, "evidence_count": 0,
            "evidence_execution_ids": [], "confidence": "unknown_insufficient_history"}


def _call(call: dict[str, Any]) -> dict[str, Any]:
    start, end = call["start"], call["end"]
    execution_id = start["execution"].get("execution_id")
    resources = end.get("resources") or {}
    command = ((start.get("input") or {}).get("requested_args") or {}).get("command")
    if not isinstance(command, str):
        command = ""
    tokens = shell_command_prefix_tokens(command) if command else []
    heads = shell_command_heads(command) if command else []
    if len(tokens) >= 3 and tokens[0] == "cd" and tokens[2] in {"&&", ";"}:
        tokens = tokens[3:]
    executable = next((head for head in heads if head not in {"cd", "export", "env", "sudo", "timeout"}), "")
    category = executable
    if executable in {"python", "python3", "python3.11", "python3.12"}:
        match = re.search(r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+([\w.]+)(?:\s+([^\s;&|]+))?", command)
        script = re.search(r"\bpython(?:3(?:\.\d+)?)?\s+([^\s;&|]+\.py)\b", command)
        category = (f"python -m {match.group(1)} {match.group(2) or ''}" if match else
                    f"python {script.group(1)}" if script else
                    "python-inline:" + hashlib.sha256(command.encode()).hexdigest()[:16])
    pmu = resources.get("pmu") or {}
    rate, reason = _pmu_rate(pmu, execution_id) if execution_id else (None, "no_execution_id")
    duration = _number(resources.get("action_duration_ns"))
    if duration is not None:
        duration /= 1e9
    cpu = _number(resources.get("cpu_time_s"))
    cpu_source = ((resources.get("cgroup_resource") or {}).get("cpu_source"))
    event = (pmu.get("events") or {}).get("llc_read_accesses") or {}
    pmu_active = _number(event.get("time_running_ns"))
    if pmu_active is not None:
        pmu_active /= 1e9
    pmu_start = _number(pmu.get("started_at"))
    pmu_end = _number(pmu.get("ended_at"))
    pmu_elapsed = pmu_end - pmu_start if pmu_start is not None and pmu_end is not None else None
    busy = pmu_active / pmu_elapsed if pmu_active is not None and pmu_elapsed and pmu_elapsed > 0 else None
    output = end.get("output") or {}
    prediction = (start.get("prediction") or {}).get("pmu_prediction") or {}
    target = (prediction.get("targets") or {}).get("llc_read_accesses_per_cpu_second") or {}
    call_targets = ((start.get("prediction") or {}).get("call_prediction") or {}).get("targets") or {}
    cpu_target = call_targets.get("cpu_time_seconds") or {}
    avg_target = call_targets.get("cpu_avg_cores") or {}
    return {"suite": call["suite"], "task_id": call["task_id"],
            "trace_path": call["trace_path"], "trace_id": start.get("trace_id"),
            "span_id": start.get("span_id"), "execution_id": execution_id,
            "sequence_no": start.get("sequence_no"), "time_ns": int(start["wall_time_ns"]),
            "repo": start.get("repo"), "stack": (pmu.get("architecture"), end.get("execution", {}).get("mode"), call["suite"]),
            "command": command, "requested_args": (start.get("input") or {}).get("requested_args"),
            "command_prefix": " ".join(tokens[:4]),
            "executable": executable, "tool_category": category,
            "duration_s": duration, "cpu_time_s": cpu, "cpu_source": cpu_source,
            "pmu_active_s": pmu_active if rate is not None else None,
            "pmu_elapsed_s": pmu_elapsed if rate is not None else None,
            "busy_ratio": busy if rate is not None else None,
            "attribution_status": resources.get("attribution_status"),
            "pmu_coverage": (pmu.get("coverage") or {}).get("status"),
            "pmu_reason": reason, "rate": rate if resources.get("attribution_status") == "attributed" else None,
            "original_prediction": {"status": target.get("status"), "p50": target.get("p50"),
                                    "evidence_count": target.get("evidence_count"),
                                    "context": target.get("context"),
                                    "schema_version": prediction.get("schema_version"),
                                    "scope": prediction.get("scope"),
                                    "metric_definition": target.get("metric_definition"),
                                    "unit": target.get("unit")},
            "original_cpu_prediction": {"status": cpu_target.get("status"),
                                        "p50": cpu_target.get("p50"),
                                        "sample_count": cpu_target.get("sample_count"),
                                        "metric_definition": cpu_target.get("metric_definition")},
            "original_cpu_avg_prediction": {"status": avg_target.get("status"),
                                            "p50": avg_target.get("p50"),
                                            "sample_count": avg_target.get("sample_count")},
            "status": (end.get("status") or {}).get("code"),
            "exit_code": output.get("exit_code"),
            "checkpoint_id": None, "replayable": False,
            "replay_exclusion": "initial_task_state_and_prefix_checkpoint_absent_from_trace_export"}


def inspect(root: Path, output: Path) -> dict[str, Any]:
    calls = [_call(c) for c in _read_spans(root)]
    counts = Counter()
    for call in calls:
        counts["complete_calls"] += 1
        counts["pmu_present"] += call["pmu_coverage"] is not None
        counts["pmu_reliable"] += call["pmu_coverage"] == "reliable"
        counts["pmu_usable_attributed"] += call["rate"] is not None
        counts["cpu_time_present"] += call["cpu_time_s"] is not None
        counts["action_duration_present"] += call["duration_s"] is not None
        counts[f"attribution_{call['attribution_status']}"] += 1
        if call["pmu_reason"]:
            counts[f"excluded_{call['pmu_reason']}"] += 1
    report = {"source": str(root.resolve()), "counts": dict(counts),
              "counter": "PERF_COUNT_HW_CACHE_LL:READ:ACCESS inherited from payload root to future descendants",
              "rate_denominator": "event time_running_ns, not wall time or independently measured cgroup CPU time",
              "cpu_time_source": "resources.cpu_time_s when present; never substituted into PMU rate",
              "execution_time": "resources.action_duration_ns; asynchronous polling span duration excluded",
              "replayable_calls": 0,
              "replay_exclusion": "trace export contains no verified initial task image plus pre-target filesystem/runtime checkpoint",
              "warning": "No placement result can be claimed until checkpoint and host vCPU mapping are verified."}
    _write(output / "quality.json", report)
    _write(output / "calls.json", calls)
    return report


def _pressure(p: str, predictions: dict[str, float]) -> float:
    return max(sum(predictions[letter] for letter in cluster) for cluster in PLACEMENTS[p])


def plan(calls_path: Path, output: Path, *, test_fraction: float = .3,
         min_duration: float = 2.0, seed: int = 42,
         predictor: str = "history") -> dict[str, Any]:
    calls = json.loads(calls_path.read_text(encoding="utf-8"))
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        by_task[f"{call['suite']}:{call['task_id']}"].append(call)
    tasks = sorted(by_task, key=lambda task: min(x["time_ns"] for x in by_task[task]))
    split = max(1, min(len(tasks)-1, int(len(tasks) * (1-test_fraction))))
    training_tasks = set(tasks[:split])
    training = [x for x in calls if f"{x['suite']}:{x['task_id']}" in training_tasks]
    test = [x for x in calls if f"{x['suite']}:{x['task_id']}" not in training_tasks]
    rates = [x["rate"] for x in training if x["rate"] is not None]
    if len(rates) < 4:
        raise ValueError("fewer than four trustworthy training PMU rates")
    q1, q3 = statistics.quantiles(rates, n=4, method="inclusive")[0::2]
    candidates = []
    funnel = Counter()
    for call in test:
        funnel["test_calls"] += 1
        if not call["command"] or not call["executable"]:
            funnel["no_shell_command"] += 1
            continue
        if re.search(r"\b(?:sleep|nohup|apt-get|wget|curl|git\s+clone|pip\s+install|qemu-system)\b", call["command"]):
            funnel["external_or_background"] += 1
            continue
        if predictor == "recorded":
            source = call["original_prediction"]
            cpu_source = call["original_cpu_prediction"]
            avg_source = call["original_cpu_avg_prediction"]
            valid = (source["status"] == "available"
                     and source["schema_version"] == "pmu_prediction.v2"
                     and source["scope"] == "tool_call"
                     and source["metric_definition"] == "llc_read_accesses_over_perf_running_seconds"
                     and source["unit"] == "accesses_per_cpu_second"
                     and (source["evidence_count"] or 0) >= 3
                     and _number(source["p50"]) is not None)
            pred = {"value": source["p50"] if valid else None,
                    "match_level": source["context"] if valid else None,
                    "evidence_count": source["evidence_count"] if valid else 0,
                    "evidence_execution_ids": [],
                    "confidence": "recorded_pre_call" if valid else "unknown_insufficient_history"}
            predicted_active = {"value": cpu_source["p50"] if cpu_source["status"] == "available"
                                and cpu_source["metric_definition"] == "owned_workload_cpu_time"
                                and (cpu_source["sample_count"] or 0) >= 3 else None}
            predicted_busy = {"value": avg_source["p50"] if avg_source["status"] == "available"
                              and (avg_source["sample_count"] or 0) >= 3 else None}
        else:
            pred = _predict_history(training, call)
            predicted_active = _predict_history(training, call, field="pmu_active_s")
            predicted_busy = _predict_history(training, call, field="busy_ratio")
        name = _predict_history(training, call, name_only=True)
        if (pred["value"] is None or predicted_active["value"] is None
                or predicted_active["value"] < min_duration
                or predicted_busy["value"] is None
                or not .6 <= predicted_busy["value"] <= 1.2):
            funnel["insufficient_history_or_not_predicted_cpu_active"] += 1
            continue
        pressure = "high" if pred["value"] >= q3 else "low" if pred["value"] <= q1 else None
        if pressure is None:
            funnel["middle_quartiles"] += 1
            continue
        funnel[f"selected_{pressure}"] += 1
        candidates.append({key: call[key] for key in (
            "suite", "task_id", "trace_path", "trace_id", "span_id", "execution_id",
            "sequence_no", "time_ns", "repo", "stack", "command", "requested_args",
            "command_prefix", "executable", "tool_category", "checkpoint_id", "replayable", "replay_exclusion")}
            | {"prediction": pred, "name_prediction": name,
               "predicted_active": predicted_active, "predicted_busy": predicted_busy,
               "pressure": pressure})
    manifest = {"schema": "placement_plan_v1", "source_sha256": hashlib.sha256(calls_path.read_bytes()).hexdigest(),
                "training_tasks": sorted(training_tasks), "test_tasks": sorted(set(tasks)-training_tasks),
                "training_cutoff_ns": max(x["time_ns"] for x in training),
                "quartiles": {"low": q1, "high": q3}, "min_duration_s": min_duration,
                "seed": seed, "predictor": predictor, "candidate_funnel": dict(funnel), "candidates": candidates, "groups": [],
                "status": "candidate_selection_pending"}
    # Never silently fill a group with weak predictions or fabricated workloads.
    highs = [x for x in candidates if x["pressure"] == "high"]
    lows = [x for x in candidates if x["pressure"] == "low"]
    used: set[str] = set()
    for index in range(2):
        hh = next(((a,b) for i,a in enumerate(highs) for b in highs[i+1:]
                   if a["execution_id"] not in used and b["execution_id"] not in used), None)
        ll = next(((a,b) for i,a in enumerate(lows) for b in lows[i+1:]
                   if a["execution_id"] not in used and b["execution_id"] not in used), None)
        if hh is None or ll is None:
            break
        selected = [*hh, *ll]
        used.update(x["execution_id"] for x in selected)
        values = {letter: selected[i]["prediction"]["value"] for i,letter in enumerate("ABCD")}
        name_values = {letter: selected[i]["name_prediction"]["value"] for i,letter in enumerate("ABCD")}
        rng = random.Random(seed + index)
        shuffled = list("ABCD"); rng.shuffle(shuffled)
        rr_pair = frozenset(shuffled[:2])
        rr = next(p for p, clusters in PLACEMENTS.items() if frozenset(clusters[0]) in
                  (rr_pair, frozenset("ABCD")-rr_pair))
        group = {"id": f"G{index+1}", "calls": {letter: selected[i]["execution_id"] for i,letter in enumerate("ABCD")},
                 "scores": {p: _pressure(p, values) for p in PLACEMENTS},
                 "selected": min(PLACEMENTS, key=lambda p: (_pressure(p, values), p)),
                 "round_robin": rr,
                 "name_selected": (min(PLACEMENTS, key=lambda p: (_pressure(p, name_values), p))
                                   if all(value is not None for value in name_values.values()) else None),
                 "status": "checkpoint_unavailable"}
        manifest["groups"].append(group)
    if manifest["groups"]:
        manifest["status"] = "frozen_decisions_checkpoint_pending"
    _write(output / "plan.json", manifest)
    return manifest


def topology(host: str, output: Path) -> dict[str, Any]:
    script = """import json,pathlib
base=pathlib.Path('/sys/devices/system/cpu'); rows=[]
for d in sorted(base.glob('cpu[0-9]*'),key=lambda p:int(p.name[3:])):
 t=d/'topology'; caches=[]
 for i in (d/'cache').glob('index*'):
  try: caches.append((int((i/'level').read_text()),(i/'type').read_text().strip(),(i/'shared_cpu_list').read_text().strip()))
  except OSError: pass
 try: rows.append({'cpu':int(d.name[3:]),'cluster':(t/'cluster_cpus_list').read_text().strip(),'core':(t/'core_cpus_list').read_text().strip(),'threads':(t/'thread_siblings_list').read_text().strip(),'caches':caches})
 except OSError: pass
print(json.dumps({'node0':(pathlib.Path('/sys/devices/system/node/node0/cpulist').read_text().strip()),'cpus':rows}))"""
    command = "python3 -c " + shlex.quote(script)
    completed = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command],
                               capture_output=True, text=True, timeout=30, check=True)
    data = json.loads(completed.stdout)
    def expand(spec: str) -> set[int]:
        found = set()
        for part in spec.split(","):
            edges = part.split("-")
            found.update(range(int(edges[0]), int(edges[-1]) + 1))
        return found
    node = expand(data["node0"])
    clusters: dict[str, dict[str, Any]] = {}
    for row in data["cpus"]:
        if row["cpu"] not in node or not row["cluster"]:
            continue
        unified = [cache for cache in row["caches"] if cache[1] == "Unified"]
        if not unified:
            continue
        llc = max(unified, key=lambda item: item[0])[2]
        key = (row["cluster"], llc)
        entry = clusters.setdefault(str(key), {"cluster": row["cluster"], "llc": llc, "cores": {}})
        entry["cores"].setdefault(row["core"], row["cpu"])
    options = list(clusters.values())
    pair = next(((a, b) for i, a in enumerate(options) for b in options[i+1:]
                 if a["cluster"] != b["cluster"] and a["llc"] == b["llc"]
                 and len(a["cores"]) >= 2 and len(b["cores"]) >= 2), None)
    if pair is None:
        data["selection_error"] = "no two verified clusters with two physical cores in one NUMA0 LLC domain"
    else:
        selected = [sorted(item["cores"].values())[:2] for item in pair]
        if len({cpu for group in selected for cpu in group}) != 4:
            raise ValueError("topology selection reuses a physical CPU")
        data["selection"] = {"clusters": [item["cluster"] for item in pair],
                             "llc_domain": pair[0]["llc"], "cores": selected,
                             "pool": sorted(cpu for group in selected for cpu in group)}
    _write(output, data)
    return data


def report(plan_path: Path, rounds_path: Path, topology_path: Path, output: Path) -> dict[str, Any]:
    """Summarize verified paired rounds; never choose a policy after seeing times."""
    frozen = json.loads(plan_path.read_text(encoding="utf-8"))
    if frozen.get("schema") != "placement_plan_v1" or not frozen.get("groups"):
        raise ValueError("a frozen plan with four-call groups is required")
    rounds = json.loads(rounds_path.read_text(encoding="utf-8"))
    selection = json.loads(topology_path.read_text(encoding="utf-8")).get("selection")
    if not selection or len(selection.get("pool", [])) != 4:
        raise ValueError("verified four-core topology selection is required")
    clusters = selection["cores"]
    pool = set(selection["pool"])
    if not isinstance(rounds, list):
        raise ValueError("rounds must be a JSON list")
    plan_digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in rounds:
        if row.get("plan_sha256") != plan_digest:
            raise ValueError("round was not bound to this frozen plan")
        if row.get("placement") not in {*PLACEMENTS, "linux"}:
            raise ValueError("unknown placement")
        if row.get("status") != "verified":
            raise ValueError("unverified round cannot enter report")
        tools = row.get("tools") or {}
        if set(tools) != set("ABCD"):
            raise ValueError("round must contain all four original tools")
        starts, ends = [], []
        occupied = set()
        for letter, tool in tools.items():
            start, end = _number(tool.get("started_s")), _number(tool.get("ended_s"))
            if start is None or end is None or end <= start:
                raise ValueError("round lacks valid original tool timing")
            if tool.get("checkpoint_verified") is not True or tool.get("mapping_verified") is not True:
                raise ValueError("round lacks checkpoint or effective host mapping proof")
            allowed = tool.get("allowed_host_cpus")
            if not isinstance(allowed, list) or any(not isinstance(cpu, int) for cpu in allowed):
                raise ValueError("round lacks effective host CPU mapping")
            if tool.get("effective_mems") != [0]:
                raise ValueError("round memory constraint is not NUMA 0")
            if row["placement"] == "linux":
                if set(allowed) != pool:
                    raise ValueError("Linux control must use exactly the same four-core pool")
            else:
                cluster_index = next(i for i, group in enumerate(PLACEMENTS[row["placement"]])
                                     if letter in group)
                if len(allowed) != 1 or allowed[0] not in clusters[cluster_index]:
                    raise ValueError("static placement does not match selected physical cluster")
                if allowed[0] in occupied:
                    raise ValueError("static placement oversubscribes a physical core")
                occupied.add(allowed[0])
            starts.append(start); ends.append(end)
        row = {**row, "makespan_s": max(ends)-min(starts),
               "overlap_s": max(0., min(ends)-max(starts))}
        grouped[str(row.get("group_id"))][row["placement"]].append(row)
    summaries = []
    for group in frozen["groups"]:
        group_id = group["id"]
        observed = grouped.get(group_id, {})
        if set(observed) != {*PLACEMENTS, "linux"}:
            raise ValueError(f"{group_id}: P1/P2/P3/linux paired rounds required")
        ids = [set(row["round"] for row in observed[p]) for p in (*PLACEMENTS, "linux")]
        if len(ids[0]) < 5 or any(values != ids[0] for values in ids[1:]):
            raise ValueError(f"{group_id}: at least five paired rounds required")
        medians = {p: {"makespan_s": statistics.median(row["makespan_s"] for row in observed[p]),
                       "tool_s": {letter: statistics.median(
                           row["tools"][letter]["ended_s"]-row["tools"][letter]["started_s"]
                           for row in observed[p]) for letter in "ABCD"},
                       "minimum_overlap_s": min(row["overlap_s"] for row in observed[p])}
                   for p in (*PLACEMENTS, "linux")}
        for item in medians.values():
            item["throughput_calls_per_s"] = 4 / item["makespan_s"]
        sensitivity = {}
        for letter in "ABCD":
            durations = [medians[p]["tool_s"][letter] for p in PLACEMENTS]
            sensitivity[letter] = max(durations)/min(durations)-1
        summaries.append({"group_id": group_id, "placements": medians,
                          "selected": group["selected"], "round_robin": group["round_robin"],
                          "name_selected": group["name_selected"],
                          "posthoc_fastest_reference": min(PLACEMENTS, key=lambda p: medians[p]["makespan_s"]),
                          "sensitivity": sensitivity,
                          "high_sensitive_count": sum(sensitivity[letter] >= .1 for letter in "AB"),
                          "low_sensitive_count": sum(sensitivity[letter] >= .1 for letter in "CD")})
    result = {"plan_sha256": plan_digest, "groups": summaries,
              "note": "Selected policy is the frozen pre-execution placement; posthoc fastest is reference only."}
    _write(output, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    audit = sub.add_parser("inspect"); audit.add_argument("--traces", type=Path, required=True); audit.add_argument("--output", type=Path, required=True)
    planning = sub.add_parser("plan"); planning.add_argument("--calls", type=Path, required=True); planning.add_argument("--output", type=Path, required=True)
    planning.add_argument("--test-fraction", type=float, default=.3)
    planning.add_argument("--min-active-seconds", type=float, default=2.)
    planning.add_argument("--seed", type=int, default=42)
    planning.add_argument("--predictor", choices=("history", "recorded"), default="history")
    topo = sub.add_parser("topology"); topo.add_argument("--host", default="kunpeng"); topo.add_argument("--output", type=Path, required=True)
    summary = sub.add_parser("report"); summary.add_argument("--plan", type=Path, required=True)
    summary.add_argument("--rounds", type=Path, required=True); summary.add_argument("--topology", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inspect": result = inspect(args.traces, args.output)
    elif args.command == "plan":
        if not 0 < args.test_fraction < 1 or args.min_active_seconds <= 0:
            parser.error("test fraction must be between 0 and 1 and active seconds positive")
        result = plan(args.calls, args.output, test_fraction=args.test_fraction,
                      min_duration=args.min_active_seconds, seed=args.seed,
                      predictor=args.predictor)
    elif args.command == "topology": result = topology(args.host, args.output)
    else: result = report(args.plan, args.rounds, args.topology, args.output)
    print(json.dumps((result if args.command == "inspect" else
                      {k:v for k,v in result.items() if k not in
                       ({"candidates","training_tasks","test_tasks"} if args.command == "plan" else {"cpus"})}),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
