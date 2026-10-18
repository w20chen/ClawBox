import csv
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "paper-figures"
OUT.mkdir(exist_ok=True)

plt.rcParams.update({
    "font.size": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 160,
})


def save(fig, name):
    fig.tight_layout()
    fig.savefig(OUT / name, bbox_inches="tight")
    plt.close(fig)


# Admission sweep: values use the review's unified start-to-terminal window.
labels = ["28 Agents / 80 GiB", "28 Agents / 64 GiB", "24 Agents / 48 GiB"]
fixed = np.array([0.117235, 0.150475, 0.049470])
predicted = np.array([0.104308, 0.134783, 0.081117])
x = np.arange(len(labels))
w = 0.36
fig, ax = plt.subplots(figsize=(6.6, 3.3))
ax.bar(x - w / 2, fixed, w, label="Fixed reservation", color="#8c96a3")
ax.bar(x + w / 2, predicted, w, label="Predictive admission", color="#3568b8")
ax.set_ylabel("Successful Agents/s")
ax.set_xticks(x, labels)
ax.set_ylim(0, 0.17)
ax.legend(frameon=False, ncol=2, loc="upper right")
for i, delta in enumerate([-11.0, -10.4, 64.0]):
    ax.text(x[i], max(fixed[i], predicted[i]) + 0.006, f"{delta:+.1f}%", ha="center")
save(fig, "admission-sweep.svg")


# Optimized-system boundary. A zero bar is intentionally visible as a marker.
labels = ["320 Agents (v3)", "640 Agents (v6)"]
full = np.array([0.322198, 0.353383])
os_default = np.array([0.0, 0.0])
fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.2))
axes[0].bar(x[:2] - w / 2, full, w, label="Optimized system", color="#3568b8")
axes[0].bar(x[:2] + w / 2, os_default, w, label="Default management", color="#8c96a3")
axes[0].scatter(x[:2] + w / 2, os_default, color="#555555", marker="x", zorder=3)
axes[0].set_xticks(x[:2], labels)
axes[0].set_ylabel("Successful Agents/s")
axes[0].set_ylim(0, 0.39)
axes[0].legend(frameon=False)

integrated_mem = np.array([601.11, 455.14])
os_mem = np.array([1051.31, 813.55])
axes[1].bar(x[:2] - w / 2, integrated_mem, w, color="#3568b8")
axes[1].bar(x[:2] + w / 2, os_mem, w, color="#8c96a3")
axes[1].set_xticks(x[:2], labels)
axes[1].set_ylabel("Peak VM-cgroup memory (GiB)")
axes[1].set_ylim(0, 1150)
axes[1].text(0, 1080, "−42.8%", ha="center")
axes[1].text(1, 850, "−44.1%", ha="center")
save(fig, "synthetic-scale-boundary.svg")


# Real-trace progress, preserving the four fixed five-minute bins.
minutes = np.array([2.5, 7.5, 12.5, 17.5])
series = {
    "Optimized system": [5.037, 5.553, 3.643, 1.370],
    "Admission disabled": [5.050, 4.917, 4.053, 2.223],
    "Default management": [4.463, 4.810, 5.510, 6.820],
}
fig, ax = plt.subplots(figsize=(6.6, 3.3))
for name, values in series.items():
    ax.plot(minutes, values, marker="o", linewidth=2, label=name)
ax.set_xlabel("Elapsed time (min)")
ax.set_ylabel("Completed tool operations/s")
ax.set_xticks(minutes, ["0–5", "5–10", "10–15", "15–20"])
ax.set_ylim(0, 7.3)
ax.legend(frameon=False, ncol=3, loc="upper center")
ax.text(0.02, 0.04, "All three arms: 0 successful Agents", transform=ax.transAxes)
save(fig, "real-trace-progress.svg")


# Throughput/tail-latency trade-off for completed, resource-matched admission arms.
points = {
    "80 GiB": {"Fixed reservation": (0.117235, 223.56), "Predictive admission": (0.104308, 251.61)},
    "64 GiB": {"Fixed reservation": (0.150475, 182.37), "Predictive admission": (0.134783, 200.13)},
    "48 GiB": {"Fixed reservation": (0.049470, 471.54), "Predictive admission": (0.081117, 274.24)},
}
colors = {"80 GiB": "#6f4e7c", "64 GiB": "#2a9d8f", "48 GiB": "#d1495b"}
fig, ax = plt.subplots(figsize=(6.6, 3.8))
for budget, methods in points.items():
    xs = [methods["Fixed reservation"][0], methods["Predictive admission"][0]]
    ys = [methods["Fixed reservation"][1], methods["Predictive admission"][1]]
    ax.plot(xs, ys, color=colors[budget], linewidth=1.4, alpha=0.75)
    ax.scatter(xs[0], ys[0], marker="s", s=58, color=colors[budget], label=f"{budget}, Fixed reservation")
    ax.scatter(xs[1], ys[1], marker="o", s=58, color=colors[budget], label=f"{budget}, Predictive admission")
    winner = "Fixed reservation" if budget != "48 GiB" else "Predictive admission"
    wx, wy = methods[winner]
    ax.scatter([wx], [wy], facecolors="none", edgecolors="black", s=125, linewidths=1.2)
ax.set_xlabel("Successful Agents/s  (higher is better)")
ax.set_ylabel("Arrival-to-completion P90 (s, lower is better)")
ax.grid(alpha=0.2)
ax.legend(frameon=False, ncol=2, fontsize=8)
ax.text(0.02, 0.96, "Black ring: Pareto winner within the same resource budget",
        transform=ax.transAxes, va="top", fontsize=8)
save(fig, "throughput-tail-pareto.svg")


# Resource efficiency derived from Tool Steps/s and observed peak VM memory.
fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.2))
labels = ["80 GiB", "64 GiB", "48 GiB"]
fixed_eff = np.array([0.0102581, 0.0164582, 0.0074215])
pred_eff = np.array([0.0103447, 0.0165477, 0.0134977])
x3 = np.arange(3)
axes[0].bar(x3 - w / 2, fixed_eff, w, label="Fixed reservation", color="#8c96a3")
axes[0].bar(x3 + w / 2, pred_eff, w, label="Predictive admission", color="#3568b8")
axes[0].set_xticks(x3, labels)
axes[0].set_ylabel("Tool Steps/s per peak GiB")
axes[0].legend(frameon=False)
axes[0].set_title("Admission sweep")

labels = ["320 Agents", "640 Agents"]
integrated_ratio = np.array([13.42, 18.30])
axes[1].bar(np.arange(2), integrated_ratio, color="#3568b8")
axes[1].axhline(1.0, color="#555555", linestyle="--", linewidth=1)
axes[1].set_xticks(np.arange(2), labels)
axes[1].set_ylabel("Memory-efficiency ratio vs default")
axes[1].set_title("Synthetic scale")
for i, value in enumerate(integrated_ratio):
    axes[1].text(i, value + 0.45, f"{value:.1f}×", ha="center")
axes[1].set_ylim(0, 20.5)
save(fig, "tool-throughput-efficiency.svg")


# Matched real-trace trade-off: execution throughput and descriptive failure density.
labels = ["Optimized", "Admission disabled", "Default management"]
steps = np.array([3.9008, 4.0608, 5.4008])
failure_density = np.array([7.05, 17.03, 28.24])
x3 = np.arange(3)
fig, ax1 = plt.subplots(figsize=(6.6, 3.5))
bars = ax1.bar(x3, steps, color=["#3568b8", "#d18f28", "#8c96a3"], width=0.58)
ax1.set_xticks(x3, labels)
ax1.set_ylabel("Tool Steps/s")
ax1.set_ylim(0, 6.2)
ax2 = ax1.twinx()
ax2.plot(x3, failure_density, color="#b22222", marker="o", linewidth=2)
ax2.set_ylabel("Independent failures / 1K tool completions", color="#b22222")
ax2.tick_params(axis="y", labelcolor="#b22222")
ax2.set_ylim(0, 32)
for bar, value in zip(bars, steps):
    ax1.text(bar.get_x() + bar.get_width() / 2, value + 0.12, f"{value:.2f}", ha="center")
save(fig, "real-trace-throughput-failures.svg")


# Complete observation-window throughput catalog. Historical records before
# execution-scope tagging are retained as legacy unclassified command rates.
def number(value, default=None):
    if value in (None, ""):
        return default
    return float(value)


observed_runs = []
with (ROOT / "Experiments.csv").open(encoding="utf-8", newline="") as stream:
    for row in csv.DictReader(stream):
        window = number(row["window_s"])
        tool_rate = number(row["Tool_Steps_per_s"])
        legacy_count = number(row["unclassified_tool_completions"])
        if tool_rate is not None:
            step_rate = tool_rate
            step_count = number(row["tool_completions"], 0.0)
            metric_scope = "application-tool"
        elif legacy_count is not None and window:
            step_rate = legacy_count / window
            step_count = legacy_count
            metric_scope = "legacy-unclassified"
        else:
            step_rate = None
            step_count = None
            metric_scope = "unavailable"

        successes = int(number(row["success_agents"], 0.0))
        target = int(number(row["c"], 0.0))
        if successes == 0:
            completion_class = "No completed Agent"
        elif successes < target:
            completion_class = "Partial Agent completion"
        else:
            completion_class = "All Agents completed"

        peak = number(row["VM_cgroup_peak_GiB"])
        peak_scope = "VM cgroup"
        if peak is None:
            peak = number(row["LOCAL_peak_GiB"])
            peak_scope = "LOCAL" if peak is not None else "unavailable"

        observed_runs.append({
            "source": "three-day review",
            "run_id": row["run_id"],
            "policy": row["policy"],
            "target_agents": target,
            "window_s": window,
            "successful_agents": successes,
            "failed_agents": int(number(row["independent_failures"], 0.0)),
            "observed_step_count": step_count,
            "observed_steps_per_s": step_rate,
            "metric_scope": metric_scope,
            "peak_memory_GiB": peak,
            "peak_memory_scope": peak_scope,
            "completion_class": completion_class,
            "result_state": row["result_status"] or row["run_state"],
        })

supplement = [
    ("Planned optimized", "Optimized system", 960.0, 2319, 0, 5, 358.3),
    ("Planned admission disabled", "Admission disabled", 960.0, 4807, 0, 20, 357.9),
    ("Gate-12 calibration", "Optimized system", 300.0, 1, 0, 0, 217.5),
    ("1.5-s arrival calibration", "Optimized system", 600.0, 3332, 0, 26, 312.0),
    ("2.5-s arrival calibration", "Optimized system", 900.0, 4007, 0, 37, 388.2),
]
for run_id, policy, window, count, successes, failures, peak in supplement:
    observed_runs.append({
        "source": "c320 supplement",
        "run_id": run_id,
        "policy": policy,
        "target_agents": 320,
        "window_s": window,
        "successful_agents": successes,
        "failed_agents": failures,
        "observed_step_count": count,
        "observed_steps_per_s": count / window,
        "metric_scope": "application-tool",
        "peak_memory_GiB": peak,
        "peak_memory_scope": "LOCAL",
        "completion_class": "No completed Agent",
        "result_state": "bounded observation",
    })

catalog_fields = list(observed_runs[0].keys())
with (ROOT / "paper-observed-throughput.csv").open("w", encoding="utf-8", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=catalog_fields)
    writer.writeheader()
    writer.writerows(observed_runs)

fig, ax = plt.subplots(figsize=(7.2, 3.8))
class_style = {
    "No completed Agent": ("#d1495b", "o"),
    "Partial Agent completion": ("#d18f28", "^"),
    "All Agents completed": ("#2a9d8f", "s"),
}
for completion_class, (color, marker) in class_style.items():
    selected = [r for r in observed_runs
                if r["completion_class"] == completion_class
                and r["observed_steps_per_s"] is not None]
    ax.scatter([r["window_s"] for r in selected],
               [r["observed_steps_per_s"] for r in selected],
               s=32, alpha=0.72, color=color, marker=marker,
               label=f"{completion_class} (n={len(selected)})")
legacy = [r for r in observed_runs if r["metric_scope"] == "legacy-unclassified"]
ax.scatter([r["window_s"] for r in legacy],
           [r["observed_steps_per_s"] for r in legacy],
           s=68, facecolors="none", edgecolors="black", marker="D",
           label=f"Legacy unclassified rate (n={len(legacy)})")
ax.set_xscale("log")
ax.set_xlabel("Observed window (s, log scale)")
ax.set_ylabel("Observed Steps/s")
ax.set_ylim(-0.12, 6.0)
ax.grid(alpha=0.18)
ax.legend(frameon=False, fontsize=8, ncol=2, loc="upper left")
ax.text(0.99, 0.03, "77/78 started runs have a measurable step rate",
        transform=ax.transAxes, ha="right", fontsize=8)
save(fig, "all-run-observed-throughput.svg")


# Window-throughput/memory Pareto analysis. Startup-only points with fewer than
# 1,000 completed application steps are shown but excluded from dominance.
def pareto_indices(xs, ys):
    result = []
    for i, (x_i, y_i) in enumerate(zip(xs, ys)):
        dominated = any(
            j != i and x_j >= x_i and y_j <= y_i
            and (x_j > x_i or y_j < y_i)
            for j, (x_j, y_j) in enumerate(zip(xs, ys))
        )
        if not dominated:
            result.append(i)
    return result


c320_points = [
    ("Planned optimized", 2319 / 960, 358.3, 2319),
    ("Admission disabled", 4807 / 960, 357.9, 4807),
    ("Gate 12", 1 / 300, 217.5, 1),
    ("1.5-s arrivals", 3332 / 600, 312.0, 3332),
    ("2.5-s arrivals", 4007 / 900, 388.2, 4007),
]
c640_points = [
    ("HIGH 352", 2.956, 291.0, 3486),
    ("HIGH 512", 4.762, 389.7, 5238),
    ("HIGH 640", 2.884, 419.4, 3216),
    ("HIGH 640 repeat", 1.350, 485.3, 3240),
    ("HIGH 896", 5.259, 413.7, 3667),
    ("Scale .75 optimized", 3.901, 510.7, 4681),
    ("Scale .75 admission off", 4.061, 537.9, 4873),
    ("Scale .75 default", 5.401, 580.2, 6481),
]
fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
for ax, title, points, ylabel in [
    (axes[0], "320-Agent supplement", c320_points, "LOCAL peak (GiB)"),
    (axes[1], "640-Agent configuration sweep", c640_points, "VM-cgroup peak (GiB)"),
]:
    candidates = [p for p in points if p[3] >= 1000]
    px = [p[1] for p in candidates]
    py = [p[2] for p in candidates]
    front = pareto_indices(px, py)
    ordered = sorted((px[i], py[i]) for i in front)
    if len(ordered) > 1:
        ax.plot([p[0] for p in ordered], [p[1] for p in ordered],
                color="black", linewidth=1.2, alpha=0.7)
    for label, step_rate, peak, count in points:
        eligible = count >= 1000
        ax.scatter(step_rate, peak, s=58,
                   color="#3568b8" if eligible else "#aaaaaa",
                   marker="o" if eligible else "x")
        ax.annotate(label, (step_rate, peak), xytext=(4, 4),
                    textcoords="offset points", fontsize=7)
    for i in front:
        ax.scatter(px[i], py[i], s=130, facecolors="none",
                   edgecolors="black", linewidths=1.2)
    ax.set_title(title)
    ax.set_xlabel("Tool Steps/s (higher is better)")
    ax.set_ylabel(ylabel + " (lower is better)")
    ax.grid(alpha=0.18)
axes[0].text(0.02, 0.97, "Black ring: non-dominated; gray: <1,000 steps",
             transform=axes[0].transAxes, va="top", fontsize=7)
save(fig, "window-throughput-memory-pareto.svg")


# Matched real-trace throughput/failure-density fronts.
matched = {
    "320 Agents": [
        ("Optimized", 2319 / 960, 1000 * 5 / 2319),
        ("Admission disabled", 4807 / 960, 1000 * 20 / 4807),
    ],
    "640 Agents": [
        ("Optimized", 3.9008, 7.05),
        ("Admission disabled", 4.0608, 17.03),
        ("Default management", 5.4008, 28.24),
    ],
}
fig, ax = plt.subplots(figsize=(6.8, 3.8))
styles = {"320 Agents": ("#3568b8", "o"), "640 Agents": ("#d18f28", "s")}
for group, points in matched.items():
    color, marker = styles[group]
    ordered = sorted(points, key=lambda p: p[1])
    ax.plot([p[1] for p in ordered], [p[2] for p in ordered],
            color=color, linewidth=1.5, alpha=0.8)
    for label, step_rate, failure_rate in ordered:
        ax.scatter(step_rate, failure_rate, s=70, color=color, marker=marker)
        ax.annotate(label, (step_rate, failure_rate), xytext=(5, 4),
                    textcoords="offset points", fontsize=8)
    ax.plot([], [], color=color, marker=marker, label=group)
ax.set_xlabel("Tool Steps/s (higher is better)")
ax.set_ylabel("Failures / 1K completed tool steps (lower is better)")
ax.grid(alpha=0.18)
ax.legend(frameon=False)
save(fig, "throughput-failure-pareto.svg")
