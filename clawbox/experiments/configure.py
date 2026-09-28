"""User-facing helpers for configuring schema-v2 experiments.

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
from .spec import ExperimentSpec, ReclamationPolicy, expand_matrix


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
    if not isinstance(payload, dict) or payload.get("schema") != "clawbox_p50_v1":
        raise ValueError("automatic static calibration requires a clawbox_p50_v1 artifact")
    if payload.get("repository") != repository or payload.get("training_validated") is not True:
        raise ValueError("automatic static calibration requires matching validated training")
    calibration = payload.get("static_tool_memory_calibration")
    if not isinstance(calibration, dict):
        raise ValueError(
            "prediction artifact has no static Tool-memory calibration; rerun experiment train"
        )
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
    static_tool_memory_mib: int | str | None = None,
    full_tool_memory_mib: int | None = None,
    prediction_artifact: str | None = None,
    oracle_measurements: str | None = None,
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
    fixed_delay_seconds: float | None = None,
    prefetch_lead_seconds: float | None = None,
    checkpoint_break_even_seconds: float | None = None,
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
    runtime = _mapping(root.get("runtime"), name="runtime")
    tool = _mapping(root.get("sandbox"), name="sandbox")
    execution = _mapping(root.get("execution"), name="execution")
    resources = _mapping(root.get("resources"), name="resources")
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
    policies = root.get("policies")
    if not isinstance(policies, list):
        raise ValueError("policies must be a YAML list")
    policy_mappings = [
        _mapping(item, name=f"policies[{index}]")
        for index, item in enumerate(policies)
    ]
    if fixed_delay_seconds is not None:
        matching = [
            item for item in policy_mappings if item.get("eviction") == "fixed_delay"
        ]
        if not matching:
            raise ValueError("fixed delay was set but no fixed-delay baseline is selected")
        for item in matching:
            item["fixed_delay_seconds"] = fixed_delay_seconds
    if prefetch_lead_seconds is not None:
        matching = [
            item for item in policy_mappings if item.get("restore") == "proactive"
        ]
        if not matching:
            raise ValueError("prefetch lead was set but no proactive baseline is selected")
        for item in matching:
            item["prefetch_lead_seconds"] = prefetch_lead_seconds
    if checkpoint_break_even_seconds is not None:
        matching = [
            item for item in policy_mappings if item.get("eviction") == "time_oracle"
        ]
        if not matching:
            raise ValueError(
                "checkpoint break-even was set but no time-oracle baseline is selected"
            )
        for item in matching:
            item["checkpoint_break_even_seconds"] = checkpoint_break_even_seconds

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
    ):
        if value is not None:
            resources[key] = value
    if full_tool_memory_mib is not None:
        resources["full_tool_memory_mib"] = full_tool_memory_mib
    if prediction_artifact is not None:
        resources["prediction_artifact"] = prediction_artifact
    if oracle_measurements is not None:
        resources["oracle_measurements"] = oracle_measurements
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


def dump_experiment(spec: ExperimentSpec) -> str:
    return yaml.safe_dump(
        spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True,
    )


def _p50_admission_summary(spec: ExperimentSpec) -> dict[str, Any] | None:
    if not any(policy.admission.value == "tool_p50" for policy in spec.policies):
        return None
    artifact = spec.resources.prediction_artifact
    if artifact is None:
        return {"error": "prediction artifact is not configured"}
    try:
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(artifact), "error": str(exc)}
    values: list[float] = []
    sources: dict[str, int] = {}
    for row in payload.get("commands") or []:
        if not isinstance(row, dict) or not isinstance(row.get("selection"), dict):
            continue
        selection = row["selection"]
        value = selection.get("predicted_incremental_memory_mib")
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and float(value) >= 0):
            values.append(float(value))
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
    concurrency = []
    for level in spec.execution.concurrency_levels:
        offered_mib = level * pair_mib
        concurrency.append({
            "agents": level,
            "vm_count": level * 2,
            "offered_vcpu": level * (spec.runtime.vcpu + spec.sandbox.vcpu),
            "runtime_memory_gib": level * runtime_mib / 1024,
            "tool_memory_gib": level * tool_mib / 1024,
            "offered_pair_memory_gib": offered_mib / 1024,
            "pool_memory_gib": pool_mib / 1024,
            "offered_to_pool_ratio": offered_mib / pool_mib,
            "memory_overcommit": offered_mib > pool_mib,
        })
    effective_resources = {
        arm.policy.name: {
            "pool_memory_gib": arm.resources.pool_memory_budget_mib / 1024,
            "shared_pool_gib": arm.resources.warm_memory_capacity_mib / 1024,
            "snapshot_enabled": (
                arm.policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
            ),
        } for arm in expand_matrix(spec)
    }
    usable_reservation_mib = max(
        0, pool_mib - spec.resources.checkpoint_restore_headroom_mib,
    )
    p50_summary = _p50_admission_summary(spec)
    static_mib = spec.resources.static_tool_memory_mib
    admission: dict[str, Any] = {
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
             + spec.resources.shared_memory_borrow_limit_mib) / 1024
            if spec.resources.local_memory_capacity_mib is not None
            and spec.resources.shared_memory_borrow_limit_mib is not None else None
        ),
        "available_after_headroom_gib": usable_reservation_mib / 1024,
        "reservation_capacity_excludes_measured_resident_memory": True,
        "static": {
            "per_command_mib": static_mib,
            "reservation_only_slots": usable_reservation_mib // static_mib,
        },
        "p50": p50_summary,
        "full_tool_memory_mib": spec.resources.full_tool_memory_mib,
        "oracle_measurements": spec.resources.oracle_measurements,
    }
    if p50_summary is not None and "max_mib" in p50_summary:
        maximum = p50_summary["max_mib"]
        p50_summary["reservation_only_slots_at_max"] = (
            math.floor(usable_reservation_mib / maximum) if maximum else None
        )
    return {
        "effective_policy_resources": effective_resources,
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
        "pair_memory_gib": pair_mib / 1024,
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
