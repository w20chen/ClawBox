from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawbox.experiments.configure import dump_experiment
from clawbox.experiments.qualification import qualification_spec
from clawbox.experiments.results import ResultEnvelope, RunStatus, utcnow
from clawbox.experiments.spec import expand_matrix, load_experiment
from clawbox.experiments.training import _percentile, _training_arm_sources


def test_training_static_calibration_uses_linear_empirical_p90() -> None:
    assert _percentile([0.0, 100.0, 200.0], .9) == pytest.approx(180.0)


def _write_run(
    root: Path, *, attempts: tuple[str, ...] = ("attempt-1",),
    qualification: bool = False, cleanup_verified: bool = True,
) -> tuple[Path, list[ResultEnvelope]]:
    spec = load_experiment(Path("examples/experiments/getting-started.yaml"))
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={
            "cases": tuple(
                case.model_copy(update={"repository": "marker"})
                for case in spec.workload.cases
            ),
        }),
    })
    if len(attempts) > 1:
        spec = spec.model_copy(update={
            "execution": spec.execution.model_copy(update={
                "concurrency_levels": tuple(range(1, len(attempts) + 1)),
                "randomized_order": False,
            }),
        })
    if qualification:
        spec = qualification_spec(spec)
    root.mkdir()
    (root / "experiment.yaml").write_text(dump_experiment(spec), encoding="utf-8")
    (root / "run-state.json").write_text(json.dumps({"state": "succeeded"}), encoding="utf-8")
    (root / "arms").mkdir()
    results: list[ResultEnvelope] = []
    for arm, attempt_id in zip(expand_matrix(spec), attempts, strict=True):
        artifact_root = root / "attempts" / attempt_id / arm.arm_id
        (artifact_root / "runtime-traces" / "session-1").mkdir(parents=True)
        resources = artifact_root / "tool-artifacts" / "session-1"
        resources.mkdir(parents=True)
        (artifact_root / "owned-sandboxes.jsonl").write_text("", encoding="utf-8")
        (artifact_root / "runtime-traces" / "session-1" / "trace.jsonl").write_text(
            "{}\n", encoding="utf-8",
        )
        (resources / "tool-bridge.jsonl").write_text("", encoding="utf-8")
        result = ResultEnvelope(
            run_id=root.name, attempt_id=attempt_id, sandbox_task_uid="task",
            experiment_id=spec.experiment_id, arm=arm, status=RunStatus.SUCCEEDED,
            started_at=utcnow(), completed_at=utcnow(),
            correctness={
                "validation_passed": True,
                "cleanup_verified": cleanup_verified,
            },
        )
        results.append(result)
        (root / "arms" / f"{arm.arm_id}.complete").write_text(
            arm.spec_digest, encoding="ascii",
        )
    (root / "summary.json").write_text(json.dumps({
        "run_id": root.name,
        "attempt_id": attempts[-1],
        "experiment_id": spec.experiment_id,
        "arms": [item.model_dump(mode="json") for item in results],
    }, default=str), encoding="utf-8")
    return root, results


def test_training_resolves_current_supervisor_arms_across_attempts(tmp_path: Path) -> None:
    run, results = _write_run(tmp_path / "run", attempts=("attempt-1", "attempt-2"))

    _, sources = _training_arm_sources(run, "marker")

    assert [source.result.attempt_id for source in sources] == ["attempt-1", "attempt-2"]
    assert [source.result.arm.arm_id for source in sources] == [
        result.arm.arm_id for result in results
    ]
    assert all(source.artifact_root.is_dir() for source in sources)


def test_training_rejects_unverified_cleanup(tmp_path: Path) -> None:
    run, _ = _write_run(tmp_path / "run", cleanup_verified=False)

    with pytest.raises(ValueError, match="cleanup was not verified"):
        _training_arm_sources(run, "marker")


def test_training_rejects_qualification_prefix_run(tmp_path: Path) -> None:
    run, _ = _write_run(tmp_path / "run", qualification=True)

    with pytest.raises(ValueError, match="qualification prefix runs"):
        _training_arm_sources(run, "marker")


def test_training_rejects_legacy_root_journal_without_arm_journal(tmp_path: Path) -> None:
    run, results = _write_run(tmp_path / "run")
    source_journal = (
        run / "attempts" / results[0].attempt_id / results[0].arm.arm_id
        / "owned-sandboxes.jsonl"
    )
    source_journal.unlink()
    (run / "owned-sandboxes.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="has no ownership journal"):
        _training_arm_sources(run, "marker")
