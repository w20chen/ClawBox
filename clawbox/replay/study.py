from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import statistics
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


def _path(base: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stats(rows: list[dict[str, Any]], field: str) -> dict[str, float | int | None]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return {"n": 0, "mean": None, "stdev": None, "ci95_half_width": None}
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return {"n": len(values), "mean": statistics.fmean(values), "stdev": deviation,
            "ci95_half_width": 1.96 * deviation / math.sqrt(len(values))}


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
    names = {"runtime.ext4", "tool.ext4", "runtime.mem", "tool.mem",
             "runtime.vmstate", "tool.vmstate"}
    for session in prepared.glob("session-*"):
        if session.parent != prepared:
            continue
        for name in names:
            path = session / name
            if path.is_file():
                path.unlink()


def _sizing_baselines(raw: dict[str, Any]) -> list[str]:
    sizing = list(raw.get("sizing_policies", ["fixed"]))
    residency = list(raw.get("memory_policies", ["resident", "snapshot"]))
    if not sizing or not set(sizing) <= {"fixed", "p90_static"}:
        raise ValueError("sizing_policies must contain fixed and/or p90_static")
    aliases = {
        ("fixed", "resident"): "fixed-explicit-resident",
        ("fixed", "snapshot"): "fixed-llm-wait-checkpoint",
        ("fixed", "llm_wait_checkpoint"): "fixed-llm-wait-checkpoint",
        ("p90_static", "resident"): "p90-static",
        ("p90_static", "snapshot"): "p90-static-llm-wait-checkpoint",
        ("p90_static", "llm_wait_checkpoint"): "p90-static-llm-wait-checkpoint",
    }
    try:
        return [aliases[(size, memory)] for size in sizing for memory in residency]
    except KeyError as exc:
        raise ValueError("unsupported sizing or memory policy") from exc


def _p90_prediction(raw: dict[str, Any], base: Path | None) -> AdmissionPrediction | None:
    if "p90_static" not in raw.get("sizing_policies", []):
        return None
    config = raw.get("p90_static")
    if not isinstance(config, dict):
        raise ValueError("p90_static configuration is required for predictive sizing")
    payload = config.get("prediction")
    if payload is None and config.get("prediction_file"):
        path = _path(base, config["prediction_file"]) if base is not None else Path(str(config["prediction_file"]))
        payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("p90_static requires prediction or prediction_file")
    prediction = AdmissionPrediction.from_payload(payload)
    expected_repo = str(
        raw.get("repository") or (raw.get("source") or {}).get("repository") or ""
    ).strip()
    if expected_repo and prediction.repo_fingerprint != expected_repo:
        raise ValueError(
            "p90_static prediction repository does not match the replay workload"
        )
    minimum = int(config.get("min_evidence", 5))
    if minimum < 1:
        raise ValueError("p90_static.min_evidence must be positive")
    if prediction.evidence_count < minimum:
        raise ValueError(
            f"p90_static prediction has {prediction.evidence_count} samples; "
            f"at least {minimum} are required"
        )
    return prediction


def _predictive_tool_memory_mib(raw: dict[str, Any], prediction: AdmissionPrediction) -> int:
    resources = raw.get("resources", {})
    fixed_mib = int(resources.get("tool_memory_mib", 4096))
    config = raw.get("p90_static") or {}
    headroom = float(config.get("headroom_fraction", 0.25))
    floor_mib = int(config.get("min_tool_memory_mib", 2048))
    if not 0 <= headroom <= 1:
        raise ValueError("p90_static.headroom_fraction must be between 0 and 1")
    if floor_mib < 256 or floor_mib > fixed_mib:
        raise ValueError("p90_static.min_tool_memory_mib must be between 256 and fixed Tool memory")
    predicted_mib = math.ceil(
        prediction.memory_p90_bytes * (1 + headroom) / (1024.0 * 1024.0)
    )
    selected = min(fixed_mib, max(floor_mib, predicted_mib))
    if selected >= fixed_mib:
        raise ValueError("p90_static prediction does not reduce Tool memory below the fixed baseline")
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
        _predictive_tool_memory_mib(raw, prediction) if prediction is not None else None
    )
    workflows = []
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
        workflows.append(resolved)
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
        "p90_static" if workflow.admission_policy is AdmissionPolicy.P90_STATIC else "fixed",
        workflow.residency_policy.value,
        workflow,
    ) for workflow in workflows]
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
        for model, sizing_policy, residency, workflow in shuffled:
            legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
            arm = output / f"r{repetition:02d}-{arm_label(model, sizing_policy, residency)}"
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
                       "--numa-node", str(resources.get("numa_node", 0))]
            if resources.get("cpu_list"):
                prepare += ["--cpu-list", str(resources["cpu_list"])]
            subprocess.run(prepare, cwd=root, check=True)
            command = [sys.executable, str(root / "scripts" / "run-openclaw-experiment.py"),
                       str(prepared / "manifest.json"), "--output", str(results),
                       "--residency-policy", residency, "--inference", model,
                       "--validation-command", str(raw.get("validation_command",
                                                           "cd /testbed && git diff --binary --no-ext-diff HEAD")),
                       "--timeout-s", str(raw.get("timeout_s", 900))]
            if model == "replay":
                command += ["--trace", str(trace), "--time-scale", str(replay.get("time_scale", 1.0))]
            else:
                command += ["--api-base-url", str(api["base_url"]), "--api-model", str(api["model"]),
                            "--api-key-env", key_env]
            arm_started_at = utcnow()
            completed = subprocess.run(command, cwd=root, check=False)
            arm_completed_at = utcnow()
            summary_path = results / "summary.json"
            if not summary_path.is_file():
                raise subprocess.CalledProcessError(getattr(completed, "returncode", 1), command)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if getattr(completed, "returncode", 0) not in (0, 1):
                raise subprocess.CalledProcessError(completed.returncode, command)
            if getattr(completed, "returncode", 0) == 1 and not summary.get("failures"):
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
                         "sizing_policy": sizing_policy,
                         "memory_policy": legacy_residency, "residency_policy": residency,
                         "configured_tool_memory_mib": workflow.resources.tool_memory_mib,
                         "sizing_prediction": prediction.as_status() if sizing_policy == "p90_static" and prediction else None,
                         "resolved_workflow": workflow.model_dump(mode="json"),
                         "result_dir": str(results), **summary})
            if not bool(raw.get("retain_vm_artifacts", True)):
                _discard_heavy_vm_artifacts(prepared)

    grouped: dict[str, dict[str, Any]] = {}
    for model, sizing_policy, residency, _workflow in arms:
        legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
        rows = [row for row in runs if row["inference_backend"] == model
                and row["sizing_policy"] == sizing_policy
                and row["residency_policy"] == residency]
        grouped[arm_label(model, sizing_policy, residency)] = {
            "runs": len(rows), "sessions_completed": sum(row["sessions_completed"] for row in rows),
            "failures": sum(len(row["failures"]) for row in rows),
            "configured_tool_memory_mib": sorted({row["configured_tool_memory_mib"] for row in rows}),
            "validation_hashes": sorted({session["validation_sha256"] for row in rows
                                          for session in row.get("sessions", [])}),
            "statistics": {field: _stats(rows, field) for field in
                           ("wall_s", "throughput_sessions_per_hour",
                            "throughput_tasks_per_minute", "throughput_steps_per_minute",
                            "mean_firecracker_rss_bytes", "peak_firecracker_rss_bytes")},
        }
    validation_sets = {tuple(value["validation_hashes"]) for value in grouped.values()}
    final = {"schema_version": 3, "manifest": manifest, "groups": grouped,
             "factorial_design": {"sizing": list(raw.get("sizing_policies", ["fixed"])),
                                  "residency": list(raw.get("memory_policies", ["resident", "snapshot"]))},
             "runs": runs, "final_state_equal": len(validation_sets) == 1}
    (output / "study-summary.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0 if all(not row["failures"] for row in runs) and final["final_state_equal"] else 1
