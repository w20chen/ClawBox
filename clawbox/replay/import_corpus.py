from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .import_trace import import_trace
from .trace import load_trace


def _records(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _source_metadata(path: Path) -> dict[str, Any]:
    rows = _records(path)
    metadata = [row for row in rows if row.get("type") == "trace_metadata"]
    if len(metadata) != 1:
        raise ValueError(f"{path}: expected exactly one trace_metadata record")
    return metadata[0]


def _repository(instance_id: str) -> str | None:
    task = re.sub(r"-\d+$", "", instance_id)
    if "__" not in task:
        return None
    owner, repository = task.split("__", 1)
    return f"{owner}/{repository}" if owner and repository else None


def _prompt(actions) -> str:
    for message in actions[0].input or ():
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
    raise ValueError("first recorded model request contains no nonempty user prompt")


def _prioritized_sources(
    paths: Iterable[Path], preferred_projects: tuple[str, ...],
) -> list[Path]:
    """Interleave preferred projects, then append all remaining traces."""
    buckets = [[] for _ in preferred_projects]
    remaining = []
    for path in sorted(paths, key=lambda item: item.name.casefold()):
        name = path.name.casefold()
        bucket = next((index for index, project in enumerate(preferred_projects)
                       if project.casefold() in name), None)
        (remaining if bucket is None else buckets[bucket]).append(path)
    selected = []
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        selected.extend(bucket[index] for bucket in buckets if index < len(bucket))
    return selected + remaining


def prepare_corpus(
    source_root: Path,
    output_root: Path,
    *,
    count: int | None = None,
    preferred_projects: Iterable[str] = ("pennylane", "sqlglot"),
    python: str = "python3",
    source_workspace: str = "/testbed",
    target_workspace: str = "/workspace",
) -> dict[str, Any]:
    """Import a deterministic, unique SWE-rebench corpus and write its manifest."""
    source_root = source_root.resolve()
    output_root = output_root.resolve()
    preferred = tuple(dict.fromkeys(
        value.strip() for value in preferred_projects if value.strip()
    ))
    sources = _prioritized_sources(source_root.glob("*.trace.jsonl"), preferred)
    if not sources:
        raise ValueError(f"no *.trace.jsonl files found in {source_root}")
    if count is not None:
        if count < 1:
            raise ValueError("corpus count must be positive")
        if count > len(sources):
            raise ValueError(
                f"requested {count} unique traces but source contains {len(sources)}"
            )
    output_root.mkdir(parents=True, exist_ok=True)
    trace_root = output_root / "traces"
    trace_root.mkdir(parents=True, exist_ok=True)
    cases = []
    skipped = []
    for source in sources:
        if count is not None and len(cases) >= count:
            break
        output = trace_root / source.name.replace(".trace.jsonl", ".replay.jsonl")
        report_path = output.with_suffix(".import.json")
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        if output.exists() and report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("source_sha256") != source_sha256:
                raise ValueError(
                    f"existing import does not match source trace: {output}"
                )
        else:
            if output.exists() or report_path.exists():
                raise ValueError(
                    f"incomplete prior import for {output}; remove that pair and retry"
                )
            try:
                report = import_trace(
                    source, output, python=python,
                    source_workspace=source_workspace,
                    target_workspace=target_workspace,
                )
            except ValueError as exc:
                skipped.append({
                    "source_reference": str(source),
                    "source_sha256": source_sha256,
                    "reason": str(exc),
                })
                continue
        actions = load_trace(output)
        instance_id = str(report.get("instance_id") or "").strip()
        if not instance_id:
            instance_id = str(_source_metadata(source).get("instance_id") or "").strip()
        if not instance_id:
            raise ValueError(f"{source}: trace metadata has no instance_id")
        recorded_duration = (
            max(action.start_s + action.duration_s for action in actions)
            - min(action.start_s for action in actions)
        )
        cases.append({
            "case_id": instance_id,
            "prompt": _prompt(actions),
            "source_reference": str(source),
            "replay_trace_reference": output.relative_to(output_root).as_posix(),
            "repository": _repository(instance_id),
            "metadata": {
                "corpus_source_root": str(source_root),
                "source_sha256": source_sha256,
                "recorded_duration_seconds": recorded_duration,
            },
        })
    if count is not None and len(cases) < count:
        raise ValueError(
            f"requested {count} compatible unique traces but only {len(cases)} "
            f"of {len(sources)} source traces could be imported; "
            f"{len(skipped)} incompatible traces were skipped"
        )
    manifest = {
        "schema_version": 1,
        "source": "swe_rebench",
        "source_root": str(source_root),
        "preferred_projects": list(preferred),
        "selection": "preferred-project-round-robin-then-lexical",
        "unique": True,
        "tasks": cases,
        "skipped": skipped,
    }
    manifest_path = output_root / "workload.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8", newline="\n",
    )
    temporary.replace(manifest_path)
    return {
        "source_root": str(source_root),
        "manifest": str(manifest_path),
        "trace_count": len(cases),
        "skipped_count": len(skipped),
        "preferred_projects": list(preferred),
    }
