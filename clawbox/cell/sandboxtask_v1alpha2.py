"""SandboxTask v1alpha2 manifest builder (ADR-003, M1-2).

Extends the immutable v1alpha1 execution contract with the Managed API
identity link (runRef / idempotencyKey / requestDigest) and the one-way
desiredState cancellation field. The v1alpha1 execution spec is reused
verbatim so the Cell Controller validation stays identical.
"""

from __future__ import annotations

from typing import Any

from clawbox.cell.controller import GROUP, PLURAL

VERSION_V1ALPHA2 = "v1alpha2"

# Only this one-way transition is permitted by the CRD CEL rule.
DESIRED_RUNNING = "Running"
DESIRED_CANCELLED = "Cancelled"


def build_sandboxtask_v1alpha2(
    *,
    name: str,
    namespace: str,
    tenant_id: str,
    run_id: str,
    attempt_id: str,
    idempotency_key: str,
    request_digest: str,
    execution_spec: dict[str, Any],
    desired_state: str = DESIRED_RUNNING,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one immutable SandboxTask for one managed Attempt."""
    if desired_state not in (DESIRED_RUNNING, DESIRED_CANCELLED):
        raise ValueError(f"desiredState must be Running or Cancelled, got {desired_state!r}")
    if not tenant_id or not run_id or not attempt_id:
        raise ValueError("runRef tenantID/runID/attemptID are required")
    if len(idempotency_key) < 1 or len(idempotency_key) > 512:
        raise ValueError("idempotencyKey must be 1..512 characters")
    if len(request_digest) != 64 or not all(c in "0123456789abcdef" for c in request_digest):
        raise ValueError("requestDigest must be a 64-char lowercase sha256 hex digest")
    if len(name) > 48:
        raise ValueError("SandboxTask name must be at most 48 characters so owned child names remain valid")

    return {
        "apiVersion": f"{GROUP}/{VERSION_V1ALPHA2}",
        "kind": "SandboxTask",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(labels or {}),
            "annotations": dict(annotations or {}),
        },
        "spec": {
            "runRef": {
                "tenantID": tenant_id,
                "runID": run_id,
                "attemptID": attempt_id,
            },
            "idempotencyKey": idempotency_key,
            "requestDigest": request_digest,
            "desiredState": desired_state,
            **execution_spec,
        },
    }


def cancel_patch() -> dict[str, Any]:
    """The only permitted spec mutation: one-way desiredState cancellation.

    `{"spec": {"desiredState": "Cancelled"}}` passes the CRD CEL rule
    (self == oldSelf || Running -> Cancelled) and nothing else does.
    """
    return {"spec": {"desiredState": DESIRED_CANCELLED}}


def spec_mutation_is_cancel_only(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Python mirror of the CRD CEL immutability rule, for tests and admission.

    True if `new` equals `old`, or the only difference is
    desiredState Running -> Cancelled.
    """
    if old == new:
        return True
    if old.get("desiredState", DESIRED_RUNNING) != DESIRED_RUNNING:
        return False  # already cancelled (or invalid); further mutation rejected
    if new.get("desiredState") != DESIRED_CANCELLED:
        return False
    rest_old = {k: v for k, v in old.items() if k != "desiredState"}
    rest_new = {k: v for k, v in new.items() if k != "desiredState"}
    return rest_old == rest_new
