"""User-facing helpers for configuring schema-v3 experiments.

This module deliberately edits only experiment data.  The generated document
is validated by the same Pydantic schema and is executed by the existing
ExperimentWorker, so convenience configuration cannot create a second runtime
or policy path.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable

import yaml

from clawbox.replay.trace import load_trace
from .baselines import ensure_supported_experiment, resolve_baseline
from .spec import ExperimentSpec, ReclamationPolicy, expand_matrix, load_workload_cases


def parse_concurrency(value: str) -> list[int]:
    """Parse ``1,5,60`` into a unique positive concurrency list."""
    try:
        levels = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError("concurrency must be a comma-separated integer list") from exc
    if not levels or any(level < 1 for level in levels):
        raise ValueError("concurrency must contain positive integers")
    if len(levels) != len(set(levels)):
        raise ValueError("concurrency must not contain duplicates")
    return levels


def gib_to_mib(value: float, *, name: str, allow_zero: bool = False) -> int:
    """Convert a GiB CLI value to the exact MiB schema unit."""
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    mib = value * 1024
    rounded = round(mib)
    if abs(mib - rounded) > 1e-9:
        raise ValueError(f"{name} must resolve to a whole number of MiB")
    return int(rounded)


def _mapping(raw: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be a YAML mapping")
    return raw


def calibrated_static_tool_memory(path: Path, repository: str) -> int:
    """Read the fixed-baseline P90 chosen by a validated training export."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read prediction artifact {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "clawbox_prediction_v2":
        raise ValueError("automatic static calibration requires a clawbox_prediction_v2 artifact")
    if payload.get("repository") != repository or payload.get("training_validated") is not True:
        raise ValueError("automatic static calibration requires matching validated training")
    calibration = payload.get("static_tool_memory_calibration")
    if not isinstance(calibration, dict):
        raise ValueError(
            "prediction artifact has no static Tool-memory calibration; rerun experiment train"
        )
    if calibration.get("metric") != "host_cgroup_memory_current_increment":
        raise ValueError("static calibration must measure host cgroup execution growth")
    value = calibration.get("recommended_mib")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("prediction artifact has an invalid static Tool-memory calibration")
    return value


def _set_template(
    section: dict[str, Any], *, label: str, template_id: str | None,
    image_reference: str | None, image_digest: str | None,
) -> None:
    current_template = section.get("template_id") or section.get("template_alias")
    if template_id is not None and template_id != current_template:
        if image_reference is None or image_digest is None:
            raise ValueError(
                f"changing the {label} template requires its image reference and digest"
            )
    if template_id is not None:
        section["template_id"] = template_id
        section.pop("template_alias", None)
    if image_reference is not None:
        section["source_image_reference"] = image_reference
    if image_digest is not None:
        section["image_digest"] = image_digest


def configure_experiment(
    base_path: Path,
    *,
    suite: str | None = None,
    suite_ablations: bool | None = None,
    runtime_control: str | None = None,
    host_profile: dict[str, Any] | None = None,
    compute_nodes: list[dict] | None = None,
    placement_policy: str | None = None,
    template_balance_slack: float | None = None,
    template_aware_placement: bool | None = None,
    snapshot_aware_residency: bool | None = None,
    template_base_sharing: str | None = None,
    warm_restore_seconds_per_gib: float | None = None,
    cold_restore_seconds_per_gib: float | None = None,
    snapshot_cost_provenance: str | None = None,
    session_compute_nodes: list[str] | None = None,
    experiment_id: str | None = None,
    trace: str | None = None,
    case_id: str | None = None,
    prompt: str | None = None,
    repository: str | None = None,
    base_commit: str | None = None,
    validation_command: str | None = None,
    repetitions: int | None = None,
    session_assignment: str | None = None,
    concurrency: str | None = None,
    baseline_names: Iterable[str] = (),
    runtime_template_id: str | None = None,
    tool_template_id: str | None = None,
    runtime_image_reference: str | None = None,
    tool_image_reference: str | None = None,
    runtime_image_digest: str | None = None,
    tool_image_digest: str | None = None,
    runtime_vcpu: int | None = None,
    tool_vcpu: int | None = None,
    runtime_memory_gib: float | None = None,
    tool_memory_gib: float | None = None,
    tool_workspace: str | None = None,
    target_node: str | None = None,
    pool_memory_gib: float | None = None,
    emergency_free_memory_gib: float | None = None,
    checkpoint_headroom_gib: float | None = None,
    local_memory_capacity_mib: int | None = None,
    local_memory_low_watermark_mib: int | None = None,
    local_memory_high_watermark_mib: int | None = None,
    shared_memory_borrow_limit_mib: int | None = None,
    warm_memory_capacity_mib: int | None = None,
    local_memory_cgroup: str | None = None,
    warm_snapshot_root: str | None = None,
    cold_snapshot_root: str | None = None,
    local_numa_node: int | None = None,
    warm_numa_node: int | None = None,
    snapshot_mechanism: str | None = None,
    snapshot_storage: str | None = None,
    memory_scheduling: str | None = None,
    static_tool_memory_mib: int | str | None = None,
    prediction_artifact: str | None = None,
    prediction_mode: str | None = None,
    prediction_reservation_scale: float | None = None,
    admission_margin_mib: int | None = None,
    arrival_schedule: str | None = None,
    stagger_seconds: float | None = None,
    random_seed: int | None = None,
    arm_timeout_seconds: int | None = None,
    command_timeout_seconds: int | None = None,
    memory_sample_interval_seconds: float | None = None,
    stabilization_seconds: float | None = None,
    time_scale: float | None = None,
    openclaw_exec_yield_ms: int | None = None,
    model_wait_prediction_seconds: float | str | None = None,
    model_wait_prediction_source: str | None = None,
    inference_backend: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
) -> ExperimentSpec:
    """Apply friendly overrides to a checked-in, already complete base spec."""
    try:
        raw = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read base experiment {base_path}: {exc}") from exc
    root = _mapping(raw, name="experiment")
    if suite is not None:
        root["suite"] = suite
        root["suite_ablations"] = False
    if suite_ablations is not None:
        root["suite_ablations"] = suite_ablations
    selected_suite = root.get("suite")
    if selected_suite and runtime_control is not None:
        root["suite_runtime_control"] = runtime_control
    if selected_suite and baseline_names:
        raise ValueError("choose --suite or diagnostic --baseline, not both")
    if selected_suite and any(value is not None for value in (
        placement_policy, session_compute_nodes, memory_scheduling,
        template_aware_placement, snapshot_aware_residency, template_base_sharing,
        snapshot_storage,
    )):
        raise ValueError("the suite owns placement, memory scheduling and snapshot policy; remove mechanism overrides")
    if selected_suite == "numa-snapshot" and prediction_artifact is not None:
        raise ValueError("numa-snapshot uses runtime feedback in every arm; a prediction artifact belongs to the admission suite")
    if selected_suite == "admission" and root.get("suite_ablations") and static_tool_memory_mib is None:
        static_tool_memory_mib = "auto"
    runtime = _mapping(root.get("runtime"), name="runtime")
    tool = _mapping(root.get("sandbox"), name="sandbox")
    execution = _mapping(root.get("execution"), name="execution")
    resources = _mapping(root.get("resources"), name="resources")
    if host_profile is not None:
        # A selected host replaces the topology, including explicit absence of
        # a shared tier. None must not retain a different host's base settings.
        for key in ("local_memory_capacity_mib", "local_memory_low_watermark_mib",
                    "local_memory_high_watermark_mib", "shared_memory_borrow_limit_mib",
                    "local_numa_node", "warm_numa_node", "local_memory_cgroup"):
            resources[key] = host_profile.get(key)
        resources["compute_nodes"] = host_profile.get("compute_nodes") or []
        resources["warm_memory_capacity_mib"] = host_profile.get("warm_capacity_mib", 0)
        resources["warm_snapshot_root"] = host_profile.get("warm_root")
        resources["cold_snapshot_root"] = host_profile.get("cold_root")
        resources["pool_memory_budget_mib"] = (
            host_profile.get("local_memory_high_watermark_mib")
            or host_profile["local_memory_capacity_mib"]
        )
    if compute_nodes is not None:
        resources["compute_nodes"] = compute_nodes
        if compute_nodes:
            resources["local_numa_node"] = None
    if placement_policy is not None:
        execution["placement_policy"] = placement_policy
        if placement_policy != "explicit" and session_compute_nodes is None:
            execution["session_compute_nodes"] = []
    if template_balance_slack is not None:
        execution["template_balance_slack"] = template_balance_slack
    if session_compute_nodes is not None:
        execution["session_compute_nodes"] = session_compute_nodes
    inference = _mapping(root.get("inference"), name="inference")
    configuration = inference.setdefault("configuration", {})
    configuration = _mapping(configuration, name="inference.configuration")

    if experiment_id is not None:
        root["experiment_id"] = experiment_id
    if any(value is not None for value in (trace, case_id, prompt, repository, base_commit, validation_command)):
        workload = _mapping(root.get("workload"), name="workload")
        cases = workload.get("cases")
        if not cases and workload.get("source") == "recorded_trace":
            source = trace or workload["input"]
            cases = [{"case_id": case_id or Path(source).stem, "source": "recorded_trace",
                      "source_reference": source, "replay_trace_reference": source}]
            workload["cases"] = cases
        if not isinstance(cases, list) or len(cases) != 1:
            raise ValueError(
                "workload overrides require one case; edit workload.cases for a multi-case experiment"
            )
        case = _mapping(cases[0], name="workload.cases[0]")
        if trace is not None:
            workload["input"] = trace
            workload["source"] = "recorded_trace"
            case["source"] = "recorded_trace"
            case["source_reference"] = trace
            case["replay_trace_reference"] = trace
        if case_id is not None:
            case["case_id"] = case_id
        if prompt is not None:
            case["prompt"] = prompt
        if repository is not None:
            case["repository"] = repository
        if base_commit is not None:
            case["base_commit"] = base_commit
        if validation_command is not None:
            case["validation"] = validation_command
    else:
        workload = _mapping(root.get("workload"), name="workload")
    if repetitions is not None:
        workload["repetitions"] = repetitions
    if session_assignment is not None:
        workload["session_assignment"] = session_assignment
    if validation_command is not None:
        validation = root.setdefault("validation", {})
        _mapping(validation, name="validation")["command"] = validation_command
    if concurrency is not None:
        execution["concurrency_levels"] = parse_concurrency(concurrency)
    selected = list(baseline_names)
    if selected:
        root["policies"] = [
            resolve_baseline(name).as_policy().model_dump(mode="json")
            for name in selected
        ]
    policies = root.get("policies", [] if selected_suite else None)
    if not isinstance(policies, list):
        raise ValueError("policies must be a YAML list")
    policy_mappings = [
        _mapping(item, name=f"policies[{index}]")
        for index, item in enumerate(policies)
    ]
    for policy in policy_mappings:
        if template_aware_placement is not None:
            policy["template_aware_placement"] = template_aware_placement
        if snapshot_aware_residency is not None:
            policy["snapshot_aware_residency"] = snapshot_aware_residency

    runtime_shape_changed = (
        (runtime_vcpu is not None and runtime_vcpu != runtime.get("vcpu"))
        or (
            runtime_memory_gib is not None
            and gib_to_mib(runtime_memory_gib, name="runtime memory")
            != runtime.get("memory_mib")
        )
    )
    tool_shape_changed = (
        (tool_vcpu is not None and tool_vcpu != tool.get("vcpu"))
        or (
            tool_memory_gib is not None
            and gib_to_mib(tool_memory_gib, name="tool memory")
            != tool.get("memory_mib")
        )
    )
    if runtime_shape_changed and runtime_template_id is None:
        raise ValueError("changing Runtime CPU or memory requires a new Runtime template")
    if tool_shape_changed and tool_template_id is None:
        raise ValueError("changing Tool CPU or memory requires a new Tool template")

    _set_template(
        runtime, label="Runtime", template_id=runtime_template_id,
        image_reference=runtime_image_reference, image_digest=runtime_image_digest,
    )
    _set_template(
        tool, label="Tool", template_id=tool_template_id,
        image_reference=tool_image_reference, image_digest=tool_image_digest,
    )
    if runtime_vcpu is not None:
        runtime["vcpu"] = runtime_vcpu
    if tool_vcpu is not None:
        tool["vcpu"] = tool_vcpu
    if runtime_memory_gib is not None:
        runtime["memory_mib"] = gib_to_mib(runtime_memory_gib, name="runtime memory")
    if tool_memory_gib is not None:
        tool["memory_mib"] = gib_to_mib(tool_memory_gib, name="tool memory")
    if tool_workspace is not None:
        if not tool_workspace.startswith('/'):
            raise ValueError('Tool workspace must be an absolute POSIX path')
        tool["workspace"] = tool_workspace

    if target_node is not None:
        resources["target_node"] = target_node
    if pool_memory_gib is not None:
        resources["pool_memory_budget_mib"] = gib_to_mib(
            pool_memory_gib, name="pool memory",
        )
    if emergency_free_memory_gib is not None:
        resources["emergency_free_memory_mib"] = gib_to_mib(
            emergency_free_memory_gib, name="emergency free memory",
        )
    if checkpoint_headroom_gib is not None:
        resources["checkpoint_restore_headroom_mib"] = gib_to_mib(
            checkpoint_headroom_gib, name="checkpoint headroom", allow_zero=True,
        )
    for key, value in (
        ("local_memory_capacity_mib", local_memory_capacity_mib),
        ("local_memory_low_watermark_mib", local_memory_low_watermark_mib),
        ("local_memory_high_watermark_mib", local_memory_high_watermark_mib),
        ("shared_memory_borrow_limit_mib", shared_memory_borrow_limit_mib),
        ("warm_memory_capacity_mib", warm_memory_capacity_mib),
        ("local_numa_node", local_numa_node),
        ("warm_numa_node", warm_numa_node),
    ):
        if value is not None:
            resources[key] = value
    for key, value in (
        ("local_memory_cgroup", local_memory_cgroup),
        ("warm_snapshot_root", warm_snapshot_root),
        ("cold_snapshot_root", cold_snapshot_root),
        ("snapshot_mechanism", snapshot_mechanism),
        ("snapshot_storage", snapshot_storage),
        ("memory_scheduling", memory_scheduling),
        ("template_base_sharing", template_base_sharing),
    ):
        if value is not None:
            resources[key] = value
    cost_values = (
        warm_restore_seconds_per_gib,
        cold_restore_seconds_per_gib,
        snapshot_cost_provenance,
    )
    if any(value is not None for value in cost_values):
        if not all(value is not None for value in cost_values):
            raise ValueError(
                "snapshot residency costs require warm, cold and provenance together"
            )
        resources["snapshot_residency_costs"] = {
            "provenance": snapshot_cost_provenance,
            "warm_restore_seconds_per_gib": warm_restore_seconds_per_gib,
            "cold_restore_seconds_per_gib": cold_restore_seconds_per_gib,
        }
    if prediction_artifact is not None:
        resources["prediction_artifact"] = prediction_artifact
    for key, value in (("prediction_mode", prediction_mode),
                       ("prediction_reservation_scale", prediction_reservation_scale),
                       ("admission_margin_mib", admission_margin_mib)):
        if value is not None:
            resources[key] = value
    if static_tool_memory_mib == "auto":
        cases = workload.get("cases")
        repositories = {
            item.get("repository") for item in cases or [] if isinstance(item, dict)
        }
        if len(repositories) != 1 or None in repositories:
            raise ValueError("automatic static calibration requires one workload repository")
        artifact = resources.get("prediction_artifact")
        if not artifact:
            raise ValueError(
                "automatic static calibration requires --prediction-artifact"
            )
        resources["static_tool_memory_mib"] = calibrated_static_tool_memory(
            Path(artifact), repositories.pop(),
        )
    elif static_tool_memory_mib is not None:
        resources["static_tool_memory_mib"] = static_tool_memory_mib

    if stagger_seconds is not None:
        if stagger_seconds <= 0:
            raise ValueError("stagger seconds must be positive")
        execution["arrival_schedule"] = "fixed_stagger"
        execution["stagger_interval_seconds"] = stagger_seconds
    if arrival_schedule is not None:
        execution["arrival_schedule"] = arrival_schedule
        if arrival_schedule == "burst":
            execution["stagger_interval_seconds"] = 0
        elif float(execution.get("stagger_interval_seconds", 0)) <= 0:
            execution["stagger_interval_seconds"] = 0.2
    if random_seed is not None:
        execution["random_seed"] = random_seed
    if arm_timeout_seconds is not None:
        execution["arm_timeout_seconds"] = arm_timeout_seconds
    if command_timeout_seconds is not None:
        execution["command_timeout_seconds"] = command_timeout_seconds
    if memory_sample_interval_seconds is not None:
        execution["memory_sample_interval_seconds"] = memory_sample_interval_seconds
    if stabilization_seconds is not None:
        execution["stabilization_seconds"] = stabilization_seconds

    selected_backend = inference_backend
    if selected_backend is None and (base_url is not None or api_key_env is not None):
        selected_backend = "api"
    if selected_backend is not None:
        inference["backend"] = selected_backend
        if selected_backend == "api":
            configuration.pop("time_scale", None)
        else:
            configuration.pop("base_url", None)
            configuration.pop("api_key_env", None)
    if time_scale is not None:
        if inference.get("backend") == "api":
            raise ValueError("time scale applies only to replay inference")
        if time_scale <= 0:
            raise ValueError("time scale must be positive")
        configuration["time_scale"] = time_scale
    if openclaw_exec_yield_ms is not None:
        configuration["openclaw_exec_yield_ms"] = openclaw_exec_yield_ms
    if model is not None:
        configuration["model"] = model
    if base_url is not None:
        configuration["base_url"] = base_url
    if api_key_env is not None:
        configuration["api_key_env"] = api_key_env

    if model_wait_prediction_seconds == "auto":
        if inference.get("backend") != "replay":
            raise ValueError("automatic model-wait prediction requires replay inference")
        trace_path = Path(str(workload.get("input") or ""))
        try:
            durations = [action.duration_s for action in load_trace(trace_path)]
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"cannot calibrate model wait from recorded trace {trace_path}: {exc}"
            ) from exc
        scale = float(configuration.get("time_scale", 1.0))
        configuration["model_wait_prediction_seconds"] = (
            statistics.median(durations) * scale
        )
        configuration["model_wait_prediction_source"] = (
            "recorded-trace-median-scaled:"
            + hashlib.sha256(trace_path.read_bytes()).hexdigest()
        )
    elif model_wait_prediction_seconds is not None:
        configuration["model_wait_prediction_seconds"] = model_wait_prediction_seconds
        if model_wait_prediction_source is not None:
            configuration["model_wait_prediction_source"] = model_wait_prediction_source
    elif model_wait_prediction_source is not None:
        configuration["model_wait_prediction_source"] = model_wait_prediction_source

    if selected_suite and resources.get("compute_nodes"):
        aggregate = sum(n["high_watermark_mib"] for n in resources["compute_nodes"])
        if pool_memory_gib is not None and gib_to_mib(pool_memory_gib, name="pool memory") != aggregate:
            raise ValueError("suite pool budget is derived from compute-node HIGH watermarks; edit the host budget and apply it")
        resources["pool_memory_budget_mib"] = aggregate
        resources["local_memory_capacity_mib"] = sum(n["memory_capacity_mib"] for n in resources["compute_nodes"])
        resources["local_memory_low_watermark_mib"] = sum(n["low_watermark_mib"] for n in resources["compute_nodes"])
        resources["local_memory_high_watermark_mib"] = resources["pool_memory_budget_mib"]
    if selected_suite:
        from .suites import expand_suite
        root = expand_suite(root, replace=True)
        policy_mappings = root["policies"]
    elif runtime_control is not None:
        for policy in policy_mappings:
            policy["runtime_control"] = runtime_control
    if any(item.get("eviction") == "wait_aware_pressure" for item in policy_mappings):
        predicted_wait = configuration.get("model_wait_prediction_seconds")
        prediction_source = str(
            configuration.get("model_wait_prediction_source") or ""
        ).strip()
        try:
            predicted_wait_value = float(predicted_wait)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "wait-aware baselines require --model-wait-prediction-seconds"
            ) from exc
        if not math.isfinite(predicted_wait_value) or predicted_wait_value <= 0:
            raise ValueError("model wait prediction must be positive")
        if not prediction_source:
            raise ValueError(
                "wait-aware baselines require --model-wait-prediction-source"
            )

    spec = ExperimentSpec.model_validate(root)
    ensure_supported_experiment(spec)
    return spec


def dump_experiment(spec: ExperimentSpec, *, compact: bool = False) -> str:
    # Omit defaults from the editable recipe. Frozen runs still serialize the
    # full resolved model, so every effective setting remains auditable.
    raw = spec.model_dump(mode="json", exclude_defaults=compact and bool(spec.suite))
    if compact and spec.suite:
        # An explicit frozen mode must survive compact serialization even though
        # it is the custom-recipe default; the admission suite defaults to runtime.
        raw["resources"]["prediction_mode"] = spec.resources.prediction_mode
        raw.pop("policies", None)
        # These are suite rules or aggregates, not independent user choices.
        for key in ("placement_policy", "session_compute_nodes"):
            raw.get("execution", {}).pop(key, None)
        for key in ("memory_scheduling", "template_base_sharing", "memory_action_costs",
                    "lifecycle_admission", "proactive_cache_reclaim"):
            raw["resources"].pop(key, None)
        if spec.resources.compute_nodes:
            for key in ("pool_memory_budget_mib", "local_memory_capacity_mib",
                        "local_memory_low_watermark_mib", "local_memory_high_watermark_mib"):
                raw["resources"].pop(key, None)
    return yaml.safe_dump(
        raw, sort_keys=False, allow_unicode=True,
    )


def _p50_admission_summary(spec: ExperimentSpec) -> dict[str, Any] | None:
    if not any(policy.admission.value == "predicted" for policy in spec.policies):
        return None
    artifact = spec.resources.prediction_artifact
    if artifact is None:
        return {"error": "prediction artifact is not configured"}
    if spec.resources.prediction_mode == "runtime":
        return {"path": str(artifact), "mode": "runtime",
                "kb_path": str(Path(artifact).with_suffix(".kb.json")),
                "reservation_scale": spec.resources.prediction_reservation_scale}
    try:
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(artifact), "error": str(exc)}
    values: list[float] = []
    sources: dict[str, int] = {}
    calibration = payload.get("host_calibration") or {}
    if calibration.get("target") != "host_cgroup_memory_current_increment":
        return {"path": str(artifact), "error": "missing host cgroup calibration"}
    for row in payload.get("commands") or []:
        if not isinstance(row, dict) or not isinstance(row.get("selection"), dict):
            continue
        selection = row["selection"]
        value = selection.get("predicted_incremental_memory_mib")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and float(value) >= 0):
            values.append(float(value) * float(calibration["scale"])
                          + float(calibration["offset_bytes"]) / 1024**2)
            source = str(selection.get("fallback_level") or "unknown")
            sources[source] = sources.get(source, 0) + 1
    if not values:
        return {"path": str(artifact), "error": "no usable command reservations"}
    return {
        "path": str(artifact),
        "entry_count": len(values),
        "fallback_levels": sources,
        "min_mib": min(values),
        "median_mib": statistics.median(values),
        "max_mib": max(values),
    }


def experiment_overview(spec: ExperimentSpec) -> dict[str, Any]:
    runtime_mib = spec.runtime.memory_mib
    tool_mib = spec.sandbox.memory_mib
    pair_mib = runtime_mib + tool_mib
    pool_mib = spec.resources.pool_memory_budget_mib
    cases = load_workload_cases(spec.workload)
    shapes = {case.case_id: case.sandbox or spec.sandbox for case in cases}
    heterogeneous = len({(shape.vcpu, shape.memory_mib) for shape in shapes.values()}) > 1
    concurrency = []
    for level in spec.execution.concurrency_levels:
        groups = ([tuple(cases[i % len(cases)] for i in range(level))]
                  if spec.workload.session_assignment.value == "round_robin"
                  else [(case,) * level for case in cases])
        for assigned in groups:
            tools = [shapes[case.case_id] for case in assigned]
            offered_mib = level * runtime_mib + sum(t.memory_mib for t in tools)
            concurrency.append({
                "case_ids": list(dict.fromkeys(case.case_id for case in assigned)),
                "agents": level,
                "vm_count": level * 2,
                "offered_vcpu": level * spec.runtime.vcpu + sum(t.vcpu for t in tools),
                "runtime_memory_gib": level * runtime_mib / 1024,
                "tool_memory_gib": sum(t.memory_mib for t in tools) / 1024,
                "offered_pair_memory_gib": offered_mib / 1024,
                "pool_memory_gib": pool_mib / 1024,
                "offered_to_pool_ratio": offered_mib / pool_mib,
                "offered_provisioned_capacity_exceeds_policy_budget": offered_mib > pool_mib,
            })
    effective_resources = {
        arm.policy.name: {
            "placement_policy": arm.execution.placement_policy,
            "lifecycle_admission": arm.resources.lifecycle_admission,
            "proactive_cache_reclaim": arm.resources.proactive_cache_reclaim,
            "runtime_control": arm.policy.runtime_control.value,
            "physical_memory_gib": (arm.resources.local_memory_capacity_mib or arm.resources.pool_memory_budget_mib) / 1024,
            "pool_memory_gib": arm.resources.pool_memory_budget_mib / 1024,
            "shared_pool_gib": arm.resources.warm_memory_capacity_mib / 1024,
            "snapshot_enabled": (
                arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
            ),
        } for arm in expand_matrix(spec)
    }
    node_budgets = [n.high_watermark_mib for n in spec.resources.compute_nodes] or [pool_mib]
    usable_node_reservations = [
        max(0, budget - spec.resources.checkpoint_restore_headroom_mib) for budget in node_budgets
    ]
    usable_reservation_mib = sum(usable_node_reservations)
    p50_summary = _p50_admission_summary(spec)
    static_mib = spec.resources.static_tool_memory_mib
    admission: dict[str, Any] = {
        "prediction_mode": spec.resources.prediction_mode,
        "prediction_reservation_scale": spec.resources.prediction_reservation_scale,
        "margin_mib": spec.resources.admission_margin_mib,
        "physical_local_capacity_gib": (
            spec.resources.local_memory_capacity_mib / 1024
            if spec.resources.local_memory_capacity_mib is not None else None
        ),
        "policy_budget_gib": pool_mib / 1024,
        "local_low_watermark_gib": (
            spec.resources.local_memory_low_watermark_mib / 1024
            if spec.resources.local_memory_low_watermark_mib is not None else None
        ),
        "local_high_watermark_gib": (
            spec.resources.local_memory_high_watermark_mib / 1024
            if spec.resources.local_memory_high_watermark_mib is not None else None
        ),
        "shared_pool_capacity_gib": spec.resources.warm_memory_capacity_mib / 1024,
        "shared_live_borrow_limit_gib": (
            spec.resources.shared_memory_borrow_limit_mib / 1024
            if spec.resources.shared_memory_borrow_limit_mib is not None else None
        ),
        "combined_live_cgroup_limit_gib": (
            (spec.resources.local_memory_capacity_mib
             + (spec.resources.shared_memory_borrow_limit_mib or 0)) / 1024
            if spec.resources.local_memory_capacity_mib is not None
            else None
        ),
        "available_after_headroom_gib": usable_reservation_mib / 1024,
        "reservation_capacity_excludes_measured_resident_memory": True,
        "static": {
            "per_command_mib": static_mib,
            "reservation_only_slots": (sum(budget // static_mib for budget in usable_node_reservations)
                                       if static_mib else None),
        },
        "p50": p50_summary,
    }
    if p50_summary is not None and "max_mib" in p50_summary:
        maximum = p50_summary["max_mib"]
        p50_summary["reservation_only_slots_at_max"] = (
            sum(math.floor(budget / maximum) for budget in usable_node_reservations) if maximum else None
        )
    return {
        "lifecycle_admission": spec.resources.lifecycle_admission,
        "proactive_cache_reclaim": spec.resources.proactive_cache_reclaim,
        "effective_policy_resources": effective_resources,
        "compute_nodes": [node.model_dump(mode="json") for node in spec.resources.compute_nodes],
        "placement_policy": spec.execution.placement_policy,
        "template_balance_slack": spec.execution.template_balance_slack,
        "template_base_sharing": spec.resources.template_base_sharing,
        "snapshot_residency_costs": (
            spec.resources.snapshot_residency_costs.model_dump(mode="json")
            if spec.resources.snapshot_residency_costs else None
        ),
        "memory_scheduling": spec.resources.memory_scheduling,
        "pool_transfer_concurrency": spec.resources.pool_transfer_concurrency,
        "borrow_cooldown_seconds": spec.resources.borrow_cooldown_seconds,
        "return_cooldown_seconds": spec.resources.return_cooldown_seconds,
        "memory_action_costs": (
            spec.resources.memory_action_costs.model_dump(mode="json")
            if spec.resources.memory_action_costs is not None else None
        ),
        "session_compute_nodes": list(spec.execution.session_compute_nodes),
        "experiment_id": spec.experiment_id,
        "agent_driver": spec.agent.driver.value,
        "inference_backend": spec.inference.backend.value,
        "inference_configuration": {
            key: value
            for key, value in spec.inference.configuration.items()
            if key != "api_key"
        },
        "runtime": {
            "template": spec.runtime.template,
            "vcpu": spec.runtime.vcpu,
            "memory_gib": runtime_mib / 1024,
        },
        "tool": {
            "template": spec.sandbox.template,
            "vcpu": spec.sandbox.vcpu,
            "memory_gib": tool_mib / 1024,
        },
        "pair_memory_gib": None if heterogeneous else (runtime_mib + next(iter(shapes.values())).memory_mib) / 1024,
        "heterogeneous_tool_shapes": heterogeneous,
        "suite": spec.suite,
        "suite_ablations": spec.suite_ablations,
        "warnings": (["Measured COLD restore is no slower than WARM; all snapshot retention values are zero. This is a negative-result condition, not evidence of a WARM benefit."]
                     if spec.resources.snapshot_residency_costs and
                     spec.resources.snapshot_residency_costs.cold_restore_seconds_per_gib <= spec.resources.snapshot_residency_costs.warm_restore_seconds_per_gib
                     and any(p.snapshot_aware_residency for p in spec.policies) else []),
        "concurrency": concurrency,
        "policies": [policy.model_dump(mode="json") for policy in spec.policies],
        "arm_count": len(expand_matrix(spec)),
        "execution": {
            "arrival_schedule": spec.execution.arrival_schedule.value,
            "stagger_interval_seconds": spec.execution.stagger_interval_seconds,
            "random_seed": spec.execution.random_seed,
            "arm_timeout_seconds": spec.execution.arm_timeout_seconds,
            "command_timeout_seconds": spec.execution.command_timeout_seconds,
            "memory_sample_interval_seconds": (
                spec.execution.memory_sample_interval_seconds
            ),
            "stabilization_seconds": spec.execution.stabilization_seconds,
        },
        "admission": admission,
        "safety": {
            "emergency_free_memory_gib": (
                spec.resources.emergency_free_memory_mib / 1024
            ),
            "checkpoint_restore_headroom_gib": (
                spec.resources.checkpoint_restore_headroom_mib / 1024
            ),
        },
    }
