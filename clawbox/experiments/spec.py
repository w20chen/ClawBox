"""Typed experiment schema and expansion into independently inspectable workflows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .spec_types import (
    AdmissionPolicy, AgentDriver, InferenceBackend, ResidencyPolicy, SandboxBackend,
    ToolTransport, WorkloadSource,
)


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkloadCase(StrictFrozenModel):
    case_id: str = Field(min_length=1)
    prompt: str = ""
    source: WorkloadSource
    source_reference: str = Field(min_length=1)
    repository: str | None = None
    base_commit: str | None = None
    replay_trace_reference: str | None = None
    validation: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkloadSpec(StrictFrozenModel):
    source: WorkloadSource
    input: str = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1)
    cases: tuple[WorkloadCase, ...] = ()

    @model_validator(mode="after")
    def cases_match_source(self) -> "WorkloadSpec":
        if mismatched := [case.case_id for case in self.cases if case.source is not self.source]:
            raise ValueError("workload.cases source mismatch: " + ", ".join(mismatched))
        return self


class AgentSpec(StrictFrozenModel):
    driver: AgentDriver


class InferenceSpec(StrictFrozenModel):
    backends: tuple[InferenceBackend, ...] = (InferenceBackend.API,)
    configuration: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("backends")
    @classmethod
    def unique_nonempty(cls, value: tuple[InferenceBackend, ...]) -> tuple[InferenceBackend, ...]:
        if not value:
            raise ValueError("inference.backends must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("inference.backends must not contain duplicates")
        return value

    @field_validator("configuration")
    @classmethod
    def known_configuration_keys(
        cls, value: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        known = {item.value for item in InferenceBackend}
        if unknown := sorted(set(value) - known):
            raise ValueError("unknown inference.configuration keys: " + ", ".join(unknown))
        return value


class SandboxSpec(StrictFrozenModel):
    backend: SandboxBackend
    tool_transport: ToolTransport
    runtime_image: str | None = None
    tool_image: str | None = None
    materialization: dict[str, Any] = Field(default_factory=dict)


class SchedulingSpec(StrictFrozenModel):
    baselines: tuple[str, ...] = ("fixed-resident",)

    @field_validator("baselines")
    @classmethod
    def unique_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("scheduling.baselines must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("scheduling.baselines must not contain duplicates")
        return value


class ExecutionSpec(StrictFrozenModel):
    concurrency: int = Field(default=1, ge=1)
    timeout_seconds: int = Field(default=1800, ge=1)
    command_timeout_seconds: int = Field(default=300, ge=1)


class ResourcesSpec(StrictFrozenModel):
    profile: str | None = Field(default=None, min_length=1)
    runtime_memory_mib: int | None = Field(default=None, ge=1)
    tool_memory_mib: int | None = Field(default=None, ge=1)
    cpu_first: int | None = Field(default=None, ge=0)
    numa_node: int | None = Field(default=None, ge=0)


class ValidationSpec(StrictFrozenModel):
    command: str | None = None
    specification: dict[str, Any] = Field(default_factory=dict)


class OutputSpec(StrictFrozenModel):
    directory: str | None = None


class ExperimentSpec(StrictFrozenModel):
    schema_version: int = Field(default=1, ge=1)
    workload: WorkloadSpec
    agent: AgentSpec
    inference: InferenceSpec
    sandbox: SandboxSpec
    scheduling: SchedulingSpec = Field(default_factory=SchedulingSpec)
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    output: OutputSpec = Field(default_factory=OutputSpec)


class ResolvedWorkflow(StrictFrozenModel):
    """One immutable point in an experiment matrix, including every axis."""

    schema_version: int = 1
    workload: WorkloadSpec
    agent_driver: AgentDriver
    inference_backend: InferenceBackend
    inference_configuration: dict[str, Any] = Field(default_factory=dict)
    sandbox_backend: SandboxBackend
    tool_transport: ToolTransport
    admission_policy: AdmissionPolicy
    residency_policy: ResidencyPolicy
    baseline: str
    execution: ExecutionSpec
    resources: ResourcesSpec
    validation: ValidationSpec
    output: OutputSpec
    sandbox: SandboxSpec


def expand_matrix(spec: ExperimentSpec, *, validate: bool = True) -> tuple[ResolvedWorkflow, ...]:
    """Cross selected inference backends and baselines without hidden axis changes."""
    from .baselines import resolve_baseline
    from .capabilities import require_capability

    workflows = []
    for inference_backend in spec.inference.backends:
        for baseline_name in spec.scheduling.baselines:
            baseline = resolve_baseline(baseline_name)
            workflow = ResolvedWorkflow(
                workload=spec.workload, agent_driver=spec.agent.driver,
                inference_backend=inference_backend, sandbox_backend=spec.sandbox.backend,
                inference_configuration=spec.inference.configuration.get(
                    inference_backend.value, {},
                ),
                tool_transport=spec.sandbox.tool_transport,
                admission_policy=baseline.admission_policy, residency_policy=baseline.residency_policy,
                baseline=baseline.name, execution=spec.execution, resources=spec.resources,
                validation=spec.validation, output=spec.output, sandbox=spec.sandbox,
            )
            if validate:
                require_capability(workflow)
            workflows.append(workflow)
    return tuple(workflows)


def load_workload_cases(workload: WorkloadSpec) -> tuple[WorkloadCase, ...]:
    """Materialize logical cases without leaking sandbox images or rootfs paths.

    Explicit ``workload.cases`` are already canonical. The small built-in
    readers make source files inspectable for planning and tests; runner
    adapters may still retain richer source-specific input formats.
    """
    if workload.cases:
        return workload.cases
    path = Path(workload.input)
    if workload.source is WorkloadSource.RECORDED_TRACE:
        return (WorkloadCase(case_id=path.stem or "recorded-trace", source=workload.source,
                             source_reference=str(path), replay_trace_reference=str(path)),)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read workload input {path}: {exc}") from exc
    if workload.source is WorkloadSource.SWE_REBENCH:
        records = raw if isinstance(raw, list) else raw.get("tasks", raw.get("instances", []))
        if not isinstance(records, list):
            raise ValueError("swe_rebench workload input must contain a task list")
        cases = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("every swe_rebench workload record must be an object")
            case_id = str(record.get("instance_id") or record.get("task_id") or record.get("id") or "").strip()
            prompt = str(record.get("problem_statement") or record.get("problem") or record.get("prompt") or "")
            if not case_id or not prompt:
                raise ValueError("every swe_rebench workload record needs an id and prompt")
            cases.append(WorkloadCase(
                case_id=case_id, prompt=prompt, source=workload.source, source_reference=str(path),
                repository=str(record.get("repo") or record.get("repository") or "") or None,
                base_commit=str(record.get("base_commit") or "") or None,
                validation=record.get("validation_command"), metadata={
                    key: value for key, value in record.items()
                    if key not in {"instance_id", "task_id", "id", "problem_statement", "problem", "prompt",
                                   "repo", "repository", "base_commit", "validation_command"}
                },
            ))
        return tuple(cases)
    records = raw if isinstance(raw, list) else raw.get("cases", [])
    if not isinstance(records, list):
        raise ValueError("synthetic workload input must contain a cases list")
    cases = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("every synthetic workload record must be an object")
        case_id = str(record.get("case_id") or record.get("id") or "").strip()
        if not case_id:
            raise ValueError("every synthetic workload record needs case_id")
        cases.append(WorkloadCase(
            case_id=case_id, prompt=str(record.get("prompt") or ""), source=workload.source,
            source_reference=str(path), validation=record.get("validation"),
            metadata=dict(record.get("metadata") or {}),
        ))
    return tuple(cases)


def load_experiment(path: Path) -> ExperimentSpec:
    return ExperimentSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))
