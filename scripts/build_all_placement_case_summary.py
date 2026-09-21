"""Build a source-labelled index of every LLC-placement case retained locally.

This is an artifact/report generator.  It intentionally keeps measured replay,
trace-only profile, timing-only, legacy formal placement, and synthetic cases
separate instead of treating them as interchangeable evidence.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / ".artifacts"
OUT = ART / "llc-placement-discovery-20260914" / "all_tested_case_metrics"
OUT.mkdir(parents=True, exist_ok=True)


def read_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def median(values):
    values = [float(v) for v in values if v not in (None, "", "null")]
    return statistics.median(values) if values else None


def f(v):
    return "" if v is None else f"{float(v):.6f}"


def trace_profile(row, sequence):
    """Read the PMU/resource record for a selected original trace tool call."""
    path = Path(row["trace_path"])
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(obj.get("sequence_no", -1)) != int(sequence):
                continue
            resources = obj.get("resources") or {}
            pmu = resources.get("pmu")
            if not pmu:
                continue
            events = pmu["events"]
            reads = float(events["llc_read_accesses"]["raw_count"])
            misses = float(events["llc_read_misses"]["raw_count"])
            instructions = float(events["instructions"]["raw_count"])
            return {
                "duration_s": float(row["duration_s"]),
                "cpu_s": float(row["cpu_time_s"]),
                "cpu_ratio": float(row["cpu_active_ratio"]),
                "rss_mib": (float(resources["rss_peak_bytes"]) / 2**20) if resources.get("rss_peak_bytes") else None,
                "memory_peak_mib": (float(resources["memory_total_peak_bytes"]) / 2**20) if resources.get("memory_total_peak_bytes") else None,
                "llc_read_m_cpu_s": float(row["llc_read_M_per_CPU_s"]),
                "llc_miss_m_cpu_s": float(row["llc_miss_M_per_CPU_s"]),
                "h_m_cpu_s": float(row["H_M_per_CPU_s"]),
                "miss_rate_pct": float(row.get("llc_miss_rate") or pmu["derived"]["llc_miss_rate"]) * 100,
                "read_mpki": reads / instructions * 1000,
                "miss_mpki": misses / instructions * 1000,
                "hit_mpki": (reads - misses) / instructions * 1000,
                "ipc": float(row.get("ipc") or pmu["derived"]["ipc"]),
                "pmu_running_pct": float(pmu["coverage"]["running_ratio"]) * 100,
            }
    raise RuntimeError(f"PMU record not found: {path} sequence {sequence}")


def base_row(case, task, tool, source, command, notes, profile=None):
    profile = profile or {}
    return {
        "case": case,
        "task": task,
        "tool": tool,
        "source_type": source,
        "command": command,
        "profile_duration_s": profile.get("duration_s"),
        "profile_cpu_s": profile.get("cpu_s"),
        "profile_cpu_ratio_or_avg_pct": profile.get("cpu_ratio"),
        "profile_rss_peak_mib": profile.get("rss_mib"),
        "profile_memory_peak_mib": profile.get("memory_peak_mib"),
        "profile_llc_read_M_per_CPU_s": profile.get("llc_read_m_cpu_s"),
        "profile_llc_miss_M_per_CPU_s": profile.get("llc_miss_m_cpu_s"),
        "profile_H_M_per_CPU_s": profile.get("h_m_cpu_s"),
        "profile_llc_miss_pct": profile.get("miss_rate_pct"),
        "profile_llc_read_MPKI": profile.get("read_mpki"),
        "profile_llc_miss_MPKI": profile.get("miss_mpki"),
        "profile_llc_hit_MPKI": profile.get("hit_mpki"),
        "profile_IPC": profile.get("ipc"),
        "profile_PMU_running_pct": profile.get("pmu_running_pct"),
        "same_makespan_s": None,
        "2slice_makespan_s": None,
        "4slice_makespan_s": None,
        "same_to_4_speedup_pct": None,
        "placement_repetitions": None,
        "placement_status": "not measured",
        "placement_same_llc_miss_pct": None,
        "placement_same_llc_miss_MPKI": None,
        "placement_same_llc_hit_MPKI": None,
        "placement_same_IPC": None,
        "placement_same_PMU_running_pct": None,
        "notes": notes,
    }


rows = []

# The three TerminalBench audit workloads.
specified = {
    x["workload"].split()[-1]: x
    for x in read_csv(ART / "llc-placement-discovery-20260914" / "specified_candidates.csv")
}
audit_summary = json.loads(
    (ART / "llc-placement-discovery-20260914" / "audit" / "audit_summary.json").read_text(encoding="utf-8")
)
audit_trials = read_csv(ART / "llc-placement-discovery-20260914" / "audit" / "audit_trial_metrics.csv")
audit_workloads = read_csv(ART / "llc-placement-discovery-20260914" / "audit" / "audit_workloads.csv")
audit_workload_by_case = {x["candidate"]: x for x in audit_workloads}
for case, item in specified.items():
    aw = audit_workload_by_case[case]
    p = {
        "duration_s": aw["trace_duration_s"],
        "cpu_s": aw["trace_cpu_time_s"],
        "cpu_ratio": aw["trace_busy_ratio"],
        "rss_mib": None,
        "memory_peak_mib": None,
        "llc_read_m_cpu_s": item["llc_read_M_per_CPU_s"],
        "llc_miss_m_cpu_s": item["llc_miss_M_per_CPU_s"],
        "h_m_cpu_s": item["H_M_per_CPU_s"],
        "miss_rate_pct": float(item["llc_miss_rate"]) * 100,
        "ipc": aw["trace_ipc"],
        "pmu_running_pct": float(aw["trace_pmu_running_ratio"]) * 100,
    }
    terminal_trace = Path("C:/Users/29068/Desktop/downloaded-runs/pair-20260913-201717-traces/terminal/traces") / aw["trace"] / "trace.jsonl"
    if terminal_trace.exists():
        trace_row = dict(aw)
        trace_row.update({"trace_path": str(terminal_trace), "duration_s": aw["trace_duration_s"], "cpu_time_s": aw["trace_cpu_time_s"], "cpu_active_ratio": aw["trace_busy_ratio"], "H_M_per_CPU_s": aw["trace_H_M_per_CPU_s"], "llc_read_M_per_CPU_s": aw["trace_llc_read_M_per_CPU_s"], "llc_miss_M_per_CPU_s": aw["trace_llc_miss_M_per_CPU_s"], "llc_miss_rate": aw["trace_llc_miss_rate"] or item["llc_miss_rate"], "ipc": aw["trace_ipc"], "pmu_running_ratio": aw["trace_pmu_running_ratio"]})
        p = trace_profile(trace_row, aw["sequence_no"])
    row = base_row(case, item["workload"], "terminal.exec (TerminalBench)", "real placement replay",
                   audit_workload_by_case[case]["command"], "fresh pre-tool checkpoint; 3 placements x 3 runs", p)
    s = audit_summary[case]
    same_s = median(s["same-slice"]["makespan_s"])
    two_s = median(s["2-slice"]["makespan_s"])
    four_s = median(s["4-slice"]["makespan_s"])
    row.update({
        "same_makespan_s": same_s,
        "2slice_makespan_s": two_s,
        "4slice_makespan_s": four_s,
        "same_to_4_speedup_pct": (same_s / four_s - 1) * 100,
        "placement_repetitions": 3,
        "placement_status": "verified screening / weak-positive calibration",
    })
    same = [x for x in audit_trials if x["candidate"] == case and x["placement"] == "same-slice"]
    row["placement_same_llc_hit_MPKI"] = median(
        (float(x["llc_read"]) - float(x["llc_miss"])) / float(x["instructions_sum"]) * 1000
        for x in same
    )
    row["placement_same_llc_miss_MPKI"] = median(float(x["llc_miss"]) / float(x["instructions_sum"]) * 1000 for x in same)
    row["placement_same_llc_miss_pct"] = median(float(x["llc_miss_rate"]) * 100 for x in same)
    row["placement_same_IPC"] = median(float(x["ipc"]) for x in same)
    row["placement_same_PMU_running_pct"] = median(float(x["pmu_running_ratio_min"]) * 100 for x in same)
    rows.append(row)

# Six SWE-ReBench replica-placement workloads.
monitor = json.loads(
    (ART / "placement-replica-analysis" / "original-monitor-records.json").read_text(encoding="utf-8")
)
placement_summary = {x["candidate"]: x for x in read_csv(ART / "placement-replica-analysis" / "summary.csv")}
commands = {
    "clique": "cd /workspace && pip install -e . -q 2>&1 | tail -3 && python -m pytest test/ -q 2>&1 | tail -8",
    "ctl": "cd /workspace && git stash && PYTHONPATH=src python -m pytest tests/ -q --deselect tests/test_plugin_pypi.py::test_validate_dist 2>&1 | tail -12; git stash pop",
    "django": "cd /workspace && python -m pytest tests/unit/test_linter.py -q -p no:cacheprovider 2>&1 | grep -E \"PASSED|passed|failed\" ; python -m pytest tests/unit/test_linter.py -v -p no:cacheprovider 2>&1 | grep -E \"PASSED|FAILED\"",
    "lilio": "cd /workspace && python -c \"import pandas as pd, numpy as np, xarray as xr, sys; sys.path.insert(0,'.'); import lilio; from lilio import Calendar; time=pd.date_range('2020-01-01','2022-01-01',freq='1d'); da=xr.DataArray(np.random.rand(len(time)),coords={'time':time},dims='time'); cal=Calendar('12-25'); cal.map_to_data(da);\ntry: lilio.resample(cal, da)\nexcept ValueError as e: print('OK:', e)\"",
    "django_sql": "python -c \"from django_migration_linter.sql_analyser import analyse_sql_statements, get_sql_analyser_class; sql=['CREATE INDEX CONCURRENTLY \\\"delete_data_after_idx\\\" ON \\\"models_prediction\\\" (\\\"delete_data_after\\\") WHERE (\\\"data_deleted_at\\\" IS NULL AND \\\"delete_data_after\\\" IS NOT NULL);']; errors, ignored, warnings=analyse_sql_statements(get_sql_analyser_class('postgresql'), sql_statements=sql); print('errors:', errors); print('warnings:', warnings)\" 2>&1 | tail -5",
    "spectree": "cd /workspace && python -c \"from pydantic import BaseModel, Field; from spectree.utils import parse_params; class HelloForm(BaseModel): user: str; msg: str = Field(description='msg test', example='aa'); index: int; class F: query='HelloForm'; models={'HelloForm':HelloForm.schema()}; import json; print(json.dumps(parse_params(F,[],models),indent=2))\"",
}
for x in monitor:
    case = x["task"]
    r = x["resources"]
    p = r["pmu"]
    d = p["derived"]
    e = p["events"]
    reads = float(e["llc_read_accesses"]["raw_count"])
    misses = float(e["llc_read_misses"]["raw_count"])
    instructions = float(e["instructions"]["raw_count"])
    prof = {
        "duration_s": float(r["action_duration_ns"]) / 1e9,
        "cpu_s": float(r["cpu_time_s"]),
        "cpu_ratio": float(r["cpu_utilization_avg_pct"]),
        "rss_mib": (float(r["rss_peak_bytes"]) / 2**20) if r.get("rss_peak_bytes") else None,
        "memory_peak_mib": (float(r["memory_total_peak_bytes"]) / 2**20) if r.get("memory_total_peak_bytes") else None,
        "llc_read_m_cpu_s": float(d["llc_read_accesses_per_cpu_second"]) / 1e6,
        "llc_miss_m_cpu_s": float(d["llc_read_misses_per_cpu_second"]) / 1e6,
        "h_m_cpu_s": (float(d["llc_read_accesses_per_cpu_second"]) - float(d["llc_read_misses_per_cpu_second"])) / 1e6,
        "miss_rate_pct": float(d["llc_miss_rate"]) * 100,
        "read_mpki": reads / instructions * 1000,
        "miss_mpki": misses / instructions * 1000,
        "hit_mpki": (reads - misses) / instructions * 1000,
        "ipc": float(d["ipc"]),
        "pmu_running_pct": float(p["coverage"]["running_ratio"]) * 100,
    }
    s = placement_summary[case]
    row = base_row(case, x.get("repo", case), "exec (SWE-ReBench shell tool)", "real placement replay",
                   commands[case], "fixed pre-tool checkpoint; 4 replicas; PMU reliable; 3 runs per placement" if int(s["rounds"]) == 3 else "fixed pre-tool checkpoint; 4 replicas; PMU reliable; 2 runs per placement", prof)
    row.update({
        "same_makespan_s": s["same_slice_median_s"],
        "2slice_makespan_s": s["two_slice_median_s"],
        "4slice_makespan_s": s["four_slice_median_s"],
        "same_to_4_speedup_pct": (float(s["same_slice_median_s"]) / float(s["four_slice_median_s"]) - 1) * 100,
        "placement_repetitions": s["rounds"],
        "placement_status": "verified placement replay; below 10% opportunity threshold",
    })
    rows.append(row)

# Trace-only candidates retained in the cache-pressure scan.  These have no
# valid same/2/4 placement replay in the local artifacts.
cache_rows = read_csv(ART / "llc-placement-discovery-20260914" / "cache_pressure_candidates.csv")
trace_only = {
    "django-read-migrations": ("983994178348e63426df", "75"),
    "hyp3-pytest-suite": ("7b01d9a221b63055069b", "11"),
    "mbed-test-mbed-program": ("d74471b12c62091c0318", "36"),
    "scim2-pytest-suite": ("1142b2bc2358172700d6", "24"),
}
for case, (task, seq) in trace_only.items():
    x = next(v for v in cache_rows if v["task_id"] == task and v["sequence_no"] == seq)
    prof = trace_profile(x, seq)
    row = base_row(case, x["repo"], "exec (SWE-ReBench shell tool)", "original trace profile only",
                   x["command"], "profile recorded; no valid placement trial retained", prof)
    row["placement_status"] = "not run / no placement trial"
    rows.append(row)

# scim2-flake8: profile exists, but the replay was rejected because of the
# stdout digest newline mismatch.  Keep the profile and the failure explicit.
x = next(v for v in cache_rows if v["task_id"] == "d5b3ab7233dd9f436750" and v["sequence_no"] == "32")
prof = trace_profile(x, "32")
row = base_row("scim2-flake8", x["repo"], "exec (SWE-ReBench shell tool)", "original trace profile only",
               x["command"], "no valid placement trial: checkpoint digest expected exit=0 but replay stdout was exit=0\\n", prof)
row["placement_status"] = "no valid placement trial"
rows.append(row)

# Conversation-only timing note; deliberately no PMU/memory values.
row = base_row("scim2-flake8 timing note", "15five/scim2-filter-parser", "exec", "conversation-only timing",
               "same command as scim2-flake8", "same [8.860356, 8.867197, 8.872501] s; 4-slice [8.594740, 8.643682, 8.605950] s; no local trial/PMU artifact")
row.update({"same_makespan_s": 8.8671968, "4slice_makespan_s": 8.6059501,
            "same_to_4_speedup_pct": (8.8671968 / 8.6059501 - 1) * 100,
            "placement_repetitions": 3, "placement_status": "timing-only, not audit-valid"})
rows.append(row)

# Legacy formal placement experiment: P1/P2/P3/linux are a different design
# (pairing groups), not the later same/2/4-slice definition.
for version in ("placement-formal", "placement-formal-v2"):
    report = json.loads((ART / version / "report.json").read_text(encoding="utf-8"))
    group = report["groups"][0]
    for tool in "ABCD":
        row = base_row(f"{version}:{tool}", "legacy formal group G1", f"tool {tool}", "legacy formal placement",
                       "see plan.json target command", "P1/P2/P3/linux pairing experiment; not same/2/4; report has no unified per-tool memory summary")
        row["same_makespan_s"] = group["placements"]["P1"]["tool_s"][tool]
        row["2slice_makespan_s"] = group["placements"]["P2"]["tool_s"][tool]
        row["4slice_makespan_s"] = group["placements"]["P3"]["tool_s"][tool]
        row["same_to_4_speedup_pct"] = (row["same_makespan_s"] / row["4slice_makespan_s"] - 1) * 100
        row["placement_status"] = "legacy counterfactual; P1/P2/P3 labels are not current slice semantics"
        rows.append(row)

# Controlled synthetic pointer-chasing sweep.  These rows are toy controls,
# not Agent tools; the partial sweep was stopped before the planned fourth
# domain rotation and is therefore descriptive only.
partial = read_csv(ART / "llc-placement-discovery-20260914" / "working_set_partial_analysis" / "summary.csv")
for x in partial:
    case = x["candidate"]
    row = base_row(case, "local synthetic benchmark", "pointer-chase microbenchmark", "synthetic partial sweep",
                   "private per-replica buffer; randomized pointer chasing; see working_set_partial_analysis/summary.csv",
                   "not a real Agent tool; same/2/4 counts are retained as recorded")
    row.update({
        "profile_duration_s": x["same-slice_makespan_s_median"],
        "profile_cpu_s": x["same-slice_cpu_time_s_median"],
        "profile_rss_peak_mib": float(x["same-slice_memory_peak_median_bytes_median"]) / 2**20,
        "profile_memory_peak_mib": float(x["same-slice_memory_peak_median_bytes_median"]) / 2**20,
        "profile_llc_read_M_per_CPU_s": float(x["same-slice_llc_loads_per_cpu_s_median"]) / 1e6,
        "profile_llc_miss_M_per_CPU_s": float(x["same-slice_llc_misses_per_cpu_s_median"]) / 1e6,
        "profile_llc_miss_pct": float(x["same-slice_llc_miss_rate_median"]) * 100,
        "profile_llc_miss_MPKI": x["same-slice_llc_mpki_median"],
        "profile_IPC": x["same-slice_ipc_median"],
        "same_makespan_s": x["same-slice_makespan_s_median"],
        "2slice_makespan_s": x["2-slice_makespan_s_median"],
        "4slice_makespan_s": x["4-slice_makespan_s_median"],
        "same_to_4_speedup_pct": (float(x["same_over_4_speedup"]) - 1) * 100,
        "placement_same_llc_miss_pct": float(x["same-slice_llc_miss_rate_median"]) * 100,
        "placement_same_llc_miss_MPKI": x["same-slice_llc_mpki_median"],
        "placement_same_IPC": x["same-slice_ipc_median"],
        "placement_repetitions": f"{x['same-slice_n']}/{x['2-slice_n']}/{x['4-slice_n']}",
        "placement_status": "partial synthetic; incomplete rotation",
    })
    rows.append(row)


fields = list(rows[0])
csv_path = OUT / "all_placement_case_summary.csv"
with csv_path.open("w", encoding="utf-8", newline="") as f_out:
    writer = csv.DictWriter(f_out, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (f(v) if isinstance(v, (int, float)) else v) for k, v in row.items()})

md_path = OUT / "all_placement_case_summary.md"
with md_path.open("w", encoding="utf-8") as f_out:
    f_out.write("# All retained LLC-placement cases\n\n")
    f_out.write("`profile_*` is execution-before-placement or original-trace data; `*_makespan_s` is placement replay data. Blank means not measured, not zero. `profile_llc_hit_MPKI = (loads - misses) / instructions * 1000`.\n\n")
    f_out.write("| case | tool | source | duration s | CPU s | RSS peak MiB | LLC miss % | miss MPKI | hit MPKI | IPC | same s | 2-slice s | 4-slice s | same→4 | status |\n|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|\n")
    for row in rows:
        def cell(key):
            return "" if row[key] in (None, "") else (f"{float(row[key]):.3f}" if key != "placement_status" else row[key])
        f_out.write("| " + " | ".join([row["case"], row["tool"], row["source_type"], cell("profile_duration_s"), cell("profile_cpu_s"), cell("profile_rss_peak_mib"), cell("profile_llc_miss_pct"), cell("profile_llc_miss_MPKI"), cell("profile_llc_hit_MPKI"), cell("profile_IPC"), cell("same_makespan_s"), cell("2slice_makespan_s"), cell("4slice_makespan_s"), cell("same_to_4_speedup_pct"), row["placement_status"]]) + " |\n")
    f_out.write("\nExact commands are in the CSV and in the source artifacts listed in the handoff response. Additional synthetic runs not duplicated as summary rows are retained in `pointer-chase-sweep/summary.csv`, `pointer-chase-final/summary.csv`, `pointer-chase-final5/summary.csv`, and `working_set_sanity/{rounds-v2,stream-v2-rounds,stream-v3-rounds}.jsonl`; they are intentionally not mixed into the real-tool rows.\n")

print(csv_path)
print(md_path)
print(f"rows={len(rows)}")
