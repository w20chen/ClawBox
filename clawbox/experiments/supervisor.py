"""Process-isolated supervision for the supported experiment workflow."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from clawbox.cube import CubeSandboxClient, OwnedSandboxJournal

from .configure import dump_experiment
from .baselines import ensure_supported_experiment
from .results import FailureCategory, ResultEnvelope, RunStatus, utcnow
from .spec import ExperimentArm, ExperimentSpec, expand_matrix, spec_digest
from .worker import atomic_json, atomic_marker, write_summary


STATE_SCHEMA_VERSION = 1
TERMINAL_STATES = {"succeeded", "failed", "cancelled", "destroyed"}


def atomic_text(path: Path, value: str) -> None:
    """Replace a text artifact only after its complete contents reach the OS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def process_identity(pid: int | None = None) -> dict[str, Any]:
    process = psutil.Process(pid or os.getpid())
    return {"pid": process.pid, "create_time": process.create_time()}


def process_is_alive(identity: dict[str, Any] | None) -> bool:
    if not identity:
        return False
    try:
        process = psutil.Process(int(identity["pid"]))
        return process.is_running() and abs(
            process.create_time() - float(identity["create_time"])
        ) < 0.01 and process.status() != psutil.STATUS_ZOMBIE
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def terminate_process_tree(pid: int, *, grace_seconds: float = 5.0) -> None:
    """Terminate a worker and every child, then force-kill survivors."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    processes = parent.children(recursive=True) + [parent]
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=grace_seconds)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=grace_seconds)


def read_run_state(run_root: Path) -> dict[str, Any]:
    path = run_root / "run-state.json"
    if not path.is_file():
        raise ValueError(f"run state does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid run state {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported run state: {path}")
    return value


def status_for_run(run_root: Path) -> dict[str, Any]:
    state = read_run_state(run_root)
    stored = str(state.get("state") or "unknown")
    alive = process_is_alive(state.get("supervisor"))
    effective = stored
    if stored == "running" and not alive:
        effective = "orphaned"
    return {
        **state,
        "state": effective,
        "stored_state": stored,
        "supervisor_alive": alive,
        "run_root": str(run_root),
    }


def _attempt_task_uid(owner_id: str, attempt_id: str, arm_id: str) -> str:
    suffix = hashlib.sha256(f"{owner_id}\0{attempt_id}\0{arm_id}".encode()).hexdigest()[:20]
    return f"clawbox-{suffix}"


def _synthetic_result(
    arm: ExperimentArm, *, run_id: str, attempt_id: str, task_uid: str,
    experiment_id: str,
    status: RunStatus, detail: str, started_at: Any, artifact_root: Path,
    cleanup_verified: bool,
) -> ResultEnvelope:
    category = (
        FailureCategory.TIMEOUT if status is RunStatus.TIMED_OUT
        else FailureCategory.INFRASTRUCTURE
    )
    return ResultEnvelope(
        run_id=run_id, attempt_id=attempt_id, sandbox_task_uid=task_uid,
        experiment_id=experiment_id, arm=arm,
        provenance={"supervised_worker": True}, status=status,
        failure_category=category, started_at=started_at, completed_at=utcnow(),
        correctness={
            "completed_sessions": 0,
            "failed_sessions": arm.concurrency,
            "validation_passed": False,
            "failure": detail,
            "cleanup_verified": cleanup_verified,
        },
        performance={}, memory={},
        artifacts={
            "worker_stdout": str(artifact_root / "worker.stdout.log"),
            "worker_stderr": str(artifact_root / "worker.stderr.log"),
        },
    )


class ExperimentSupervisor:
    def __init__(self, spec: ExperimentSpec, *, run_id: str, attempt_id: str,
                 owner_id: str, output_root: Path, resume: bool = False,
                 qualification_receipt: dict[str, Any] | None = None,
                 poll_interval_seconds: float = 0.25,
                 cleanup_timeout_seconds: float = 180.0) -> None:
        self.spec = spec
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.owner_id = owner_id
        self.output_root = output_root
        self.resume = resume
        self.qualification_receipt = qualification_receipt
        self.poll_interval_seconds = poll_interval_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        ensure_supported_experiment(spec)
        self.state_path = output_root / "run-state.json"
        self.state: dict[str, Any] = {}
        self.results: list[ResultEnvelope] = []

    def _write_state(self) -> None:
        self.state["updated_unix_s"] = time.time()
        atomic_json(self.state_path, self.state)

    def _recover_for_resume(self, state: dict[str, Any]) -> None:
        """Stop and clean any arm whose previous attempt was interrupted."""
        failures: list[str] = []
        recovered: list[str] = []
        for arm_id, arm_state in state.get("arms", {}).items():
            worker = arm_state.get("worker")
            if process_is_alive(worker):
                terminate_process_tree(int(worker["pid"]))
            if arm_state.get("cleanup_verified") is True:
                continue
            task_uid = arm_state.get("task_uid")
            artifact_root = arm_state.get("artifact_root")
            if not task_uid or not artifact_root:
                continue
            artifact_path = Path(artifact_root)
            try:
                cleaned, detail = self._cleanup(
                    task_uid=str(task_uid),
                    journal=artifact_path / "owned-sandboxes.jsonl",
                    artifact_root=artifact_path,
                )
            except Exception as exc:
                cleaned, detail = False, f"{type(exc).__name__}: {exc}"
            arm_state["cleanup_verified"] = cleaned
            arm_state["cleanup_error"] = detail
            if cleaned:
                recovered.append(str(arm_id))
                if arm_state.get("status") == "running":
                    arm_state["status"] = "interrupted"
            else:
                failures.append(f"{arm_id}: {detail or 'cleanup was not verified'}")
        state["resume_recovery"] = {
            "completed_unix_s": time.time(),
            "recovered_arms": recovered,
            "failures": failures,
        }
        atomic_json(self.state_path, state)
        if failures:
            raise RuntimeError(
                "cannot resume until interrupted resources are cleaned: "
                + "; ".join(failures)
            )

    def _initialize(self, arms: list[ExperimentArm]) -> None:
        digest = spec_digest(self.spec)
        if self.resume:
            state = read_run_state(self.output_root)
            if state.get("state") == "running" and process_is_alive(state.get("supervisor")):
                raise RuntimeError(f"run is already active: {self.run_id}")
            if state.get("spec_digest") != digest:
                raise ValueError("resume spec digest does not match the frozen run spec")
            self._recover_for_resume(state)
            self.state = state
            self.state.update({
                "attempt_id": self.attempt_id,
                "state": "running",
                "failure": None,
                "supervisor": process_identity(),
                "resumed_unix_s": time.time(),
            })
        else:
            if self.output_root.exists():
                raise ValueError(
                    f"result directory already exists: {self.output_root}; "
                    "use experiment resume for an existing run"
                )
            self.output_root.mkdir(parents=True)
            atomic_text(
                self.output_root / "experiment.yaml", dump_experiment(self.spec),
            )
            if self.qualification_receipt is not None:
                atomic_json(
                    self.output_root / "qualification.json",
                    self.qualification_receipt,
                )
            self.state = {
                "schema_version": STATE_SCHEMA_VERSION,
                "run_id": self.run_id,
                "attempt_id": self.attempt_id,
                "owner_id": self.owner_id,
                "experiment_id": self.spec.experiment_id,
                "spec_digest": digest,
                "state": "running",
                "failure": None,
                "supervisor": process_identity(),
                "created_unix_s": time.time(),
                "current_arm_id": None,
                "phase": "initializing",
                "qualification_receipt": (
                    str(self.output_root / "qualification.json")
                    if self.qualification_receipt is not None else None
                ),
                "arms": {
                    arm.arm_id: {
                        "status": "pending", "attempt_id": None,
                        "task_uid": None, "cleanup_verified": False,
                    }
                    for arm in arms
                },
            }
        for arm in arms:
            self.state.setdefault("arms", {}).setdefault(arm.arm_id, {
                "status": "pending", "attempt_id": None,
                "task_uid": None, "cleanup_verified": False,
            })
        self._write_state()

    def _successful_result(self, arm: ExperimentArm) -> ResultEnvelope | None:
        result_path = self.output_root / "arms" / f"{arm.arm_id}.json"
        marker_path = self.output_root / "arms" / f"{arm.arm_id}.complete"
        if not result_path.is_file() or not marker_path.is_file():
            return None
        try:
            result = ResultEnvelope.model_validate_json(result_path.read_text(encoding="utf-8"))
            marker = marker_path.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            return None
        if (
            result.status is RunStatus.SUCCEEDED
            and result.arm.arm_id == arm.arm_id
            and result.arm.spec_digest == arm.spec_digest
            and marker == arm.spec_digest
            and result.correctness.get("cleanup_verified") is True
        ):
            return result
        return None

    def _worker_command(self, frozen_spec: Path, arm: ExperimentArm,
                        task_uid: str, artifact_root: Path) -> list[str]:
        return [
            sys.executable, "-m", "clawbox.experiments.worker",
            "--spec", str(frozen_spec), "--run-id", self.run_id,
            "--attempt-id", self.attempt_id, "--task-uid", task_uid,
            "--output-root", str(artifact_root), "--arm-id", arm.arm_id,
            "--no-summary",
        ]

    def _cleanup(self, *, task_uid: str, journal: Path,
                 artifact_root: Path) -> tuple[bool, str | None]:
        stdout_path = artifact_root / "cleanup.stdout.log"
        stderr_path = artifact_root / "cleanup.stderr.log"
        command = [
            sys.executable, "-m", "clawbox.experiments.supervisor", "cleanup",
            "--journal", str(journal), "--task-uid", task_uid,
        ]
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            try:
                return_code = process.wait(timeout=self.cleanup_timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_tree(process.pid)
                return False, (
                    f"cleanup timed out after {self.cleanup_timeout_seconds:g} seconds"
                )
        if return_code:
            return False, f"cleanup process exited with status {return_code}"
        return True, None

    def _store_result(self, result: ResultEnvelope, *, successful: bool) -> None:
        result_path = self.output_root / "arms" / f"{result.arm.arm_id}.json"
        marker_path = self.output_root / "arms" / f"{result.arm.arm_id}.complete"
        atomic_json(result_path, result.model_dump(mode="json"))
        if successful:
            atomic_marker(marker_path, result.arm.spec_digest)
        else:
            marker_path.unlink(missing_ok=True)

    def _run_arm(self, arm: ExperimentArm, frozen_spec: Path) -> ResultEnvelope:
        started_at = utcnow()
        task_uid = _attempt_task_uid(self.owner_id, self.attempt_id, arm.arm_id)
        artifact_root = self.output_root / "attempts" / self.attempt_id / arm.arm_id
        artifact_root.mkdir(parents=True, exist_ok=False)
        arm_state = self.state["arms"][arm.arm_id]
        arm_state.update({
            "status": "running", "attempt_id": self.attempt_id,
            "task_uid": task_uid, "artifact_root": str(artifact_root),
            "cleanup_verified": False, "started_unix_s": time.time(),
        })
        self.state.update({"current_arm_id": arm.arm_id, "phase": "worker"})
        self._write_state()

        stdout_path = artifact_root / "worker.stdout.log"
        stderr_path = artifact_root / "worker.stderr.log"
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(
                self._worker_command(frozen_spec, arm, task_uid, artifact_root),
                stdout=stdout, stderr=stderr,
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
            arm_state["worker"] = process_identity(process.pid)
            self._write_state()
            deadline = time.monotonic() + arm.execution.arm_timeout_seconds
            last_heartbeat = 0.0
            timed_out = False
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        timed_out = True
                        terminate_process_tree(process.pid)
                        break
                    if now - last_heartbeat >= 1.0:
                        last_heartbeat = now
                        arm_state["heartbeat_unix_s"] = time.time()
                        event_path = artifact_root / "events" / f"{arm.arm_id}.jsonl"
                        if event_path.exists():
                            stat = event_path.stat()
                            arm_state["semantic_progress"] = {
                                "event_bytes": stat.st_size,
                                "event_modified_unix_s": stat.st_mtime,
                            }
                        self._write_state()
                    time.sleep(self.poll_interval_seconds)
            except BaseException:
                terminate_process_tree(process.pid)
                raise
            return_code = process.poll()

        self.state["phase"] = "cleanup"
        self._write_state()
        cleanup_verified, cleanup_error = self._cleanup(
            task_uid=task_uid,
            journal=artifact_root / "owned-sandboxes.jsonl",
            artifact_root=artifact_root,
        )
        arm_state["cleanup_verified"] = cleanup_verified
        arm_state["cleanup_error"] = cleanup_error

        child_result_path = artifact_root / "arms" / f"{arm.arm_id}.json"
        result: ResultEnvelope | None = None
        if child_result_path.is_file() and not timed_out:
            try:
                candidate = ResultEnvelope.model_validate_json(
                    child_result_path.read_text(encoding="utf-8")
                )
                if (
                    candidate.arm.arm_id != arm.arm_id
                    or candidate.arm.spec_digest != arm.spec_digest
                ):
                    raise ValueError("worker result identity does not match selected arm")
                result = candidate
            except (OSError, ValueError) as exc:
                arm_state["result_error"] = f"{type(exc).__name__}: {exc}"

        if timed_out:
            detail = f"arm exceeded hard timeout of {arm.execution.arm_timeout_seconds} seconds"
            result = _synthetic_result(
                arm, run_id=self.run_id, attempt_id=self.attempt_id,
                task_uid=task_uid, experiment_id=self.spec.experiment_id,
                status=RunStatus.TIMED_OUT, detail=detail,
                started_at=started_at, artifact_root=artifact_root,
                cleanup_verified=cleanup_verified,
            )
        elif result is None:
            detail = f"worker exited with status {return_code} without a valid arm result"
            result = _synthetic_result(
                arm, run_id=self.run_id, attempt_id=self.attempt_id,
                task_uid=task_uid, experiment_id=self.spec.experiment_id,
                status=RunStatus.FAILED, detail=detail,
                started_at=started_at, artifact_root=artifact_root,
                cleanup_verified=cleanup_verified,
            )
        elif return_code != 0 and result.status is RunStatus.SUCCEEDED:
            result = result.model_copy(update={
                "status": RunStatus.FAILED,
                "failure_category": FailureCategory.INFRASTRUCTURE,
                "correctness": {
                    **result.correctness,
                    "validation_passed": False,
                    "failure": f"worker exited with status {return_code}",
                },
            })

        if not cleanup_verified:
            result = result.model_copy(update={
                "status": RunStatus.FAILED,
                "failure_category": FailureCategory.INFRASTRUCTURE,
                "correctness": {
                    **result.correctness,
                    "validation_passed": False,
                    "failure": cleanup_error,
                    "cleanup_verified": False,
                },
            })
        else:
            result = result.model_copy(update={
                "correctness": {**result.correctness, "cleanup_verified": True},
            })

        succeeded = result.status is RunStatus.SUCCEEDED and cleanup_verified
        self._store_result(result, successful=succeeded)
        arm_state.update({
            "status": result.status.value,
            "completed_unix_s": time.time(),
            "result": str(self.output_root / "arms" / f"{arm.arm_id}.json"),
        })
        self._write_state()
        return result

    def run(self) -> list[ResultEnvelope]:
        arms = list(expand_matrix(self.spec))
        self._initialize(arms)
        frozen_spec = self.output_root / "experiment.yaml"
        try:
            for arm in arms:
                previous = self._successful_result(arm)
                if previous is not None:
                    self.results.append(previous)
                    self.state["arms"][arm.arm_id]["status"] = "succeeded"
                    continue
                result = self._run_arm(arm, frozen_spec)
                self.results.append(result)
                write_summary(
                    self.output_root, run_id=self.run_id,
                    attempt_id=self.attempt_id, task_uid=self.owner_id,
                    experiment_id=self.spec.experiment_id, results=self.results,
                )
                if result.status is not RunStatus.SUCCEEDED:
                    break
        except BaseException as exc:
            self.state.update({
                "state": "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
                "failure": f"{type(exc).__name__}: {exc}",
                "phase": "terminal", "current_arm_id": None,
            })
            self._write_state()
            raise

        complete = len(self.results) == len(arms) and all(
            item.status is RunStatus.SUCCEEDED for item in self.results
        )
        for arm in arms:
            if self.state["arms"][arm.arm_id]["status"] == "pending":
                self.state["arms"][arm.arm_id]["status"] = "not_run"
        self.state.update({
            "state": "succeeded" if complete else "failed",
            "failure": None if complete else "an arm failed; remaining arms were not run",
            "phase": "terminal", "current_arm_id": None,
            "completed_unix_s": time.time(),
        })
        write_summary(
            self.output_root, run_id=self.run_id,
            attempt_id=self.attempt_id, task_uid=self.owner_id,
            experiment_id=self.spec.experiment_id, results=self.results,
        )
        self._write_state()
        return self.results


def cleanup_owned_sandboxes(journal: Path, task_uid: str) -> None:
    client = CubeSandboxClient(journal=OwnedSandboxJournal(journal))
    client.kill_owned_sandboxes(task_uid)


def cleanup_run(run_root: Path) -> dict[str, Any]:
    state = read_run_state(run_root)
    failures: list[str] = []
    cleaned: list[str] = []
    for arm_id, arm in state.get("arms", {}).items():
        task_uid = arm.get("task_uid")
        artifact_root = arm.get("artifact_root")
        if not task_uid or not artifact_root:
            continue
        artifact_path = Path(artifact_root)
        stdout_path = artifact_path / "manual-cleanup.stdout.log"
        stderr_path = artifact_path / "manual-cleanup.stderr.log"
        try:
            command = [
                sys.executable, "-m", "clawbox.experiments.supervisor", "cleanup",
                "--journal", str(artifact_path / "owned-sandboxes.jsonl"),
                "--task-uid", str(task_uid),
            ]
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                try:
                    return_code = process.wait(timeout=180)
                except subprocess.TimeoutExpired as exc:
                    terminate_process_tree(process.pid)
                    raise RuntimeError("cleanup timed out after 180 seconds") from exc
            if return_code:
                raise RuntimeError(f"cleanup process exited with status {return_code}")
            cleaned.append(arm_id)
            arm["cleanup_verified"] = True
            arm["cleanup_error"] = None
        except Exception as exc:
            failures.append(f"{arm_id}: {type(exc).__name__}: {exc}")
            arm["cleanup_verified"] = False
            arm["cleanup_error"] = str(exc)
    state["state"] = "destroyed" if not failures else "failed"
    state["phase"] = "terminal"
    state["failure"] = None if not failures else "; ".join(failures)
    atomic_json(run_root / "run-state.json", state)
    if failures:
        raise RuntimeError("run cleanup incomplete: " + "; ".join(failures))
    return {"run_id": state["run_id"], "cleaned_arms": cleaned, "state": state["state"]}


def abort_run(run_root: Path) -> dict[str, Any]:
    state = read_run_state(run_root)
    identity = state.get("supervisor")
    if process_is_alive(identity):
        terminate_process_tree(int(identity["pid"]))
    result = cleanup_run(run_root)
    state = read_run_state(run_root)
    state["state"] = "cancelled"
    state["failure"] = "cancelled by user"
    atomic_json(run_root / "run-state.json", state)
    return {**result, "state": "cancelled"}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--journal", type=Path, required=True)
    cleanup.add_argument("--task-uid", required=True)
    args = parser.parse_args(argv)
    if args.command == "cleanup":
        try:
            cleanup_owned_sandboxes(args.journal, args.task_uid)
        except Exception as exc:
            print(f"cleanup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
