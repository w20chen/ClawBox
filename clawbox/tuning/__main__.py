"""Offline research pipeline over real traces (P4).

``python -m clawbox.tuning RUN_DIR... --output-dir OUT``

For each run directory, find the ClawTune span JSONL (``traces/*.jsonl``) and
the tool-bridge execution JSONL (``tool-bridge.jsonl``), join on execution_id
(exact), validate, and collect the trusted observations.  Then:

* export command-disjoint + stratified train/eval splits (jsonl/parquet),
* fit estimators and run the two-scenario ablation,
* emit a ``summary.json`` + ``summary.md`` (+ PNG charts when matplotlib is
  available).

Real trace layout on the target: a runtime pod's ``/state/<cell>/traces/``
holds span JSONL and ``tool-bridge.jsonl``; collected evidence lands under
``release-evidence/<release>/<cluster>/<run>/``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clawbox.tuning.ablation import run_ablation
from clawbox.tuning.dataset import build_joined_dataset, export_dataset
from clawbox.tuning.schema import ToolObservation


def find_run_traces(run_dir: Path) -> tuple[Path, Path]:
    """Locate (trace_dir, bridge_path) for one run directory."""
    candidates = [
        run_dir,
        run_dir / "traces",
        run_dir / "state",
        run_dir / "state" / "traces",
    ]
    trace_dir = next((c for c in candidates if c.is_dir() and list(c.glob("*.jsonl"))), None)
    if trace_dir is None:
        raise FileNotFoundError(f"no trace JSONL under {run_dir}")
    bridge = next(
        (
            c / "tool-bridge.jsonl"
            for c in [run_dir, run_dir / "traces", trace_dir]
            if (c / "tool-bridge.jsonl").is_file()
        ),
        None,
    )
    if bridge is None:
        raise FileNotFoundError(f"no tool-bridge.jsonl under {run_dir}")
    return trace_dir, bridge


def collect_observations(
    run_dirs: list[Path], ingest_secret: str | None
) -> list[ToolObservation]:
    all_trusted: list[ToolObservation] = []
    report: dict[str, object] = {}
    for run_dir in run_dirs:
        try:
            trace_dir, bridge = find_run_traces(run_dir)
        except FileNotFoundError as exc:
            report[str(run_dir)] = str(exc)
            continue
        joined, trusted = build_joined_dataset(trace_dir, bridge, ingest_secret=ingest_secret)
        all_trusted.extend(trusted)
        report[str(run_dir)] = {
            "spans": joined.span_count,
            "joined": len(joined.joined),
            "unmatched_spans": len(joined.unmatched_spans),
            "join_rate": joined.join_rate,
            "trusted": len(trusted),
        }
    print(json.dumps({"runs": report}, indent=2, sort_keys=True))
    return all_trusted


def write_summary(
    output_dir: Path, observations: list[ToolObservation], ablation_result
) -> None:
    summary = {
        "n_observations": len(observations),
        "ablation": ablation_result.summary(),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    lines = [
        "# ClawTune research summary (real traces)",
        "",
        f"- trusted observations: **{len(observations)}**",
        "",
        "## Cold-start (command-disjoint) — value of learning at all",
        "",
        "| predictor | MAE (s) | bucket acc | calibration p90 | over-alloc % |",
        "|---|---|---|---|---|",
        _scenario_row(ablation_result.cold_start),
        "",
        "## Known-command (stratified) — value of the KB per-command layer",
        "",
        "| predictor | MAE (s) | bucket acc | calibration p90 | over-alloc % |",
        "|---|---|---|---|---|",
        _scenario_row(ablation_result.known_command),
        "",
        "KB MAE delta vs fixed profile: "
        f"cold-start {ablation_result.cold_start.kb_mae_delta_pct()}%, "
        f"known-command {ablation_result.known_command.kb_mae_delta_pct()}%",
        "",
    ]
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _scenario_row(scenario) -> str:
    def cells(predictor: str) -> str:
        metrics = getattr(scenario, predictor)
        return (
            f"{predictor} | {metrics['mae_sec']:.3f} | {metrics['bucket_accuracy']:.3f} "
            f"| {metrics['calibration_p90']:.3f} | {metrics['mean_over_allocation_pct']:.2f}"
        )

    return "\n".join(cells(name) for name in ("baseline", "global_only", "kb"))


def write_charts(output_dir: Path, ablation_result) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    predictors = ("baseline", "global_only", "kb")
    for name, scenario in (("cold_start", ablation_result.cold_start), ("known_command", ablation_result.known_command)):
        mae = [getattr(scenario, p)["mae_sec"] for p in predictors]
        acc = [getattr(scenario, p)["bucket_accuracy"] for p in predictors]
        cal = [getattr(scenario, p)["calibration_p90"] for p in predictors]
        fig, axes = plt.subplots(1, 3, figsize=(14, 4))
        axes[0].bar(predictors, mae, color=["#888", "#4C72B0", "#DD8452"])
        axes[0].set_title(f"{name}: MAE (s)")
        axes[1].bar(predictors, acc, color=["#888", "#4C72B0", "#DD8452"])
        axes[1].set_title(f"{name}: bucket accuracy")
        axes[2].bar(predictors, cal, color=["#888", "#4C72B0", "#DD8452"])
        axes[2].set_title(f"{name}: calibration p90")
        for ax in axes:
            ax.set_ylim(bottom=0)
        fig.tight_layout()
        fig.savefig(output_dir / f"ablation-{name}.png", dpi=150)
        plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="run dirs (trace JSONL + tool-bridge.jsonl)")
    parser.add_argument("--output-dir", type=Path, default=Path("research-out"))
    parser.add_argument("--ingest-secret", default=None, help="HMAC secret for validation (offline traces usually unsigned)")
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--no-parquet", action="store_true", help="skip parquet export")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    observations = collect_observations(args.runs, args.ingest_secret)
    if not observations:
        print("no trusted observations; nothing to evaluate", file=sys.stderr)
        return 1
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = export_dataset(
        observations,
        output_dir / "split-command",
        train_frac=args.train_frac,
        seed=args.seed,
        write_parquet=not args.no_parquet,
        split="command",
    )
    stratified_counts = export_dataset(
        observations,
        output_dir / "split-stratified",
        train_frac=args.train_frac,
        seed=args.seed,
        write_parquet=not args.no_parquet,
        split="stratified",
    )
    ablation_result = run_ablation(observations, train_frac=args.train_frac, seed=args.seed)
    write_summary(output_dir, observations, ablation_result)
    write_charts(output_dir, ablation_result)
    print(f"splits: {counts} / {stratified_counts}")
    print(f"wrote {output_dir / 'summary.md'} + summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
