"""Smoke test for the research CLI (python -m clawbox.tuning, P4)."""

from __future__ import annotations

import json

from clawbox.tuning import __main__ as tuning_main


def _write_run(run_dir, prefix):
    from tests.test_tuning import span_end

    (run_dir / "traces").mkdir(parents=True)
    commands = ["python -m pytest -q", "make -j12 all", "git diff HEAD"]
    spans = [
        span_end(f"{prefix}-{i:03d}", command=command, duration_sec=(i + 1) * 2.0)
        for i, command in enumerate(commands)
    ]
    (run_dir / "traces" / "run.jsonl").write_text(
        "\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8"
    )
    bridges = [
        {
            "timestamp": "2026-08-19T00:00:00Z",
            "execution_id": f"{prefix}-{i:03d}",
            "execution_source": "runtime-envelope",
            "duration_ms": (i + 1) * 2.0 * 1000,
            "exit_code": 0,
            "timed_out": False,
            "stdout_bytes": 128,
            "stderr_bytes": 0,
        }
        for i in range(len(commands))
    ]
    (run_dir / "traces" / "tool-bridge.jsonl").write_text(
        "\n".join(json.dumps(b) for b in bridges) + "\n", encoding="utf-8"
    )


def test_cli_pipeline_over_synthetic_runs(tmp_path):
    run0 = tmp_path / "run0"
    run1 = tmp_path / "run1"
    _write_run(run0, "cli-a")
    _write_run(run1, "cli-b")
    output = tmp_path / "out"
    code = tuning_main.main([str(run0), str(run1), "--output-dir", str(output), "--no-parquet"])
    assert code == 0
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_observations"] == 6
    assert output.joinpath("summary.md").is_file()
    assert output.joinpath("split-command", "train.jsonl").is_file()
    assert output.joinpath("split-command", "eval.jsonl").is_file()


def test_cli_reports_no_trusted_when_missing_bridge(tmp_path):
    run = tmp_path / "run"
    from tests.test_tuning import span_end

    (run / "traces").mkdir(parents=True)
    (run / "traces" / "run.jsonl").write_text(
        json.dumps(span_end("orphan-1")) + "\n", encoding="utf-8"
    )
    # No tool-bridge.jsonl -> run skipped, no trusted observations -> exit 1.
    code = tuning_main.main([str(run), "--output-dir", str(tmp_path / "out"), "--no-parquet"])
    assert code == 1
