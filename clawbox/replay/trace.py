from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


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

    def shell_command(self) -> str:
        if self.kind != "tool":
            raise ValueError(f"action {self.action_id} is not a tool action")
        args = self.input
        if isinstance(args, str):
            try:
                decoded = json.loads(args)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                args = decoded
        if isinstance(args, dict):
            for key in ("command", "cmd", "script"):
                value = args.get(key)
                if isinstance(value, str) and value:
                    return value
            tool_name = self.name.rsplit(".", 1)[-1].rsplit("/", 1)[-1].lower()
            path = args.get("path", args.get("file_path"))
            if tool_name in {"read", "read_file"} and isinstance(path, str) and path:
                offset = max(1, _coerce_positive_int(args.get("offset"), 1))
                limit = max(1, _coerce_positive_int(args.get("limit"), 2000))
                script = (
                    "from pathlib import Path; import sys; "
                    "p=Path(sys.argv[1]); a=int(sys.argv[2]); n=int(sys.argv[3]); "
                    "lines=p.read_text(errors='replace').splitlines(True); "
                    "sys.stdout.write(''.join(lines[a-1:a-1+n]))"
                )
                return _python_command(script, path, str(offset), str(limit))
            if tool_name in {"write", "write_file"} and isinstance(path, str) and path:
                content = args.get("content")
                if not isinstance(content, str):
                    raise ValueError(f"write action {self.action_id} has no string content")
                script = (
                    "from pathlib import Path; import sys; "
                    "p=Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True); "
                    "p.write_text(sys.argv[2])"
                )
                return _python_command(script, path, content)
            if tool_name in {"edit", "edit_file"} and isinstance(path, str) and path:
                edit_items = args.get("edits")
                if edit_items is None:
                    edit_items = [{
                        "oldText": args.get("oldText", args.get("old_string")),
                        "newText": args.get("newText", args.get("new_string")),
                    }]
                if not isinstance(edit_items, list) or not edit_items:
                    raise ValueError(f"edit action {self.action_id} has no edits")
                replacements: list[tuple[str, str]] = []
                for item in edit_items:
                    if not isinstance(item, dict):
                        raise ValueError(f"edit action {self.action_id} has a non-object edit")
                    old = item.get("oldText", item.get("old_string"))
                    new = item.get("newText", item.get("new_string"))
                    if not isinstance(old, str) or not isinstance(new, str):
                        raise ValueError(f"edit action {self.action_id} has invalid old/new text")
                    replacements.append((old, new))
                script = (
                    "from pathlib import Path; import json,sys; p=Path(sys.argv[1]); "
                    "s=p.read_text(); edits=json.loads(sys.argv[2]); "
                    "exec(compile('for old,new in edits:\\n c=s.count(old)\\n "
                    "if c != 1: raise ValueError(f\\\"expected one match, got {c}\\\")\\n "
                    "s=s.replace(old,new,1)', '<replay-edit>', 'exec')); p.write_text(s)"
                )
                return _python_command(script, path, json.dumps(replacements))
        if isinstance(args, str) and args:
            return args
        raise ValueError(
            f"tool action {self.action_id} ({self.name}) has no replayable shell command"
        )


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
    """Load agent-test-bench v4 actions or ClawTune trace-v6 spans."""
    records = _read_jsonl(path)
    if any(record.get("record_type") in {"span_start", "span_end"} for record in records):
        actions = _load_v6(records, path)
    elif any(record.get("type") == "action" for record in records):
        actions = _load_v4(records, path)
    else:
        raise ValueError(f"{path}: no replayable v4 actions or v6 spans")
    if not actions:
        raise ValueError(f"{path}: trace contains no LLM or tool actions")
    return actions


def _load_v4(records: Iterable[dict[str, Any]], path: Path) -> list[ReplayAction]:
    actions: list[ReplayAction] = []
    for ordinal, record in enumerate(records):
        if record.get("type") != "action":
            continue
        action_type = record.get("action_type")
        if action_type not in {"llm_call", "tool_exec"}:
            continue
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        start = _finite_float(record.get("ts_start"), path, "ts_start")
        end = _finite_float(record.get("ts_end"), path, "ts_end")
        duration = max(0.0, end - start)
        if action_type == "llm_call":
            latency_ms = data.get("llm_latency_ms", data.get("duration_ms"))
            if latency_ms is not None:
                duration = max(0.0, _finite_float(latency_ms, path, "llm_latency_ms") / 1000.0)
            input_value = data.get("messages_in", data.get("raw_request"))
            output_value = data.get("raw_response", data.get("content"))
            name = str(data.get("model") or "unknown-model")
            kind = "llm"
            expected_exit_code = None
        else:
            input_value = data.get("args", data.get("tool_args", data.get("arguments")))
            output_value = data.get("result", data.get("output"))
            name = str(data.get("tool_name") or data.get("name") or "unknown-tool")
            kind = "tool"
            expected_exit_code = _optional_int(data.get("exit_code"))
        actions.append(ReplayAction(
            kind=kind,
            action_id=str(record.get("action_id") or f"v4-{ordinal}"),
            sequence_no=int(record.get("iteration", ordinal)),
            start_s=start,
            duration_s=duration,
            name=name,
            input=input_value,
            output=output_value,
            expected_exit_code=expected_exit_code,
            metadata={"trace_format": 4, "source_record": record},
        ))
    return sorted(actions, key=lambda action: (action.start_s, action.sequence_no, action.action_id))


def _load_v6(records: Iterable[dict[str, Any]], path: Path) -> list[ReplayAction]:
    starts: dict[tuple[str, str], dict[str, Any]] = {}
    actions: list[ReplayAction] = []
    for ordinal, record in enumerate(records):
        record_type = record.get("record_type")
        kind = record.get("kind")
        if record_type not in {"span_start", "span_end"} or kind not in {"llm", "tool"}:
            continue
        span_id = str(record.get("span_id") or "")
        trace_id = str(record.get("trace_id") or "")
        if not span_id:
            raise ValueError(f"{path}: trace-v6 {record_type} is missing span_id")
        key = (trace_id, span_id)
        if record_type == "span_start":
            if key in starts:
                raise ValueError(f"{path}: duplicate span_start for {key!r}")
            starts[key] = record
            continue
        start_record = starts.pop(key, None)
        if start_record is None:
            raise ValueError(f"{path}: span_end without span_start for {key!r}")
        if start_record.get("kind") != kind:
            raise ValueError(f"{path}: span kind changed for {key!r}")
        start_ns = _finite_float(start_record.get("wall_time_ns"), path, "wall_time_ns")
        duration_ns = _finite_float(record.get("duration_ns"), path, "duration_ns")
        input_block = start_record.get("input") if isinstance(start_record.get("input"), dict) else {}
        output_block = record.get("output") if isinstance(record.get("output"), dict) else {}
        if kind == "llm":
            input_value = input_block.get("messages")
            output_value = output_block.get("content")
            expected_exit_code = None
        else:
            input_value = input_block.get("requested_args")
            output_value = output_block.get("result")
            expected_exit_code = _optional_int(output_block.get("exit_code"))
        actions.append(ReplayAction(
            kind=str(kind),
            action_id=span_id,
            sequence_no=int(record.get("sequence_no", ordinal)),
            start_s=start_ns / 1_000_000_000.0,
            duration_s=max(0.0, duration_ns / 1_000_000_000.0),
            name=str(record.get("name") or start_record.get("name") or "unknown"),
            input=input_value,
            output=output_value,
            expected_exit_code=expected_exit_code,
            metadata={"trace_format": 6, "trace_id": trace_id},
        ))
    if starts:
        missing = ", ".join(f"{trace}/{span}" for trace, span in sorted(starts)[:5])
        raise ValueError(f"{path}: incomplete trace-v6 spans: {missing}")
    return sorted(actions, key=lambda action: (action.start_s, action.sequence_no, action.action_id))


def _finite_float(value: Any, path: Path, field_name: str) -> float:
    import math

    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: invalid {field_name}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path}: non-finite {field_name}: {value!r}")
    return result


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _python_command(script: str, *arguments: str) -> str:
    return " ".join(["python3", "-c", shlex.quote(script), *(shlex.quote(item) for item in arguments)])
