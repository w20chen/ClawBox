"""Observation quality gate / signature / dedup (ADR-008).

An observation is an immutable, signable artifact.  Only observations that pass
every check may enter the trusted (active-KB-training) set; everything else goes
to a diagnostic store and never trains.

Checks, in order:

1. **Schema**: normalized schema version must match and be known.
2. **Identity**: execution_id present and well-formed; tool_name present.
3. **Signature**: when an ``ingest_secret`` is configured, the observation must
   carry a valid HMAC-SHA256 over its canonical payload.  This stops forged or
   cross-tenant-injected observations.
4. **Quality gate**: ``complete``, valid collection quality, duration present,
   coverage not degraded (for span-sourced observations).
5. **Dedup**: ``(execution_id, tool_name, sequence_no)`` must be unique within a
   batch; a repeat of an already-seen key is dropped (idempotent), not trained.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from .schema import CollectionQuality, OBSERVATION_SCHEMA_VERSION, ToolObservation


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> "ValidationResult":
        return cls(valid=True)

    @classmethod
    def reject(cls, reason: str) -> "ValidationResult":
        return cls(valid=False, reason=reason)


def dedup_key(observation: ToolObservation) -> tuple[str, str, int]:
    """Deterministic dedup key: execution_id + tool + sequence."""
    return (observation.execution_id, observation.tool_name, observation.sequence_no)


def canonical_payload(
    observation: ToolObservation,
    tenant_id: str | None = None,
    repo_fingerprint: str | None = None,
) -> bytes:
    """Stable canonical JSON for signature verification.

    Only signature-relevant fields are included; signature is never part of the
    signed payload.  JSON is emitted with sorted keys and no whitespace so the
    signer and verifier agree regardless of key order.
    """
    observation_data = observation.model_dump(mode="json", exclude={"trusted", "created_at"})
    data = {
        "tenant_id": tenant_id,
        "repo_fingerprint": repo_fingerprint,
        "observation": observation_data,
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_observation(
    observation: ToolObservation,
    secret: str,
    tenant_id: str | None = None,
    repo_fingerprint: str | None = None,
) -> str:
    """HMAC-SHA256 signature over the canonical payload."""
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical_payload(observation, tenant_id, repo_fingerprint),
        hashlib.sha256,
    )
    return digest.hexdigest()


def verify_signature(
    observation: ToolObservation,
    secret: str,
    signature: str | None,
    tenant_id: str | None = None,
    repo_fingerprint: str | None = None,
) -> bool:
    if signature is None:
        return False
    expected = sign_observation(observation, secret, tenant_id, repo_fingerprint)
    return hmac.compare_digest(expected, signature)


class ObservationValidator:
    """Quality gate with optional HMAC signature verification."""

    def __init__(
        self,
        ingest_secret: str | None = None,
        tenant_id: str | None = None,
        repo_fingerprint: str | None = None,
    ) -> None:
        self.ingest_secret = ingest_secret
        self.tenant_id = tenant_id
        self.repo_fingerprint = repo_fingerprint

    def validate(self, observation: ToolObservation, signature: str | None = None) -> ValidationResult:
        checks = [
            self._check_schema(observation),
            self._check_identity(observation),
            self._check_signature(observation, signature),
            self._check_quality(observation),
        ]
        for result in checks:
            if not result.valid:
                return result
        return ValidationResult.ok()

    def _check_schema(self, observation: ToolObservation) -> ValidationResult:
        if observation.schema_version != OBSERVATION_SCHEMA_VERSION:
            return ValidationResult.reject(
                f"unknown schema_version {observation.schema_version}; expected {OBSERVATION_SCHEMA_VERSION}"
            )
        return ValidationResult.ok()

    def _check_identity(self, observation: ToolObservation) -> ValidationResult:
        if not observation.execution_id:
            return ValidationResult.reject("missing execution_id")
        if len(observation.execution_id) > 128:
            return ValidationResult.reject("execution_id too long")
        if not observation.tool_name:
            return ValidationResult.reject("missing tool_name")
        if self.repo_fingerprint is not None and observation.repo_fingerprint != self.repo_fingerprint:
            return ValidationResult.reject("repo_fingerprint does not match signed batch scope")
        return ValidationResult.ok()

    def _check_signature(self, observation: ToolObservation, signature: str | None) -> ValidationResult:
        if self.ingest_secret is None:
            return ValidationResult.ok()  # signature not enforced
        if not verify_signature(
            observation,
            self.ingest_secret,
            signature,
            self.tenant_id,
            self.repo_fingerprint,
        ):
            return ValidationResult.reject("invalid HMAC signature")
        return ValidationResult.ok()

    def _check_quality(self, observation: ToolObservation) -> ValidationResult:
        if not observation.complete:
            return ValidationResult.reject("incomplete observation (complete=false)")
        if observation.collection_quality != CollectionQuality.VALID:
            return ValidationResult.reject(
                f"collection_quality={observation.collection_quality} (requires valid)"
            )
        if observation.exit_code != 0:
            return ValidationResult.reject(f"exit_code={observation.exit_code} (censored)")
        if observation.duration_sec is None:
            return ValidationResult.reject("missing duration_sec")
        return ValidationResult.ok()


class Deduplicator:
    """Batch-level dedup on ``(execution_id, tool_name, sequence_no)``."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, int]] = set()

    def is_duplicate(self, observation: ToolObservation) -> bool:
        key = dedup_key(observation)
        return key in self._seen

    def claim(self, observation: ToolObservation) -> bool:
        """Return True if newly claimed (not a duplicate), else False."""
        key = dedup_key(observation)
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


@dataclass
class ValidationReport:
    trusted: list[ToolObservation] = field(default_factory=list)
    rejected: list[tuple[ToolObservation, str]] = field(default_factory=list)
    duplicates: list[ToolObservation] = field(default_factory=list)


def classify_observations(
    observations: list[ToolObservation],
    validator: ObservationValidator,
    signatures: dict[str, str] | None = None,
) -> ValidationReport:
    """Validate + dedup a batch.  Returns trusted / rejected / duplicates.

    Parameters
    ----------
    signatures:
        Optional mapping from ``dedup_key`` (tuple) or execution_id to the HMAC
        signature carried alongside the observation.
    """
    signatures = signatures or {}
    report = ValidationReport()
    dedup = Deduplicator()
    for observation in observations:
        sig = signatures.get(dedup_key(observation)) or signatures.get(observation.execution_id)
        result = validator.validate(observation, sig)
        if not result.valid:
            report.rejected.append((observation, result.reason or "validation failed"))
            continue
        if not dedup.claim(observation):
            report.duplicates.append(observation)
            continue
        trusted = observation.model_copy(update={"trusted": True})
        report.trusted.append(trusted)
    return report
