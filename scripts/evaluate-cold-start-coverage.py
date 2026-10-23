"""Measure cold-start admission coverage on a workload without starting VMs."""
from __future__ import annotations

import argparse
from collections import Counter
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clawbox.experiments.prediction import (
    ColdStartPredictionProvider,
    PredictionUnavailable,
    command_sha256,
)
from clawbox.experiments.training import trace_commands


IDENTITY_FIELDS = (
    "template_id", "image_digest", "vcpu", "memory_mib", "architecture",
)


def _distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(q: float) -> float:
        return ordered[max(0, int((len(ordered) * q) + 0.999999) - 1)]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p50": percentile(.5),
        "p90": percentile(.9),
        "p99": percentile(.99),
        "max": ordered[-1],
    }


def _json_object(path: Path, description: str) -> tuple[dict, bytes]:
    raw = path.resolve(strict=True).read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {description} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value, raw


def evaluate(seed: Path, workload: Path) -> dict:
    seed = seed.resolve(strict=True)
    workload = workload.resolve(strict=True)
    catalog, workload_raw = _json_object(workload, "workload catalog")
    tasks = catalog.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("workload catalog requires a nonempty tasks list")

    levels: Counter[str] = Counter()
    unavailable: Counter[str] = Counter()
    task_failures: Counter[str] = Counter()
    skipped_levels: Counter[str] = Counter()
    skipped_reasons: Counter[str] = Counter()
    partial_by_level: Counter[str] = Counter()
    reservation_mib: dict[str, list[float]] = defaultdict(list)
    command_count = available_count = partial_count = tasks_with_commands = 0
    partial_dynamic_count = 0
    seed_manifest_sha256 = hashlib.sha256(
        (seed / "manifest.json").read_bytes()
    ).hexdigest()

    for task in tasks:
        if not isinstance(task, dict):
            task_failures["invalid_task"] += 1
            continue
        sandbox = task.get("sandbox")
        if not isinstance(sandbox, dict):
            task_failures["missing_sandbox"] += 1
            continue
        identity = {field: sandbox.get(field) for field in IDENTITY_FIELDS}
        trace_value = task.get("replay_trace_reference") or task.get("source_reference")
        repository = task.get("repository") or task.get("case_id")
        if not isinstance(trace_value, str) or not trace_value:
            task_failures["missing_trace"] += 1
            continue
        if not isinstance(repository, str) or not repository:
            task_failures["missing_repository"] += 1
            continue
        try:
            commands = trace_commands(Path(trace_value))
            provider = ColdStartPredictionProvider(
                seed, repository=repository, sandbox_identity=identity,
            )
        except (OSError, ValueError) as exc:
            task_failures[f"{type(exc).__name__}:{exc}"] += 1
            continue
        if commands:
            tasks_with_commands += 1
        for command in commands:
            command_count += 1
            try:
                prediction = provider.resolve(command, {
                    "raw_command_sha256": command_sha256(command),
                })
            except PredictionUnavailable as exc:
                unavailable[str(exc)] += 1
                continue
            available_count += 1
            level = str(prediction.get("fallback_level") or "unknown")
            levels[level] += 1
            for skipped_level, skipped_reason in (
                prediction.get("unavailable_models") or {}
            ).items():
                skipped_levels[str(skipped_level)] += 1
                skipped_reasons[f"{skipped_level}:{skipped_reason}"] += 1
            reservation = prediction.get("predicted_incremental_memory_mib")
            if isinstance(reservation, (int, float)) and not isinstance(reservation, bool):
                reservation_mib[level].append(float(reservation))
            target = prediction.get("prediction_signal_target")
            targets = (prediction.get("call_prediction") or {}).get("targets") or {}
            assumptions = (targets.get(target) or {}).get("assumptions") or []
            if "partial_clause_evidence" in assumptions:
                partial_count += 1
                partial_by_level[level] += 1
            if "partial_dynamic_structure_evidence" in assumptions:
                partial_dynamic_count += 1

    from clawtune_sidecar.predictors import call_load
    call_load_path = Path(call_load.__file__).resolve(strict=True)

    return {
        "schema": "clawbox.cold_start_coverage.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": str(seed),
        "seed_manifest_sha256": seed_manifest_sha256,
        "workload": str(workload),
        "workload_sha256": hashlib.sha256(workload_raw).hexdigest(),
        "task_count": len(tasks),
        "tasks_with_commands": tasks_with_commands,
        "command_count": command_count,
        "available_count": available_count,
        "availability_rate": available_count / command_count if command_count else 0.0,
        "partial_clause_evidence_count": partial_count,
        "partial_clause_evidence_by_level": dict(sorted(partial_by_level.items())),
        "partial_dynamic_structure_evidence_count": partial_dynamic_count,
        "clawtune_call_load_sha256": hashlib.sha256(
            call_load_path.read_bytes()
        ).hexdigest(),
        "fallback_counts": dict(sorted(levels.items())),
        "skipped_level_counts": dict(sorted(skipped_levels.items())),
        "skipped_reason_counts": dict(skipped_reasons.most_common()),
        "predicted_incremental_memory_mib_by_level": {
            level: _distribution(values)
            for level, values in sorted(reservation_mib.items())
        },
        "unavailable_counts": dict(unavailable.most_common()),
        "task_failure_counts": dict(task_failures.most_common()),
        "interpretation": (
            "Corpus replay coverage only; this report does not measure held-out "
            "prediction accuracy or start CubeSandbox VMs."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, type=Path,
                        help="validated immutable ClawTune seed directory")
    parser.add_argument("--workload", required=True, type=Path,
                        help="ClawBox workload catalog to inspect")
    parser.add_argument("--output", required=True, type=Path,
                        help="new JSON report path (never overwritten)")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("output report already exists; use a new path")
    try:
        report = evaluate(args.seed, args.workload)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
