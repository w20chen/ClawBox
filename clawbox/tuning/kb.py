"""Immutable KB snapshot + generation from trusted observations (ADR-008 §4).

A KB snapshot is a self-contained, provenance-tracked artifact:

* ``generation`` — monotonically increasing per builder.
* ``builder_version`` — code version that produced the snapshot.
* ``input_range`` — (min, max) created_at over the observations folded in, plus
  a content digest so rollback/rebuild is reproducible.
* ``quality`` — summary of the trusted observations (count, buckets, coverage).

``KnowledgeBase.predict`` answers the same query ClawTune's RuntimeToolResourceKB
answers (per-repo/per-tool/per-command latency/cpu/memory) but is standalone and
offline-evaluable.  ``rollback`` returns the previous generation snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .estimators import CpuEstimator, LatencyBucketClassifier, LatencyEstimator, MemoryEstimator
from .schema import ToolObservation, utcnow


def _content_digest(observations: list[ToolObservation]) -> str:
    """Deterministic digest over sorted canonical observation rows."""
    rows = [
        json.dumps(obs.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        for obs in observations
    ]
    joined = "\n".join(sorted(rows))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SnapshotMetadata:
    generation: int
    builder_version: str
    input_digest: str
    input_count: int
    created_at: datetime
    quality: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "builder_version": self.builder_version,
            "input_digest": self.input_digest,
            "input_count": self.input_count,
            "created_at": self.created_at.isoformat(),
            "quality": self.quality,
        }


@dataclass(frozen=True)
class Prediction:
    latency_p50_sec: float
    latency_p90_sec: float
    time_bucket: str | None
    cpu_p90_cores: float
    memory_p90_bytes: float
    sample_count: int
    match_level: str  # "per-command" | "global-default"
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "latency_p50_sec": self.latency_p50_sec,
            "latency_p90_sec": self.latency_p90_sec,
            "time_bucket": self.time_bucket,
            "cpu_p90_cores": self.cpu_p90_cores,
            "memory_p90_bytes": self.memory_p90_bytes,
            "sample_count": self.sample_count,
            "match_level": self.match_level,
            "confidence": self.confidence,
        }


BUILDER_VERSION = "clawbox-tune-kb-v1"


@dataclass
class KnowledgeBase:
    """Immutable snapshot with prediction logic (read-only after build)."""

    observations: tuple[ToolObservation, ...]
    metadata: SnapshotMetadata
    _latency: LatencyEstimator = field(init=False, repr=False)
    _buckets: LatencyBucketClassifier = field(init=False, repr=False)
    _memory: MemoryEstimator = field(init=False, repr=False)
    _cpu: CpuEstimator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        obs = list(self.observations)
        self._latency = LatencyEstimator(obs)
        self._buckets = LatencyBucketClassifier(obs)
        self._memory = MemoryEstimator(obs)
        self._cpu = CpuEstimator(obs)

    def predict(self, observation: ToolObservation) -> Prediction:
        latency = self._latency.predict(observation)
        memory_p90, sample_count, match = self._memory.predict(observation)
        cpu_p90, _, _ = self._cpu.predict(observation)
        bucket = self._buckets.predict(observation) or latency.bucket
        confidence = min(1.0, latency.sample_count / 10.0)
        return Prediction(
            latency_p50_sec=latency.p50_sec,
            latency_p90_sec=latency.p90_sec,
            time_bucket=bucket,
            cpu_p90_cores=cpu_p90,
            memory_p90_bytes=memory_p90,
            sample_count=latency.sample_count,
            match_level="per-command" if latency.source == "per-command" else "global-default",
            confidence=confidence,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "metadata": self.metadata.to_dict(),
            "observations": [obs.model_dump(mode="json") for obs in self.observations],
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "KnowledgeBase":
        observations = tuple(ToolObservation.model_validate(item) for item in data["observations"])
        raw_meta = data["metadata"]
        metadata = SnapshotMetadata(
            generation=int(raw_meta["generation"]),
            builder_version=str(raw_meta["builder_version"]),
            input_digest=str(raw_meta["input_digest"]),
            input_count=int(raw_meta["input_count"]),
            created_at=datetime.fromisoformat(raw_meta["created_at"]),
            quality=raw_meta["quality"],
        )
        return cls(observations=observations, metadata=metadata)

    def to_json(self) -> str:
        return json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))


class KnowledgeBaseBuilder:
    """Append-only builder producing immutable snapshots (generation++).

    Trusted observations are accumulated via ``add`` / ``add_many``; each
    ``build`` freezes the current accumulation into a new immutable snapshot
    with the next generation number.  ``rollback`` drops the newest snapshot
    (the accumulated observations remain, so rebuilding is reproducible).
    """

    def __init__(self, builder_version: str = BUILDER_VERSION) -> None:
        self.builder_version = builder_version
        self._trusted: list[ToolObservation] = []
        self._history: list[KnowledgeBase] = []

    @property
    def current(self) -> KnowledgeBase | None:
        return self._history[-1] if self._history else None

    @property
    def pending_count(self) -> int:
        return len(self._trusted)

    def add(self, observation: ToolObservation) -> None:
        if observation.trusted:
            self._trusted.append(observation)

    def add_many(self, observations: list[ToolObservation]) -> None:
        for observation in observations:
            self.add(observation)

    def build(self) -> KnowledgeBase:
        trusted = list(self._trusted)
        generation = 1 if not self._history else self._history[-1].metadata.generation + 1
        created_at = utcnow()
        if trusted:
            min_created = min(obs.created_at for obs in trusted)
            max_created = max(obs.created_at for obs in trusted)
        else:
            min_created = max_created = created_at
        metadata = SnapshotMetadata(
            generation=generation,
            builder_version=self.builder_version,
            input_digest=_content_digest(trusted),
            input_count=len(trusted),
            created_at=created_at,
            quality={
                "observations": len(trusted),
                "valid": sum(1 for obs in trusted if obs.collection_quality.value == "valid"),
                "commands": len({obs.command_digest or obs.command for obs in trusted}),
                "input_range": [min_created.isoformat(), max_created.isoformat()],
            },
        )
        kb = KnowledgeBase(observations=tuple(trusted), metadata=metadata)
        self._history.append(kb)
        return kb

    def rollback(self) -> KnowledgeBase | None:
        """Drop the latest snapshot and return the previous one (if any)."""
        if self._history:
            self._history.pop()
        return self.current
