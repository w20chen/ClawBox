"""The deliberately small, immutable scheduling-baseline registry."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .spec_types import AdmissionPolicy, ResidencyPolicy


@dataclass(frozen=True)
class Baseline:
    """A baseline controls scheduling policy only; it never selects a backend."""

    name: str
    admission_policy: AdmissionPolicy
    residency_policy: ResidencyPolicy
    implementation_status: str


BASELINES = MappingProxyType({
    "fixed-resident": Baseline(
        "fixed-resident", AdmissionPolicy.FIXED_PROFILE, ResidencyPolicy.RESIDENT, "implemented",
    ),
    "fixed-explicit-resident": Baseline(
        "fixed-explicit-resident", AdmissionPolicy.FIXED_EXPLICIT, ResidencyPolicy.RESIDENT,
        "implemented-direct-firecracker",
    ),
    "fixed-llm-wait-checkpoint": Baseline(
        "fixed-llm-wait-checkpoint", AdmissionPolicy.FIXED_EXPLICIT,
        ResidencyPolicy.LLM_WAIT_CHECKPOINT, "implemented-direct-firecracker",
    ),
    "p90-static": Baseline(
        "p90-static", AdmissionPolicy.P90_STATIC, ResidencyPolicy.RESIDENT,
        "implemented-kubernetes-research",
    ),
    "p90-elastic": Baseline(
        "p90-elastic", AdmissionPolicy.P90_ELASTIC, ResidencyPolicy.RESIDENT,
        "implemented-kubernetes-research",
    ),
    "p90-static-llm-wait-checkpoint": Baseline(
        "p90-static-llm-wait-checkpoint", AdmissionPolicy.P90_STATIC,
        ResidencyPolicy.LLM_WAIT_CHECKPOINT, "implemented-direct-firecracker-research",
    ),
    "p90-elastic-pressure-checkpoint": Baseline(
        "p90-elastic-pressure-checkpoint", AdmissionPolicy.P90_ELASTIC,
        ResidencyPolicy.PRESSURE_CHECKPOINT, "not-implemented",
    ),
})


def resolve_baseline(name: str) -> Baseline:
    try:
        return BASELINES[name]
    except KeyError as exc:
        raise ValueError(f"unknown baseline {name!r}; choose one of: {', '.join(BASELINES)}") from exc
