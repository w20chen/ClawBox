"""Single authority for implemented ClawBox workflow combinations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .spec import ResolvedWorkflow
from .baselines import resolve_baseline
from .spec_types import (
    AdmissionPolicy, AgentDriver, InferenceBackend, ResidencyPolicy, SandboxBackend,
    ToolTransport, WorkloadSource,
)


class WorkflowClassification(StrEnum):
    PRODUCTION = "production"
    RESEARCH = "research"
    HISTORICAL = "historical"
    LEGACY = "legacy"
    LOCAL_TEST_ONLY = "local-test-only"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CapabilityResult:
    implemented: bool
    classification: WorkflowClassification
    reason: str


class CapabilityError(ValueError):
    pass


def validate_workflow(workflow: ResolvedWorkflow) -> CapabilityResult:
    """Classify a full workflow and reject combinations we must not imply exist."""
    baseline = resolve_baseline(workflow.baseline)
    if (workflow.admission_policy, workflow.residency_policy) != (
        baseline.admission_policy, baseline.residency_policy,
    ):
        return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
            f"baseline={workflow.baseline} does not match the resolved admission_policy "
            "and residency_policy")
    if workflow.residency_policy is ResidencyPolicy.PRESSURE_CHECKPOINT:
        return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
            "residency_policy=pressure_checkpoint is not implemented")
    if workflow.admission_policy is AdmissionPolicy.P90_ELASTIC:
        return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
            "admission_policy=p90_elastic is not implemented by any runner")
    if workflow.admission_policy is AdmissionPolicy.P90_STATIC:
        return CapabilityResult(False, WorkflowClassification.LEGACY,
            "admission_policy=p90_static exists only in the legacy Tool-only "
            "scheduler/allocator/controller path; it is not a Runtime + Tool workflow")
    if workflow.sandbox_backend is SandboxBackend.KUBERNETES:
        if workflow.residency_policy is not ResidencyPolicy.RESIDENT:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "sandbox_backend=kubernetes does not implement VM checkpoint residency")
        if workflow.admission_policy is not AdmissionPolicy.FIXED_PROFILE:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "production Cell p90 admission is not connected or safety-gated")
        if workflow.resources.profile is None:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "resources.profile is required when admission_policy=fixed_profile")
    if (workflow.sandbox_backend is SandboxBackend.LOCAL
            and workflow.residency_policy is not ResidencyPolicy.RESIDENT):
        return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
            "sandbox_backend=local cannot use Firecracker checkpoint residency")
    if (workflow.workload.source is WorkloadSource.SWE_REBENCH
            and workflow.agent_driver is AgentDriver.OPENCLAW
            and workflow.inference_backend.value == "api"
            and workflow.sandbox_backend is SandboxBackend.KUBERNETES
            and workflow.tool_transport is ToolTransport.SSH
            and workflow.admission_policy is AdmissionPolicy.FIXED_PROFILE
            and workflow.residency_policy is ResidencyPolicy.RESIDENT):
        return CapabilityResult(True, WorkflowClassification.PRODUCTION,
            "accepted Runtime + Tool Kubernetes Cell; ClawTune remains shadow-only")
    if (workflow.workload.source is WorkloadSource.RECORDED_TRACE
            and workflow.agent_driver is AgentDriver.OPENCLAW
            and workflow.sandbox_backend is SandboxBackend.DIRECT_FIRECRACKER
            and workflow.tool_transport is ToolTransport.SSH
            and workflow.admission_policy is AdmissionPolicy.FIXED_EXPLICIT
            and workflow.residency_policy in {ResidencyPolicy.RESIDENT, ResidencyPolicy.LLM_WAIT_CHECKPOINT}):
        if workflow.resources.runtime_memory_mib is None:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "resources.runtime_memory_mib is required when admission_policy=fixed_explicit")
        if workflow.resources.tool_memory_mib is None:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "resources.tool_memory_mib is required when admission_policy=fixed_explicit")
        required = {"runtime_rootfs", "tool_rootfs", "prompt"}
        if missing := sorted(required - workflow.sandbox.materialization.keys()):
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "sandbox.materialization is missing direct_firecracker fields: " + ", ".join(missing))
        if workflow.inference_backend is InferenceBackend.REPLAY:
            try:
                time_scale = float(workflow.inference_configuration.get("time_scale", 1.0))
            except (TypeError, ValueError):
                time_scale = -1
            if time_scale < 0:
                return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                    "inference.configuration.replay.time_scale must be a non-negative number")
        return CapabilityResult(True, WorkflowClassification.RESEARCH,
            "active paired Runtime + Tool direct-Firecracker paper workflow")
    if workflow.agent_driver is AgentDriver.REPLAY_ENGINE and workflow.sandbox_backend in {
        SandboxBackend.DIRECT_FIRECRACKER, SandboxBackend.LOCAL,
    }:
        if workflow.workload.source is not WorkloadSource.RECORDED_TRACE:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "agent_driver=replay_engine requires workload.source=recorded_trace")
        if workflow.admission_policy is not AdmissionPolicy.FIXED_EXPLICIT:
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "historical ReplayEngine workflows use admission_policy=fixed_explicit")
        if (workflow.sandbox_backend is SandboxBackend.LOCAL
                and workflow.residency_policy is not ResidencyPolicy.RESIDENT):
            return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
                "sandbox_backend=local cannot use Firecracker checkpoint residency")
        return CapabilityResult(True, WorkflowClassification.HISTORICAL,
            "ReplayEngine is retained for mechanism testing, not comparable Cell execution")
    return CapabilityResult(False, WorkflowClassification.UNSUPPORTED,
        "unsupported combination: workload/driver/backend/transport/policy axes do not form an implemented workflow")


def require_capability(workflow: ResolvedWorkflow) -> CapabilityResult:
    result = validate_workflow(workflow)
    if not result.implemented:
        raise CapabilityError(result.reason)
    return result
