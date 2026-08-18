"""M1-2 contract tests: SandboxTask v1alpha2 identity + one-way cancel (ADR-003)."""

import pytest

from clawbox.cell.controller import GROUP
from clawbox.cell.sandboxtask_v1alpha2 import (
    DESIRED_CANCELLED,
    DESIRED_RUNNING,
    VERSION_V1ALPHA2,
    build_sandboxtask_v1alpha2,
    cancel_patch,
    spec_mutation_is_cancel_only,
)


EXECUTION_SPEC = {
    "toolImage": "127.0.0.1:5000/clawbox/swe-rebench-arm64@sha256:" + "b" * 64,
    "problemStatement": "fix the parser",
    "baseCommit": "abc123",
    "llmSecretName": "swe-rebench-secret",
    "llmEgressCIDR": "0.0.0.0/0",
    "llmEgressPort": 443,
    "toolEgressCIDRs": [],
    "profile": "small",
    "timeoutSeconds": 1800,
    "commandTimeoutSeconds": 300,
    "outputLimitBytes": 4194304,
}


def build(**overrides):
    params = dict(
        name="swe-run0000000001-15five__scim2-13",
        namespace="clawbox-benchmarks",
        tenant_id="tenant-a",
        run_id="01M0AAGWB8PD4K5Y6AQ9ZMBNTM",
        attempt_id="01M0AAGWB8PD4K5Y6AQ9ZMBNTQ",
        idempotency_key="key-1",
        request_digest="d" * 64,
        execution_spec=EXECUTION_SPEC,
    )
    params.update(overrides)
    return build_sandboxtask_v1alpha2(**params)


def test_v1alpha2_manifest_has_identity_link():
    task = build()
    assert task["apiVersion"] == f"{GROUP}/{VERSION_V1ALPHA2}"
    assert task["kind"] == "SandboxTask"
    spec = task["spec"]
    assert spec["runRef"] == {
        "tenantID": "tenant-a",
        "runID": "01M0AAGWB8PD4K5Y6AQ9ZMBNTM",
        "attemptID": "01M0AAGWB8PD4K5Y6AQ9ZMBNTQ",
    }
    assert spec["idempotencyKey"] == "key-1"
    assert spec["requestDigest"] == "d" * 64
    assert spec["desiredState"] == DESIRED_RUNNING


def test_v1alpha2_keeps_execution_spec_verbatim():
    task = build()
    spec = task["spec"]
    for key, value in EXECUTION_SPEC.items():
        assert spec[key] == value


def test_v1alpha2_validates_identity_fields():
    with pytest.raises(ValueError):
        build(tenant_id="")
    with pytest.raises(ValueError):
        build(attempt_id="")
    with pytest.raises(ValueError):
        build(idempotency_key="")
    with pytest.raises(ValueError):
        build(request_digest="short")
    with pytest.raises(ValueError):
        build(request_digest="G" * 64)
    with pytest.raises(ValueError):
        build(desired_state="Paused")
    with pytest.raises(ValueError):
        build(name="x" * 49)


def test_cancel_patch_is_one_way_running_to_cancelled():
    task = build()
    assert spec_mutation_is_cancel_only(task["spec"], {**task["spec"], "desiredState": DESIRED_CANCELLED})
    assert spec_mutation_is_cancel_only(task["spec"], dict(task["spec"]))
    # Any other change is rejected (mirrors the CRD CEL rule).
    changed = dict(task["spec"])
    changed["timeoutSeconds"] = 3600
    assert spec_mutation_is_cancel_only(task["spec"], changed) is False
    # Once cancelled, it cannot mutate further.
    cancelled = dict(task["spec"])
    cancelled["desiredState"] = DESIRED_CANCELLED
    assert spec_mutation_is_cancel_only(cancelled, dict(cancelled))
    assert spec_mutation_is_cancel_only(cancelled, {**cancelled, "timeoutSeconds": 1}) is False
    # Cancel is not reversible.
    assert spec_mutation_is_cancel_only(cancelled, {**cancelled, "desiredState": DESIRED_RUNNING}) is False


def test_cancel_patch_shape():
    assert cancel_patch() == {"spec": {"desiredState": DESIRED_CANCELLED}}
