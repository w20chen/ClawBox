"""Immutable command-specific prediction provenance for managed Tool calls."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from threading import Lock
from typing import Any

from clawbox.tuning.clawtune import shell_command_prefix_tokens


class PredictionUnavailable(RuntimeError):
    """Raised when a managed Tool call has no command-specific prediction."""


def command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode()).hexdigest()


class CommandPredictionProvider:
    """Load one immutable P90 artifact once and resolve exact commands."""

    def __init__(self, path: Path, *, repository: str | None = None) -> None:
        self.path = path
        self.repository = repository
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("P90 prediction artifact must be an object")
        self.payload = payload
        self._by_digest: dict[str, dict[str, Any]] = {}
        self._load_entries(payload)
        self._fallback_counts: dict[str, int] = {}
        self._lock = Lock()

    def _load_entries(self, payload: dict[str, Any]) -> None:
        candidates: list[dict[str, Any]] = []
        per_tool = payload.get("per_tool_memory")
        if isinstance(per_tool, dict):
            workloads = per_tool.get("workloads") or {}
            if isinstance(workloads, dict):
                for workload in workloads.values():
                    if isinstance(workload, dict):
                        candidates.extend(
                            item for item in workload.get("tool_invocations", [])
                            if isinstance(item, dict)
                        )
        for key in ("predictions", "tool_invocations", "commands"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                for command, item in value.items():
                    if isinstance(item, dict):
                        candidates.append({"command": command, **item})
        for item in candidates:
            command = item.get("command")
            if not isinstance(command, str) or not command.strip():
                continue
            digest = str(item.get("command_sha256") or command_sha256(command))
            metadata = self._metadata(item, command, digest)
            previous = self._by_digest.get(digest)
            if previous is not None and previous != metadata:
                raise ValueError(f"P90 artifact has conflicting entries for command {digest}")
            self._by_digest[digest] = metadata

    @staticmethod
    def _metadata(item: dict[str, Any], command: str, digest: str) -> dict[str, Any]:
        value = item.get("predicted_command_memory_p90_mib")
        if value is None:
            value = item.get("incremental_p90_mib")
        if value is None and item.get("incremental_p90_kib") is not None:
            value = float(item["incremental_p90_kib"]) / 1024.0
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"P90 entry for {digest} has no positive command prediction")
        normalized = " ".join(shell_command_prefix_tokens(command))
        return {
            "raw_command_sha256": digest,
            "canonical_prediction_key": normalized,
            "prediction_source": "runtime_clawtune_immutable_kb",
            "fallback_level": item.get("key_kind") or item.get("scope") or "unknown",
            "fallback_path": list(item.get("fallback_path") or []),
            "predicted_incremental_memory_mib": float(value),
            "evidence_count": int(item.get("evidence_count") or 0),
            "kb_generation": item.get("kb_generation") or item.get("generation"),
        }

    @property
    def manifest(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._by_digest.items()}

    def resolve(self, command: str, runtime_metadata: dict[str, Any] | None) -> dict[str, Any]:
        digest = command_sha256(command)
        expected = self._by_digest.get(digest)
        if expected is None:
            with self._lock:
                self._fallback_counts["missing_command"] = self._fallback_counts.get("missing_command", 0) + 1
            raise PredictionUnavailable(
                f"no command-specific Runtime prediction for command sha256 {digest}"
            )
        if not isinstance(runtime_metadata, dict):
            with self._lock:
                self._fallback_counts["runtime_metadata_missing"] = self._fallback_counts.get("runtime_metadata_missing", 0) + 1
            raise PredictionUnavailable("Runtime did not provide command prediction metadata")
        if runtime_metadata.get("raw_command_sha256") != digest:
            raise PredictionUnavailable("Runtime prediction command identity mismatch")
        if runtime_metadata.get("canonical_prediction_key") != expected["canonical_prediction_key"]:
            raise PredictionUnavailable("Runtime prediction canonical key mismatch")
        if runtime_metadata.get("prediction_source") != expected["prediction_source"]:
            raise PredictionUnavailable("Runtime prediction source mismatch")
        return {
            **expected,
            "session_runtime_metadata": dict(runtime_metadata),
        }

    def provenance(self, observed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        observed = observed or []
        levels: dict[str, int] = {}
        for item in observed:
            level = str(item.get("fallback_level") or "unknown")
            levels[level] = levels.get(level, 0) + 1
        with self._lock:
            fallback_counts = dict(self._fallback_counts)
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "entry_count": len(self._by_digest),
            "fallback_counts": fallback_counts,
            "observed_prediction_count": len(observed),
            "observed_fallback_levels": levels,
            "observed_fallback_rate": (
                sum(value for key, value in levels.items() if key != "exact_command")
                / len(observed) if observed else None
            ),
        }
