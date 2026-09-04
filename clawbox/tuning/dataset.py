"""Offline dataset export: ClawTune traces + bridge records -> train/eval.

Reads the two raw JSONL sources produced on the real machine, joins them by
execution_id (exact), validates the result, and writes a stratified train/eval
split as JSONL (and parquet when pyarrow is available).  The split is stratified
on the command digest so repeated invocations of the same command do not leak
across the split.

Layout of a real runtime pod:

* ClawTune trace: ``/state/<cell>/traces/<run>-*.jsonl``  (span JSONL, v6)
* Bridge record:  ``/testbed/.clawbox/tool-bridge.jsonl``   (execution JSONL)
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .join import JoinResult, join_trace_and_bridge
from .schema import (
    BridgeRecord,
    CgroupResource,
    ToolObservation,
    bridge_record_to_observation,
    cgroup_artifact_to_resource,
)
from .validate import ObservationValidator, classify_observations


def read_trace_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a ClawTune v6 trace JSONL into raw records."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trace JSONL at {path}:{lineno}: {exc}") from exc
    return records


def read_bridge_jsonl(path: Path) -> list[BridgeRecord]:
    """Parse a tool-bridge execution JSONL into validated BridgeRecords."""
    records: list[BridgeRecord] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid bridge JSONL at {path}:{lineno}: {exc}") from exc
            try:
                records.append(bridge_record_to_observation(raw))
            except Exception as exc:  # pydantic ValidationError
                raise ValueError(f"invalid bridge record at {path}:{lineno}: {exc}") from exc
    return records


def iter_trace_dir(trace_dir: Path) -> Iterator[dict[str, Any]]:
    """Yield raw records from every ``*.jsonl`` under a trace directory."""
    for path in sorted(trace_dir.glob("*.jsonl")):
        yield from read_trace_jsonl(path)


def read_cgroup_artifacts(trace_dir: Path) -> dict[str, CgroupResource]:
    """Load Tool-VM ``cgroup-resource-<execution_id>.json`` artifacts.

    Scans ``<trace_dir>/tool-resource/*.json`` (the ClawTune layout) plus any
    ``<trace_dir>/cgroup-resource-*.json`` at the top level.  Malformed or
    non-cgroup artifacts are skipped; the result is keyed by execution_id.
    """
    artifacts: dict[str, CgroupResource] = {}
    candidates: list[Path] = []
    tool_resource = trace_dir / "tool-resource"
    if tool_resource.is_dir():
        candidates.extend(sorted(tool_resource.glob("*.json")))
    candidates.extend(sorted(trace_dir.glob("cgroup-resource-*.json")))
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        resource = cgroup_artifact_to_resource(raw)
        if resource is None or resource.execution_id is None:
            continue
        artifacts[resource.execution_id] = resource
    return artifacts


def build_joined_dataset(
    trace_dir: Path,
    bridge_path: Path,
    ingest_secret: str | None = None,
) -> tuple[JoinResult, list[ToolObservation]]:
    """Join + validate everything under ``trace_dir`` against the bridge log.

    Returns ``(join_result, trusted_observations)`` where ``trusted`` are the
    validated, deduplicated observations that may train the KB.  When the
    Tool-VM cgroup/procfs collector produced ``cgroup-resource-*.json``
    artifacts, they are joined by execution_id and their quality gate and
    measured CPU/RSS values supersede span-side proxies.
    """
    # Validation and output-hash commands are harness work, not agent
    # trajectory observations. They may be retained in raw artifacts, but
    # cannot enter Tool latency/P90 training or policy statistics.
    span_records = [
        record for record in iter_trace_dir(trace_dir)
        if record.get("phase", "agent") == "agent"
    ]
    bridges = [
        record for record in read_bridge_jsonl(bridge_path)
        if record.phase == "agent"
    ]
    cgroup_artifacts = read_cgroup_artifacts(trace_dir)
    joined = join_trace_and_bridge(span_records, bridges, cgroup_artifacts)
    validator = ObservationValidator(ingest_secret)
    report = classify_observations(list(joined.joined), validator)
    return joined, report.trusted


def _group_by_command(observations: list[ToolObservation]) -> dict[str, list[ToolObservation]]:
    groups: dict[str, list[ToolObservation]] = {}
    for observation in observations:
        key = observation.command_digest or observation.command or observation.execution_id
        groups.setdefault(key, []).append(observation)
    return groups


def command_disjoint_split(
    observations: list[ToolObservation],
    train_frac: float = 0.8,
    seed: int = 42,
) -> tuple[list[ToolObservation], list[ToolObservation]]:
    """Command-disjoint split: eval commands never appear in train.

    This measures honest cold-start generalization — the estimator has no
    history for eval commands and must fall back to the global default.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1)")
    rng = random.Random(seed)
    groups = _group_by_command(observations)
    keys = sorted(groups)
    rng.shuffle(keys)
    split_at = max(1, min(len(keys) - 1, int(round(len(keys) * train_frac))))
    train_keys = set(keys[:split_at])
    train: list[ToolObservation] = []
    eval_: list[ToolObservation] = []
    for key in keys:
        (train if key in train_keys else eval_).extend(groups[key])
    return train, eval_


def stratified_split(
    observations: list[ToolObservation],
    train_frac: float = 0.8,
    seed: int = 42,
) -> tuple[list[ToolObservation], list[ToolObservation]]:
    """Within-command stratified split (same commands appear in both sides).

    Used to measure prediction quality on commands the KB already has history
    for (the per-command estimator path).  Not for leakage-free cold-start
    reporting — use :func:`command_disjoint_split` for that.
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError("train_frac must be in (0, 1)")
    rng = random.Random(seed)
    groups = _group_by_command(observations)
    train: list[ToolObservation] = []
    eval_: list[ToolObservation] = []
    for key in sorted(groups):
        group = list(groups[key])
        if len(group) == 1:
            (train if rng.random() < train_frac else eval_).extend(group)
            continue
        split_at = max(1, int(round(len(group) * train_frac)))
        rng.shuffle(group)
        train.extend(group[:split_at])
        eval_.extend(group[split_at:])
    return train, eval_


def observation_to_row(observation: ToolObservation) -> dict[str, Any]:
    return observation.model_dump(mode="json")


def export_dataset(
    observations: list[ToolObservation],
    output_dir: Path,
    train_frac: float = 0.8,
    seed: int = 42,
    write_parquet: bool = True,
    split: str = "command",
) -> dict[str, int]:
    """Write ``train.jsonl`` + ``eval.jsonl`` (and parquet) under ``output_dir``.

    ``split="command"`` uses a command-disjoint split (no leakage across the
    boundary, honest cold-start eval); ``split="stratified"`` uses a
    within-command split for measuring known-command prediction.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if split == "command":
        train, eval_ = command_disjoint_split(observations, train_frac=train_frac, seed=seed)
    elif split == "stratified":
        train, eval_ = stratified_split(observations, train_frac=train_frac, seed=seed)
    else:
        raise ValueError(f"unknown split mode {split!r}; expected 'command' or 'stratified'")
    for name, subset in (("train", train), ("eval", eval_)):
        jsonl_path = output_dir / f"{name}.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as handle:
            for observation in subset:
                handle.write(json.dumps(observation_to_row(observation), sort_keys=True) + "\n")
        if write_parquet:
            _write_parquet(subset, output_dir / f"{name}.parquet")
    return {"train": len(train), "eval": len(eval_)}


def _write_parquet(observations: list[ToolObservation], path: Path) -> None:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError:
        return
    rows = [observation_to_row(observation) for observation in observations]
    if not rows:
        table = pa.table({})
    else:
        import pandas as pd  # type: ignore

        table = pa.Table.from_pandas(pd.DataFrame(rows))
    pq.write_table(table, path)
