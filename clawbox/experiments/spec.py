"""Strict schema-v2 experiment specification and deterministic arm planning."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .spec_types import (
    AdmissionPolicy, AgentDriver, EvictionPolicy, InferenceBackend,
    ReclamationPolicy, RestorePolicy, WorkloadSource,
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
    driver: AgentDriver = AgentDriver.OPENCLAW


class InferenceSpec(StrictFrozenModel):
    backend: InferenceBackend = InferenceBackend.REPLAY
    configuration: dict[str, Any] = Field(default_factory=dict)


class SandboxSpec(StrictFrozenModel):
    template_id: str | None = Field(default=None, min_length=1)
    template_alias: str | None = Field(default=None, min_length=1)
    source_image_reference: str | None = Field(default=None, min_length=1)
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    architecture: str = Field(default="arm64", pattern=r"^arm64$")
    vcpu: int = Field(default=2, ge=1)
    memory_mib: int = Field(default=4096, ge=128)
    workspace: str = Field(default="/workspace", pattern=r"^/")
    allow_internet_access: bool = True

    @model_validator(mode="after")
    def exactly_one_template_reference(self) -> "SandboxSpec":
        if (self.template_id is None) == (self.template_alias is None):
            raise ValueError("sandbox requires exactly one of template_id or template_alias")
        return self

    @property
    def template(self) -> str:
        return self.template_id or self.template_alias or ""


class ExecutionSpec(StrictFrozenModel):
    concurrency_levels: tuple[int, ...] = (1,)
    randomized_order: bool = True
    random_seed: int = 0
    arm_timeout_seconds: int = Field(default=1800, ge=1)
    command_timeout_seconds: int = Field(default=300, ge=1)
    memory_sample_interval_seconds: float = Field(default=0.2, gt=0)
    stabilization_seconds: float = Field(default=1.0, ge=0)

    @field_validator("concurrency_levels")
    @classmethod
    def valid_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(level < 1 for level in value):
            raise ValueError("execution.concurrency_levels must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("execution.concurrency_levels must not contain duplicates")
        return value


class ResourcesSpec(StrictFrozenModel):
    target_node: str = Field(min_length=1)
    pool_memory_budget_mib: int = Field(ge=1)
    emergency_free_memory_mib: int = Field(ge=1)
    checkpoint_restore_headroom_mib: int = Field(default=1024, ge=0)
    static_tool_memory_mib: int | None = Field(default=None, ge=1)
    full_tool_memory_mib: int | None = Field(default=None, ge=1)
    p90_predictions: str | None = None
    oracle_measurements: str | None = None


class PolicySpec(StrictFrozenModel):
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    admission: AdmissionPolicy
    reclamation: ReclamationPolicy
    eviction: EvictionPolicy
    restore: RestorePolicy
    fixed_delay_seconds: float | None = Field(default=None, ge=0)
    prefetch_lead_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_tuple(self) -> "PolicySpec":
        if self.reclamation is ReclamationPolicy.RESIDENT:
            if self.eviction is not EvictionPolicy.NONE or self.restore is not RestorePolicy.NONE:
                raise ValueError("resident requires eviction=none and restore=none")
        elif self.eviction is EvictionPolicy.NONE or self.restore is RestorePolicy.NONE:
            raise ValueError("snapshot_pause requires an eviction and restore policy")
        if (self.eviction is EvictionPolicy.FIXED_DELAY) != (self.fixed_delay_seconds is not None):
            raise ValueError("fixed_delay_seconds is required only for eviction=fixed_delay")
        if (self.restore is RestorePolicy.PROACTIVE) != (self.prefetch_lead_seconds is not None):
            raise ValueError("prefetch_lead_seconds is required only for restore=proactive")
        return self


class ValidationSpec(StrictFrozenModel):
    command: str | None = None


class OutputSpec(StrictFrozenModel):
    directory: str = Field(default="/results", min_length=1)


class ExperimentSpec(StrictFrozenModel):
    schema_version: int
    experiment_id: str = Field(default="experiment", min_length=1)
    workload: WorkloadSpec
    agent: AgentSpec = Field(default_factory=AgentSpec)
    inference: InferenceSpec = Field(default_factory=InferenceSpec)
    runtime: SandboxSpec
    sandbox: SandboxSpec
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    resources: ResourcesSpec
    policies: tuple[PolicySpec, ...]
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    output: OutputSpec = Field(default_factory=OutputSpec)

    @model_validator(mode="after")
    def validate_v2(self) -> "ExperimentSpec":
        if self.schema_version != 2:
            raise ValueError("only schema_version: 2 is supported; migrate schema v1")
        if not self.policies:
            raise ValueError("policies must not be empty")
        if self.agent.driver is AgentDriver.OPENCLAW:
            if self.inference.backend is not InferenceBackend.API:
                raise ValueError("agent.driver=openclaw currently requires inference.backend=api")
            if "api_key" in self.inference.configuration:
                raise ValueError("model credentials must be injected from a Kubernetes Secret")
        names = [policy.name for policy in self.policies]
        if len(names) != len(set(names)):
            raise ValueError("policy names must be unique")
        admissions = {policy.admission for policy in self.policies}
        if AdmissionPolicy.TOOL_ORACLE in admissions:
            if self.inference.backend is not InferenceBackend.REPLAY:
                raise ValueError("tool_oracle is evaluation-only and requires inference.backend=replay")
            if not self.resources.oracle_measurements:
                raise ValueError("tool_oracle requires resources.oracle_measurements")
        required = {
            AdmissionPolicy.TOOL_STATIC: (self.resources.static_tool_memory_mib, "static_tool_memory_mib"),
            AdmissionPolicy.TOOL_FULL: (self.resources.full_tool_memory_mib, "full_tool_memory_mib"),
            AdmissionPolicy.TOOL_P90: (self.resources.p90_predictions, "p90_predictions"),
        }
        for policy, (value, field) in required.items():
            if policy in admissions and value is None:
                raise ValueError(f"{policy.value} requires resources.{field}")
        return self


class ExperimentArm(StrictFrozenModel):
    arm_id: str
    spec_digest: str
    case: WorkloadCase
    repetition: int
    concurrency: int
    policy: PolicySpec
    agent: AgentSpec
    inference: InferenceSpec
    runtime: SandboxSpec
    sandbox: SandboxSpec
    execution: ExecutionSpec
    resources: ResourcesSpec
    validation: ValidationSpec


def canonical_json(value: BaseModel | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_digest(spec: ExperimentSpec) -> str:
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


def expand_matrix(spec: ExperimentSpec) -> tuple[ExperimentArm, ...]:
    digest = spec_digest(spec)
    arms: list[ExperimentArm] = []
    for case in load_workload_cases(spec.workload):
        for repetition in range(spec.workload.repetitions):
            for concurrency in spec.execution.concurrency_levels:
                for policy in spec.policies:
                    identity = {"spec_digest": digest, "case_id": case.case_id,
                                "repetition": repetition, "concurrency": concurrency,
                                "policy": policy.model_dump(mode="json")}
                    arm_id = hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24]
                    arms.append(ExperimentArm(
                        arm_id=arm_id, spec_digest=digest, case=case,
                        repetition=repetition, concurrency=concurrency, policy=policy,
                        agent=spec.agent, inference=spec.inference, runtime=spec.runtime,
                        sandbox=spec.sandbox,
                        execution=spec.execution, resources=spec.resources,
                        validation=spec.validation,
                    ))
    if spec.execution.randomized_order:
        random.Random(spec.execution.random_seed).shuffle(arms)
    return tuple(arms)


def load_workload_cases(workload: WorkloadSpec) -> tuple[WorkloadCase, ...]:
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
    records = raw if isinstance(raw, list) else raw.get(
        "tasks" if workload.source is WorkloadSource.SWE_REBENCH else "cases", [])
    if not isinstance(records, list):
        raise ValueError("workload input must contain a list")
    cases: list[WorkloadCase] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("every workload record must be an object")
        case_id = str(record.get("instance_id") or record.get("task_id") or record.get("case_id") or record.get("id") or "").strip()
        if not case_id:
            raise ValueError("every workload record needs an id")
        cases.append(WorkloadCase(
            case_id=case_id,
            prompt=str(record.get("problem_statement") or record.get("problem") or record.get("prompt") or ""),
            source=workload.source, source_reference=str(path),
            repository=str(record.get("repo") or record.get("repository") or "") or None,
            base_commit=str(record.get("base_commit") or "") or None,
            validation=record.get("validation_command") or record.get("validation"),
            metadata={key: value for key, value in record.items() if key not in {
                "instance_id", "task_id", "case_id", "id", "problem_statement", "problem",
                "prompt", "repo", "repository", "base_commit", "validation_command", "validation"}},
        ))
    return tuple(cases)


def load_experiment(path: Path) -> ExperimentSpec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read experiment {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("experiment document must be an object")
    if raw.get("schema_version") != 2:
        raise ValueError("only schema_version: 2 is supported; migrate schema v1")
    return ExperimentSpec.model_validate(raw)
