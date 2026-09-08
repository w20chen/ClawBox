"""Presentation and selection of existing presets, without new execution policies."""
from __future__ import annotations

from .baselines import BASELINES


DIMENSIONS = {
    "reserve_during": ("session", "command"),
    "estimate": ("capacity", "fixed", "predicted", "measured"),
    "idle": ("resident", "immediate", "timeout", "pressure", "known-wait",
             "tiered-lru", "tiered-wait"),
    "resume": ("none", "on-demand", "ahead"),
}


def dimensions(policy) -> dict[str, str]:
    admission = policy.admission.value
    return {
        "reserve_during": "session" if admission == "lifetime_full" else "command",
        "estimate": {"lifetime_full": "capacity", "tool_full": "capacity",
                     "tool_static": "fixed", "tool_p90": "predicted",
                     "tool_oracle": "measured"}[admission],
        "idle": {"none": "resident", "eager": "immediate", "fixed_delay": "timeout",
                 "wait_aware_pressure": "pressure", "time_oracle": "known-wait",
                 "tiered_lru_oracle": "tiered-lru",
                 "tiered_time_oracle": "tiered-wait"}[policy.eviction.value],
        "resume": {"none": "none", "reactive": "on-demand",
                   "proactive": "ahead"}[policy.restore.value],
    }


def select_presets(names=(), **filters) -> list[str]:
    """OR within a dimension, AND across dimensions; only catalog recipes qualify."""
    if not any(filters.values()):
        return list(names)
    for key, values in filters.items():
        if key not in DIMENSIONS or any(value not in DIMENSIONS[key] for value in (values or ())):
            raise ValueError(f"unknown preset dimension or value: {key}")
    candidates = list(names) or [name for name, recipe in BASELINES.items()
                                if recipe.implementation_status == "implemented"]
    result = []
    for name in candidates:
        if name not in BASELINES:
            raise ValueError(f"unknown baseline: {name}")
        row = dimensions(BASELINES[name].as_policy())
        if all(not values or row[key] in values for key, values in filters.items()):
            result.append(name)
    if not result:
        raise ValueError("No implemented preset matches these dimensions; see experiment baselines. "
                         "Dimension selection does not create new policy combinations.")
    return result
