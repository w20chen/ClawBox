from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from clawbox.replay.paper_report import (
    DENSITY_BASELINES,
    MAIN_BASELINES,
    METRICS,
    build_report,
    load_complete_suite,
    markdown_report,
)


def _suite(root: Path, concurrency: int, baselines: tuple[str, ...]) -> None:
    root.mkdir()
    macro = {}
    for offset, baseline in enumerate(baselines, 1):
        macro[f"c{concurrency:02d}/{baseline}"] = {
            metric: {"n": 1, "mean": float(offset), "ci95_half_width": None}
            for metric in METRICS
        }
    summary = {
        "all_successful": True,
        "concurrency_levels": [concurrency],
        "independent_units": ["owner/repo#1"],
        "runs": [{
            "status": 0,
            "final_state_equal": True,
            "groups": {baseline: {"runs": 1} for baseline in baselines},
        }],
        "macro_statistics": macro,
        "paired_contrasts": {
            f"c{concurrency:02d}/checkpoint_vs_resident/fixed2": {
                "wall_s": {
                    "n": 1,
                    "mean": 2.0,
                    "ci95_half_width": None,
                    "pair_count": 3,
                    "percent_effect_statistics": {"n": 1, "mean": 4.0},
                }
            }
        },
    }
    (root / "suite-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    fields = (
        "workload", "concurrency", "baseline", "repetition", "inference_backend",
        "failure_count", "sessions_requested", "sessions_completed",
        "block_final_state_equal", "correctness_pass_fraction",
        "throughput_correct_tasks_per_minute",
    )
    with (root / "measurements.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for baseline in baselines:
            writer.writerow({
                "baseline": baseline,
                "workload": "trace-a",
                "concurrency": concurrency,
                "repetition": 0,
                "inference_backend": "replay",
                "failure_count": 0,
                "sessions_requested": concurrency,
                "sessions_completed": concurrency,
                "block_final_state_equal": True,
                "correctness_pass_fraction": 1.0,
                "throughput_correct_tasks_per_minute": 1.0,
            })


def test_build_report_uses_registered_macro_statistics(tmp_path: Path) -> None:
    main = tmp_path / "main"
    density = tmp_path / "density"
    _suite(main, 1, MAIN_BASELINES)
    _suite(density, 40, DENSITY_BASELINES)
    report = build_report(main, density)
    assert report["inference"]["independent_task_n"] == 1
    assert report["inference"]["ci95_estimable"] is False
    assert len(report["main"]["rows"]) == 6
    assert len(report["density"]["rows"]) == 2
    assert report["main"]["rows"][0]["correct_tasks_per_min"] == 1.0
    assert report["main"]["rows"][0]["mean_rss_gib"] == pytest.approx(1 / 2**30)
    rendered = markdown_report(report)
    assert "Independent task n = 1" in rendered
    assert "not estimable" in rendered


def test_load_complete_suite_rejects_incorrect_arm(tmp_path: Path) -> None:
    root = tmp_path / "bad"
    _suite(root, 1, MAIN_BASELINES)
    text = (root / "measurements.csv").read_text(encoding="utf-8")
    (root / "measurements.csv").write_text(text.replace(",1.0,1.0\n", ",0.0,1.0\n", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity/correctness"):
        load_complete_suite(root)


def test_load_complete_suite_rejects_missing_arm(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    _suite(root, 1, MAIN_BASELINES)
    lines = (root / "measurements.csv").read_text(encoding="utf-8").splitlines()
    (root / "measurements.csv").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 6"):
        load_complete_suite(root)


def test_load_complete_suite_rejects_duplicate_arm(tmp_path: Path) -> None:
    root = tmp_path / "duplicate"
    _suite(root, 1, MAIN_BASELINES)
    lines = (root / "measurements.csv").read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[1]
    (root / "measurements.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate arm identity"):
        load_complete_suite(root)
