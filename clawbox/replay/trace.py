from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplayAction:
    kind: str
    action_id: str
    sequence_no: int
    start_s: float
    duration_s: float
    name: str
    input: Any = None
    output: Any = None
    expected_exit_code: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def input_chars(self) -> int:
        return _payload_chars(self.input)

    @property
    def output_chars(self) -> int:
        return _payload_chars(self.output)


def _payload_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"JSONL record at {path}:{line_number} is not an object")
            records.append(record)
    return records


def load_trace(path: Path) -> list[ReplayAction]:
    """Read LLM spans from an unmodified ClawTune schema-6 recording.

    Tool spans and resource records remain in the source file for analysis.
    OpenClaw, not this reader, executes tools during replay.
    """
    records = _read_jsonl(path)
    starts = {}
    actions = []
    for record in records:
        if record.get("schema_version") != 6:
            raise ValueError(f"{path}: expected a ClawTune schema-6 recording")
        if record.get("kind") != "llm":
            continue
        key = (record.get("trace_id"), record.get("span_id"))
        if not all(isinstance(value, str) and value for value in key):
            raise ValueError(f"{path}: LLM span requires trace_id and span_id")
        record_type = record.get("record_type")
        if record_type == "span_start":
            if key in starts:
                raise ValueError(f"{path}: duplicate LLM span start")
            starts[key] = record
        elif record_type == "span_end":
            before = starts.pop(key, None)
            if before is None:
                raise ValueError(f"{path}: LLM span end without start")
            messages = (before.get("input") or {}).get("messages")
            if not isinstance(messages, list):
                raise ValueError(f"{path}: LLM span requires complete input.messages")
            if any(isinstance(message, dict) and message.get("truncated") for message in messages):
                raise ValueError(f"{path}: truncated messages cannot be replayed")
            output = (record.get("output") or {}).get("content")
            if output is None:
                raise ValueError(f"{path}: LLM span has no output.content")
            if (record.get("status") or {}).get("code") != "ok":
                raise ValueError(f"{path}: failed LLM span cannot be replayed")
            duration = _finite_float(record.get("duration_ns"), path, "duration_ns") / 1e9
            if duration < 0:
                raise ValueError(f"{path}: negative LLM duration")
            actions.append(ReplayAction(
                kind="llm", action_id=str(key[1]),
                sequence_no=int(record["sequence_no"]),
                start_s=_finite_float(before.get("wall_time_ns"), path, "wall_time_ns") / 1e9,
                duration_s=duration, name=str(record.get("name") or ""),
                input=messages, output=output,
                metadata={"trace_id": key[0]},
            ))
        else:
            raise ValueError(f"{path}: invalid LLM span record")
    if starts:
        raise ValueError(f"{path}: incomplete LLM spans")
    if not actions:
        raise ValueError(f"{path}: recording contains no LLM spans")
    if len({a.metadata["trace_id"] for a in actions}) != 1:
        raise ValueError(f"{path}: select one agent run for replay")
    return sorted(actions, key=lambda action: action.sequence_no)


def find_recordings(paths, runtime_id: str) -> list[Path]:
    """Select sidecar recordings belonging to this Runtime, without rewriting them."""
    selected = []
    for value in paths:
        path = Path(value)
        if path.suffix != ".jsonl":
            continue
        rows = _read_jsonl(path)
        if any(row.get("kind") == "llm" and row.get("runtime_id") == runtime_id for row in rows):
            load_trace(path)
            selected.append(path)
    return sorted(selected)


def _finite_float(value: Any, path: Path, field_name: str) -> float:
    import math

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid {field_name}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path}: non-finite {field_name}: {value!r}")
    return result
