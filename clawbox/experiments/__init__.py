"""Canonical, read-only experiment planning primitives for ClawBox."""

from .baselines import BASELINES, resolve_baseline
from .capabilities import CapabilityError, CapabilityResult, validate_workflow
from .results import ResultEnvelope, failure_category_for
from .spec import (
    AdmissionPolicy,
    AgentDriver,
    ExperimentSpec,
    InferenceBackend,
    ResolvedWorkflow,
    ResidencyPolicy,
    SandboxBackend,
    ToolTransport,
    WorkloadCase,
    WorkloadSource,
    expand_matrix,
    load_workload_cases,
    load_experiment,
)

__all__ = [
    "AdmissionPolicy", "AgentDriver", "BASELINES", "CapabilityError", "CapabilityResult",
    "ExperimentSpec", "InferenceBackend", "ResolvedWorkflow", "ResidencyPolicy",
    "ResultEnvelope", "SandboxBackend", "ToolTransport", "WorkloadCase", "WorkloadSource",
    "expand_matrix", "failure_category_for", "load_experiment", "resolve_baseline",
    "load_workload_cases",
    "validate_workflow",
]
