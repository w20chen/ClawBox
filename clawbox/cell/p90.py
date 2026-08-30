"""Safe, auditable p90 admission decisions for two-VM Cells."""
from __future__ import annotations

import json
import math
import os
import argparse
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from pathlib import Path

from .capacity import CellSize, ClawTunePredictionSizer


FIXED_BASELINE = "fixed-resident"
P90_STATIC_BASELINE = "p90-static"
P90_ELASTIC_BASELINE = "p90-elastic"
SUPPORTED_CELL_BASELINES = {
    FIXED_BASELINE, P90_STATIC_BASELINE, P90_ELASTIC_BASELINE,
}


class PredictionUnavailable(RuntimeError):
    """The requested immutable prediction does not currently exist."""


@dataclass(frozen=True)
class AdmissionPrediction:
    tenant_id: str
    repo_fingerprint: str
    generation: int
    pair_digest: str
    source_digest: str
    artifact_count: int
    clawtune_revision: str
    latency_p90_sec: float
    cpu_p90_cores: float
    memory_p90_bytes: float
    evidence_count: int
    scopes: dict[str, str | None]
    fallback_paths: dict[str, list[str]]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AdmissionPrediction":
        prediction = payload.get("prediction")
        if not isinstance(prediction, dict):
            raise ValueError("admission prediction response is missing prediction")
        value = cls(
            tenant_id=str(payload["tenant_id"]),
            repo_fingerprint=str(payload["repo_fingerprint"]),
            generation=int(payload["generation"]),
            pair_digest=str(payload["pair_digest"]),
            source_digest=str(payload["source_digest"]),
            artifact_count=int(payload["artifact_count"]),
            clawtune_revision=str(payload["clawtune_revision"]),
            latency_p90_sec=float(prediction["latency_p90_sec"]),
            cpu_p90_cores=float(prediction["cpu_p90_cores"]),
            memory_p90_bytes=float(prediction["memory_p90_bytes"]),
            evidence_count=int(prediction["evidence_count"]),
            scopes=dict(prediction.get("scopes") or {}),
            fallback_paths={
                str(key): [str(item) for item in items]
                for key, items in (prediction.get("fallback_paths") or {}).items()
            },
        )
        if value.generation < 1 or value.artifact_count < 1 or value.evidence_count < 1:
            raise ValueError("admission prediction has no usable evidence")
        for name, number in (
            ("latency p90", value.latency_p90_sec),
            ("CPU p90", value.cpu_p90_cores),
            ("memory p90", value.memory_p90_bytes),
        ):
            if not math.isfinite(number) or number <= 0:
                raise ValueError(f"admission prediction {name} must be finite and positive")
        for name, digest in (("pair", value.pair_digest), ("source", value.source_digest)):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError(f"admission prediction {name} digest is invalid")
        return value

    def as_status(self) -> dict[str, Any]:
        return {
            "tenantID": self.tenant_id,
            "repoFingerprint": self.repo_fingerprint,
            "generation": self.generation,
            "pairDigest": self.pair_digest,
            "sourceDigest": self.source_digest,
            "artifactCount": self.artifact_count,
            "clawtuneRevision": self.clawtune_revision,
            "latencyP90Seconds": self.latency_p90_sec,
            "cpuP90Cores": self.cpu_p90_cores,
            "memoryP90Bytes": self.memory_p90_bytes,
            "evidenceCount": self.evidence_count,
            "scopes": self.scopes,
            "fallbackPaths": self.fallback_paths,
        }

    def as_payload(self) -> dict[str, Any]:
        """Return the immutable, replay-study-compatible API representation."""
        return {
            "tenant_id": self.tenant_id,
            "repo_fingerprint": self.repo_fingerprint,
            "generation": self.generation,
            "pair_digest": self.pair_digest,
            "source_digest": self.source_digest,
            "artifact_count": self.artifact_count,
            "clawtune_revision": self.clawtune_revision,
            "prediction": {
                "latency_p90_sec": self.latency_p90_sec,
                "cpu_p90_cores": self.cpu_p90_cores,
                "memory_p90_bytes": self.memory_p90_bytes,
                "evidence_count": self.evidence_count,
                "scopes": self.scopes,
                "fallback_paths": self.fallback_paths,
            },
        }


class AdmissionPredictionProvider(Protocol):
    def get(
        self, *, tenant_id: str, repo_fingerprint: str, generation: int | None,
    ) -> AdmissionPrediction: ...


class HTTPAdmissionPredictionClient:
    def __init__(self, endpoint: str, token: str, *, timeout_seconds: float = 10.0) -> None:
        if not endpoint:
            raise ValueError("ClawTune KB endpoint is required for p90 admission")
        if not token:
            raise ValueError("ClawTune KB token is required for p90 admission")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def get(
        self, *, tenant_id: str, repo_fingerprint: str, generation: int | None,
    ) -> AdmissionPrediction:
        query: dict[str, str | int] = {
            "tenant_id": tenant_id,
            "repo": repo_fingerprint,
        }
        if generation is not None:
            query["generation"] = generation
        request = urllib.request.Request(
            f"{self.endpoint}/v1/kb/admission-prediction?{urllib.parse.urlencode(query)}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                raise PredictionUnavailable(detail or "ClawTune snapshot not found") from exc
            raise PredictionUnavailable(
                f"ClawTune prediction API returned HTTP {exc.code}: {detail}"
            ) from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise PredictionUnavailable(
                f"ClawTune prediction API is unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        prediction = AdmissionPrediction.from_payload(payload)
        if prediction.tenant_id != tenant_id or prediction.repo_fingerprint != repo_fingerprint:
            raise ValueError("ClawTune prediction crossed its tenant/repository identity boundary")
        if generation is not None and prediction.generation != generation:
            raise ValueError("ClawTune prediction did not return the requested immutable generation")
        return prediction


@dataclass(frozen=True)
class SizingDecision:
    baseline: str
    prediction: AdmissionPrediction
    cell_size: CellSize

    def as_status(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline,
            "prediction": self.prediction.as_status(),
            "cellSize": self.cell_size.as_status(),
        }


def resolve_p90_decision(
    task: dict[str, Any],
    *,
    provider: AdmissionPredictionProvider,
    sizer: ClawTunePredictionSizer,
    min_evidence: int,
) -> SizingDecision:
    spec = task.get("spec") or {}
    baseline = str(spec.get("baseline", FIXED_BASELINE))
    if baseline not in {P90_STATIC_BASELINE, P90_ELASTIC_BASELINE}:
        raise ValueError(f"baseline {baseline!r} is not a p90 Cell baseline")
    labels = task.get("metadata", {}).get("labels", {}) or {}
    annotations = task.get("metadata", {}).get("annotations", {}) or {}
    run_ref = spec.get("runRef") or {}
    tenant_id = str(run_ref.get("tenantID") or labels.get("clawbox.openai.com/tenant") or "").strip()
    repo = str(
        spec.get("repoKey") or annotations.get("clawbox.openai.com/repository") or ""
    ).strip()
    if not tenant_id:
        raise ValueError("p90 admission requires a stable tenant identity")
    if not repo:
        raise ValueError("p90 admission requires a stable repository identity")
    raw_generation = spec.get("kbGeneration")
    if baseline == P90_STATIC_BASELINE:
        if raw_generation is None:
            raise ValueError("p90-static requires spec.kbGeneration")
        generation = int(raw_generation)
        if generation < 1:
            raise ValueError("spec.kbGeneration must be positive")
    else:
        if raw_generation is not None:
            raise ValueError("p90-elastic must resolve the latest generation at admission")
        generation = None
    prediction = provider.get(
        tenant_id=tenant_id, repo_fingerprint=repo, generation=generation,
    )
    if prediction.evidence_count < min_evidence:
        raise PredictionUnavailable(
            f"ClawTune prediction has {prediction.evidence_count} samples; "
            f"at least {min_evidence} are required"
        )
    cell_size = sizer.size(
        str(spec.get("profile", "small")),
        cpu_p90_cores=prediction.cpu_p90_cores,
        memory_p90_bytes=prediction.memory_p90_bytes,
    )
    return SizingDecision(baseline=baseline, prediction=prediction, cell_size=cell_size)


def export_main() -> None:
    parser = argparse.ArgumentParser(
        description="Export an immutable ClawTune admission prediction for replay studies"
    )
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-env", default="CLAWBOX_KB_TOKEN")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--generation", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.getenv(args.token_env, "")
    if not token:
        parser.error(f"{args.token_env} is empty")
    prediction = HTTPAdmissionPredictionClient(args.endpoint, token).get(
        tenant_id=args.tenant,
        repo_fingerprint=args.repository,
        generation=args.generation,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".next")
    temporary.write_text(
        json.dumps(prediction.as_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(args.output)
