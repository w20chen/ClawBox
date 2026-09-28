from __future__ import annotations

import json
import sys
from pathlib import Path

from clawbox.experiments.results import ResultEnvelope, RunStatus, utcnow
from clawbox.experiments.spec import expand_matrix, load_experiment
from clawbox.experiments.supervisor import ExperimentSupervisor, status_for_run
from clawbox.experiments.worker import atomic_json, atomic_marker


class _NoCubeSupervisor(ExperimentSupervisor):
    def _cleanup(self, *, task_uid: str, journal: Path,
                 artifact_root: Path) -> tuple[bool, str | None]:
        return True, None


class _HangingWorkerSupervisor(_NoCubeSupervisor):
    def _worker_command(self, frozen_spec, arm, task_uid, artifact_root):
        return [sys.executable, "-c", "import time; time.sleep(60)"]


class _RecordingCleanupSupervisor(_NoCubeSupervisor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleaned: list[str] = []

    def _cleanup(self, *, task_uid: str, journal: Path,
                 artifact_root: Path) -> tuple[bool, str | None]:
        self.cleaned.append(task_uid)
        return True, None


def _short_spec():
    spec = load_experiment(Path("examples/experiments/getting-started.yaml"))
    return spec.model_copy(update={
        "execution": spec.execution.model_copy(update={
            "arm_timeout_seconds": 1,
            "stabilization_seconds": 0,
        }),
    })


def test_supervisor_hard_timeout_is_terminal_and_fail_fast(tmp_path: Path) -> None:
    spec = _short_spec()
    run_root = tmp_path / "run-timeout"
    supervisor = _HangingWorkerSupervisor(
        spec, run_id="run-timeout", attempt_id="attempt-1", owner_id="owner-1",
        output_root=run_root, poll_interval_seconds=0.02,
    )

    results = supervisor.run()

    assert len(results) == 1
    assert results[0].status is RunStatus.TIMED_OUT
    assert results[0].correctness["cleanup_verified"] is True
    assert not (run_root / "arms" / f"{results[0].arm.arm_id}.complete").exists()
    state = status_for_run(run_root)
    assert state["state"] == "failed"
    assert state["supervisor_alive"] is True
    statuses = [item["status"] for item in state["arms"].values()]
    assert statuses[0] == "timed_out"
    assert all(item == "not_run" for item in statuses[1:])


def test_resume_skips_only_cleanup_verified_successes(tmp_path: Path) -> None:
    spec = _short_spec()
    run_root = tmp_path / "run-resume"
    first = _NoCubeSupervisor(
        spec, run_id="run-resume", attempt_id="attempt-1", owner_id="owner-1",
        output_root=run_root,
    )
    arms = list(expand_matrix(spec))
    first._initialize(arms)
    for arm in arms:
        result = ResultEnvelope(
            run_id="run-resume", attempt_id="attempt-1", sandbox_task_uid="task",
            experiment_id=spec.experiment_id, arm=arm, status=RunStatus.SUCCEEDED,
            started_at=utcnow(), completed_at=utcnow(),
            correctness={"validation_passed": True, "cleanup_verified": True},
        )
        atomic_json(
            run_root / "arms" / f"{arm.arm_id}.json",
            result.model_dump(mode="json"),
        )
        atomic_marker(run_root / "arms" / f"{arm.arm_id}.complete", arm.spec_digest)
    first.state["state"] = "failed"
    first._write_state()

    resumed = _NoCubeSupervisor(
        spec, run_id="run-resume", attempt_id="attempt-2", owner_id="owner-1",
        output_root=run_root, resume=True,
    )
    results = resumed.run()

    assert len(results) == len(arms)
    assert all(item.status is RunStatus.SUCCEEDED for item in results)
    assert not (run_root / "attempts" / "attempt-2").exists()
    assert json.loads((run_root / "summary.json").read_text())["attempt_id"] == "attempt-2"


def test_resume_cleans_an_interrupted_attempt_before_starting(tmp_path: Path) -> None:
    spec = _short_spec()
    arms = list(expand_matrix(spec))
    run_root = tmp_path / "run-interrupted"
    first = _NoCubeSupervisor(
        spec, run_id="run-interrupted", attempt_id="attempt-1", owner_id="owner-1",
        output_root=run_root,
    )
    first._initialize(arms)
    arm = arms[0]
    artifact_root = run_root / "attempts" / "attempt-1" / arm.arm_id
    artifact_root.mkdir(parents=True)
    first.state["supervisor"] = {"pid": 999_999_999, "create_time": 0}
    first.state["arms"][arm.arm_id].update({
        "status": "running",
        "task_uid": "interrupted-task",
        "artifact_root": str(artifact_root),
        "cleanup_verified": False,
    })
    first._write_state()

    resumed = _RecordingCleanupSupervisor(
        spec, run_id="run-interrupted", attempt_id="attempt-2", owner_id="owner-1",
        output_root=run_root, resume=True,
    )
    resumed._initialize(arms)

    assert resumed.cleaned == ["interrupted-task"]
    assert resumed.state["resume_recovery"]["recovered_arms"] == [arm.arm_id]
    assert resumed.state["arms"][arm.arm_id]["status"] == "interrupted"
    assert resumed.state["arms"][arm.arm_id]["cleanup_verified"] is True
