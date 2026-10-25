from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from clawbox.experiments import ExperimentSpec, expand_matrix, load_experiment, spec_digest
from clawbox.experiments.audit import audit_experiments
from clawbox.experiments.baselines import BASELINES, resolve_baseline
from clawbox.replay.trace import load_trace
from clawbox.experiments.worker import (
    ArmActivityTracker, DependencyInstallGate, EventWriter, ExperimentWorker,
    _execute_idempotent, dependency_install_command,
    _runtime_network_deny_out, build_time_spans,
    WatermarkController, policy_event_workers, policy_operation_headroom_mib,
    policy_progress_budget_mib,
    read_priority_memory_charge, session_case_for, session_offer_offsets,
)
from clawbox.experiments.snapshot_pool import WarmSnapshotPool
from clawbox.experiments.numa_borrow import NumaBorrowRecord
from clawbox.replay.lifecycle import CommandResult
from clawbox.experiments.spec import (
    ExecutionSpec, ReclamationPolicy, WorkloadSpec, load_workload_cases,
)


def test_explicit_null_disables_arm_deadline() -> None:
    from clawbox.experiments.spec import ExecutionSpec
    with pytest.raises(ValueError):
        ExecutionSpec(arm_timeout_seconds=None)
    assert ExecutionSpec().arm_timeout_seconds == 3600
    assert ExecutionSpec().arm_finalization_timeout_seconds == 300
    with pytest.raises(ValueError):
        ExecutionSpec(arm_finalization_timeout_seconds=0)


def test_priority_memory_charge_treats_a_cleaned_cgroup_as_zero(tmp_path: Path) -> None:
    removed = tmp_path / "session-0001"
    assert read_priority_memory_charge(removed, None) == (0, 0)

    live = tmp_path / "session-0002"
    live.mkdir()
    with pytest.raises(FileNotFoundError):
        read_priority_memory_charge(live, None)


def test_policy_event_workers_honors_snapshot_transfer_concurrency() -> None:
    arm = SimpleNamespace(
        resources=SimpleNamespace(pool_transfer_concurrency=7),
    )
    assert policy_event_workers(arm) == 7


def test_incremental_snapshot_is_default() -> None:
    from clawbox.experiments.spec import ResourcesSpec
    resources = ResourcesSpec(target_node="node-a", pool_memory_budget_mib=8192,
                              emergency_free_memory_mib=512)
    assert resources.snapshot_mechanism == "incremental-cow"
    assert resources.model_copy(update={"snapshot_mechanism": "full-copy"}).snapshot_mechanism == "full-copy"


def test_shared_memory_watermarks_are_ordered_and_use_half_pool() -> None:
    from clawbox.experiments.spec import ResourcesSpec
    resources = ResourcesSpec(
        target_node="node-a", pool_memory_budget_mib=32768,
        emergency_free_memory_mib=512,
        local_memory_low_watermark_mib=28672,
        local_memory_high_watermark_mib=32768,
        local_memory_capacity_mib=36864,
        shared_memory_borrow_limit_mib=65536,
        warm_memory_capacity_mib=131072,
        local_numa_node=0, warm_numa_node=1,
    )
    assert resources.shared_memory_borrow_limit_mib * 2 == resources.warm_memory_capacity_mib
    arm = SimpleNamespace(resources=resources)
    assert policy_progress_budget_mib(arm) == 36864
    with pytest.raises(ValidationError, match="LOW < HIGH < LOCAL hard"):
        resources.model_copy(
            update={"local_memory_low_watermark_mib": 32768},
        ).__class__.model_validate(
            resources.model_dump() | {"local_memory_low_watermark_mib": 32768}
        )
    with pytest.raises(ValidationError, match="shared NUMA pool"):
        resources.__class__.model_validate(
            resources.model_dump() | {"shared_memory_borrow_limit_mib": 131073}
        )


def test_watermark_controller_drains_to_low_after_crossing_high() -> None:
    gib = 1024 ** 3
    usage = [33 * gib, 0, 33 * gib]

    class Sampler:
        interval_s = 0.01

        def tier_usage(self):
            return tuple(usage)

    class Coordinator:
        def __init__(self):
            self.evictions = 0
            self.new_sessions_blocked = False

        def set_new_session_admission_blocked(self, blocked):
            self.new_sessions_blocked = blocked

        def evict_one_for_watermark(self):
            self.evictions += 1
            return True

    class Borrower:
        node_id = "node0"
        def __init__(self):
            self.shared_pool = WarmSnapshotPool(128 * gib, borrow_capacity_bytes=64 * gib)

        def borrowed(self):
            return ()

    class Events:
        def __init__(self):
            self.rows = []

        def write(self, row):
            self.rows.append(row)

    arm = SimpleNamespace(
        resources=SimpleNamespace(
            local_memory_low_watermark_mib=28 * 1024,
            local_memory_high_watermark_mib=32 * 1024,
            local_memory_capacity_mib=36 * 1024,
        ),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.SNAPSHOT_PAUSE),
    )
    coordinator, events = Coordinator(), Events()
    controller = WatermarkController(arm, coordinator, Sampler(), Borrower(), events)

    latched = controller._sample_once(0.5, False)
    assert latched and coordinator.evictions == 1
    assert coordinator.new_sessions_blocked is True
    usage[:] = [31 * gib, 0, 31 * gib]
    latched = controller._sample_once(0.5, latched)
    assert latched and coordinator.evictions == 2
    usage[:] = [28 * gib, 0, 28 * gib]
    assert controller._sample_once(0.5, latched) is False
    assert coordinator.new_sessions_blocked is False
    assert [row["event"] for row in events.rows if row["event"] != "memory_scheduling_decision"] == [
        "local_high_watermark_crossed", "local_low_watermark_reached",
    ]


def test_watermark_controller_rejects_shared_live_plus_snapshots_over_pool() -> None:
    gib = 1024 ** 3
    pool = WarmSnapshotPool(128 * gib, borrow_capacity_bytes=64 * gib)
    from clawbox.experiments.snapshot_pool import SnapshotKey
    key = SnapshotKey("session", "tool", 1)
    pool.reserve(key, 70 * gib)

    sampler = SimpleNamespace(
        interval_s=0.01,
        tier_usage=lambda: (10 * gib, 60 * gib, 70 * gib),
    )
    borrower = SimpleNamespace(shared_pool=pool, borrowed=lambda: (), node_id=None)
    arm = SimpleNamespace(
        resources=SimpleNamespace(
            local_memory_low_watermark_mib=28 * 1024,
            local_memory_high_watermark_mib=32 * 1024,
            local_memory_capacity_mib=36 * 1024,
        ),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.RESIDENT),
    )
    controller = WatermarkController(
        arm, SimpleNamespace(), sampler, borrower, SimpleNamespace(write=lambda row: None),
    )
    with pytest.raises(RuntimeError, match="shared NUMA physical capacity exceeded"):
        controller._sample_once(0.1, False)


def test_predicted_hard_crossing_routes_existing_tool_to_shared_memory() -> None:
    gib = 1024 ** 3
    borrowed = set()
    shared_policy = set()
    pool = WarmSnapshotPool(128 * gib, borrow_capacity_bytes=64 * gib)

    class Borrower:
        node_id = "node0"
        shared_pool = pool

        def allocates_shared(self, sandbox_id):
            return sandbox_id in shared_policy

        def borrowed(self):
            return tuple(borrowed)

    class Lifecycle:
        sandbox_id = "tool-1"
        role = "tool"
        resident = True
        configured_memory_bytes = 4 * gib

        def borrow_shared(self):
            borrowed.add(self.sandbox_id)
            shared_policy.add(self.sandbox_id)
            return NumaBorrowRecord(
                self.sandbox_id, "/cgroup/tool-1", 4 * gib, 0, 1,
                1.0, 1.1, 0.1, 33 * gib, 33 * gib, 0, 0,
            )

    coordinator = SimpleNamespace(
        local_commitment_exceeds=lambda amount, limit: (35 * gib) + amount * 1024 ** 2 > limit,
    )
    arm = SimpleNamespace(
        resources=SimpleNamespace(
            local_memory_low_watermark_mib=28 * 1024,
            local_memory_high_watermark_mib=32 * 1024,
            local_memory_capacity_mib=36 * 1024,
        ),
        policy=SimpleNamespace(reclamation=ReclamationPolicy.RESIDENT),
    )
    events = []
    controller = WatermarkController(
        arm, coordinator,
        SimpleNamespace(interval_s=.2, tier_usage=lambda: (35 * gib, 0, 35 * gib)),
        Borrower(), SimpleNamespace(write=events.append),
    )

    assert controller.route_progress_to_shared(Lifecycle(), 2048) is True
    assert controller.route_progress_to_shared(Lifecycle(), 2048) is True
    assert controller.borrow_count == 1
    assert events[0]["reason"] == "predicted_local_hard_guard"
    shared_policy.clear()  # LOCAL return retained the existing reservation
    assert controller.route_progress_to_shared(Lifecycle(), 2048) is True
    assert controller.borrow_count == 2


@pytest.mark.parametrize("root", [None, "", "   "])
def test_incremental_reclamation_requires_cold_destination(root) -> None:
    raw = raw_spec()
    raw["resources"]["cold_snapshot_root"] = root
    with pytest.raises(ValidationError, match="incremental-cow reclamation requires"):
        ExperimentSpec.model_validate(raw)
    raw["resources"]["snapshot_mechanism"] = "full-copy"
    assert ExperimentSpec.model_validate(raw).resources.snapshot_mechanism == "full-copy"


def test_policy_drain_reports_already_completed_failure() -> None:
    from concurrent.futures import Future
    from threading import Lock
    from clawbox.experiments.worker import drain_policy_futures
    success, failed = Future(), Future()
    success.set_result(None)
    failed.set_exception(RuntimeError("model wait policy failed"))
    with pytest.raises(RuntimeError, match="model wait policy failed"):
        drain_policy_futures([success, failed], Lock(), 1)


def test_policy_drain_observes_work_submitted_by_pending_event() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from clawbox.experiments.worker import drain_policy_futures
    pending, lock, release = [], Lock(), Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        def fail():
            raise RuntimeError("follow-up failed")
        def submit_followup():
            release.wait(2)
            with lock:
                pending.append(executor.submit(fail))
        pending.append(executor.submit(submit_followup))
        release.set()
        with pytest.raises(RuntimeError, match="follow-up failed"):
            drain_policy_futures(pending, lock, 2)


def test_runtime_network_policy_honors_explicit_internet_access() -> None:
    allowlist = ["192.0.2.10/32"]

    assert _runtime_network_deny_out(True, allowlist) is None
    assert _runtime_network_deny_out(False, allowlist) == ["0.0.0.0/0"]
    assert _runtime_network_deny_out(False, []) is None


def raw_spec() -> dict:
    return {
        "schema_version": 3,
        "experiment_id": "unit",
        "workload": {
            "source": "recorded_trace", "input": "trace.jsonl", "repetitions": 2,
            "session_assignment": "single_case",
            "cases": [{"case_id": "case-a", "source": "recorded_trace",
                       "source_reference": "trace.jsonl", "replay_trace_reference": "trace.jsonl"}],
        },
        "agent": {"driver": "openclaw"},
        "inference": {"backend": "replay", "configuration": {
        }},
        "runtime": {"template_alias": "runtime-arm64", "vcpu": 1, "memory_mib": 2048},
        "sandbox": {"template_alias": "task-arm64", "vcpu": 2, "memory_mib": 4096},
        "execution": {"concurrency_levels": [1, 4], "random_seed": 17},
        "resources": {
            "target_node": "node-a", "pool_memory_budget_mib": 100000,
            "emergency_free_memory_mib": 10000,
            "cold_snapshot_root": "/cold", "prediction_artifact": "frozen.json",
        },
        "policies": [
            {"name": "resident", "admission": "off", "reclamation": "resident",
             "eviction": "none", "restore": "none"},
            {"name": "proposed", "admission": "predicted", "reclamation": "snapshot_pause",
             "eviction": "wait_aware_pressure", "restore": "reactive"},
        ],
    }


def test_v2_matrix_is_complete_stable_and_randomized() -> None:
    spec = ExperimentSpec.model_validate(raw_spec())
    first = expand_matrix(spec)
    second = expand_matrix(spec)
    assert len(first) == 2 * 2 * 2
    assert [arm.arm_id for arm in first] == [arm.arm_id for arm in second]
    assert all(arm.spec_digest == spec_digest(spec) for arm in first)
    assert {arm.policy.name for arm in first} == {"resident", "proposed"}


def test_checkpoint_headroom_is_only_charged_to_snapshot_policy() -> None:
    arms = expand_matrix(ExperimentSpec.model_validate(raw_spec()))
    effective = {
        arm.policy.name: policy_operation_headroom_mib(arm)
        for arm in arms if arm.concurrency == 1 and arm.repetition == 0
    }
    assert effective == {"resident": 0, "proposed": 1024}


def test_warm_capacity_is_disabled_for_non_tiered_arms() -> None:
    raw = raw_spec()
    raw["resources"].update(warm_memory_capacity_mib=65536,
                            local_memory_capacity_mib=65536,
                            pool_memory_budget_mib=65536,
                            warm_snapshot_root="/warm", cold_snapshot_root="/cold",
                            local_numa_node=0, warm_numa_node=1)
    arms = expand_matrix(ExperimentSpec.model_validate(raw))
    assert all(arm.resources.warm_memory_capacity_mib == 0 for arm in arms)


def test_shared_pool_capacity_remains_for_live_borrow_in_resident_arms() -> None:
    raw = raw_spec()
    raw["resources"].update(
        warm_memory_capacity_mib=131072,
        shared_memory_borrow_limit_mib=65536,
        pool_memory_budget_mib=32768,
        local_memory_capacity_mib=36864,
        local_memory_low_watermark_mib=28672,
        local_memory_high_watermark_mib=32768,
        warm_snapshot_root="/warm",
        cold_snapshot_root="/cold",
        local_numa_node=0,
        warm_numa_node=1,
    )
    arms = expand_matrix(ExperimentSpec.model_validate(raw))
    assert all(arm.resources.warm_memory_capacity_mib == 131072 for arm in arms)
    assert all(
        arm.resources.shared_memory_borrow_limit_mib == 65536 for arm in arms
    )


def test_round_robin_workload_builds_one_arm_and_stable_session_mix() -> None:
    raw = raw_spec()
    raw["workload"]["repetitions"] = 1
    raw["workload"]["session_assignment"] = "round_robin"
    raw["workload"]["cases"].append({
        "case_id": "case-b", "source": "recorded_trace",
        "source_reference": "trace-b.jsonl",
        "replay_trace_reference": "trace-b.jsonl",
    })
    spec = ExperimentSpec.model_validate(raw)
    arms = expand_matrix(spec)
    assert len(arms) == 2 * 2  # concurrency levels x policies, not x cases
    arm = arms[0]
    assert [item.case_id for item in arm.session_cases] == ["case-a", "case-b"]
    assert [session_case_for(arm, index).case_id for index in range(5)] == [
        "case-a", "case-b", "case-a", "case-b", "case-a",
    ]


def test_round_robin_cases_keep_complete_tool_templates_and_reuse_inputs() -> None:
    raw = raw_spec()
    raw["workload"].update(
        repetitions=1,
        session_assignment="round_robin",
        cases=[
            {
                "case_id": "case-a", "source": "recorded_trace",
                "source_reference": "a.jsonl", "replay_trace_reference": "a.jsonl",
                "snapshot_reuse_probability": 0.75,
                "sandbox": {
                    "template_id": "tpl-a", "architecture": "arm64",
                    "vcpu": 2, "memory_mib": 3072, "workspace": "/a",
                },
            },
            {
                "case_id": "case-b", "source": "recorded_trace",
                "source_reference": "b.jsonl", "replay_trace_reference": "b.jsonl",
                "snapshot_reuse_probability": 0.25,
                "sandbox": {
                    "template_id": "tpl-b", "architecture": "arm64",
                    "vcpu": 4, "memory_mib": 5120, "workspace": "/b",
                },
            },
        ],
    )
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]

    selected = [session_case_for(arm, index) for index in range(4)]
    assert [case.sandbox.template for case in selected] == ["tpl-a", "tpl-b"] * 2
    assert [case.sandbox.workspace for case in selected] == ["/a", "/b"] * 2
    assert [case.snapshot_reuse_probability for case in selected] == [0.75, 0.25] * 2


def test_file_workload_keeps_snapshot_reuse_probability(tmp_path: Path) -> None:
    manifest = tmp_path / "workload.json"
    manifest.write_text(json.dumps({"tasks": [{
        "instance_id": "case-a",
        "replay_trace_reference": "trace.jsonl",
        "snapshot_reuse_probability": 0.75,
        "metadata": {"origin": "qualification"},
    }]}), encoding="utf-8")

    cases = load_workload_cases(WorkloadSpec(
        source="swe_rebench",
        input=str(manifest),
        session_assignment="single_case",
    ))

    assert cases[0].snapshot_reuse_probability == 0.75
    assert cases[0].metadata == {"origin": "qualification"}


def test_round_robin_requires_multiple_explicit_cases() -> None:
    raw = raw_spec()
    raw["workload"]["session_assignment"] = "round_robin"
    with pytest.raises(ValidationError, match="at least two cases"):
        ExperimentSpec.model_validate(raw)


def test_snapshot_aware_policy_requires_measured_costs_and_case_reuse() -> None:
    raw = raw_spec()
    raw["resources"].update(
        snapshot_storage="tiered", warm_snapshot_root="/warm",
        warm_memory_capacity_mib=8192,
    )
    raw["policies"][1]["snapshot_aware_residency"] = True
    with pytest.raises(ValidationError, match="snapshot_residency_costs"):
        ExperimentSpec.model_validate(raw)
    raw["resources"]["snapshot_residency_costs"] = {
        "provenance": "three measured repetitions",
        "warm_restore_seconds_per_gib": 0.2,
        "cold_restore_seconds_per_gib": 0.1,
    }
    with pytest.raises(ValidationError, match="snapshot_reuse_probability"):
        ExperimentSpec.model_validate(raw)
    raw["workload"]["cases"][0]["snapshot_reuse_probability"] = 0.5
    spec = ExperimentSpec.model_validate(raw)
    assert spec.resources.snapshot_residency_costs.cold_restore_seconds_per_gib == 0.1


def test_arrival_schedule_is_explicit_and_validated() -> None:
    raw = raw_spec()
    execution = ExperimentSpec.model_validate(raw).execution
    assert execution.arrival_schedule.value == "poisson"
    assert execution.poisson_arrival_window_fraction == 2.0
    assert execution.poisson_arrival_window_seconds is None
    raw["execution"].update({
        "arrival_schedule": "fixed_stagger", "stagger_interval_seconds": 0.25,
    })
    assert ExperimentSpec.model_validate(raw).execution.stagger_interval_seconds == 0.25
    raw["execution"]["stagger_interval_seconds"] = 0
    with pytest.raises(ValidationError, match="positive stagger"):
        ExperimentSpec.model_validate(raw)


def test_light_load_poisson_window_can_exceed_ten_trace_spans() -> None:
    raw = raw_spec()
    raw['execution']['poisson_arrival_window_fraction'] = 100.0
    assert ExperimentSpec.model_validate(raw).execution.poisson_arrival_window_fraction == 100.0


def test_poisson_arrivals_are_deterministic_fair_and_trace_scaled() -> None:
    raw = raw_spec()
    raw["workload"].update(
        repetitions=1,
        session_assignment="unique",
        cases=[{
            "case_id": f"case-{index}", "source": "recorded_trace",
            "source_reference": f"trace-{index}.jsonl",
            "replay_trace_reference": f"trace-{index}.jsonl",
            "metadata": {"recorded_duration_seconds": 400.0},
        } for index in range(4)],
    )
    raw["execution"]["concurrency_levels"] = [4]
    arms = expand_matrix(ExperimentSpec.model_validate(raw))
    first, second = session_offer_offsets(arms[0]), session_offer_offsets(arms[1])
    assert first == second
    assert first[0] == 0
    assert all(left < right for left, right in zip(first, first[1:]))
    # The configured expected window is twice the 400-second trace span.
    # One deterministic sample need not equal its expectation, but it must be
    # a genuine non-uniform Poisson realization rather than fixed staggering.
    assert len({round(right - left, 9)
                for left, right in zip(first, first[1:])}) > 1


def test_poisson_arrivals_accept_a_fixed_expected_window() -> None:
    raw = raw_spec()
    raw["workload"].update(
        repetitions=1,
        session_assignment="unique",
        cases=[{
            "case_id": f"case-{index}", "source": "recorded_trace",
            "source_reference": f"trace-{index}.jsonl",
            "replay_trace_reference": f"trace-{index}.jsonl",
            "metadata": {"recorded_duration_seconds": 10.0 + index},
        } for index in range(4)],
    )
    raw["execution"].update(
        concurrency_levels=[4], poisson_arrival_window_seconds=800.0,
    )
    arm = expand_matrix(ExperimentSpec.model_validate(raw))[0]
    offsets = session_offer_offsets(arm)
    assert offsets[0] == 0.0
    # The generator is deterministic and uses W/(N-1) as the exponential mean;
    # it does not rescale this realization to end at W.
    assert offsets == session_offer_offsets(arm)
    assert offsets[-1] != pytest.approx(800.0)


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf")])
def test_poisson_fixed_window_must_be_positive_and_finite(value: float) -> None:
    raw = raw_spec()
    raw["execution"]["poisson_arrival_window_seconds"] = value
    with pytest.raises(ValidationError, match="poisson_arrival_window_seconds"):
        ExperimentSpec.model_validate(raw)


def test_unique_assignment_requires_one_case_per_offered_session() -> None:
    raw = raw_spec()
    raw["workload"]["session_assignment"] = "unique"
    with pytest.raises(ValidationError, match="requires at least 4"):
        ExperimentSpec.model_validate(raw)


def test_build_time_spans_reports_agent_and_sandbox_durations() -> None:
    spans = build_time_spans({
        "session_started": 1.0,
        "sandbox_create_start": 2.0,
        "tool_create_start": 2.0,
        "tool_ready": 3.5,
        "runtime_create_start": 3.0,
        "runtime_ready": 4.0,
        "sandbox_ready": 4.0,
        "agent_execution_start": 4.5,
        "final_agent_completion": 9.0,
        "validation_start": 9.0,
        "validation_end": 9.5,
        "sandbox_cleanup_start": 10.0,
        "sandbox_cleanup_end": 11.0,
        "session_finished": 11.0,
        "sandbox_create_gate_wait_start": 1.0,
        "sandbox_create_gate_acquired": 2.0,
        "recorded_time_spans": [{
            "name": "tool.operation", "role": "tool", "operation": "exec",
            "start_unix_s": 4.6, "end_unix_s": 4.8,
            "duration_seconds": 0.2,
        }],
        "lifecycle_timings": [{
            "role": "runtime", "operation": "checkpoint",
            "started_unix_s": 6.0, "completed_unix_s": 6.25,
            "started_monotonic_s": 100.0, "completed_monotonic_s": 100.25,
            "service_seconds": 0.25, "state_before": "running",
            "state_after": "swapped", "status": "ok",
        }],
    })
    by_name = {item["name"]: item for item in spans}
    assert by_name["agent"]["duration_seconds"] == 4.5
    assert by_name["sandbox.create"]["duration_seconds"] == 2.0
    assert by_name["sandbox.create.queue"]["duration_seconds"] == 1.0
    assert by_name["sandbox.cleanup"]["duration_seconds"] == 1.0
    assert by_name["tool.operation"]["role"] == "tool"
    checkpoint = by_name["sandbox.runtime.checkpoint"]
    assert checkpoint["duration_seconds"] == 0.25
    assert checkpoint["start_monotonic_s"] == 100.0
    assert checkpoint["end_monotonic_s"] == 100.25
    assert checkpoint["service_seconds"] == 0.25
    assert checkpoint["state_before"] == "running"
    assert checkpoint["state_after"] == "swapped"


def test_worker_provenance_separates_runtime_and_tool_artifacts(monkeypatch) -> None:
    monkeypatch.delenv("CUBE_SOURCE_DIR", raising=False)
    raw = raw_spec()
    raw["runtime"].update({
        "template_id": "runtime-id", "template_alias": None,
        "source_image_reference": "registry/runtime",
        "image_digest": "sha256:" + "a" * 64,
    })
    raw["sandbox"].update({
        "template_id": "tool-id", "template_alias": None,
        "source_image_reference": "registry/tool",
        "image_digest": "sha256:" + "b" * 64,
    })
    worker = object.__new__(ExperimentWorker)
    worker.spec = ExperimentSpec.model_validate(raw)
    provenance = worker._provenance()
    assert provenance["runtime_template_reference"] == "runtime-id"
    assert provenance["runtime_template_image_digest"] == "sha256:" + "a" * 64
    assert provenance["tool_template_reference"] == "tool-id"
    assert provenance["tool_template_image_digest"] == "sha256:" + "b" * 64
    assert provenance["template_reference"] == "tool-id"  # legacy key
    assert provenance["cubesandbox_deployment"] == "standalone"
    assert provenance["cubesandbox_commit"] == "unknown"
    assert "kubernetes_version" not in provenance


def test_arm_provenance_records_clawbox_memory_overcommit() -> None:
    raw = raw_spec()
    raw["execution"]["concurrency_levels"] = [60]
    raw["runtime"]["memory_mib"] = 2048
    raw["sandbox"]["memory_mib"] = 4096
    raw["resources"]["pool_memory_budget_mib"] = 16384
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]

    scope = ExperimentWorker._arm_provenance(arm)["resource_scope"]

    assert scope["pair_memory_mib_per_agent"] == 6144
    assert scope["offered_provisioned_memory_mib"] == 368640
    assert scope["offered_to_policy_pool_ratio"] == 22.5
    assert scope["offered_provisioned_capacity_exceeds_policy_budget"] is True


def test_arm_provenance_uses_each_assigned_case_vm_shape() -> None:
    raw = raw_spec()
    raw["workload"]["session_assignment"] = "round_robin"
    raw["workload"]["cases"] = [
        {"case_id": "small", "source": "recorded_trace", "source_reference": "small",
         "sandbox": {"template_id": "small-tool", "vcpu": 1, "memory_mib": 1024}},
        {"case_id": "large", "source": "recorded_trace", "source_reference": "large",
         "sandbox": {"template_id": "large-tool", "vcpu": 2, "memory_mib": 4096}},
    ]
    raw["execution"]["concurrency_levels"] = [3]
    raw["runtime"]["memory_mib"] = 2048
    spec = ExperimentSpec.model_validate(raw)
    arm = expand_matrix(spec)[0]

    scope = ExperimentWorker._arm_provenance(arm)["resource_scope"]

    assert scope["session_tool_memory_mib"] == [1024, 4096, 1024]
    assert scope["session_pair_memory_mib"] == [3072, 6144, 3072]
    assert scope["offered_provisioned_memory_mib"] == 12288
    assert scope["tool_memory_mib_per_agent"] is None
    assert scope["pair_memory_mib_per_agent"] is None


def test_event_writer_assigns_joinable_wall_and_monotonic_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventWriter(
        path,
        context={"run_id": "run-a", "attempt_id": "attempt-a", "arm_id": "arm-a"},
        session_case_ids={"session-a": "case-a"},
    )
    writer.write({"event": "agent_started", "session_id": "session-a"})
    writer.write({"event": "agent_finished", "session_id": "session-a"})
    writer.close()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["sequence_no"] for row in rows] == [0, 1]
    assert all(row["schema_version"] == 1 for row in rows)
    assert all(row["session_id"] == "session-a" for row in rows)
    assert all(row["run_id"] == "run-a" for row in rows)
    assert all(row["attempt_id"] == "attempt-a" for row in rows)
    assert all(row["arm_id"] == "arm-a" for row in rows)
    assert all(row["case_id"] == "case-a" for row in rows)
    assert all(row["event_writer_lock_wait_ns"] >= 0 for row in rows)
    assert all(int(row["wall_time_ns"]) > 0 for row in rows)
    assert rows[0]["monotonic_time_ns"] <= rows[1]["monotonic_time_ns"]


def test_event_writer_tracks_low_cost_queue_and_task_activity(tmp_path: Path) -> None:
    tracker = ArmActivityTracker(2)
    writer = EventWriter(tmp_path / "events.jsonl", activity_tracker=tracker)
    session = "arm-0000"
    writer.write({"event": "sandbox_create_gate_wait_started", "session_id": session})
    writer.write({"event": "sandbox_create_gate_acquired", "session_id": session})
    writer.write({"event": "sandbox_created", "session_id": session, "role": "tool"})
    assert tracker.snapshot()["vm_creating_sessions"] == 1
    writer.write({"event": "sandbox_created", "session_id": session, "role": "runtime"})
    assert tracker.snapshot()["vm_creating_sessions"] == 1
    writer.write({"event": "pair_ready", "session_id": session})
    assert tracker.snapshot()["pair_ready_agent_starting_sessions"] == 1
    writer.write({"event": "agent_runnable", "session_id": session})
    writer.write({"event": "model_wait_started", "session_id": session})
    assert tracker.snapshot()["idle_at_llm_sessions"] == 1
    writer.write({"event": "model_response_ready", "session_id": session})
    writer.write({"event": "tool_admission_queued", "session_id": session,
                  "execution_id": "exec-1"})
    writer.write({"event": "dependency_install_gate_wait_started",
                  "session_id": session, "execution_id": "exec-1"})
    snapshot = tracker.snapshot()
    assert snapshot["tool_admission_waiting_tasks"] == 1
    assert snapshot["dependency_install_waiting_tasks"] == 1
    assert snapshot["offered_not_activated_sessions"] == 0
    writer.write({"event": "dependency_install_gate_acquired",
                  "session_id": session, "execution_id": "exec-1"})
    assert tracker.snapshot()["dependency_install_active_operations"] == 1
    writer.write({"event": "tool_admitted", "session_id": session,
                  "execution_id": "exec-1"})
    writer.write({"event": "tool_execution_progress", "session_id": session,
                  "execution_id": "exec-1", "phase": "start"})
    assert tracker.snapshot()["tool_executing_tasks"] == 1
    writer.write({"event": "tool_execution_progress", "session_id": session,
                  "execution_id": "exec-1", "phase": "end"})
    writer.write({"event": "dependency_install_gate_released",
                  "session_id": session, "execution_id": "exec-1"})
    assert tracker.snapshot()["dependency_install_active_operations"] == 0
    writer.write({"event": "session_complete", "session_id": session, "valid": True})
    snapshot = tracker.snapshot()
    assert snapshot["completed_sessions"] == 1
    assert snapshot["activated_running_sessions"] == 0
    assert snapshot["not_yet_offered_sessions"] == 1
    writer.close()


def test_event_writer_buffers_concurrent_rows_without_reordering(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path)
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(writer.write, {"event": "sample", "value": value})
            for value in range(4096)
        ]
        for future in futures:
            future.result()
    writer.close()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4096
    assert [row["sequence_no"] for row in rows] == list(range(4096))


def test_idempotent_cube_command_retries_transport_failure(monkeypatch) -> None:
    monkeypatch.setattr("clawbox.experiments.worker.time.sleep", lambda _: None)

    class Executor:
        calls = 0

        def execute(self, command: str, timeout_s: float) -> CommandResult:
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("transient")
            return CommandResult(0, command, "", timeout_s)

    executor = Executor()
    result = _execute_idempotent(executor, "test -f result.txt", 12)

    assert executor.calls == 3
    assert result.exit_code == 0


def test_formal_openclaw_replay_c40_artifact_is_loadable() -> None:
    spec = load_experiment(Path("examples/experiments/openclaw-cube-replay-c40.yaml"))
    actions = load_trace(Path("examples/traces/openclaw-cube-replay.jsonl"))
    assert spec.agent.driver.value == "openclaw"
    assert spec.execution.concurrency_levels == (40,)
    assert [policy.name for policy in spec.policies] == ["uncontrolled-resident"]
    assert actions[0].output["tool_calls"][0]["function"]["name"] == "exec"
    assert json.loads(actions[0].output["tool_calls"][0]["function"]["arguments"])[
        "command"
    ].startswith("printf openclaw-cube-replay-ok")


def test_checked_in_baseline_matrices_are_schema_v2_and_plan_c40() -> None:
    paths = list(Path("examples/experiments").glob("openclaw*.yaml"))
    audits = audit_experiments(paths)
    assert len(audits) == len(paths)
    by_id = {str(item["experiment_id"]): item for item in audits}
    assert by_id["openclaw-cube-replay-c40"]["concurrency_levels"] == [40]
    assert by_id["openclaw-cube-replay-c60-overcommit"]["concurrency_levels"] == [60]
    assert all(item["tool_template"] != "sandbox-code" for item in audits)
    openclaw = by_id["openclaw-cube-replay-c40"]["artifact_provenance"]
    assert openclaw["runtime"]["template_id"] == "tpl-ec97143fa76e409981055c2f"
    assert openclaw["tool"]["template_id"] == "tpl-72f8a42d8fe746279d0bb80a"
    assert openclaw["runtime"]["image_digest"].startswith("sha256:")
    assert openclaw["tool"]["image_digest"].startswith("sha256:")


def test_openclaw_matrix_requires_immutable_runtime_and_tool_provenance(
    tmp_path: Path,
) -> None:
    raw = raw_spec()
    raw["agent"] = {"driver": "openclaw"}
    raw["inference"] = {
        "backend": "replay", "configuration": {
            "model": "recorded-model",
        },
    }
    path = tmp_path / "openclaw.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable template_id"):
        audit_experiments([path])

    raw["runtime"] = {
        "template_id": "runtime-id", "source_image_reference": "registry/runtime",
        "image_digest": "sha256:" + "a" * 64,
    }
    raw["sandbox"] = {
        "template_id": "tool-id", "source_image_reference": "registry/tool",
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="missing image_digest"):
        audit_experiments([path])


def test_baseline_catalog_and_independent_policy_names():
    from clawbox.experiments.baselines import ACTIVE_BASELINES, ensure_supported_experiment
    assert len(ACTIVE_BASELINES) == 7
    assert 'no_control' not in ACTIVE_BASELINES
    assert all(BASELINES[n].implementation_status == 'implemented' for n in ACTIVE_BASELINES)
    normalized = [
        BASELINES[name].as_policy(name='normalized').model_dump(mode='json')
        for name in ACTIVE_BASELINES
    ]
    assert len({json.dumps(policy, sort_keys=True) for policy in normalized}) == len(normalized)
    wait_snapshot = BASELINES['predicted-wait-snapshot'].as_policy()
    assert not wait_snapshot.coordinates_release
    assert wait_snapshot.runtime_snapshot
    groups = {
        group: {name for name, baseline in BASELINES.items()
                if group in baseline.comparison_groups}
        for group in {group for baseline in BASELINES.values()
                      for group in baseline.comparison_groups}
    }
    assert all(len(names) >= 2 for names in groups.values())
    assert groups['integrated'] == {
        'predicted-resident', 'predicted-wait-snapshot',
    }
    raw = raw_spec()
    raw['policies'] = [resolve_baseline('uncontrolled-resident').as_policy(name='custom-label').model_dump(mode='json')]
    spec = ExperimentSpec.model_validate(raw)
    ensure_supported_experiment(spec)
    arm = expand_matrix(spec)[0]
    assert ExperimentWorker._tool_reservation_mib(None, arm, prediction=None) == 0
    assert ExperimentWorker._startup_headroom_mib(arm, None) == 256
    raw['policies'][0]['admission'] = 'tool_congestion'
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(raw)


def test_paused_tool_is_restored_before_growth_admission_and_routing():
    calls = []
    lifecycle = SimpleNamespace(resident=False)
    coordinator = SimpleNamespace(
        set_tool_active=lambda session_id, active: calls.append(
            ("active", session_id, active)
        ),
        begin_tool_admission=lambda session_id, local, timeout, total_mib: (
            calls.append(("admit", lifecycle.resident, local, total_mib)) or 0.25
        ),
    )
    watermark = SimpleNamespace(
        route_progress_to_shared=lambda candidate, amount: (
            calls.append(("route", candidate.resident, amount)) or True
        )
    )
    worker = object.__new__(ExperimentWorker)

    def restore(*_args, **_kwargs):
        calls.append(("restore", lifecycle.resident))
        lifecycle.resident = True

    worker._restore_with_one_victim = restore
    arm = SimpleNamespace(execution=SimpleNamespace(arm_timeout_seconds=30))

    result = worker._prepare_tool_admission(
        arm, "session", lifecycle, coordinator, SimpleNamespace(),
        watermark, 3, wait_for_admission=lambda operation: operation(),
    )

    assert result == (0, "shared", 0.25, True)
    assert calls == [
        ("active", "session", True),
        ("restore", False),
        ("route", True, 3),
        ("admit", True, 0, 3),
    ]


def test_tool_closed_during_admission_releases_new_claim():
    from clawbox.replay.lifecycle import LifecycleError

    lifecycle = SimpleNamespace(resident=True)
    released = []
    coordinator = SimpleNamespace(
        set_tool_active=lambda *_: None,
        begin_tool_admission=lambda *_, **__: 0.25,
        release=lambda session, local, total_mib: released.append(
            (session, local, total_mib)
        ),
    )
    worker = object.__new__(ExperimentWorker)
    arm = SimpleNamespace(execution=SimpleNamespace(arm_timeout_seconds=30))

    def wait_while_cleanup_closes_vm(operation):
        result = operation()
        lifecycle.resident = False
        return result

    with pytest.raises(LifecycleError, match="unavailable while waiting"):
        worker._prepare_tool_admission(
            arm, "session", lifecycle, coordinator, SimpleNamespace(), None, 3,
            wait_for_admission=wait_while_cleanup_closes_vm,
        )
    assert released == [("session", 3, 3)]


def test_failed_tool_restore_does_not_reserve_growth():
    calls = []
    lifecycle = SimpleNamespace(resident=False)
    coordinator = SimpleNamespace(
        set_tool_active=lambda *_args: calls.append("active"),
        begin_tool_admission=lambda *_args, **_kwargs: calls.append("admit"),
    )
    watermark = SimpleNamespace(
        route_progress_to_shared=lambda *_args: calls.append("route")
    )
    worker = object.__new__(ExperimentWorker)

    def restore(*_args, **_kwargs):
        calls.append("restore")
        raise RuntimeError("restore failed")

    worker._restore_with_one_victim = restore
    arm = SimpleNamespace(execution=SimpleNamespace(arm_timeout_seconds=30))

    with pytest.raises(RuntimeError, match="restore failed"):
        worker._prepare_tool_admission(
            arm, "session", lifecycle, coordinator, SimpleNamespace(),
            watermark, 3, wait_for_admission=lambda operation: operation(),
        )

    assert calls == ["active", "restore"]


@pytest.mark.parametrize("field,value", [
    ("model_wait_prediction_seconds", 1),
    ("model_wait_prediction_source", "independent-recording"),
    ("oracle_model_wait_seconds", 1),
])
def test_llm_duration_settings_are_rejected_even_for_resident_arms(field, value):
    raw = raw_spec()
    raw['inference']['configuration'][field] = value
    with pytest.raises(ValueError, match='prediction/oracle policy is unsupported'):
        ExperimentSpec.model_validate(raw)


def test_unsupported_oracle_is_rejected_at_schema_boundary() -> None:
    raw = raw_spec()
    raw["policies"][1]["eviction"] = "time_oracle"
    with pytest.raises(ValidationError, match="supported eviction"):
        ExperimentSpec.model_validate(raw)


def test_openclaw_cube_example_uses_native_tool_names() -> None:
    spec = load_experiment(Path("examples/experiments/openclaw-cube.yaml"))
    assert "cube_shell" not in spec.workload.cases[0].prompt
    assert "sandboxed exec" in spec.workload.cases[0].prompt


def test_worker_uses_explicit_pair_creation_concurrency() -> None:
    assert ExecutionSpec().sandbox_create_concurrency == 8
    assert ExperimentWorker._sandbox_create_limit(40, 3) == 3
    assert ExperimentWorker._sandbox_create_limit(2, 3) == 2
    with pytest.raises(ValidationError, match="sandbox_create_concurrency"):
        ExecutionSpec(sandbox_create_concurrency=0)


def test_dependency_install_concurrency_is_optional_and_positive() -> None:
    assert ExecutionSpec().dependency_install_concurrency is None
    assert ExecutionSpec(dependency_install_concurrency=8).dependency_install_concurrency == 8
    with pytest.raises(ValidationError, match="dependency_install_concurrency"):
        ExecutionSpec(dependency_install_concurrency=0)


def test_apt_cache_proxy_is_optional_and_requires_plain_http_host_port() -> None:
    assert ExecutionSpec().apt_cache_proxy_url is None
    configured = ExecutionSpec(
        apt_cache_proxy_url="http://193.124.7.2:3142/",
    )
    assert configured.apt_cache_proxy_url == "http://193.124.7.2:3142"
    for invalid in (
        "https://cache.example:3142",
        "http://cache.example",
        "http://user:secret@cache.example:3142",
        "http://cache.example:3142/path",
    ):
        with pytest.raises(ValidationError, match="apt_cache_proxy_url"):
            ExecutionSpec(apt_cache_proxy_url=invalid)


def test_pypi_cache_index_is_optional_and_requires_devpi_simple_url() -> None:
    assert ExecutionSpec().pypi_cache_index_url is None
    configured = ExecutionSpec(
        pypi_cache_index_url="http://193.124.7.2:3141/root/pypi/+simple/",
    )
    assert configured.pypi_cache_index_url == (
        "http://193.124.7.2:3141/root/pypi/+simple/"
    )
    for invalid in (
        "https://cache.example:3141/root/pypi/+simple/",
        "http://cache.example/root/pypi/+simple/",
        "http://user:secret@cache.example:3141/root/pypi/+simple/",
        "http://cache.example:3141/root/pypi/",
    ):
        with pytest.raises(ValidationError, match="pypi_cache_index_url"):
            ExecutionSpec(pypi_cache_index_url=invalid)


@pytest.mark.parametrize("command", [
    "apt-get update && apt-get install -y pytest",
    "cd /testbed && sudo -n apt install python3-pip",
    "DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -qq install git",
    "pip3 install pytest",
    "/opt/venv/bin/python3 -m pip install -e .",
    "bash -lc 'python -m pip install numpy'",
])
def test_dependency_install_command_matches_package_downloads(command: str) -> None:
    assert dependency_install_command(command)


@pytest.mark.parametrize("command", [
    "pytest -q",
    "echo apt-get install pytest",
    "python -m pytest",
    "pip list",
    "apt-cache show pytest",
])
def test_dependency_install_command_ignores_other_tools(command: str) -> None:
    assert not dependency_install_command(command)


def test_dependency_install_gate_holds_permit_until_release() -> None:
    records: list[dict] = []
    second_waiting = Event()
    second_acquired = Event()

    def emit(event: dict) -> None:
        records.append(event)
        if (event["event"] == "dependency_install_gate_wait_started"
                and event["execution_id"] == "exec-2"):
            second_waiting.set()

    gate = DependencyInstallGate(1, emit)
    gate.acquire("exec-1", "session-1", timeout_seconds=1)

    def acquire_second() -> None:
        gate.acquire("exec-2", "session-2", timeout_seconds=1)
        second_acquired.set()

    thread = Thread(target=acquire_second)
    thread.start()
    assert second_waiting.wait(1)
    assert not second_acquired.is_set()
    assert gate.release("exec-1", reason="completed")
    assert second_acquired.wait(1)
    assert gate.release("exec-2", reason="completed")
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert sum(
        event["event"] == "dependency_install_gate_acquired"
        for event in records
    ) == 2


def test_worker_thread_scheduling_is_configured_and_restored():
    import sys
    from clawbox.experiments.worker import worker_thread_scheduling
    assert ExecutionSpec().worker_thread_switch_interval_seconds == .001
    for invalid in (0, -.01, float('nan'), float('inf'), .2):
        with pytest.raises(ValidationError):
            ExecutionSpec(worker_thread_switch_interval_seconds=invalid)
    original = sys.getswitchinterval()
    with pytest.raises(RuntimeError):
        with worker_thread_scheduling(.002):
            assert sys.getswitchinterval() == pytest.approx(.002)
            raise RuntimeError('restore even on failed run')
    assert sys.getswitchinterval() == original


def test_vm_startup_headroom_matches_the_arm_admission_policy() -> None:
    raw = raw_spec()
    raw["workload"]["repetitions"] = 1
    raw["execution"]["concurrency_levels"] = [1]
    raw["resources"].update(
        static_tool_memory_mib=512,
        non_command_tool_memory_mib=16,
        prediction_artifact="p50.json",
    )
    raw["policies"] = [{
        "name": "static", "admission": "fixed", "reclamation": "resident",
        "eviction": "none", "restore": "none",
    }]
    static_arm, = expand_matrix(ExperimentSpec.model_validate(raw))
    assert ExperimentWorker._startup_headroom_mib(static_arm, None) == 256

    raw["policies"][0].update(name="p50", admission="predicted")
    p50_arm, = expand_matrix(ExperimentSpec.model_validate(raw))
    provider = SimpleNamespace(max_incremental_memory_mib=201.3)
    assert ExperimentWorker._startup_headroom_mib(p50_arm, provider) == 256


def test_v2_rejects_backend_transport_and_invalid_policy() -> None:
    raw = raw_spec()
    raw["sandbox"]["backend"] = "kubernetes"
    with pytest.raises(ValidationError, match="backend"):
        ExperimentSpec.model_validate(raw)
    raw = raw_spec()
    raw["policies"][0]["eviction"] = "eager"
    with pytest.raises(ValidationError, match="supported eviction"):
        ExperimentSpec.model_validate(raw)


def test_openclaw_allows_managed_api_or_replay_and_keeps_secret_credentials_out_of_spec() -> None:
    raw = raw_spec()
    raw["agent"] = {"driver": "openclaw"}
    assert ExperimentSpec.model_validate(raw).inference.backend.value == "replay"
    raw["inference"] = {"backend": "api", "configuration": {"api_key": "do-not-store"}}
    with pytest.raises(ValidationError, match="environment variable"):
        ExperimentSpec.model_validate(raw)


def test_yaml_loader_and_v1_error(tmp_path: Path) -> None:
    path = tmp_path / "experiment.yaml"
    import yaml
    path.write_text(yaml.safe_dump(raw_spec()), encoding="utf-8")
    assert load_experiment(path).schema_version == 3
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version: 4"):
        load_experiment(path)


def test_worker_only_reuses_successful_complete_arm(tmp_path: Path) -> None:
    result = tmp_path / "arm.json"
    marker = tmp_path / "arm.complete"
    marker.write_text("digest\n", encoding="ascii")
    result.write_text(json.dumps({"arm": {"spec_digest": "digest"}, "status": "failed"}))
    assert not ExperimentWorker._completed(result, marker, "digest")
    result.write_text(json.dumps({"arm": {"spec_digest": "digest"}, "status": "succeeded"}))
    assert ExperimentWorker._completed(result, marker, "digest")
