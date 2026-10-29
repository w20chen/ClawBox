from __future__ import annotations

import json
import sys
import pytest
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


class _BrokenWorkerSupervisor(_NoCubeSupervisor):
    def _worker_command(self, frozen_spec, arm, task_uid, artifact_root):
        return [sys.executable, '-c', 'raise RuntimeError("infrastructure failure")']


class _ResultWorkerSupervisor(_NoCubeSupervisor):
    def _worker_command(self, frozen_spec, arm, task_uid, artifact_root):
        result = ResultEnvelope(
            run_id=self.run_id, attempt_id=self.attempt_id,
            sandbox_task_uid=task_uid, experiment_id=self.spec.experiment_id,
            arm=arm, status=RunStatus.SUCCEEDED,
            started_at=utcnow(), completed_at=utcnow(),
            correctness={"validation_passed": True},
            performance={"large_raw_series": list(range(1000))},
        )
        child = artifact_root / "arms" / f"{arm.arm_id}.json"
        atomic_json(child, result.model_dump(mode="json"))
        atomic_marker(child.with_suffix(".complete"), arm.spec_digest)
        return [sys.executable, "-c", "pass"]


def test_infrastructure_failure_still_stops_the_matrix(tmp_path):
    supervisor = _BrokenWorkerSupervisor(_short_spec(), run_id='broken', attempt_id='a',
        owner_id='o', output_root=tmp_path / 'broken', poll_interval_seconds=.02)
    results = supervisor.run()
    assert len(results) == 1
    assert results[0].failure_category.value == 'infrastructure'
    assert not (tmp_path / 'broken' / 'arms' / f'{results[0].arm.arm_id}.complete').exists()


def test_supervisor_retains_one_canonical_result_and_compact_summary(tmp_path):
    run_root = tmp_path / "compact"
    supervisor = _ResultWorkerSupervisor(
        _short_spec(), run_id="compact", attempt_id="attempt-1",
        owner_id="owner-1", output_root=run_root, poll_interval_seconds=.02,
    )

    results = supervisor.run()

    result = results[0]
    canonical = run_root / "arms" / f"{result.arm.arm_id}.json"
    child = (
        run_root / "attempts" / "attempt-1" / result.arm.arm_id
        / "arms" / f"{result.arm.arm_id}.json"
    )
    summary = json.loads((run_root / "summary.json").read_text())
    entry = summary["arms"][0]
    assert canonical.is_file()
    assert not child.exists()
    assert not child.with_suffix(".complete").exists()
    assert entry["result"] == f"arms/{result.arm.arm_id}.json"
    assert "performance" not in entry
    assert json.loads(canonical.read_text())["performance"]["large_raw_series"][-1] == 999


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
            "arm_finalization_timeout_seconds": 1,
            "stabilization_seconds": 0,
        }),
    })


def test_overload_classification_preserves_failed_trials_without_hiding_infrastructure_errors():
    from clawbox.experiments.worker import classify_arm_failure
    from clawbox.experiments.results import FailureCategory
    from clawbox.experiments.supervisor import is_trial_outcome

    rpc_error = RuntimeError('CubeSandbox command failed: HTTP 504')
    assert classify_arm_failure(rpc_error, domain_oom_kills=1) is FailureCategory.OOM
    assert classify_arm_failure(TimeoutError('command stream exceeded deadline')) is FailureCategory.TIMEOUT
    assert classify_arm_failure(rpc_error, domain_oom_kills=1,
        infrastructure_error=RuntimeError('controller failed')) is FailureCategory.INFRASTRUCTURE
    arm = expand_matrix(_short_spec())[0]
    result = ResultEnvelope(run_id='r', attempt_id='a', experiment_id='e', arm=arm,
        status=RunStatus.FAILED, failure_category=FailureCategory.OOM,
        started_at=utcnow(), completed_at=utcnow(),
        correctness={'cleanup_verified': True, 'native_tool_telemetry_loss_total': 1})
    assert is_trial_outcome(result)  # an OOM may prevent final telemetry delivery
    assert not is_trial_outcome(result.model_copy(update={'failure_category': FailureCategory.INFRASTRUCTURE}))
    assert not is_trial_outcome(result.model_copy(update={'correctness': {'cleanup_verified': False}}))
    isolated = result.model_copy(update={
        'failure_category': FailureCategory.AGENT,
        'correctness': {
            'cleanup_verified': True, 'arm_interrupted': False,
            'infrastructure_failure': None,
            'session_failures': [{'session_id': 'arm-0001', 'error': 'agent failed'}],
        },
    })
    assert is_trial_outcome(isolated)
    assert not is_trial_outcome(isolated.model_copy(update={'correctness': {
        **isolated.correctness, 'arm_interrupted': True,
    }}))
    assert not is_trial_outcome(isolated.model_copy(update={
        'failure_category': FailureCategory.INFRASTRUCTURE,
    }))


def test_interruption_cleans_owned_vms_after_terminating_worker(tmp_path, monkeypatch):
    from clawbox.experiments import supervisor as module

    class Interrupted(_RecordingCleanupSupervisor):
        def _worker_command(self, *args):
            return [sys.executable, '-c', 'import time; time.sleep(60)']

    instance = Interrupted(_short_spec(), run_id='interrupt', attempt_id='a', owner_id='o',
                           output_root=tmp_path/'run')
    real_sleep = module.time.sleep
    def interrupt_once(seconds):
        monkeypatch.setattr(module.time, 'sleep', real_sleep)
        raise KeyboardInterrupt
    monkeypatch.setattr(module.time, 'sleep', interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        instance.run()
    assert len(instance.cleaned) == 1
    state = json.loads((tmp_path/'run/run-state.json').read_text())
    assert state['state'] == 'cancelled'
    arm = next(a for a in state['arms'].values() if a['status'] == 'cancelled')
    assert arm['cleanup_verified'] is True
    assert not module.process_is_alive(arm['worker'])


def test_supervisor_preserves_timeouts_and_continues_remaining_trials(tmp_path: Path) -> None:
    spec = _short_spec()
    run_root = tmp_path / "run-timeout"
    supervisor = _HangingWorkerSupervisor(
        spec, run_id="run-timeout", attempt_id="attempt-1", owner_id="owner-1",
        output_root=run_root, poll_interval_seconds=0.02,
    )

    results = supervisor.run()

    assert len(results) == len(expand_matrix(spec))
    assert results[0].status is RunStatus.TIMED_OUT
    assert results[0].correctness["cleanup_verified"] is True
    assert (run_root / "arms" / f"{results[0].arm.arm_id}.complete").exists()
    state = status_for_run(run_root)
    assert state["state"] == "failed"
    assert state["supervisor_alive"] is True
    statuses = [item["status"] for item in state["arms"].values()]
    assert statuses[0] == "timed_out"
    assert all(item == "timed_out" for item in statuses)
    # A resume retains timeouts as outcomes and does not launch another worker.
    supervisor.state['supervisor'] = {'pid': 999_999_999, 'create_time': 0}
    supervisor._write_state()
    resumed = _NoCubeSupervisor(spec, run_id='run-timeout', attempt_id='attempt-2',
        owner_id='owner-1', output_root=run_root, resume=True)
    assert all(r.status is RunStatus.TIMED_OUT for r in resumed.run())
    assert not (run_root / 'attempts' / 'attempt-2').exists()


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


def test_abort_updates_active_arms_and_preserves_terminal_outcomes(tmp_path, monkeypatch):
    from clawbox.experiments import supervisor as module
    statuses = ('pending', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')
    state = {'schema_version': 1, 'run_id': 'r', 'state': 'running', 'arms': {
        status: {'status': status} for status in statuses}}
    atomic_json(tmp_path/'run-state.json', state)
    monkeypatch.setattr(module, 'process_is_alive', lambda identity: False)
    monkeypatch.setattr(module, 'cleanup_run', lambda *a, **kw: {'state': 'destroyed'})
    assert module.abort_run(tmp_path)['state'] == 'cancelled'
    saved = json.loads((tmp_path/'run-state.json').read_text())
    assert {key: value['status'] for key, value in saved['arms'].items()} == {
        status: 'cancelled' if status in {'pending', 'running'} else status
        for status in statuses}


def test_abort_does_not_claim_cancellation_when_cleanup_fails(tmp_path, monkeypatch):
    from clawbox.experiments import supervisor as module
    atomic_json(tmp_path/'run-state.json', {'schema_version': 1, 'run_id': 'r', 'state': 'running',
        'arms': {'a': {'status': 'running'}}})
    monkeypatch.setattr(module, 'process_is_alive', lambda identity: False)
    def failed(*args, **kwargs):
        state = json.loads((tmp_path/'run-state.json').read_text())
        state.update(state='failed', failure='cleanup incomplete')
        state['arms']['a'].update(cleanup_verified=False, cleanup_error='still present')
        atomic_json(tmp_path/'run-state.json', state)
        raise RuntimeError('cleanup incomplete')
    monkeypatch.setattr(module, 'cleanup_run', failed)
    with pytest.raises(RuntimeError, match='cleanup incomplete'):
        module.abort_run(tmp_path)
    saved = json.loads((tmp_path/'run-state.json').read_text())
    assert saved['state'] == 'failed'
    assert saved['arms']['a']['cleanup_verified'] is False
    assert saved['arms']['a']['cleanup_error'] == 'still present'
