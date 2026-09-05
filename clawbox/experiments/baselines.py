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
) -> Baseline:
    return Baseline(
        name, admission, ReclamationPolicy.SNAPSHOT_PAUSE, eviction, restore,
        status, fixed_delay_seconds, prefetch_lead_seconds,
    )


# Keep the catalog explicit and deterministic. In particular, do not infer an
# allocator, network transport, or sandbox backend from a baseline name.
BASELINES = MappingProxyType({
    # Canonical schema-v2 recipes used by the checked-in matrices.
    "lifetime-full-resident": _resident(
        "lifetime-full-resident", AdmissionPolicy.LIFETIME_FULL,
    ),
    "tool-full-resident": _resident(
        "tool-full-resident", AdmissionPolicy.TOOL_FULL,
    ),
    "tool-static-resident": _resident(
        "tool-static-resident", AdmissionPolicy.TOOL_STATIC,
    ),
    "tool-p90-resident": _resident(
        "tool-p90-resident", AdmissionPolicy.TOOL_P90,
    ),
    "tool-oracle-resident": _resident(
        "tool-oracle-resident", AdmissionPolicy.TOOL_ORACLE,
    ),
    "tool-static-eager-reactive": _snapshot(
        "tool-static-eager-reactive", AdmissionPolicy.TOOL_STATIC,
        EvictionPolicy.EAGER,
    ),
    "tool-p90-eager-reactive": _snapshot(
        "tool-p90-eager-reactive", AdmissionPolicy.TOOL_P90,
        EvictionPolicy.EAGER,
    ),
    "tool-p90-fixed-reactive": _snapshot(
        "tool-p90-fixed-reactive", AdmissionPolicy.TOOL_P90,
        EvictionPolicy.FIXED_DELAY, fixed_delay_seconds=0.5,
    ),
    "tool-p90-wait-reactive": _snapshot(
        "tool-p90-wait-reactive", AdmissionPolicy.TOOL_P90,
        EvictionPolicy.WAIT_AWARE_PRESSURE,
    ),
    "tool-p90-wait-proactive": _snapshot(
        "tool-p90-wait-proactive", AdmissionPolicy.TOOL_P90,
        EvictionPolicy.WAIT_AWARE_PRESSURE, restore=RestorePolicy.PROACTIVE,
        prefetch_lead_seconds=0.5,
    ),

    # Pre-schema-v2 names retained as explicit compatibility aliases. They no
    # longer select Kubernetes, direct Firecracker, or any other backend.
    "fixed-resident": _resident(
        "fixed-resident", AdmissionPolicy.LIFETIME_FULL,
        status="compatibility-alias",
    ),
    "fixed-explicit-resident": _resident(
        "fixed-explicit-resident", AdmissionPolicy.TOOL_FULL,
        status="compatibility-alias",
    ),
    "fixed-llm-wait-checkpoint": _snapshot(
        "fixed-llm-wait-checkpoint", AdmissionPolicy.TOOL_FULL,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="compatibility-alias",
    ),
    "p90-static": _resident(
        "p90-static", AdmissionPolicy.TOOL_STATIC, status="compatibility-alias",
    ),
    "p90-elastic": _resident(
        "p90-elastic", AdmissionPolicy.TOOL_P90, status="compatibility-alias",
    ),
    "p90-static-llm-wait-checkpoint": _snapshot(
        "p90-static-llm-wait-checkpoint", AdmissionPolicy.TOOL_STATIC,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="compatibility-alias",
    ),
    "p90-elastic-pressure-checkpoint": _snapshot(
        "p90-elastic-pressure-checkpoint", AdmissionPolicy.TOOL_P90,
        EvictionPolicy.WAIT_AWARE_PRESSURE, status="compatibility-alias",
    ),
})


def resolve_baseline(name: str) -> Baseline:
    try:
        return BASELINES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown baseline {name!r}; choose one of: {', '.join(BASELINES)}"
        ) from exc
