"""Strict schema-v3 experiment specification and deterministic arm planning."""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Literal, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .spec_types import (
    AdmissionPolicy, AgentDriver, ArrivalSchedule, EvictionPolicy, InferenceBackend,
    ReclamationPolicy, RestorePolicy, RuntimeControl, SessionAssignment, SnapshotTier, WorkloadSource,
)
from .topology import ComputeNode, validate_compute_nodes


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryActionCostSpec(StrictFrozenModel):
    """Empirical costs used to compare live borrowing with WARM eviction.

    The rates are workload- and host-specific measurements over configured VM
    GiB.  They intentionally do not derive an application slowdown from a raw
    NUMA latency or bandwidth number.
    """

    provenance: str = Field(min_length=1)
    live_borrow_service_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    live_borrow_runtime_penalty_seconds_per_gib: float = Field(
        ge=0, allow_inf_nan=False,
    )
    warm_checkpoint_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    warm_restore_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    cold_checkpoint_seconds_per_gib: float | None = Field(
        default=None, gt=0, allow_inf_nan=False,
    )
    cold_restore_seconds_per_gib: float | None = Field(
        default=None, gt=0, allow_inf_nan=False,
    )
    expected_restore_probability: float = Field(ge=0, le=1, allow_inf_nan=False)
    remote_access_latency_ns: float | None = Field(
        default=None, gt=0, allow_inf_nan=False,
    )
    remote_stream_bandwidth_mib_per_second: float | None = Field(
        default=None, gt=0, allow_inf_nan=False,
    )


class SnapshotResidencyCostSpec(StrictFrozenModel):
    """Measured restore costs used by the WARM/SSD residency policy."""

    provenance: str = Field(min_length=1)
    warm_restore_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    cold_restore_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)


class IdleReclaimCostSpec(StrictFrozenModel):
    provenance: str = Field(min_length=1)
    warm_checkpoint_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    warm_restore_seconds_per_gib: float = Field(gt=0, allow_inf_nan=False)
    cold_checkpoint_seconds_per_gib: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cold_restore_seconds_per_gib: float | None = Field(default=None, gt=0, allow_inf_nan=False)

class WorkloadCase(StrictFrozenModel):
    case_id: str = Field(min_length=1)
    prompt: str = ""
    source: WorkloadSource
    source_reference: str = Field(min_length=1)
    repository: str | None = None
    base_commit: str | None = None
    replay_trace_reference: str | None = None
    validation: str | dict[str, Any] | None = None
    sandbox: "SandboxSpec | None" = None
    snapshot_reuse_probability: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkloadSpec(StrictFrozenModel):
    source: WorkloadSource
    input: str = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1)
    cases: tuple[WorkloadCase, ...] = ()
    session_assignment: SessionAssignment = SessionAssignment.SINGLE_CASE

    @model_validator(mode="after")
    def cases_match_source(self) -> "WorkloadSpec":
        if mismatched := [case.case_id for case in self.cases if case.source is not self.source]:
            raise ValueError("workload.cases source mismatch: " + ", ".join(mismatched))
        if self.session_assignment is SessionAssignment.ROUND_ROBIN and len(self.cases) < 2:
            raise ValueError("round_robin session assignment requires at least two cases")
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
    preflight_command: str | None = Field(default=None, min_length=1)
    allow_internet_access: bool = True

    @model_validator(mode="after")
    def exactly_one_template_reference(self) -> "SandboxSpec":
        if (self.template_id is None) == (self.template_alias is None):
            raise ValueError("sandbox requires exactly one of template_id or template_alias")
        return self

    @property
    def template(self) -> str:
        return self.template_id or self.template_alias or ""


# Resolve the per-case Tool VM override after SandboxSpec exists.  WorkloadCase
# intentionally appears first in the public YAML model so existing documents
# retain their field order.
WorkloadCase.model_rebuild()


class ExecutionSpec(StrictFrozenModel):
    placement_policy: Literal["os_default", "round_robin", "load_aware", "explicit"] = "round_robin"
    placement_cpu_overcommit: float = Field(default=8.0, ge=1, allow_inf_nan=False)
    template_balance_slack: float = Field(default=0.10, ge=0, le=1, allow_inf_nan=False)
    session_compute_nodes: tuple[str, ...] = ()
    concurrency_levels: tuple[int, ...] = (1,)
    randomized_order: bool = True
    random_seed: int = 0
    # Every supported run is supervised at the process boundary.  An
    # unbounded arm cannot provide a reliable terminal state or cleanup SLA.
    arm_timeout_seconds: int = Field(default=1800, ge=1)
    command_timeout_seconds: int = Field(default=300, ge=1)
    memory_sample_interval_seconds: float = Field(default=0.2, gt=0)
    stabilization_seconds: float = Field(default=1.0, ge=0)
    arrival_schedule: ArrivalSchedule = ArrivalSchedule.BURST
    stagger_interval_seconds: float = Field(default=0.0, ge=0)

    @field_validator("concurrency_levels")
    @classmethod
    def valid_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(level < 1 for level in value):
            raise ValueError("execution.concurrency_levels must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("execution.concurrency_levels must not contain duplicates")
        return value

    @model_validator(mode="after")
    def valid_arrival_schedule(self) -> "ExecutionSpec":
        if (
            self.arrival_schedule is ArrivalSchedule.FIXED_STAGGER
            and self.stagger_interval_seconds <= 0
        ):
            raise ValueError("fixed_stagger requires a positive stagger_interval_seconds")
        if (
            self.arrival_schedule is ArrivalSchedule.BURST
            and self.stagger_interval_seconds != 0
        ):
            raise ValueError("burst requires stagger_interval_seconds=0")
        return self


class PressureControlSpec(StrictFrozenModel):
    release_history_samples: int = Field(default=3, ge=3, le=8)
    psi_ratio: float = Field(default=.10, gt=0, le=1, allow_inf_nan=False)
    exit_hysteresis_seconds: float = Field(default=1., gt=0, allow_inf_nan=False)
    cooldown_seconds: float = Field(default=10., gt=0, allow_inf_nan=False)
    action_window_seconds: float = Field(default=2., gt=0, allow_inf_nan=False)
    protection_budget_mib: int = Field(default=256, ge=0)
    max_boosted_vms: int = Field(default=1, ge=0, le=4)
    boost_cpu_weight: int = Field(default=200, ge=100, le=400)
    min_release_mib: int = Field(default=64, ge=1)
    amortization_factor: float = Field(default=2., ge=1, allow_inf_nan=False)


class ResourcesSpec(StrictFrozenModel):
    pressure_control: PressureControlSpec = Field(default_factory=PressureControlSpec)
    lifecycle_admission: Literal["configured-capacity", "os-managed"] = "configured-capacity"
    proactive_cache_reclaim: bool = True
    compute_nodes: tuple[ComputeNode, ...] = ()
    target_node: str = Field(min_length=1)
    pool_memory_budget_mib: int = Field(ge=1)
    physical_live_limit_mib: int | None = Field(default=None, ge=1)
    local_memory_low_watermark_mib: int | None = Field(default=None, ge=1)
    local_memory_high_watermark_mib: int | None = Field(default=None, ge=1)
    shared_memory_borrow_limit_mib: int | None = Field(default=None, ge=0)
    emergency_free_memory_mib: int = Field(ge=1)
    startup_headroom_mib: int = Field(default=256, ge=0)
    admission_margin_mib: int = Field(default=256, ge=0)
    checkpoint_restore_headroom_mib: int = Field(default=1024, ge=0)
    static_tool_memory_mib: int | None = Field(default=None, ge=1)
    non_command_tool_memory_mib: int | None = Field(default=None, ge=1)
    prediction_artifact: str | None = None
    prediction_mode: Literal["runtime", "frozen"] = "frozen"
    observe_predictions: bool = False
    prediction_reservation_scale: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    agentcgroup_source: str | None = None
    agentcgroup_poll_interval_seconds: float = Field(default=0.5, gt=0, allow_inf_nan=False)
    agentcgroup_event_threshold: int = Field(default=1, ge=1)
    agentcgroup_pressure_ratio: float = Field(default=0.85, gt=0, lt=1, allow_inf_nan=False)
    agentcgroup_protection_window_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    local_memory_capacity_mib: int | None = Field(default=None, ge=1)
    warm_memory_capacity_mib: int = Field(default=0, ge=0)
    snapshot_mechanism: Literal["full-copy", "incremental-cow"] = "incremental-cow"
    snapshot_storage: Literal["tiered", "warm-only"] = "tiered"
    snapshot_destination: Literal["auto", "cold", "warm"] = "auto"
    lazy_restore: bool = True
    warm_snapshot_root: str | None = None
    cold_snapshot_root: str | None = None
    local_numa_node: int | None = Field(default=None, ge=0)
    local_memory_cgroup: str | None = "/sys/fs/cgroup/cube_sandbox/sandbox"
    warm_numa_node: int | None = Field(default=None, ge=0)
    memory_scheduling: Literal["static", "local_only", "snapshot_only", "joint"] = "static"
    pool_transfer_concurrency: int = Field(default=1, ge=1)
    pool_transfer_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    borrow_cooldown_seconds: float = Field(default=5, ge=0, allow_inf_nan=False)
    return_cooldown_seconds: float = Field(default=5, ge=0, allow_inf_nan=False)
    memory_action_costs: MemoryActionCostSpec | None = None
    snapshot_residency_costs: SnapshotResidencyCostSpec | None = None
    idle_reclaim_costs: IdleReclaimCostSpec | None = None
    template_base_sharing: Literal["disabled", "required"] = "disabled"

    def lifecycle_reservation_mib(self, configured_mib: int) -> int:
        """A scheduling promise, never a measurement of resident RAM."""
        return configured_mib if self.lifecycle_admission == "configured-capacity" else 0

    @property
    def default_snapshot_tier(self) -> SnapshotTier | None:
        """Common destination for worker and qualification lifecycle calls.

        Explicit calibrated action destinations take precedence at the call site.
        """
        if self.snapshot_destination == "cold":
            return SnapshotTier.COLD
        if self.snapshot_destination == "warm":
            return SnapshotTier.WARM
        if self.snapshot_storage == "warm-only" or (
            self.memory_scheduling in {"snapshot_only", "joint"}
            and self.warm_memory_capacity_mib > 0
        ):
            return SnapshotTier.WARM
        return None

    @model_validator(mode="after")
    def valid_shared_memory_watermarks(self) -> "ResourcesSpec":
        if self.snapshot_mechanism == 'incremental-cow' and not self.lazy_restore:
            raise ValueError('incremental-cow requires lazy_restore=true')
        if self.lifecycle_admission == "os-managed":
            if self.proactive_cache_reclaim:
                raise ValueError("os-managed requires proactive_cache_reclaim=false; the kernel owns cache reclaim")
            if self.shared_memory_borrow_limit_mib or self.memory_scheduling in {"joint", "snapshot_only"}:
                raise ValueError("os-managed requires resident local memory without shared borrowing or snapshot scheduling")
        if self.compute_nodes:
            validate_compute_nodes(self.compute_nodes, self.warm_numa_node)
            if self.local_numa_node is not None:
                raise ValueError("compute_nodes replaces local_numa_node; do not specify both")
            if not self.local_memory_cgroup:
                raise ValueError("compute_nodes requires a VM cgroup")
            for field, node_field in (
                ("local_memory_capacity_mib", "memory_capacity_mib"),
                ("local_memory_low_watermark_mib", "low_watermark_mib"),
                ("local_memory_high_watermark_mib", "high_watermark_mib"),
            ):
                total = sum(getattr(node, node_field) for node in self.compute_nodes)
                if getattr(self, field) not in (None, total):
                    raise ValueError(f"{field} must equal the sum over compute_nodes")
                object.__setattr__(self, field, total)
        values = (
            self.local_memory_low_watermark_mib,
            self.local_memory_high_watermark_mib,
            self.local_memory_capacity_mib,
            self.shared_memory_borrow_limit_mib,
        )
        if self.shared_memory_borrow_limit_mib is not None:
            if any(value is None for value in values):
                raise ValueError(
                    "LOW/HIGH shared borrowing requires low, high, LOCAL hard, and borrow limit"
                )
            low, high, hard, borrow = (int(value) for value in values)
            if not low < high < hard:
                raise ValueError("memory watermarks must satisfy LOW < HIGH < LOCAL hard")
            if self.warm_numa_node is None or (self.local_numa_node is None and not self.compute_nodes):
                raise ValueError("shared borrowing requires LOCAL and shared NUMA nodes")
            if self.warm_numa_node == self.local_numa_node:
                raise ValueError("LOCAL and shared NUMA nodes must differ")
            if borrow * 2 > self.warm_memory_capacity_mib:
                raise ValueError(
                    "live borrow limit cannot exceed 50% of the shared NUMA pool"
                )
            if self.pool_memory_budget_mib != high:
                raise ValueError("pool_memory_budget_mib must equal the HIGH watermark")
        elif self.compute_nodes:
            if self.pool_memory_budget_mib != self.local_memory_high_watermark_mib:
                raise ValueError("pool_memory_budget_mib must equal the sum of compute HIGH watermarks")
        elif any(value is not None for value in values[:2]):
            if any(value is None for value in values[:3]) or not values[0] < values[1] < values[2]:
                raise ValueError("local watermarks require LOW < HIGH < capacity")
        if self.local_memory_capacity_mib is not None and self.pool_memory_budget_mib > self.local_memory_capacity_mib:
            raise ValueError("pool memory budget cannot exceed physical LOCAL capacity")
        if self.memory_action_costs is not None and self.memory_scheduling != "joint":
            raise ValueError("memory_action_costs is supported only for memory_scheduling=joint")
        if (self.memory_action_costs is not None and self.snapshot_storage == "tiered"
                and (self.memory_action_costs.cold_checkpoint_seconds_per_gib is None
                     or self.memory_action_costs.cold_restore_seconds_per_gib is None)):
            raise ValueError(
                "tiered memory_action_costs requires COLD checkpoint and restore costs"
            )
        return self


class PolicySpec(StrictFrozenModel):
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    deployment: Literal["managed", "os-default"] = "managed"
    admission: AdmissionPolicy
    runtime_control: RuntimeControl = RuntimeControl.OFF
    reclamation: ReclamationPolicy
    eviction: EvictionPolicy
    restore: RestorePolicy
    template_aware_placement: bool = False
    snapshot_aware_residency: bool = False
    memory_protection: bool = False
    cpu_release_boost: bool = False
    idle_release_ranking: bool = False
    runtime_snapshot: bool = True
    resource_overrides: dict[str, Any] = Field(default_factory=dict)
    execution_overrides: dict[str, Any] = Field(default_factory=dict)
    mechanisms: dict[str, Any] = Field(default_factory=dict)

    @property
    def coordinates_release(self):
        return self.idle_release_ranking or self.runtime_control.coordinates_release

    @model_validator(mode="after")
    def valid_tuple(self) -> "PolicySpec":
        if (self.runtime_control is RuntimeControl.PRESSURE_LIFECYCLE
                and self.reclamation is ReclamationPolicy.RESIDENT):
            raise ValueError('pressure_lifecycle requires explicit snapshot_pause, wait_aware_pressure and reactive restore')
        if self.deployment == "os-default" and (
            self.admission is not AdmissionPolicy.OFF
            or self.runtime_control not in {RuntimeControl.OFF, RuntimeControl.NO_CONTROL}
            or self.reclamation is not ReclamationPolicy.RESIDENT
            or self.template_aware_placement or self.snapshot_aware_residency
        ):
            raise ValueError("os-default deployment requires resident VMs with admission and runtime control off")
        if self.eviction not in {EvictionPolicy.NONE, EvictionPolicy.WAIT_AWARE_PRESSURE}:
            raise ValueError("supported eviction policies: none, wait_aware_pressure")
        if self.restore not in {RestorePolicy.NONE, RestorePolicy.REACTIVE}:
            raise ValueError("supported restore policies: none, reactive")
        if self.reclamation is ReclamationPolicy.RESIDENT:
            if self.eviction is not EvictionPolicy.NONE or self.restore is not RestorePolicy.NONE:
                raise ValueError("resident requires eviction=none and restore=none")
        elif self.eviction is EvictionPolicy.NONE or self.restore is RestorePolicy.NONE:
            raise ValueError("snapshot_pause requires an eviction and restore policy")
        if self.snapshot_aware_residency and (
            self.reclamation is not ReclamationPolicy.SNAPSHOT_PAUSE
            or self.eviction is EvictionPolicy.NONE
        ):
            raise ValueError(
                "snapshot_aware_residency requires snapshot_pause with an eviction policy"
            )
        return self


class ValidationSpec(StrictFrozenModel):
    command: str | None = None


class OutputSpec(StrictFrozenModel):
    directory: str = Field(default="/results", min_length=1)


class ExperimentSpec(StrictFrozenModel):
    schema_version: int
    design: dict[str, Any] | None = None
    suite: Literal["admission", "numa-snapshot"] | None = None
    suite_ablations: bool = False
    suite_runtime_control: RuntimeControl = RuntimeControl.OFF
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

    @model_validator(mode="before")
    @classmethod
    def resolve_suite(cls, raw):
        if isinstance(raw, dict) and raw.get('schema_version') == 4 and 'environment' in raw:
            from .design import compile_design
            return compile_design(raw)
        from .suites import expand_suite
        return expand_suite(raw) if isinstance(raw, dict) else raw

    @model_validator(mode="after")
    def validate_v2(self) -> "ExperimentSpec":
        if self.schema_version == 4:
            if not self.design or self.suite is not None:
                raise ValueError('schema 4 requires a six-dimension design, not a legacy suite')
            if not self.policies or len({p.name for p in self.policies}) != len(self.policies):
                raise ValueError('comparison requires nonempty, uniquely named arms')
            for policy in self.policies:
                effective_spec(self, policy)
            return self
        if (any(p.runtime_control.adaptive for p in self.policies)
                and not self.resources.local_memory_cgroup):
            raise ValueError('pressure control requires resources.local_memory_cgroup and verified preallocation VM routing')
        if self.resources.lifecycle_admission == "os-managed" and any(
            policy.reclamation is not ReclamationPolicy.RESIDENT for policy in self.policies
        ):
            raise ValueError("os-managed lifecycle admission requires resident policies")
        scheduling = self.resources.memory_scheduling
        cases = self.workload.cases
        if len({case.case_id for case in cases}) != len(cases):
            raise ValueError("workload.cases case_id values must be unique")
        if any(policy.template_aware_placement for policy in self.policies):
            if not self.resources.compute_nodes:
                raise ValueError("template_aware_placement requires resources.compute_nodes")
            if self.execution.placement_policy != "load_aware":
                raise ValueError(
                    "template_aware_placement requires load_aware placement for matched comparisons"
                )
            if self.resources.template_base_sharing != "required":
                raise ValueError("template_aware_placement requires template_base_sharing=required")
        if any(policy.snapshot_aware_residency for policy in self.policies):
            if not cases:
                raise ValueError("snapshot_aware_residency requires explicit workload.cases with reuse probabilities")
            if self.resources.snapshot_storage != "tiered":
                raise ValueError("snapshot_aware_residency requires tiered snapshot storage")
            if self.resources.snapshot_residency_costs is None:
                raise ValueError(
                    "snapshot_aware_residency requires measured resources.snapshot_residency_costs"
                )
            missing_reuse = [
                case.case_id for case in cases
                if case.snapshot_reuse_probability is None
            ]
            if missing_reuse:
                raise ValueError(
                    "snapshot_aware_residency requires snapshot_reuse_probability for "
                    + ", ".join(missing_reuse)
                )
        if scheduling == "snapshot_only" and any(
            p.reclamation is not ReclamationPolicy.SNAPSHOT_PAUSE for p in self.policies
            if p.deployment != "os-default"
        ):
            raise ValueError("snapshot_only/joint memory scheduling requires snapshot_pause policies")
        if scheduling == "local_only" and any(
            p.reclamation is not ReclamationPolicy.RESIDENT for p in self.policies
        ):
            raise ValueError("local_only memory scheduling requires resident policies")
        if scheduling == "joint" and not self.resources.shared_memory_borrow_limit_mib:
            raise ValueError("joint memory scheduling requires a positive shared_memory_borrow_limit_mib")
        if (scheduling in {"snapshot_only", "joint"} and self.resources.warm_memory_capacity_mib
                and any(p.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE for p in self.policies)):
            if not (self.resources.warm_snapshot_root or "").strip():
                raise ValueError("snapshot_only/joint WARM snapshots require resources.warm_snapshot_root")
            if (self.resources.snapshot_storage == "tiered"
                    and not (self.resources.cold_snapshot_root or "").strip()):
                raise ValueError("tiered snapshot_only/joint requires resources.cold_snapshot_root for capacity fallback")
        assignment = self.execution.session_compute_nodes
        nodes = self.resources.compute_nodes
        if self.execution.placement_policy == "explicit":
            if not nodes or len(assignment) < max(self.execution.concurrency_levels):
                raise ValueError("explicit placement requires compute_nodes and one node ID per offered session")
            if set(assignment) - {node.node_id for node in nodes}:
                raise ValueError("session_compute_nodes contains unknown compute node IDs")
        elif assignment:
            raise ValueError("session_compute_nodes requires placement_policy=explicit")
        if self.execution.placement_policy == "load_aware" and not nodes:
            raise ValueError("load_aware placement requires resources.compute_nodes")
        if self.execution.placement_policy == "os_default" and self.resources.shared_memory_borrow_limit_mib is not None:
            raise ValueError("os_default requires a local-only host profile: unbound allocation cannot enforce a separate shared-NUMA live-borrow limit")
        if self.schema_version != 3:
            raise ValueError("only schema_version: 3 is supported")
        if not self.policies:
            raise ValueError("policies must not be empty")
        if nodes and self.execution.placement_policy != "os_default" and self.suite != "admission" and any(p.deployment != "os-default" for p in self.policies):
            tool_shapes = tuple(
                case.sandbox or self.sandbox for case in cases
            ) or (self.sandbox,)
            pair_mib = self.resources.lifecycle_reservation_mib(self.runtime.memory_mib + max(
                sandbox.memory_mib for sandbox in tool_shapes
            ))
            operation_headroom_mib = (
                self.resources.checkpoint_restore_headroom_mib
                if any(
                    policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                    for policy in self.policies
                )
                else 0
            )
            required_mib = pair_mib + operation_headroom_mib
            by_id = {node.node_id: node for node in nodes}
            maximum_concurrency = max(self.execution.concurrency_levels)
            if self.execution.placement_policy == "explicit":
                eligible = tuple(
                    by_id[node_id]
                    for node_id in dict.fromkeys(assignment[:maximum_concurrency])
                )
            elif self.execution.placement_policy == "round_robin":
                eligible = nodes[:min(len(nodes), maximum_concurrency)]
            else:  # load_aware may select any configured compute node.
                eligible = nodes
            fits = tuple(
                node for node in eligible
                if required_mib <= node.high_watermark_mib
            )
            if (
                self.execution.placement_policy == "load_aware" and not fits
            ) or (
                self.execution.placement_policy != "load_aware"
                and len(fits) != len(eligible)
            ):
                raise ValueError(
                    "a VM pair plus policy operation headroom does not fit the "
                    "HIGH watermark of every eligible compute node"
                )
        if self.agent.driver is AgentDriver.OPENCLAW:
            if self.inference.backend not in {InferenceBackend.API, InferenceBackend.REPLAY}:
                raise ValueError("agent.driver=openclaw requires inference.backend=api or replay")
            if "api_key" in self.inference.configuration:
                raise ValueError("model credentials must be injected from an environment variable")
        names = [policy.name for policy in self.policies]
        if len(names) != len(set(names)):
            raise ValueError("policy names must be unique")
        admissions = {policy.admission for policy in self.policies}
        if (self.resources.snapshot_mechanism == "incremental-cow"
                and self.resources.snapshot_storage != "warm-only"
                and any(policy.reclamation is ReclamationPolicy.SNAPSHOT_PAUSE
                        for policy in self.policies)
                and not (self.resources.cold_snapshot_root or "").strip()):
            raise ValueError("incremental-cow reclamation requires resources.cold_snapshot_root")
        if self.resources.snapshot_storage == "warm-only":
            if not self.resources.warm_snapshot_root or self.resources.warm_memory_capacity_mib <= 0:
                raise ValueError("warm-only requires warm_snapshot_root and positive warm_memory_capacity_mib")
        if not self.resources.local_memory_cgroup:
            raise ValueError("experiments require resources.local_memory_cgroup for physical accounting")
        if AdmissionPolicy.FIXED in admissions and self.resources.static_tool_memory_mib is None:
            raise ValueError("fixed admission requires resources.static_tool_memory_mib")
        if AdmissionPolicy.PREDICTED in admissions and not self.resources.prediction_artifact:
            raise ValueError("predicted admission requires a validated prediction_artifact")
        if self.resources.observe_predictions and not self.resources.prediction_artifact:
            raise ValueError("observe_predictions requires prediction_artifact from experiment train")
        return self


class ExperimentArm(StrictFrozenModel):
    arm_id: str
    spec_digest: str
    case: WorkloadCase
    session_cases: tuple[WorkloadCase, ...] = ()
    session_assignment: SessionAssignment = SessionAssignment.SINGLE_CASE
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


def effective_spec(spec: ExperimentSpec, policy: PolicySpec) -> ExperimentSpec:
    """Validate one fully resolved arm through the same worker schema."""
    raw = spec.model_dump(mode='json')
    raw.update(schema_version=3, design=None, suite=None, suite_ablations=False)
    raw['resources'].update(policy.resource_overrides)
    raw['execution'].update(policy.execution_overrides)
    raw['policies'] = [policy.model_dump(mode='json')]
    raw['policies'][0].update(resource_overrides={}, execution_overrides={})
    return ExperimentSpec.model_validate(raw)


def canonical_json(value: BaseModel | dict[str, Any]) -> str:
    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def spec_digest(spec: ExperimentSpec) -> str:
    if spec.schema_version == 4:
        from .design import export_design
        return hashlib.sha256(canonical_json(export_design(spec)).encode()).hexdigest()
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()


def expand_matrix(spec: ExperimentSpec) -> tuple[ExperimentArm, ...]:
    digest = spec_digest(spec)
    arms: list[ExperimentArm] = []
    workload_cases = load_workload_cases(spec.workload)
    case_groups = (
        ((workload_cases[0], workload_cases),)
        if spec.workload.session_assignment is SessionAssignment.ROUND_ROBIN
        else tuple((case, (case,)) for case in workload_cases)
    )
    for case, session_cases in case_groups:
        for repetition in range(spec.workload.repetitions):
            for concurrency in spec.execution.concurrency_levels:
                for policy in spec.policies:
                    resources = spec.resources
                    execution = spec.execution
                    if spec.schema_version == 4:
                        resolved = effective_spec(spec, policy)
                        resources, execution = resolved.resources, resolved.execution
                    elif policy.deployment == "os-default" or spec.suite == "admission":
                        # Inherit the same host VM domain and combined live limit.
                        # WARM capacity is not an additional live-memory allowance.
                        capacity = (resources.local_memory_capacity_mib or resources.pool_memory_budget_mib) + (resources.shared_memory_borrow_limit_mib or 0)
                        resources = ResourcesSpec.model_validate({**resources.model_dump(),
                            "lifecycle_admission": "os-managed", "proactive_cache_reclaim": False,
                            "compute_nodes": (), "local_numa_node": None,
                            "local_memory_capacity_mib": capacity, "pool_memory_budget_mib": capacity,
                            "local_memory_low_watermark_mib": None, "local_memory_high_watermark_mib": None,
                            "shared_memory_borrow_limit_mib": None, "warm_memory_capacity_mib": 0,
                            "warm_numa_node": None, "memory_scheduling": "local_only",
                            "snapshot_storage": "tiered", "memory_action_costs": None,
                            "snapshot_residency_costs": None, "template_base_sharing": "disabled",
                            "prediction_artifact": resources.prediction_artifact,
                            "observe_predictions": resources.observe_predictions})
                        execution = execution.model_copy(update={"placement_policy": "os_default", "session_compute_nodes": ()})
                    if resources.snapshot_storage != "warm-only" and resources.shared_memory_borrow_limit_mib is None and resources.memory_scheduling not in {
                        "snapshot_only", "joint",
                    }:
                        resources = resources.model_copy(update={"warm_memory_capacity_mib": 0})
                    identity = {"spec_digest": digest,
                                "case_ids": [item.case_id for item in session_cases],
                                "session_assignment": spec.workload.session_assignment,
                                "repetition": repetition, "concurrency": concurrency,
                                "policy": policy.model_dump(mode="json")}
                    arm_id = hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:24]
                    arms.append(ExperimentArm(
                        arm_id=arm_id, spec_digest=digest, case=case,
                        session_cases=session_cases,
                        session_assignment=spec.workload.session_assignment,
                        repetition=repetition, concurrency=concurrency, policy=policy,
                        agent=spec.agent, inference=spec.inference, runtime=spec.runtime,
                        sandbox=spec.sandbox,
                        execution=execution, resources=resources,
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
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read workload input {path}: {exc}") from exc
    if not isinstance(raw, (dict, list)):
        raise ValueError(f"{path}: workload input must be an object or a list")
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
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read experiment {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("experiment document must be an object")
    if raw.get("schema_version") not in {3, 4}:
        raise ValueError("use schema_version: 4 for six-dimension experiments")
    return ExperimentSpec.model_validate(raw)
