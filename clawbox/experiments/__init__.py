"""Canonical schema-v2 experiment primitives."""

from .results import FailureCategory, ResultEnvelope, RunStatus, failure_category_for
from .spec import (
    AdmissionPolicy, AgentDriver, EvictionPolicy, ExperimentArm, ExperimentSpec,
    InferenceBackend, PolicySpec, ReclamationPolicy, RestorePolicy, WorkloadCase,
    WorkloadSource, expand_matrix, load_experiment, load_workload_cases, spec_digest,
)

__all__ = [
    "AdmissionPolicy", "AgentDriver", "EvictionPolicy", "ExperimentArm",
    "ExperimentSpec", "FailureCategory", "InferenceBackend", "PolicySpec",
    "ReclamationPolicy", "RestorePolicy", "ResultEnvelope", "RunStatus",
    "WorkloadCase", "WorkloadSource", "expand_matrix", "failure_category_for",
    "load_experiment", "load_workload_cases", "spec_digest",
]
