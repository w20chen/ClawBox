"""Small, fixed comparisons expanded into the existing experiment worker schema."""
from __future__ import annotations

from copy import deepcopy


SUITES = ("admission", "numa-snapshot")


def expand_suite(raw: dict, *, replace: bool = False) -> dict:
    """Fill suite-owned settings; reject edits that would confound the comparison.

    configure deliberately replaces the base recipe. Reading a saved experiment
    only fills missing settings and verifies the complete frozen recipe.
    """
    root = deepcopy(raw)
    suite = root.get("suite")
    if suite is None:
        if root.get("suite_ablations"):
            raise ValueError("suite_ablations requires suite")
        return root
    if suite not in SUITES:
        raise ValueError(f"suite must be one of {', '.join(SUITES)}")
    resources = root.setdefault("resources", {})
    execution = root.setdefault("execution", {})

    def assign(target, key, value, path):
        if not replace and key in target and target[key] != value:
            raise ValueError(f"{suite} suite requires {path}.{key}={value!r}; remove the conflicting override")
        target[key] = value

    if resources.get("compute_nodes"):
        for total, per_node in (
            ("pool_memory_budget_mib", "high_watermark_mib"),
            ("local_memory_capacity_mib", "memory_capacity_mib"),
            ("local_memory_low_watermark_mib", "low_watermark_mib"),
            ("local_memory_high_watermark_mib", "high_watermark_mib"),
        ):
            try:
                value = sum(node[per_node] for node in resources["compute_nodes"])
            except (KeyError, TypeError) as exc:
                raise ValueError(f"suite compute_nodes require integer {per_node}") from exc
            assign(resources, total, value, "resources")

    assign(execution, "placement_policy", "load_aware" if resources.get("compute_nodes") else "round_robin", "execution")
    assign(execution, "session_compute_nodes", [], "execution")
    assign(resources, "lifecycle_admission", "configured-capacity", "resources")
    assign(resources, "proactive_cache_reclaim", True, "resources")
    assign(resources, "memory_action_costs", None, "resources")
    policies = []

    def policy(name, admission, *, template=False, snapshot=False, deployment="managed", runtime=None):
        snapshots = suite == "numa-snapshot" and deployment != "os-default"
        policies.append(dict(
            name=name, admission=admission, deployment=deployment,
            runtime_control=runtime or ("off" if deployment == "os-default" else root.get("suite_runtime_control", "off")),
            reclamation="snapshot_pause" if snapshots else "resident",
            eviction="wait_aware_pressure" if snapshots else "none",
            restore="reactive" if snapshots else "none",
            template_aware_placement=template, snapshot_aware_residency=snapshot,
        ))

    if suite == "admission":
        # Compare command admission against the same ordinary resident deployment.
        resources.setdefault("prediction_mode", "runtime")
        assign(resources, "memory_scheduling", "local_only", "resources")
        assign(resources, "template_base_sharing", "disabled", "resources")
        assign(resources, "snapshot_residency_costs", None, "resources")
        if not resources.get("prediction_artifact"):
            raise ValueError("admission suite requires resources.prediction_artifact from a separate validated training run")
        policy("default-resident", "off", runtime="off")
        policy("predicted-resident", "predicted")
        if root.get("suite_ablations"):
            policy("fixed-resident", "fixed", runtime="off")
            policy("runtime-resident", "off", runtime="agentcgroup_cgroup")
    else:
        if not resources.get("compute_nodes"):
            raise ValueError("numa-snapshot suite requires an applied host profile with compute_nodes")
        if not resources.get("warm_memory_capacity_mib"):
            raise ValueError("numa-snapshot suite requires positive WARM capacity")
        assign(resources, "memory_scheduling", "joint" if resources.get("shared_memory_borrow_limit_mib") else "snapshot_only", "resources")
        assign(resources, "snapshot_storage", "tiered", "resources")
        assign(resources, "template_base_sharing", "required", "resources")
        # No prediction artifact or arbitrary fixed allowance is needed here.
        # Both arms keep lifecycle admission and measured runtime pressure control.
        assign(resources, "prediction_artifact", None, "resources")
        policy("default-resident", "off", deployment="os-default")
        if root.get("suite_ablations"):
            policy("load-aware-lru", "off")
            policy("template-aware-only", "off", template=True)
            policy("snapshot-aware-only", "off", snapshot=True)
        policy("template-snapshot-aware", "off", template=True, snapshot=True)

    if not replace and root.get("policies") is not None:
        # Pydantic dumps include defaults. Compare the mechanism fields here;
        # unsupported fields are rejected by PolicySpec itself.
        actual = root["policies"]
        if len(actual) != len(policies) or any(
            set(a) - set(expected) or any(a.get(key, False if key in ("template_aware_placement", "snapshot_aware_residency") else None) != value
                for key, value in expected.items())
            for a, expected in zip(actual, policies)
        ):
            raise ValueError("suite policies are generated; remove policies or regenerate with configure --suite")
    root["policies"] = policies
    return root
