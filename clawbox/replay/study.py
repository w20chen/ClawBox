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


def study_experiment_spec(raw: dict[str, Any], *, base: Path | None = None) -> ExperimentSpec:
    """Translate the established paper-study JSON into the canonical schema.

    The old file is intentionally retained: ``memory_policies.snapshot`` is a
    compatibility spelling for the direct-Firecracker LLM-wait checkpoint arm.
    """
    source = raw["source"]
    inference = list(raw.get("inference_backends", ["replay", "api"]))
    legacy_memory = list(raw.get("memory_policies", ["resident", "snapshot"]))
    if not set(inference) <= {"replay", "api"}:
        raise ValueError("unsupported inference backend")
    aliases = {"resident": "fixed-explicit-resident", "snapshot": "fixed-llm-wait-checkpoint",
               "llm_wait_checkpoint": "fixed-llm-wait-checkpoint"}
    try:
        baselines = [aliases[value] for value in legacy_memory]
    except KeyError as exc:
        raise ValueError("unsupported memory policy") from exc
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
    workflows = expand_matrix(spec)
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
    manifest = {"schema_version": 2, "started_unix_s": time.time(), "commit": commit,
                "source_tree_sha256": _source_hash(root), "host": platform.node(),
                "machine": platform.machine(), "trace_sha256": _sha256(trace), "config": raw}
    (output / "study-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    arms = [(workflow.inference_backend.value, workflow.residency_policy.value, workflow)
            for workflow in workflows]
    order = random.Random(int(raw.get("seed", 0)))
    runs: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        shuffled = list(arms)
        order.shuffle(shuffled)
        for model, residency, workflow in shuffled:
            legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
            arm = output / f"r{repetition:02d}-{model}-{legacy_residency}"
            prepared, results = arm / "input", arm / "results"
            arm.mkdir()
            exposed_model = str(raw.get("exposed_model", "experiment-model"))
            prepare = [sys.executable, str(root / "scripts" / "prepare-openclaw-experiment.py"),
                       "--output", str(prepared), "--sessions", str(sessions),
                       "--runtime-rootfs", str(runtime_rootfs), "--tool-rootfs", str(tool_rootfs),
                       "--prompt", str(prompt), "--model-id", exposed_model,
                       "--network-prefix", str(raw.get("network_prefix", "172.30")),
                       "--runtime-memory-mib", str(resources.get("runtime_memory_mib", 2048)),
                       "--tool-memory-mib", str(resources.get("tool_memory_mib", 4096)),
                       "--cpu-first", str(resources.get("cpu_first", 0)),
                       "--numa-node", str(resources.get("numa_node", 0))]
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
                         "vm_checkpoints": session.get("snapshots", 0)},
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
                         "memory_policy": legacy_residency, "residency_policy": residency,
                         "resolved_workflow": workflow.model_dump(mode="json"),
                         "result_dir": str(results), **summary})

    grouped: dict[str, dict[str, Any]] = {}
    for model, residency, _workflow in arms:
        legacy_residency = "snapshot" if residency == "llm_wait_checkpoint" else residency
        rows = [row for row in runs if row["inference_backend"] == model
                and row["residency_policy"] == residency]
        grouped[f"{model}-{legacy_residency}"] = {
            "runs": len(rows), "sessions_completed": sum(row["sessions_completed"] for row in rows),
            "failures": sum(len(row["failures"]) for row in rows),
            "validation_hashes": sorted({session["validation_sha256"] for row in rows
                                          for session in row.get("sessions", [])}),
            "statistics": {field: _stats(rows, field) for field in
                           ("wall_s", "throughput_sessions_per_hour",
                            "mean_firecracker_rss_bytes", "peak_firecracker_rss_bytes")},
        }
    validation_sets = {tuple(value["validation_hashes"]) for value in grouped.values()}
    final = {"schema_version": 2, "manifest": manifest, "groups": grouped,
             "runs": runs, "final_state_equal": len(validation_sets) == 1}
    (output / "study-summary.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n")
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0 if all(not row["failures"] for row in runs) and final["final_state_equal"] else 1
