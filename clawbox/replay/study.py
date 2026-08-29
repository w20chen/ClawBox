from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import math
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


def _add(options: list[str], name: str, value: object | None) -> None:
    if value is not None:
        options.extend([name, str(value)])


_SOURCE_PATHS = ("README.md", "pyproject.toml", "clawbox", "scripts", "deploy", "docker", "toolbridge")


def _source_tree_hash(root: Path) -> tuple[str, list[str]]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others",
         "--exclude-standard", "--", *_SOURCE_PATHS],
        check=True, capture_output=True, text=True,
    )
    files = sorted(line for line in result.stdout.splitlines() if line)
    digest = hashlib.sha256()
    for relative in files:
        path = root / relative
        if path.is_file():
            digest.update(relative.replace("\\", "/").encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest(), files


def _stats(rows: list[dict[str, Any]], field: str) -> dict[str, float | int | None]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    if not values:
        return {"n": 0, "mean": None, "stdev": None, "ci95_half_width": None}
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    return {
        "n": len(values), "mean": statistics.fmean(values), "stdev": stdev,
        "ci95_half_width": 1.96 * stdev / math.sqrt(len(values)),
    }


def run_study(config_path: Path) -> int:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.resolve().parent
    source = raw["source"]
    resources = raw.get("resources", {})
    policy = raw.get("policy", {})
    api = raw.get("api", {})
    output = _path(base, raw["output"])
    output.mkdir(parents=True, exist_ok=False)

    trace = _path(base, source["trace"])
    calibration = _path(base, source["calibration"])
    workspace = _path(base, source["workspace"])
    runtime_rootfs = _path(base, source["runtime_rootfs"])
    tool_rootfs = _path(base, source.get("tool_rootfs", runtime_rootfs))
    sessions = int(raw.get("sessions", 1))
    repetitions = int(raw.get("repetitions", 1))
    resident_slots = int(raw.get("resident_slots", sessions))
    tool_resident_slots = int(raw.get("tool_resident_slots", resident_slots))
    if min(sessions, repetitions, resident_slots, tool_resident_slots) < 1:
        raise ValueError("sessions, repetitions, and resident slot counts must be positive")

    inference_backends = list(raw.get("inference_backends", ["replay", "api"]))
    memory_policies = list(raw.get("memory_policies", ["resident", "snapshot"]))
    if not set(inference_backends) <= {"replay", "api"}:
        raise ValueError("inference_backends may contain only replay and api")
    if not set(memory_policies) <= {"resident", "snapshot"}:
        raise ValueError("memory_policies may contain only resident and snapshot")
    if "api" in inference_backends:
        key_env = str(api.get("key_env", "OPENAI_API_KEY"))
        if not api.get("base_url") or not api.get("model") or not os.environ.get(key_env):
            raise ValueError("api arm requires api.base_url, api.model, and its key_env")

    root = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_tree_sha256, source_files = _source_tree_hash(root)
    metadata = {
        "schema_version": 1,
        "started_unix_s": time.time(),
        "commit": commit,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": len(source_files),
        "host": platform.node(),
        "machine": platform.machine(),
        "trace_sha256": _sha256(trace),
        "calibration_sha256": _sha256(calibration),
        "config": raw,
    }
    (output / "study-manifest.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )

    arms = [(backend, memory) for backend in inference_backends for memory in memory_policies]
    summaries: list[dict[str, Any]] = []
    rng = random.Random(int(raw.get("seed", 0)))
    for repetition in range(repetitions):
        ordered = list(arms)
        rng.shuffle(ordered)
        for backend, memory in ordered:
            arm_name = f"r{repetition:02d}-{backend}-{memory}"
            arm = output / arm_name
            prepared = arm / "input"
            results = arm / "results"
            arm.mkdir()
            prepare = [
                sys.executable, str(root / "scripts" / "prepare-high-density-experiment.py"),
                "--output", str(prepared), "--sessions", str(sessions),
                "--workspace-source", str(workspace), "--base-commit", str(source["base_commit"]),
                "--rootfs-source", str(runtime_rootfs),
                "--tool-rootfs-source", str(tool_rootfs),
                "--trace", str(trace), "--calibration", str(calibration),
                "--memory-mib", str(resources.get("memory_mib", 512)),
                "--cpu-first", str(resources.get("cpu_first", 0)),
                "--numa-node", str(resources.get("numa_node", 0)),
                "--guest-agent",
                "--guest-touch-mib", str(resources.get("runtime_touch_mib", 128)),
                "--tool-guest-touch-mib", str(resources.get("tool_touch_mib", 256)),
            ]
            subprocess.run(prepare, cwd=root, check=True)

            command = [
                sys.executable, "-m", "clawbox.replay.cli", "experiment",
                str(prepared / "manifest.json"), "--output-dir", str(results),
                "--mode", memory, "--inference-backend", backend,
                "--resident-slots", str(resident_slots),
                "--tool-resident-slots", str(tool_resident_slots),
                "--numa-node", str(resources.get("numa_node", 0)),
            ]
            _add(command, "--validation-command", raw.get("validation_command"))
            for flag, key, default in (
                ("--sleep-scale", "sleep_scale", 1.0),
                ("--tool-time-scale", "tool_time_scale", 1.0),
                ("--snapshot-threshold-s", "runtime_threshold_s", 20.0),
                ("--tool-snapshot-threshold-s", "tool_threshold_s", 30.0),
                ("--estimated-snapshot-s", "estimated_snapshot_s", 1.0),
                ("--estimated-restore-s", "estimated_restore_s", 1.0),
                ("--estimated-refault-s", "estimated_refault_s", 0.0),
                ("--safety-margin-s", "safety_margin_s", 2.0),
            ):
                _add(command, flag, policy.get(key, default))
            if backend == "api":
                _add(command, "--api-base-url", api["base_url"])
                _add(command, "--api-model", api["model"])
                _add(command, "--api-key-env", api.get("key_env", "OPENAI_API_KEY"))
                _add(command, "--api-timeout-s", api.get("timeout_s", 600))
                if api.get("trust_env", False):
                    command.append("--api-trust-env")
            subprocess.run(command, cwd=root, check=True)
            summary = json.loads((results / "summary.json").read_text(encoding="utf-8"))
            summaries.append({
                "repetition": repetition,
                "inference_backend": backend,
                "memory_policy": memory,
                "result_dir": str(results),
                **summary,
            })

    grouped: dict[str, dict[str, Any]] = {}
    for backend, memory in arms:
        rows = [row for row in summaries if row["inference_backend"] == backend
                and row["memory_policy"] == memory]
        key = f"{backend}-{memory}"
        grouped[key] = {
            "runs": len(rows),
            "sessions_completed": sum(int(row["sessions_completed"]) for row in rows),
            "failures": sum(len(row["failures"]) for row in rows),
            "mean_wall_s": sum(float(row["wall_s"]) for row in rows) / len(rows),
            "mean_throughput_sessions_per_hour": sum(
                float(row["throughput_sessions_per_hour"]) for row in rows
            ) / len(rows),
            "mean_firecracker_rss_bytes": sum(
                float(row["mean_firecracker_rss_bytes"]) for row in rows
            ) / len(rows),
            "max_peak_firecracker_rss_bytes": max(
                int(row["peak_firecracker_rss_bytes"]) for row in rows
            ),
            "max_peak_numa_memory_used_bytes": max(
                (int(row["peak_numa_memory_used_bytes"])
                 for row in rows if row["peak_numa_memory_used_bytes"] is not None),
                default=None,
            ),
            "max_peak_cgroup_memory_delta_bytes": max(
                (int(row["peak_cgroup_memory_delta_bytes"])
                 for row in rows if row.get("peak_cgroup_memory_delta_bytes") is not None),
                default=None,
            ),
            "validation_hashes": sorted({
                str(item["validation_sha256"])
                for row in rows for item in row.get("sessions", [])
                if item.get("validation_sha256") is not None
            }),
            "statistics": {
                field: _stats(rows, field) for field in (
                    "wall_s", "throughput_sessions_per_hour",
                    "mean_firecracker_rss_bytes", "peak_cgroup_memory_delta_bytes",
                )
            },
        }
    comparisons: dict[str, dict[str, float | None]] = {}
    for backend in inference_backends:
        resident = grouped.get(f"{backend}-resident")
        snapshot = grouped.get(f"{backend}-snapshot")
        if resident is None or snapshot is None:
            continue
        def change(field: str) -> float | None:
            baseline = resident.get(field)
            value = snapshot.get(field)
            return None if baseline in (None, 0) or value is None else (value / baseline - 1) * 100
        comparisons[backend] = {
            "snapshot_wall_time_change_percent": change("mean_wall_s"),
            "snapshot_throughput_change_percent": change("mean_throughput_sessions_per_hour"),
            "snapshot_mean_rss_change_percent": change("mean_firecracker_rss_bytes"),
            "snapshot_peak_cgroup_memory_change_percent": change(
                "max_peak_cgroup_memory_delta_bytes"
            ),
        }
    all_validation_hashes = sorted({
        value for group in grouped.values() for value in group["validation_hashes"]
    })
    correctness_equal = not raw.get("validation_command") or len(all_validation_hashes) == 1
    report = {
        "metadata": metadata, "groups": grouped, "comparisons": comparisons,
        "correctness": {"equal": correctness_equal, "validation_hashes": all_validation_hashes},
        "runs": summaries,
    }
    (output / "study-summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps(grouped, indent=2, sort_keys=True))
    return 0 if correctness_equal else 1
