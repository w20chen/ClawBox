"""Canonical standalone experiment primitives."""

from .results import FailureCategory, ResultEnvelope, RunStatus, failure_category_for
from .spec import (
    ensure_supported_experiment, AdmissionPolicy, AgentDriver, ArrivalSchedule, EvictionPolicy, ExperimentArm, ExperimentSpec,
    InferenceBackend, PolicySpec, ReclamationPolicy, RestorePolicy, WorkloadCase,
    SessionAssignment, WorkloadSource, expand_matrix, load_experiment,
    load_workload_cases, spec_digest,
)

__all__ = [
    "AdmissionPolicy", "AgentDriver", "ArrivalSchedule",
    "EvictionPolicy", "ExperimentArm",
    "ExperimentSpec", "FailureCategory", "InferenceBackend", "PolicySpec",
    "ReclamationPolicy", "RestorePolicy", "ResultEnvelope", "RunStatus",
    "SessionAssignment", "WorkloadCase", "WorkloadSource", "expand_matrix",
    "failure_category_for",
    "ensure_supported_experiment", "load_experiment", "load_workload_cases",
    "spec_digest",
]
