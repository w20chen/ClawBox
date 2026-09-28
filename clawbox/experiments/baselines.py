"""Immutable policy recipes for the schema-v2 experiment runner.

The catalog is deliberately backend-free. A baseline selects one complete
``PolicySpec`` tuple; the experiment specification still selects the agent,
inference backend, CubeSandbox templates, workload, and concurrency.

The names retained from the pre-schema-v2 planner are compatibility aliases.
They resolve to the closest supported schema-v2 policy and are marked as such
so callers do not mistake them for a second execution architecture.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .spec import PolicySpec
from .spec_types import (
    AdmissionPolicy,
    EvictionPolicy,
    ReclamationPolicy,
    RestorePolicy,
)


@dataclass(frozen=True)
class Baseline:
    """A complete, backend-independent schema-v2 policy recipe."""

    name: str
    admission_policy: AdmissionPolicy
    reclamation_policy: ReclamationPolicy
    eviction_policy: EvictionPolicy
    restore_policy: RestorePolicy
    implementation_status: str = "implemented"
    fixed_delay_seconds: float | None = None
    prefetch_lead_seconds: float | None = None
    checkpoint_break_even_seconds: float | None = None

    def as_policy(self, *, name: str | None = None) -> PolicySpec:
        """Materialize this recipe as the canonical immutable policy model."""
        return PolicySpec(
            name=name or self.name,
            admission=self.admission_policy,
            reclamation=self.reclamation_policy,
            eviction=self.eviction_policy,
            restore=self.restore_policy,
            fixed_delay_seconds=self.fixed_delay_seconds,
            prefetch_lead_seconds=self.prefetch_lead_seconds,
            checkpoint_break_even_seconds=self.checkpoint_break_even_seconds,
        )


def _resident(
    name: str,
    admission: AdmissionPolicy,
    *,
    status: str = "implemented",
) -> Baseline:
    return Baseline(
        name, admission, ReclamationPolicy.RESIDENT,
        EvictionPolicy.NONE, RestorePolicy.NONE, status,
    )


def _snapshot(
    name: str,
    admission: AdmissionPolicy,
    eviction: EvictionPolicy,
    *,
    restore: RestorePolicy = RestorePolicy.REACTIVE,
    status: str = "implemented",
    fixed_delay_seconds: float | None = None,
    prefetch_lead_seconds: float | None = None,
    checkpoint_break_even_seconds: float | None = None,
) -> Baseline:
    return Baseline(
        name, admission, ReclamationPolicy.SNAPSHOT_PAUSE, eviction, restore,
        status, fixed_delay_seconds, prefetch_lead_seconds,
        checkpoint_break_even_seconds,
    )


ACTIVE_BASELINES = (
    "tool-static-resident",       # A: calibrated fixed overcommit
    "tool-p50-resident",          # A+B: command-specific P50 admission
    "tool-p50-wait-reactive",     # A+B+C: wait-aware WARM reclamation
)


# Keep the catalog explicit and deterministic. In particular, do not infer an
# allocator, network transport, or sandbox backend from a baseline name.
BASELINES = MappingProxyType({
    # Retained research policies that are no longer part of the main ablation.
    "lifetime-full-resident": _resident(
        "lifetime-full-resident", AdmissionPolicy.LIFETIME_FULL,
        status="deprecated",
    ),
    "tool-full-resident": _resident(
        "tool-full-resident", AdmissionPolicy.TOOL_FULL,
        status="deprecated",
    ),
    # A: calibrated fixed overcommit.
    "tool-static-resident": _resident(
        "tool-static-resident", AdmissionPolicy.TOOL_STATIC,
    ),
    # A+B: command-specific P50 admission.
    "tool-p50-resident": _resident(
        "tool-p50-resident", AdmissionPolicy.TOOL_P50,
    ),
    "tool-oracle-resident": _resident(
        "tool-oracle-resident", AdmissionPolicy.TOOL_ORACLE,
        status="deprecated",
    ),
    "tool-static-eager-reactive": _snapshot(
        "tool-static-eager-reactive", AdmissionPolicy.TOOL_STATIC,
        EvictionPolicy.EAGER, status="deprecated",
    ),
    "tool-p50-eager-reactive": _snapshot(
        "tool-p50-eager-reactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.EAGER, status="deprecated",
    ),
    "tool-p50-fixed-reactive": _snapshot(
        "tool-p50-fixed-reactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.FIXED_DELAY, status="deprecated", fixed_delay_seconds=0.5,
    ),
    # A+B+C: prediction plus wait-aware WARM checkpoint and reactive restore.
    "tool-p50-wait-reactive": _snapshot(
        "tool-p50-wait-reactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.WAIT_AWARE_PRESSURE,
    ),
    "tool-p50-wait-proactive": _snapshot(
        "tool-p50-wait-proactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.WAIT_AWARE_PRESSURE, restore=RestorePolicy.PROACTIVE,
        status="deprecated", prefetch_lead_seconds=0.5,
    ),
    "tool-static-time-oracle-reactive": _snapshot(
        "tool-static-time-oracle-reactive", AdmissionPolicy.TOOL_STATIC,
        EvictionPolicy.TIME_ORACLE, status="deprecated", checkpoint_break_even_seconds=4.0,
    ),
    "tool-p50-tiered-lru-oracle-reactive": _snapshot(
        "tool-p50-tiered-lru-oracle-reactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.TIERED_LRU_ORACLE, status="deprecated",
    ),
    "tool-p50-tiered-time-oracle-reactive": _snapshot(
        "tool-p50-tiered-time-oracle-reactive", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.TIERED_TIME_ORACLE, status="deprecated",
    ),

    # Pre-schema-v2 names retained as explicit compatibility aliases. They no
    # longer select Kubernetes, direct Firecracker, or any other backend.
    "fixed-resident": _resident(
        "fixed-resident", AdmissionPolicy.LIFETIME_FULL,
        status="deprecated",
    ),
    "fixed-explicit-resident": _resident(
        "fixed-explicit-resident", AdmissionPolicy.TOOL_FULL,
        status="deprecated",
    ),
    "fixed-llm-wait-checkpoint": _snapshot(
        "fixed-llm-wait-checkpoint", AdmissionPolicy.TOOL_FULL,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="deprecated",
    ),
    "p90-static": _resident(
        "p90-static", AdmissionPolicy.TOOL_STATIC, status="deprecated",
    ),
    "p90-elastic": _resident(
        "p90-elastic", AdmissionPolicy.TOOL_P50, status="deprecated",
    ),
    "p90-static-llm-wait-checkpoint": _snapshot(
        "p90-static-llm-wait-checkpoint", AdmissionPolicy.TOOL_STATIC,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="deprecated",
    ),
    "p90-elastic-pressure-checkpoint": _snapshot(
        "p90-elastic-pressure-checkpoint", AdmissionPolicy.TOOL_P50,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="deprecated",
    ),
})


def resolve_baseline(name: str) -> Baseline:
    try:
        return BASELINES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown baseline {name!r}; choose one of: {', '.join(BASELINES)}"
        ) from exc
