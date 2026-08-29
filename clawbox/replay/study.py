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


def run_study(config_path: Path) -> int:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.resolve().parent
    root = Path(__file__).resolve().parents[2]
    source, resources = raw["source"], raw.get("resources", {})
    replay, api = raw.get("replay", {}), raw.get("api", {})
    output = _path(base, raw["output"])
    output.mkdir(parents=True, exist_ok=False)

    runtime_rootfs = _path(base, source["runtime_rootfs"])
    tool_rootfs = _path(base, source["tool_rootfs"])
    prompt = _path(base, source["prompt"])
    trace = _path(base, source["trace"])
    sessions, repetitions = int(raw.get("sessions", 1)), int(raw.get("repetitions", 1))
    if sessions < 1 or repetitions < 1:
        raise ValueError("sessions and repetitions must be positive")
    inference = list(raw.get("inference_backends", ["replay", "api"]))
    memory = list(raw.get("memory_policies", ["resident", "snapshot"]))
    if not set(inference) <= {"replay", "api"} or not set(memory) <= {"resident", "snapshot"}:
        raise ValueError("unsupported inference backend or memory policy")
    key_env = str(api.get("key_env", "OPENAI_API_KEY"))
    if "api" in inference and (not api.get("base_url") or not api.get("model") or not os.getenv(key_env)):
        raise ValueError("api mode requires api.base_url, api.model, and its key environment variable")

    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()
    manifest = {"schema_version": 2, "started_unix_s": time.time(), "commit": commit,
                "source_tree_sha256": _source_hash(root), "host": platform.node(),
                "machine": platform.machine(), "trace_sha256": _sha256(trace), "config": raw}
    (output / "study-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    arms = [(model, residency) for model in inference for residency in memory]
    order = random.Random(int(raw.get("seed", 0)))
    runs: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        shuffled = list(arms)
        order.shuffle(shuffled)
        for model, residency in shuffled:
            arm = output / f"r{repetition:02d}-{model}-{residency}"
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
                       "--mode", residency, "--inference", model,
                       "--validation-command", str(raw.get("validation_command",
                                                           "cd /testbed && git diff --binary --no-ext-diff HEAD")),
                       "--timeout-s", str(raw.get("timeout_s", 900))]
            if model == "replay":
                command += ["--trace", str(trace), "--time-scale", str(replay.get("time_scale", 1.0))]
            else:
                command += ["--api-base-url", str(api["base_url"]), "--api-model", str(api["model"]),
                            "--api-key-env", key_env]
            subprocess.run(command, cwd=root, check=True)
            summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
            runs.append({"repetition": repetition, "inference_backend": model,
                         "memory_policy": residency, "result_dir": str(results), **summary})

    grouped: dict[str, dict[str, Any]] = {}
    for model, residency in arms:
        rows = [row for row in runs if row["inference_backend"] == model
                and row["memory_policy"] == residency]
        grouped[f"{model}-{residency}"] = {
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
