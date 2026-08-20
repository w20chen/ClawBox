#!/usr/bin/env python3
"""Runtime-side KB flush: join span + bridge traces, sign, POST observations.

Self-contained (stdlib only) so it runs inside the runtime image without the
clawbox package.  Mirrors the canonical pipeline in ``clawbox/tuning/``:

* ``span_end_to_observation`` (schema.py) — extract a normalized observation
  from a ClawTune v6 ``span_end`` tool record;
* ``join_trace_and_bridge`` (join.py) — exact ``execution_id`` join with the
  tool-bridge execution JSONL (no time window);
* ``sign_observation`` (validate.py) — HMAC-SHA256 over the canonical payload.

The control-plane projector re-validates (schema / HMAC / quality / dedup)
before anything trains the KB, so a bug here can only drop observations, never
train a bad one.

Environment (or CLI flags):
  KB_ENDPOINT / KB_TOKEN / KB_INGEST_SECRET / KB_TENANT / KB_REPO
  KB_TRACE_DIR (span JSONL dir) / KB_BRIDGE (tool-bridge.jsonl path)
  KB_LOG (log path, default stderr)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = 6
OBSERVATION_SCHEMA_VERSION = 1

#: Keys signed by the runtime; MUST match clawbox/tuning/validate.py
#: canonical_payload() so the projector's HMAC check passes.
SIGNED_FIELDS = (
    "schema_version",
    "execution_id",
    "tool_name",
    "command_digest",
    "run_id",
    "sequence_no",
    "exit_code",
    "duration_sec",
    "cpu_time_sec",
    "rss_peak_bytes",
    "collection_quality",
    "complete",
)


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def span_end_to_obs(record: dict[str, Any]) -> dict[str, Any] | None:
    """Mirror of clawbox/tuning/schema.py:span_end_to_observation."""
    if record.get("record_type") != "span_end":
        return None
    if record.get("schema_version") != TRACE_SCHEMA_VERSION:
        return None
    if record.get("kind") != "tool":
        return None
    execution = record.get("execution") or {}
    execution_id = execution.get("execution_id")
    if not execution_id:
        return None
    resources = record.get("resources") or {}
    output = record.get("output") or {}
    status = record.get("status") or {}
    try:
        end_time = datetime.fromtimestamp(int(record["wall_time_ns"]) / 1e9, tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        end_time = None
    try:
        start_time = datetime.fromtimestamp(
            int(resources.get("monitor_start_wall_time_ns") or record["wall_time_ns"]) / 1e9,
            tz=timezone.utc,
        )
    except (KeyError, TypeError, ValueError):
        start_time = end_time
    duration_sec = as_float(record.get("duration_sec")) or as_float(resources.get("action_duration_ns"))
    if duration_sec is None:
        duration_sec = as_float(record.get("duration_ns"))
        if duration_sec is not None:
            duration_sec = duration_sec / 1e9
    coverage_ratio = as_float(resources.get("coverage_ratio"))
    status_code = status.get("code")
    exit_code = output.get("exit_code")
    exit_code = as_int(exit_code) if exit_code is not None else None
    if exit_code is None:
        exit_code = 0 if status_code == "ok" else 1
    if status_code == "ok" and coverage_ratio is not None and coverage_ratio >= 0.9:
        quality = "valid"
    elif status_code in ("ok", "error", "timeout") and coverage_ratio is not None and coverage_ratio >= 0.5:
        quality = "degraded"
    else:
        quality = "invalid"
    complete = status_code in ("ok", "error", "timeout", "cancelled")
    requested_command = execution.get("requested_command") or execution.get("payload_command")
    try:
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "execution_id": execution_id,
            "tool_name": record.get("name") or "exec",
            "command": requested_command,
            "command_digest": execution.get("command_digest"),
            "repo_fingerprint": record.get("repo"),
            "run_id": record.get("run_id"),
            "session_id": record.get("session_id"),
            "sequence_no": int(record.get("sequence_no") or 0),
            "start_time": _iso(start_time),
            "end_time": _iso(end_time),
            "duration_sec": duration_sec,
            "status_code": status_code,
            "exit_code": exit_code,
            "complete": complete,
            "collection_quality": quality,
            "coverage_ratio": coverage_ratio,
            "coverage_reason": resources.get("coverage_reason"),
            "cpu_time_sec": as_float(resources.get("cpu_time_s")),
            "cpu_utilization_avg_cores": as_float(resources.get("cpu_utilization_avg_cores")),
            "rss_peak_bytes": as_int(resources.get("rss_peak_bytes")),
            "memory_rss_bytes_after": as_int(resources.get("memory_rss_bytes_after")),
            "source": "clawtune_span",
        }
    except (TypeError, ValueError):
        return None


def canonical_payload(obs: dict[str, Any]) -> bytes:
    """Mirror of clawbox/tuning/validate.py:canonical_payload."""
    data = {key: obs.get(key) for key in SIGNED_FIELDS}
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_observation(obs: dict[str, Any], secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_payload(obs), hashlib.sha256).hexdigest()


def read_span_records(trace_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(trace_dir.glob("*.jsonl")):
        if path.name == "tool-bridge.jsonl":
            continue  # bridge records are handled separately
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def read_bridge_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def read_cgroup_artifacts(trace_dir: Path) -> dict[str, dict[str, Any]]:
    """Load Tool-VM ``cgroup-resource-<execution_id>.json`` artifacts.

    Mirrors ``clawbox.tuning.dataset.read_cgroup_artifacts``.  These are the
    real per-execution measurements written by the tool-bridge (process-tree
    and/or cgroup v2).  Scans ``<trace_dir>/tool-resource/*.json`` (ClawTune
    layout) plus any top-level ``cgroup-resource-*.json``.
    """
    artifacts: dict[str, dict[str, Any]] = {}
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
        if not isinstance(raw, dict):
            continue
        execution_id = raw.get("execution_id")
        if not execution_id:
            continue
        artifacts[execution_id] = raw
    return artifacts


def join_observations(
    trace_dir: Path, bridge_path: Path, *, repo: str
) -> list[dict[str, Any]]:
    """Exact execution_id join -> normalized observation dicts.

    When the tool-bridge produced a ``cgroup_resource_v1`` artifact for the
    execution, its real cpu/memory values REPLACE the span-side estimates
    before the observation is signed.  The projector only trains trusted
    observations, so a missing artifact simply keeps the (weaker) span values.
    """
    spans = [span_end_to_obs(r) for r in read_span_records(trace_dir)]
    spans = [s for s in spans if s is not None]
    by_id: dict[str, list[dict[str, Any]]] = {}
    for bridge in read_bridge_records(bridge_path):
        by_id.setdefault(bridge.get("execution_id"), []).append(bridge)
    artifacts = read_cgroup_artifacts(trace_dir)
    merged: list[dict[str, Any]] = []
    used: set[str] = set()
    for span in spans:
        bridge = (by_id.get(span["execution_id"]) or [None])[0]
        if bridge is None:
            continue  # unmatched span -> not joinable -> dropped (never trusted)
        out = dict(span)
        out["exit_code"] = bridge.get("exit_code", span.get("exit_code"))
        if out.get("duration_sec") is None:
            ms = bridge.get("duration_ms")
            if ms is not None:
                out["duration_sec"] = float(ms) / 1000.0
        if out.get("stdout_bytes") is None:
            out["stdout_bytes"] = bridge.get("stdout_bytes")
        if out.get("stderr_bytes") is None:
            out["stderr_bytes"] = bridge.get("stderr_bytes")
        if bridge.get("output_truncated"):
            out["output_truncated"] = True
        out["complete"] = True
        if bridge.get("timed_out"):
            out["status_code"] = "timeout"
        if out.get("exit_code") != 0 and out.get("status_code") == "ok":
            out["status_code"] = "error"
        # Real per-execution measurement from the Tool-VM collector, when
        # present, supersedes the ClawTune span-side proxy values.
        artifact = artifacts.get(span["execution_id"])
        if artifact is not None:
            cpu_time_s = as_float(artifact.get("cpu_time_s"))
            rss_peak = as_int(artifact.get("memory_rss_peak_bytes"))
            source = artifact.get("source")
            if cpu_time_s is not None:
                out["cpu_time_sec"] = cpu_time_s
            if rss_peak is not None:
                out["rss_peak_bytes"] = rss_peak
            sampling_quality = artifact.get("sampling_quality")
            if sampling_quality in ("valid", "degraded", "invalid"):
                out["collection_quality"] = sampling_quality
            if source in ("cgroup-v2", "process-tree"):
                out["source"] = source
                out["resource_source"] = source
        out["trusted"] = (
            out.get("collection_quality") == "valid"
            and out.get("complete") is True
            and out.get("exit_code") == 0
        )
        if out.get("repo_fingerprint") is None:
            out["repo_fingerprint"] = repo
        merged.append(out)
        used.add(span["execution_id"])
    return merged


def post_batch(
    endpoint: str,
    token: str,
    tenant: str,
    repo: str,
    observations: list[dict[str, Any]],
    secret: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    signed = [
        {"observation": obs, "signature": sign_observation(obs, secret)}
        for obs in observations
    ]
    body = {
        "tenant_id": tenant,
        "repo_fingerprint": repo,
        "observations": signed,
    }
    request = urllib.request.Request(
        f"{endpoint.rstrip('/')}/v1/kb/observations",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.getenv("KB_ENDPOINT", os.getenv("CLAWBOX_KB_ENDPOINT", "")))
    parser.add_argument("--token", default=os.getenv("KB_TOKEN", os.getenv("CLAWBOX_KB_TOKEN", "")))
    parser.add_argument("--secret", default=os.getenv("KB_INGEST_SECRET", os.getenv("CLAWBOX_KB_INGEST_SECRET", "")))
    parser.add_argument("--tenant", default=os.getenv("KB_TENANT", os.getenv("TENANT_ID", "")))
    parser.add_argument("--repo", default=os.getenv("KB_REPO", os.getenv("CLAWBOX_REPO_KEY", os.getenv("CLAWTUNE_REPO_KEY", ""))))
    parser.add_argument("--trace-dir", default=os.getenv("KB_TRACE_DIR", os.getenv("CLAWTUNE_TRACE_DIR", "")))
    parser.add_argument("--bridge", default=os.getenv("KB_BRIDGE", ""))
    parser.add_argument("--log", default=os.getenv("KB_LOG", ""))
    args = parser.parse_args()

    if not args.endpoint or not args.token or not args.secret:
        print("kb-flush: missing endpoint/token/secret; skipping", file=sys.stderr)
        return 0
    if not args.tenant or not args.repo or not args.trace_dir:
        print("kb-flush: missing tenant/repo/trace-dir; skipping", file=sys.stderr)
        return 0

    log_path = Path(args.log) if args.log else None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(message: str) -> None:
        line = f"kb-flush: {message}"
        if log_path:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        print(line, file=sys.stderr)

    observations = join_observations(Path(args.trace_dir), Path(args.bridge), repo=args.repo)
    log(f"joined {len(observations)} observations (trace={args.trace_dir}, bridge={args.bridge})")
    if not observations:
        return 0
    try:
        result = post_batch(
            args.endpoint, args.token, args.tenant, args.repo, observations, args.secret
        )
        log(f"POST result={json.dumps(result)}")
    except Exception as exc:  # fail-open: never block finalization on KB flush
        log(f"POST failed (non-fatal): {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
