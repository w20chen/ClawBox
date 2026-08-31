from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from clawbox.experiments import (
    ExperimentSpec,
    expand_matrix,
    validate_workflow,
)
from clawbox.experiments.results import (
    ResultEnvelope, RunStatus, failure_category_for, utcnow,
)
from clawbox.experiments.spec_types import AdmissionPolicy
from clawbox.cell.p90 import AdmissionPrediction
from clawbox.replay.stats import summary_stats


def _path(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stats(rows: list[dict[str, Any]], field: str) -> dict[str, float | int | str | None]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return summary_stats(values)


def _source_hash(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard",
         "--", "README.md", "pyproject.toml", "clawbox", "scripts", "deploy", "docker", "toolbridge"],
        check=True, capture_output=True, text=True,
    )
    digest = hashlib.sha256()
    for relative in sorted(result.stdout.splitlines()):
        path = root / relative
        if path.is_file():
            digest.update(relative.replace("\\", "/").encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _discard_heavy_vm_artifacts(prepared: Path) -> None:
    """Remove only reproducible large files below a newly-created arm input."""
    names = {
        "runtime.ext4", "tool.ext4", "runtime.mem", "tool.mem",
        "runtime.vmstate", "tool.vmstate", "runtime.mem.next", "tool.mem.next",
        "runtime.vmstate.next", "tool.vmstate.next",
    }
    for session in prepared.glob("session-*"):
        if session.parent != prepared:
            continue
        for name in names:
            path = session / name
            if path.is_file():
                path.unlink()


def _validate_firecracker_socket_paths(arm: Path, *, force: bool = False) -> None:
    if os.name == "nt" and not force:
        return
    for name in ("runtime.sock", "tool.sock"):
        path = arm / "input" / "session-0000" / name
        size = len(os.fsencode(str(path)))
        if size > 107:
            raise ValueError(
                f"Firecracker Unix socket path is {size} bytes (maximum 107): {path}"
            )


def _sizing_baselines(raw: dict[str, Any]) -> list[str]:
    sizing = list(raw.get("sizing_policies", ["fixed"]))
    residency = list(raw.get("memory_policies", ["resident", "snapshot"]))
    if not sizing or not set(sizing) <= {"fixed", "p90_static", "p90_reservation"}:
        raise ValueError("sizing_policies must contain fixed and/or p90_reservation")
    aliases = {
        ("fixed", "resident"): "fixed-explicit-resident",
        ("fixed", "snapshot"): "fixed-llm-wait-checkpoint",
        ("fixed", "llm_wait_checkpoint"): "fixed-llm-wait-checkpoint",
        ("p90_static", "resident"): "p90-static",
        ("p90_static", "snapshot"): "p90-static-llm-wait-checkpoint",
        ("p90_static", "llm_wait_checkpoint"): "p90-static-llm-wait-checkpoint",
        ("p90_reservation", "resident"): "p90-static",
        ("p90_reservation", "snapshot"): "p90-static-llm-wait-checkpoint",
        ("p90_reservation", "llm_wait_checkpoint"): "p90-static-llm-wait-checkpoint",
    }
    try:
        return [aliases[(size, memory)] for size in sizing for memory in residency]
    except KeyError as exc:
        raise ValueError("unsupported sizing or memory policy") from exc


def _p90_prediction(raw: dict[str, Any], base: Path | None) -> AdmissionPrediction | None:
    if not {"p90_static", "p90_reservation"}.intersection(raw.get("sizing_policies", [])):
        return None
    config = raw.get("p90_reservation") or raw.get("p90_static")
    if not isinstance(config, dict):
        raise ValueError("p90_reservation configuration is required for predictive admission")
    payload = config.get("prediction")
    if payload is None and config.get("prediction_file"):
        path = _path(base, config["prediction_file"]) if base is not None else Path(str(config["prediction_file"]))
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("p90_reservation requires prediction or prediction_file")
    prediction = AdmissionPrediction.from_payload(payload)
    expected_repo = str(
        raw.get("repository") or (raw.get("source") or {}).get("repository") or ""
    ).strip()
    if expected_repo and prediction.repo_fingerprint != expected_repo:
        raise ValueError(
            "p90_reservation prediction repository does not match the replay workload"
        )
    minimum = int(config.get("min_evidence", 5))
    if minimum < 1:
        raise ValueError("p90_reservation.min_evidence must be positive")
    if prediction.evidence_count < minimum:
        raise ValueError(
            f"p90_reservation prediction has {prediction.evidence_count} samples; "
            f"at least {minimum} are required"
        )
    return prediction


def _predictive_tool_memory_mib(
    raw: dict[str, Any], prediction: AdmissionPrediction, base: Path | None = None,
) -> int:
    """Return the fixed Tool-VM capacity used by a predictive arm.

    Per-tool P90 values are admission commitments only.  They must never be
    translated into per-run or per-invocation Firecracker RAM resizing.  The
    selected size class in an exported plan is retained only as a capacity
    sufficiency check for older plan files.
    """
    resources = raw.get("resources", {})
    fixed_mib = int(resources.get("tool_memory_mib", 4096))
    config = raw.get("p90_reservation") or raw.get("p90_static") or {}
    if config.get("use_per_tool_memory_plan"):
        payload = config.get("prediction")
        if payload is None and config.get("prediction_file"):
            path = _path(base, config["prediction_file"]) if base is not None else Path(str(config["prediction_file"]))
            payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("per-tool memory planning requires a prediction payload")
        plan = payload.get("per_tool_memory")
        workload_name = str(config.get("workload_name") or "")
        workloads = plan.get("workloads") if isinstance(plan, dict) else None
        selected = workloads.get(workload_name) if isinstance(workloads, dict) else None
        if not isinstance(selected, dict):
            raise ValueError("per-tool memory plan does not contain the configured workload")
        reservations = (
            selected.get("incremental_p90_distinct_kib")
            or selected.get("reservation_distinct_kib")
        )
        if not isinstance(reservations, list) or len(reservations) < 2:
            raise ValueError("per-tool predictive arm requires heterogeneous reservations")
        selected_mib = int(selected.get("selected_vm_size_class_mib", 0))
        if selected_mib < 256 or selected_mib > fixed_mib:
            raise ValueError(
                "per-tool Tool size class must be between 256 MiB and the "
                "fixed Tool-VM capacity"
            )
        return fixed_mib
    headroom = float(config.get("headroom_fraction", 0.25))
    floor_mib = int(config.get("min_tool_memory_mib", 2048))
    if not 0 <= headroom <= 1:
        raise ValueError("p90_reservation.headroom_fraction must be between 0 and 1")
    if floor_mib < 256 or floor_mib > fixed_mib:
        raise ValueError("p90_reservation.min_tool_memory_mib must be between 256 and fixed Tool memory")
    predicted_mib = math.ceil(
        prediction.memory_p90_bytes * (1 + headroom) / (1024.0 * 1024.0)
    )
    selected = min(fixed_mib, max(floor_mib, predicted_mib))
    if selected >= fixed_mib:
        raise ValueError("p90_reservation prediction does not select a smaller fixed VM class")
    return selected


def study_experiment_spec(raw: dict[str, Any], *, base: Path | None = None) -> ExperimentSpec:
    """Translate the established paper-study JSON into the canonical schema.

    The old file is intentionally retained: ``memory_policies.snapshot`` is a
    compatibility spelling for the direct-Firecracker LLM-wait checkpoint arm.
    """
    source = raw["source"]
    inference = list(raw.get("inference_backends", ["replay", "api"]))
    if not set(inference) <= {"replay", "api"}:
        raise ValueError("unsupported inference backend")
    baselines = _sizing_baselines(raw)
    prediction = _p90_prediction(raw, base)
    resources = raw.get("resources", {})
    def configured_path(value: object) -> str:
        return str(_path(base, value)) if base is not None else str(value)

    return ExperimentSpec.model_validate({
        "workload": {"source": "recorded_trace", "input": configured_path(source["trace"]),
                     "repetitions": int(raw.get("repetitions", 1))},
        "agent": {"driver": "openclaw"},
        "inference": {"backends": inference, "configuration": {
            "replay": {"time_scale": raw.get("replay", {}).get("time_scale", 1.0)},
            "api": {"base_url": raw.get("api", {}).get("base_url"),
                    "model": raw.get("api", {}).get("model"),
                    "key_env": raw.get("api", {}).get("key_env", "OPENAI_API_KEY")},
        }},
        "sandbox": {
            "backend": "direct_firecracker", "tool_transport": "ssh",
            "materialization": {"runtime_rootfs": configured_path(source["runtime_rootfs"]),
                                "tool_rootfs": configured_path(source["tool_rootfs"]),
                                "prompt": configured_path(source["prompt"]),
                                "network_prefix": raw.get("network_prefix", "172.30"),
                                "exposed_model": raw.get("exposed_model", "experiment-model")},
        },
        "scheduling": {"baselines": baselines},
        "execution": {"concurrency": int(raw.get("sessions", 1)),
                      "timeout_seconds": int(raw.get("timeout_s", 900)),
                      "command_timeout_seconds": 300},
        "resources": {"runtime_memory_mib": resources.get("runtime_memory_mib", 2048),
                      "tool_memory_mib": resources.get("tool_memory_mib", 4096),
                      "kb_generation": prediction.generation if prediction else None,
                      "cpu_first": resources.get("cpu_first", 0),
                      "numa_node": resources.get("numa_node", 0)},
        "validation": {"command": raw.get("validation_command",
                       "cd /testbed && git diff --binary --no-ext-diff HEAD")},
        "output": {"directory": configured_path(raw["output"])},
    })


def run_study(config_path: Path) -> int:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.resolve().parent
    root = Path(__file__).resolve().parents[2]
    source, resources = raw["source"], raw.get("resources", {})
    replay, api = raw.get("replay", {}), raw.get("api", {})
    spec = study_experiment_spec(raw, base=base)
    prediction = _p90_prediction(raw, base)
    predictive_tool_memory_mib = (
        _predictive_tool_memory_mib(raw, prediction, base) if prediction is not None else None
    )
    arm_workflows: list[tuple[str, Any]] = []
    predictive_label = (
        "p90_reservation"
        if "p90_reservation" in raw.get("sizing_policies", []) else "p90_static"
    )
    fixed_control_mib = raw.get("fixed_control_tool_memory_mib")
    if fixed_control_mib is not None:
        if "fixed" not in raw.get("sizing_policies", ["fixed"]):
            raise ValueError("fixed_control_tool_memory_mib requires the fixed sizing policy")
        fixed_control_mib = int(fixed_control_mib)
        fixed_tool_mib = int(resources.get("tool_memory_mib", 4096))
        if fixed_control_mib < 256 or fixed_control_mib >= fixed_tool_mib:
            raise ValueError(
                "fixed_control_tool_memory_mib must be at least 256 and below "
                "resources.tool_memory_mib"
            )
    for workflow in expand_matrix(spec):
        predictive = workflow.admission_policy is AdmissionPolicy.P90_STATIC
        resources_for_arm = workflow.resources.model_copy(update={
            "tool_memory_mib": (
                predictive_tool_memory_mib if predictive else workflow.resources.tool_memory_mib
            ),
            "kb_generation": prediction.generation if predictive and prediction else None,
        })
        resolved = workflow.model_copy(update={"resources": resources_for_arm})
        validate_workflow(resolved)
        arm_workflows.append((predictive_label if predictive else "fixed", resolved))
        if fixed_control_mib is not None and not predictive:
            control_resources = resolved.resources.model_copy(
                update={"tool_memory_mib": fixed_control_mib, "kb_generation": None}
            )
            control = resolved.model_copy(update={"resources": control_resources})
            validate_workflow(control)
            arm_workflows.append(("fixed2", control))
    sessions, repetitions = int(raw.get("sessions", 1)), int(raw.get("repetitions", 1))
    if sessions < 1 or repetitions < 1:
        raise ValueError("sessions and repetitions must be positive")
    inference = [item.value for item in spec.inference.backends]
    key_env = str(api.get("key_env", "OPENAI_API_KEY"))
    if "api" in inference and (not api.get("base_url") or not api.get("model") or not os.getenv(key_env)):
        raise ValueError("api mode requires api.base_url, api.model, and its key environment variable")

    runtime_rootfs = _path(base, source["runtime_rootfs"])
    tool_rootfs = _path(base, source["tool_rootfs"])
    prompt = _path(base, source["prompt"])
    trace = _path(base, source["trace"])
    output = _path(base, raw["output"])
    output.mkdir(parents=True, exist_ok=False)

    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"schema_version": 3, "started_unix_s": time.time(), "commit": commit,
                "source_tree_sha256": _source_hash(root), "host": platform.node(),
                "machine": platform.machine(), "trace_sha256": _sha256(trace), "config": raw}
    (output / "study-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    arms = [(
        workflow.inference_backend.value,
        sizing_label,
        workflow.residency_policy.value,
        workflow,
    ) for sizing_label, workflow in arm_workflows]
    explicit_sizing_matrix = "sizing_policies" in raw

    def arm_label(model: str, sizing_policy: str, residency: str) -> str:
        legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
        if not explicit_sizing_matrix and sizing_policy == "fixed":
            return f"{model}-{legacy_residency}"
        return f"{model}-{sizing_policy}-{legacy_residency}"

    order = random.Random(int(raw.get("seed", 0)))
    runs: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        shuffled = list(arms)
        order.shuffle(shuffled)
        for arm_position, (model, sizing_policy, residency, workflow) in enumerate(shuffled):
            legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
            arm = output / f"r{repetition:02d}-{arm_label(model, sizing_policy, residency)}"
            _validate_firecracker_socket_paths(arm)
            prepared, results = arm / "input", arm / "results"
            arm.mkdir()
            exposed_model = str(raw.get("exposed_model", "experiment-model"))
            prepare = [sys.executable, str(root / "scripts" / "prepare-openclaw-experiment.py"),
                       "--output", str(prepared), "--sessions", str(sessions),
                       "--runtime-rootfs", str(runtime_rootfs), "--tool-rootfs", str(tool_rootfs),
                       "--prompt", str(prompt), "--model-id", exposed_model,
                       "--network-prefix", str(raw.get("network_prefix", "172.30")),
                       "--runtime-memory-mib", str(resources.get("runtime_memory_mib", 2048)),
                       "--tool-memory-mib", str(workflow.resources.tool_memory_mib),
                       "--cpu-first", str(resources.get("cpu_first", 0)),
                       "--numa-node", str(resources.get("numa_node", 0)),
                       "--firecracker-api-timeout-s",
                       str(raw.get("firecracker_api_timeout_s", 15.0)),
                       "--firecracker-snapshot-api-timeout-s",
                       str(raw.get("firecracker_snapshot_api_timeout_s", 300.0))]
            if resources.get("cpu_list"):
                prepare += ["--cpu-list", str(resources["cpu_list"])]
            try:
                subprocess.run(prepare, cwd=root, check=True)
            except Exception:
                if not bool(raw.get("retain_vm_artifacts", True)):
                    _discard_heavy_vm_artifacts(prepared)
                raise
            command = [sys.executable, str(root / "scripts" / "run-openclaw-experiment.py"),
                       str(prepared / "manifest.json"), "--output", str(results),
                       "--residency-policy", residency, "--inference", model,
                       "--validation-command", str(raw.get("validation_command",
                                                           "cd /testbed && git diff --binary --no-ext-diff HEAD")),
                       "--timeout-s", str(raw.get("timeout_s", 900))]
            if raw.get("correctness_command"):
                command += [
                    "--correctness-command", str(raw["correctness_command"]),
                    "--correctness-timeout-s",
                    str(raw.get("correctness_timeout_s", 300)),
                ]
            if model == "replay":
                command += ["--trace", str(trace), "--time-scale", str(replay.get("time_scale", 1.0))]
            else:
                command += ["--api-base-url", str(api["base_url"]), "--api-model", str(api["model"]),
                            "--api-key-env", key_env]
            if raw.get("resident_memory_budget_mib") is not None:
                command += [
                    "--resident-memory-budget-mib",
                    str(int(raw["resident_memory_budget_mib"])),
                ]
            if raw.get("tool_reservation_budget_mib") is not None:
                command += [
                    "--tool-reservation-budget-mib",
                    str(int(raw["tool_reservation_budget_mib"])),
                    "--tool-admission-safety-headroom-mib",
                    str(int(raw.get("tool_admission_safety_headroom_mib", 1024))),
                    "--idle-tool-vm-rss-mib",
                    str(float(raw["idle_tool_vm_rss_mib"])),
                ]
                if sizing_policy in {"p90_static", "p90_reservation"}:
                    p90_config = raw.get("p90_reservation") or raw.get("p90_static") or {}
                    plan_path = _path(base, p90_config["prediction_file"])
                    command += [
                        "--tool-memory-plan", str(plan_path),
                        "--tool-memory-workload", str(p90_config["workload_name"]),
                    ]
                else:
                    command += [
                        "--static-tool-reservation-mib",
                        str(workflow.resources.tool_memory_mib),
                    ]
            arm_started_at = utcnow()
            try:
                completed = subprocess.run(command, cwd=root, check=False)
            except Exception:
                if not bool(raw.get("retain_vm_artifacts", True)):
                    _discard_heavy_vm_artifacts(prepared)
                raise
            arm_completed_at = utcnow()
            summary_path = results / "summary.json"
            if not summary_path.is_file():
                if not bool(raw.get("retain_vm_artifacts", True)):
                    _discard_heavy_vm_artifacts(prepared)
                raise subprocess.CalledProcessError(getattr(completed, "returncode", 1), command)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if getattr(completed, "returncode", 0) not in (0, 1):
                if not bool(raw.get("retain_vm_artifacts", True)):
                    _discard_heavy_vm_artifacts(prepared)
                raise subprocess.CalledProcessError(completed.returncode, command)
            if getattr(completed, "returncode", 0) == 1 and not summary.get("failures"):
                if not bool(raw.get("retain_vm_artifacts", True)):
                    _discard_heavy_vm_artifacts(prepared)
                raise subprocess.CalledProcessError(completed.returncode, command)
            capability = validate_workflow(workflow)
            provenance = {"commit": commit, "source_tree_sha256": manifest["source_tree_sha256"],
                          "trace_sha256": manifest["trace_sha256"]}
            envelopes = [ResultEnvelope(
                run_id=f"{arm.name}-s{session.get('session', index)}", case_id=f"session-{session.get('session', index)}",
                baseline=workflow.baseline, classification=capability.classification,
                resolved_workflow=workflow,
                provenance=provenance,
                status=RunStatus.SUCCEEDED,
                started_at=arm_started_at, completed_at=arm_completed_at,
                metrics={"validation_sha256": session.get("validation_sha256"),
                         "vm_checkpoints": session.get("snapshots", 0),
                         "correctness_evaluated": session.get("correctness_evaluated", False),
                         "correctness_exit_code": session.get("correctness_exit_code"),
                         "session_wall_s": session.get("session_wall_s"),
                         "configured_tool_memory_mib": workflow.resources.tool_memory_mib,
                         "sizing_kb_generation": workflow.resources.kb_generation},
                artifacts={"summary": str(summary_path)},
                backend_details={"topology": "Runtime + Tool", "checkpoint_kind": "vm_checkpoint"},
            ).model_dump(mode="json") for index, session in enumerate(summary.get("sessions", []))]
            for index, failure in enumerate(summary.get("failures", [])):
                detail = str(failure.get("error", ""))
                failure_type = str(failure.get("type", ""))
                failed_status = (
                    RunStatus.TIMED_OUT
                    if failure_type in {"TimeoutError", "TimeoutExpired"} or "timed out" in detail.lower()
                    else RunStatus.FAILED
                )
                session_id = failure.get("session", index)
                envelopes.append(ResultEnvelope(
                    run_id=f"{arm.name}-s{session_id}", case_id=f"session-{session_id}",
                    baseline=workflow.baseline, classification=capability.classification,
                    resolved_workflow=workflow, provenance=provenance, status=failed_status,
                    failure_category=failure_category_for(failed_status, detail),
                    started_at=arm_started_at, completed_at=arm_completed_at,
                    artifacts={"summary": str(summary_path)},
                    backend_details={"topology": "Runtime + Tool",
                                     "checkpoint_kind": "vm_checkpoint",
                                     "exception_type": failure_type},
                ).model_dump(mode="json"))
            (results / "result-envelopes.json").write_text(
                json.dumps(envelopes, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            runs.append({"repetition": repetition, "inference_backend": model,
                         "arm_order_position": arm_position,
                         "sizing_policy": sizing_policy,
                         "memory_policy": legacy_residency, "residency_policy": residency,
                         "configured_tool_memory_mib": workflow.resources.tool_memory_mib,
                         "sizing_prediction": prediction.as_status() if sizing_policy in {"p90_static", "p90_reservation"} and prediction else None,
                         "resolved_workflow": workflow.model_dump(mode="json"),
                         "result_dir": str(results), **summary})
            if not bool(raw.get("retain_vm_artifacts", True)):
                _discard_heavy_vm_artifacts(prepared)

    grouped: dict[str, dict[str, Any]] = {}
    for model, sizing_policy, residency, _workflow in arms:
        legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
        all_rows = [row for row in runs if row["inference_backend"] == model
                    and row["sizing_policy"] == sizing_policy
                    and row["residency_policy"] == residency]
        rows = [row for row in all_rows if not row.get("failures")
                and int(row.get("sessions_completed", -1))
                == int(row.get("sessions_requested", -2))]
        grouped[arm_label(model, sizing_policy, residency)] = {
            "runs": len(all_rows), "valid_runs": len(rows),
            "sessions_completed": sum(row["sessions_completed"] for row in all_rows),
            "failures": sum(len(row["failures"]) for row in all_rows),
            "configured_tool_memory_mib": sorted({row["configured_tool_memory_mib"] for row in rows}),
            "validation_hashes": sorted({session["validation_sha256"] for row in rows
                                          for session in row.get("sessions", [])}),
            "statistics": {field: _stats(rows, field) for field in
                           ("wall_s", "throughput_sessions_per_hour",
                            "throughput_tasks_per_minute",
                            "throughput_correct_tasks_per_minute",
                            "correctness_pass_fraction",
                            "throughput_steps_per_minute",
                            "mean_firecracker_rss_bytes", "peak_firecracker_rss_bytes",
                            "p95_firecracker_rss_bytes", "firecracker_rss_time_byte_seconds",
                            "mean_numa_memory_used_bytes", "peak_numa_memory_used_bytes",
                            "mean_numa_memory_delta_bytes", "peak_numa_memory_delta_bytes",
                            "mean_cgroup_memory_delta_bytes", "peak_cgroup_memory_delta_bytes",
                            "peak_resident_vms", "checkpoint_cycles",
                            "vm_snapshot_operations", "vm_restore_operations",
                            "checkpoint_snapshot_service_s",
                            "checkpoint_restore_service_s", "admission_wait_s",
                            "admission_acquisitions",
                            "mean_admission_wait_event_s",
                            "p95_admission_wait_event_s",
                            "max_admission_wait_event_s",
                            "mean_session_wall_s", "p50_session_wall_s",
                            "p95_session_wall_s", "p99_session_wall_s",
                            "snapshot_allocated_bytes",
                            "tool_reservation_events",
                            "mean_tool_reservation_mib",
                            "max_tool_reservation_mib",
                            "tool_reservation_wait_s",
                            "max_tool_reservation_wait_s",
                            "tool_reservation_time_mib_s",
                            "tool_working_set_observations",
                            "max_actual_tool_command_memory_mib",
                            "mean_actual_tool_command_memory_mib",
                            "max_actual_tool_working_set_mib",
                            "prediction_observations",
                            "prediction_memory_coverage_fraction",
                            "mean_prediction_error_mib")},
        }
    validation_hashes = {
        session["validation_sha256"]
        for row in runs
        for session in row.get("sessions", [])
        if session.get("validation_sha256")
    }
    expected_successes = sum(int(row.get("sessions_completed", 0)) for row in runs)
    represented_successes = sum(
        1 for row in runs for session in row.get("sessions", [])
        if session.get("validation_sha256")
    )
    final_state_equal = (
        expected_successes > 0
        and represented_successes == expected_successes
        and len(validation_hashes) == 1
    )
    sizing_factors = list(raw.get("sizing_policies", ["fixed"]))
    if fixed_control_mib is not None:
        sizing_factors.insert(sizing_factors.index("fixed") + 1, "fixed2")
    final = {"schema_version": 3, "manifest": manifest, "groups": grouped,
             "factorial_design": {"sizing": sizing_factors,
                                  "residency": list(raw.get("memory_policies", ["resident", "snapshot"]))},
             "runs": runs, "final_state_equal": final_state_equal,
             "validation_hashes": sorted(validation_hashes),
             "validation_results_represented": represented_successes,
             "validation_results_expected": expected_successes}
    (output / "study-summary.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0 if all(not row["failures"] for row in runs) and final["final_state_equal"] else 1
