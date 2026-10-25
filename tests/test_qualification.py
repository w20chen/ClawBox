from __future__ import annotations

import json
from pathlib import Path

import pytest

import clawbox.experiments.qualification as qualification_module
from clawbox.experiments.baselines import BASELINES
from clawbox.experiments.qualification import (
    QUALIFICATION_SCHEMA_VERSION, current_qualification_input_manifest,
    implementation_digest, qualification_contract_digest,
    live_borrow_roundtrip_test, qualification_spec, snapshot_roundtrip_test,
    qualification_input_manifest, template_base_sharing_test, validate_receipt,
)
from clawbox.experiments.worker import session_worker_count
from clawbox.experiments.numa_borrow import NumaBorrowRecord
from clawbox.experiments.spec import (
    expand_matrix, load_experiment, load_workload_cases, spec_digest,
)


EXAMPLE = Path("examples/experiments/getting-started.yaml")


def test_sandbox_mapping_aggregation_does_not_double_first_process(monkeypatch):
    identity = ("00:01", 7)
    monkeypatch.setattr(qualification_module, "_sandbox_pids", lambda _sandbox: [11, 12])
    monkeypatch.setattr(
        qualification_module,
        "_file_mappings",
        lambda pid: {identity: {
            "path": "/warm/base.mem", "size_kib": 100,
            "rss_kib": pid, "pss_kib": 5, "shared_clean_kib": 4,
        }},
    )

    result = qualification_module._sandbox_file_mappings("sandbox-a")[identity]

    assert result == {
        "path": "/warm/base.mem", "size_kib": 200,
        "rss_kib": 23, "pss_kib": 10, "shared_clean_kib": 8,
    }


def test_template_sharing_evidence_excludes_shared_root_filesystems():
    ram = {"path": "/data/cubelet/storage/xfs/objects/volumes/tpl-a-memory/tpl-a-memory"}
    rootfs = {"path": "/var/lib/cube/images/rootfs.ext4"}

    assert qualification_module._same_template_ram_mapping(ram, dict(ram))
    assert not qualification_module._same_template_ram_mapping(rootfs, dict(rootfs))


def test_template_sharing_retries_only_explicit_cube_ttrpc_create_errors():
    class ApiError(RuntimeError):
        pass

    assert qualification_module._retryable_template_probe_error(ApiError(
        "failed to create shim task: ttrpc err: Receive packet timeout Elapsed(())"
    ))
    assert qualification_module._retryable_template_probe_error(ApiError(
        "failed to create shim task: ttrpc err: Send packet to sender error SendError"
    ))
    assert qualification_module._retryable_template_probe_error(ApiError(
        "failed to create shim task: Recv len invalid:0"
    ))
    assert not qualification_module._retryable_template_probe_error(ApiError(
        "template has no physically shared mapping"
    ))
    assert not qualification_module._retryable_template_probe_error(RuntimeError(
        "ttrpc err: Receive packet timeout"
    ))


def test_snapshot_probe_uses_one_template_per_vm_shape() -> None:
    spec = load_experiment(EXAMPLE)
    cases = []
    for index, memory_mib in enumerate((3072, 3072, 4096)):
        sandbox = spec.sandbox.model_copy(update={
            "template_alias": f"tool-{index}",
            "image_digest": "sha256:" + f"{index + 1:064x}",
            "memory_mib": memory_mib,
        })
        cases.append(spec.workload.cases[0].model_copy(update={
            "case_id": f"case-{index}", "sandbox": sandbox,
        }))
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": tuple(cases)}),
    })

    observed, representatives = (
        qualification_module._snapshot_sandbox_representatives(spec)
    )

    assert observed == 3
    assert list(representatives) == ["tool-0", "tool-2"]


def test_qualification_rejects_populated_vm_domain_before_expensive_work(
    tmp_path: Path,
) -> None:
    (tmp_path / "cgroup.events").write_text(
        "populated 1\nfrozen 0\n", encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="populated before qualification"):
        qualification_module.require_empty_vm_domain(tmp_path)

    (tmp_path / "cgroup.events").write_text(
        "populated 0\nfrozen 0\n", encoding="utf-8",
    )
    qualification_module.require_empty_vm_domain(tmp_path)


def test_template_sharing_probe_releases_each_pair_before_next_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_experiment(EXAMPLE)
    cases = []
    for index in range(3):
        sandbox = spec.sandbox.model_copy(update={
            "template_alias": f"tool-{index}",
            "image_digest": "sha256:" + f"{index + 1:064x}",
        })
        cases.append(spec.workload.cases[0].model_copy(update={
            "case_id": f"case-{index}", "sandbox": sandbox,
        }))
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": tuple(cases)}),
        "execution": spec.execution.model_copy(update={"sandbox_create_concurrency": 1}),
        "resources": spec.resources.model_copy(update={"template_base_sharing": "required"}),
    })
    active = 0
    peak = 0
    template_by_id = {}
    cleanup_calls = []
    start_attempts = {}

    class ApiError(RuntimeError):
        pass

    class Client:
        def __init__(self, **_kwargs):
            pass

        def kill_owned_sandboxes(self, owner_id):
            cleanup_calls.append(owner_id)

    class Lifecycle:
        def __init__(self, _client, *, template, **_kwargs):
            self.template = template
            self.sandbox_id = f"sandbox-{template}-{len(template_by_id)}"

        def start(self):
            nonlocal active, peak
            attempt = start_attempts.get(self.template, 0)
            start_attempts[self.template] = attempt + 1
            if self.template == "tool-1" and attempt == 0:
                raise ApiError(
                    "failed to create shim task: ttrpc err: Receive packet timeout Elapsed(())"
                )
            template_by_id[self.sandbox_id] = self.template
            active += 1
            peak = max(peak, active)

        def close(self):
            nonlocal active
            active -= 1

    def mappings(sandbox_id):
        template = template_by_id[sandbox_id]
        return {("00:01", 7): {
            "path": f"/data/cubelet/volumes/{template}-memory/{template}-memory",
            "size_kib": 3 * 1024 * 1024,
            "rss_kib": 1, "pss_kib": 1, "shared_clean_kib": 1,
        }}

    monkeypatch.setattr(qualification_module, "CubeSandboxClient", Client)
    monkeypatch.setattr(qualification_module, "CubeSandboxLifecycle", Lifecycle)
    monkeypatch.setattr(qualification_module, "_sandbox_file_mappings", mappings)

    prior = tmp_path / "prior-result.json"
    prior.write_text(json.dumps({
        "passed": False,
        "cleanup_errors": [],
        "templates": {"tool-0": [{
            "device_inode": ["00:01", 7],
            "vm_0": {
                "path": "/data/cubelet/volumes/tool-0-memory/tool-0-memory",
                "size_kib": 3 * 1024 * 1024,
                "rss_kib": 1, "pss_kib": 1, "shared_clean_kib": 1,
            },
            "vm_1": {
                "path": "/data/cubelet/volumes/tool-0-memory/tool-0-memory",
                "size_kib": 3 * 1024 * 1024,
                "rss_kib": 1, "pss_kib": 1, "shared_clean_kib": 1,
            },
        }]},
    }), encoding="utf-8")

    result = template_base_sharing_test(
        spec, artifact_root=tmp_path / "probe", prior_results=(prior,),
    )

    assert result["passed"] is True
    assert result["template_count"] == 3
    assert result["max_parallel_templates"] == 1
    assert result["live_probe_vm_bound"] == 2
    assert result["peak_live_probe_vms"] == 2
    assert result["template_retry_attempts"] == 1
    assert list(result["template_retry_errors"]) == ["tool-1"]
    assert result["template_count_reused"] == 1
    assert result["template_count_probed_this_attempt"] == 2
    assert "tool-0" not in start_attempts
    assert peak == 2 and active == 0
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("qualification-base-")


def test_template_sharing_artifact_reuses_exact_templates_and_raw_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
        "resources": spec.resources.model_copy(update={
            "template_base_sharing": "required",
        }),
    })
    template = spec.sandbox.template
    proof_root = tmp_path / "raw"
    proof_root.mkdir()
    (proof_root / "result.json").write_text('{"passed":true}', encoding="utf-8")
    result = {
        "required": True,
        "passed": True,
        "templates": {template: [{"mapping": "measured"}]},
        "template_count": 1,
        "max_parallel_templates": 1,
        "live_probe_vm_bound": 2,
        "peak_live_probe_vms": 2,
        "metric": "test mapping",
    }
    monkeypatch.setattr(
        qualification_module, "_template_sharing_backend_identity",
        lambda: {"files": {"cubelet": "a" * 64}},
    )
    artifact = tmp_path / "sharing.json"
    qualification_module.write_template_base_sharing_artifact(
        spec, result, artifact_root=proof_root, output=artifact,
    )
    reused = spec.model_copy(update={
        "resources": spec.resources.model_copy(update={
            "template_base_sharing_artifact": str(artifact),
        }),
    })
    # An unrelated Cubelet/config update does not invalidate physical mapping
    # evidence for the same immutable templates. Changes to the mapping
    # mechanism itself require an explicitly new proof contract.
    monkeypatch.setattr(
        qualification_module, "_template_sharing_backend_identity",
        lambda: {"files": {"cubelet": "b" * 64}},
    )

    verified = template_base_sharing_test(reused, artifact_root=tmp_path / "unused")

    assert verified["passed"] is True and verified["reused"] is True
    assert verified["template_count"] == 1
    manifest = qualification_input_manifest(reused)
    assert manifest["template_base_sharing_artifact"]["path"] == str(artifact)

    changed = reused.model_copy(update={
        "sandbox": reused.sandbox.model_copy(update={"memory_mib": 4096}),
    })
    with pytest.raises(ValueError, match="exact Tool templates"):
        template_base_sharing_test(changed, artifact_root=tmp_path / "changed")


def test_dense_multi_numa_qualification_does_not_require_shared_memory(tmp_path):
    from clawbox.experiments.topology import ComputeNode
    spec = load_experiment(EXAMPLE)
    nodes = tuple(ComputeNode(node_id=f'n{i}', numa_node=i, cpus=str(i),
        memory_capacity_mib=1024, low_watermark_mib=512, high_watermark_mib=768)
        for i in range(2))
    spec = spec.model_copy(update={'resources': spec.resources.model_copy(update={
        'compute_nodes': nodes, 'shared_memory_borrow_limit_mib': None,
        'warm_memory_capacity_mib': 0, 'warm_numa_node': None})})
    result = live_borrow_roundtrip_test(spec, artifact_root=tmp_path/'borrow')
    assert result == {'required': False, 'passed': True, 'borrow_limit_bytes': 0,
                      'shared_capacity_bytes': 0}


def _receipt(spec, *, concurrency: int | None = None) -> dict:
    qualified = qualification_spec(spec, concurrency=concurrency)
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "source_spec_digest": spec_digest(spec),
        "qualification_contract_digest": qualification_contract_digest(spec),
        "input_manifest": qualification_input_manifest(spec),
        "implementation_digest": implementation_digest(),
        "qualification_spec_digest": spec_digest(qualified),
        "qualified_unix_s": 1.0,
        "concurrency": qualified.inference.configuration[
            "qualification_max_parallel_sessions"
        ],
        "qualified_session_count": max(qualified.execution.concurrency_levels),
        "qualified_case_ids": [case.case_id for case in load_workload_cases(spec.workload)],
        "target_concurrency": max(spec.execution.concurrency_levels),
        "scope": "capability_smoke",
        "policy_names": [policy.name for policy in qualified.policies],
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "tool_image_digests": sorted({
            (case.sandbox or spec.sandbox).image_digest
            for case in spec.workload.cases
        }, key=lambda value: value or ""),
        "run_root": "/results/qualification-test",
        "supervision_fault_test": {"passed": True},
        "runtime_control_test": {"neutral_placement_verified": True, "passed": True, "restore_verified": True},
        "cleanup_verified": True,
        "status": "succeeded",
    }


def test_qualification_uses_all_policies_at_target_concurrency() -> None:
    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
        "execution": spec.execution.model_copy(update={"arm_timeout_seconds": 1800}),
    })
    qualified = qualification_spec(spec, concurrency=3)

    assert qualified.inference.configuration["max_model_steps"] == 1
    assert qualified.inference.configuration["time_scale"] == 0.0
    assert spec.inference.configuration["time_scale"] == 1.0
    assert qualified.execution.concurrency_levels == (3,)
    assert qualified.execution.randomized_order is False
    assert qualified.execution.arm_timeout_seconds == 1800
    assert qualified.workload.repetitions == 1
    assert qualified.validation.command == "true"
    assert [item.name for item in qualified.policies] == [
        item.name for item in spec.policies
    ]


def test_receipt_reuses_capability_across_run_scale_but_not_template_change(
    tmp_path: Path,
) -> None:
    spec = load_experiment(EXAMPLE)
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(_receipt(spec)), encoding="utf-8")

    assert validate_receipt(spec, path)["status"] == "succeeded"

    changed = spec.model_copy(update={
        "execution": spec.execution.model_copy(update={
            "concurrency_levels": (1, 2),
        }),
    })
    assert validate_receipt(changed, path)["status"] == "succeeded"

    transfer_tuned = spec.model_copy(update={
        "resources": spec.resources.model_copy(update={
            "pool_transfer_concurrency": 32,
            "pool_transfer_timeout_seconds": 300,
        }),
        "execution": spec.execution.model_copy(update={
            "sandbox_create_concurrency": 4,
            "dependency_install_concurrency": 8,
        }),
    })
    assert validate_receipt(transfer_tuned, path)["status"] == "succeeded"

    watch_tuned = spec.model_copy(update={
        "inference": spec.inference.model_copy(update={
            "configuration": {**spec.inference.configuration,
                              "runtime_completion_watch_seconds": 30},
        }),
    })
    assert validate_receipt(watch_tuned, path)["status"] == "succeeded"

    changed_template = spec.model_copy(update={
        "runtime": spec.runtime.model_copy(update={
            "template_id": "different-runtime-template",
        }),
    })
    with pytest.raises(ValueError, match="qualification_contract_digest"):
        validate_receipt(changed_template, path)

    stale = _receipt(spec)
    stale["implementation_digest"] = "0" * 64
    path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="implementation_digest"):
        validate_receipt(spec, path)


def test_receipt_reuse_rehashes_inputs_without_repeating_semantic_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = load_experiment(EXAMPLE)
    semantic_manifest = qualification_input_manifest(spec)
    assert current_qualification_input_manifest(spec) == semantic_manifest
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(_receipt(spec)), encoding="utf-8")
    monkeypatch.setattr(
        qualification_module,
        "validate_inputs",
        lambda _spec: pytest.fail("receipt reuse repeated semantic validation"),
    )

    assert validate_receipt(spec, path)["status"] == "succeeded"


def test_receipt_fast_manifest_matches_semantic_manifest_for_design_spec() -> None:
    spec = load_experiment(Path("examples/experiments/six-dimension.yaml"))
    assert current_qualification_input_manifest(spec) == qualification_input_manifest(spec)


def test_receipt_is_bound_to_trace_contents(tmp_path: Path) -> None:
    spec = load_experiment(EXAMPLE)
    source = Path(spec.workload.cases[0].replay_trace_reference)
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(source.read_bytes())
    case = spec.workload.cases[0].model_copy(update={
        "source_reference": str(trace),
        "replay_trace_reference": str(trace),
    })
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={
            "input": str(trace),
            "cases": (case,),
        }),
    })
    receipt = tmp_path / "qualification.json"
    receipt.write_text(json.dumps(_receipt(spec)), encoding="utf-8")
    assert validate_receipt(spec, receipt)["status"] == "succeeded"

    trace.write_text(trace.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="input_manifest"):
        validate_receipt(spec, receipt)


def test_explicit_bounded_gate_preserves_target_and_case_coverage(tmp_path):
    spec = load_experiment(EXAMPLE)
    case = spec.workload.cases[0]
    cases = tuple(
        case.model_copy(update={"case_id": f"qualification-case-{index}"})
        for index in range(3)
    )
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": cases}),
        "execution": spec.execution.model_copy(
            update={"concurrency_levels": (320, 640, 1280)}),
    })
    path = tmp_path / 'bounded.json'
    receipt = _receipt(spec, concurrency=1)
    path.write_text(json.dumps(receipt))
    verified = validate_receipt(spec, path)
    assert verified['concurrency'] == 1
    assert verified['target_concurrency'] == 1280
    assert verified['scope'] == 'capability_smoke'
    qualified = qualification_spec(spec, concurrency=1)
    assert qualified.execution.concurrency_levels == (3,)
    assert len(qualified.workload.cases) == 3
    assert session_worker_count(expand_matrix(qualified)[0]) == 1
    assert verified['qualified_session_count'] == 3
    assert verified['qualified_case_ids'] == [case.case_id for case in cases]
    receipt['concurrency'] = 0
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='concurrency'):
        validate_receipt(spec, path)


def test_default_gate_uses_create_limit_without_repeating_cases():
    spec = load_experiment(EXAMPLE)
    case = spec.workload.cases[0]
    cases = tuple(
        case.model_copy(update={"case_id": f"qualification-case-{index}"})
        for index in range(3)
    )
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": cases}),
        "execution": spec.execution.model_copy(update={
            "concurrency_levels": (1280,),
            "sandbox_create_concurrency": 2,
        }),
    })

    qualified = qualification_spec(spec)

    assert qualified.inference.configuration[
        "qualification_max_parallel_sessions"
    ] == 2
    assert qualified.execution.concurrency_levels == (3,)
    assert session_worker_count(expand_matrix(qualified)[0]) == 2


def test_zero_qualification_concurrency_is_rejected():
    with pytest.raises(ValueError, match="qualification concurrency must be positive"):
        qualification_spec(load_experiment(EXAMPLE), concurrency=0)


@pytest.mark.parametrize('pair', [False, True])
@pytest.mark.parametrize('storage,scheduling,fail_spill', [('warm-only', 'static', False), ('tiered', 'snapshot_only', False), ('tiered', 'joint', False), ('tiered', 'joint', True)])
def test_snapshot_roundtrip_uses_configured_mechanism_and_cleans(
    tmp_path: Path, monkeypatch, storage, scheduling, fail_spill, pair,
) -> None:
    import clawbox.experiments.qualification as module

    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
        "policies": (BASELINES["predicted-wait-snapshot"].as_policy().model_copy(
            update={"runtime_snapshot": pair}),),
        "resources": spec.resources.model_copy(update={
            "prediction_artifact": "/data/p50.json",
            "snapshot_storage": storage,
            "memory_scheduling": scheduling,
            "pool_transfer_concurrency": 2,
            "pool_transfer_timeout_seconds": 17,
            "warm_snapshot_root": "/mnt/warm",
            "warm_memory_capacity_mib": 8192,
        }),
    })
    calls: list[str] = []

    class CommandResult:
        exit_code = 0
        stdout = ""

    class Commands:
        def run(self, command, timeout):
            calls.append(command)
            result = CommandResult()
            result.stdout = ("4242" if command.startswith("nohup sleep")
                             else command.removeprefix("printf "))
            return result

    class Files:
        value = ""

        def write(self, path, value):
            assert path == "/tmp/clawbox-qualification-state"
            self.value = value

        def read(self, path):
            assert path == "/tmp/clawbox-qualification-state"
            return self.value

    class Client:
        def __init__(self, **kwargs):
            pass

        def kill_owned_sandboxes(self, task_uid):
            calls.append("cleanup:" + task_uid)

        def list_owned_sandboxes(self, task_uid):
            return []

    class Lifecycle:
        def __init__(self, client, **kwargs):
            assert kwargs["snapshot_mechanism"] == "incremental-cow"
            assert kwargs["snapshot_storage"] == storage
            assert kwargs['default_snapshot_tier'].value == 'warm'
            assert kwargs['snapshot_pool'].transfer_concurrency == 2
            assert kwargs['snapshot_pool'].transfer_timeout_seconds == 17
            self.sandbox = type(
                "Sandbox", (), {"commands": Commands(), "files": Files()},
            )()
            self.numa_borrower = kwargs.get("numa_borrower")
            self.timings = []
            self.generation = 0
            self.pool = kwargs['snapshot_pool']
            self.snapshot_key = None
            self.role = kwargs['role']
            self.resident = False

        def start(self):
            calls.append("start")
            self.resident = True

        def checkpoint_and_evict(self):
            from clawbox.experiments.snapshot_pool import SnapshotKey
            calls.append("checkpoint")
            self.generation += 1
            if self.snapshot_key is not None:
                self.pool.remove(self.snapshot_key)
            self.snapshot_key = SnapshotKey('qualification', self.role, self.generation)
            self.pool.reserve(self.snapshot_key, 128)
            def spill(_manifest):
                calls.append('spill')
                if fail_spill:
                    raise LookupError('external Base unavailable')
            self.pool.commit(
                self.snapshot_key, path=f'/warm/g{self.generation}.mem',
                logical_bytes=4096, allocated_bytes=128,
                transferred_bytes=128,
                spiller=spill,
            )
            self.timings.append({
                "operation": "checkpoint", "status": "ok",
                "snapshot_metrics": {
                    "full_base": self.generation == 1,
                    "lineage_layers": self.generation,
                    "logical_bytes": 4096,
                    "transferred_bytes": 4096 if self.generation == 1 else 128,
                },
            })
            self.resident = False
            return .1

        def restore(self):
            calls.append("restore")
            self.resident = True

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "CubeSandboxClient", Client)
    monkeypatch.setattr(module, "CubeSandboxLifecycle", Lifecycle)
    if fail_spill:
        with pytest.raises(RuntimeError, match="snapshot qualification failed.*external Base unavailable"):
            snapshot_roundtrip_test(spec, artifact_root=tmp_path / "probe")
        receipt = json.loads((tmp_path / "probe/result.json").read_text())
        assert receipt["passed"] is False and receipt["cleanup_errors"] == []
        assert calls[-2] == "close" and calls[-1].startswith("cleanup:")
        return
    result = snapshot_roundtrip_test(spec, artifact_root=tmp_path / "probe")

    assert result["passed"] is True
    assert result["incremental_delta_verified"] is True
    assert result['pair_checkpoint_verified'] is pair
    if pair:
        assert calls.count('checkpoint') == 4
        assert calls.count('restore') == 4
        assert calls.count('kill -0 4242') == 2
        assert calls.count('close') == 2
        assert len(result['runtime_timings']) == 2
        assert result['cold_relocation_verified'] is (True if storage == 'tiered' else None)
        return
    assert calls[0:5] == [
        "start", "printf qualification-before", "checkpoint", "restore",
        "checkpoint",
    ]
    tail = ["spill", "restore", "close"] if storage == "tiered" else ["restore", "close"]
    assert calls[5:5 + len(tail)] == tail
    assert calls[5 + len(tail)].startswith("cleanup:qualification-snapshot-")
    assert result["cold_relocation_verified"] == (
        True if storage == "tiered" else None
    )


@pytest.mark.parametrize('residual', [0, 1])
@pytest.mark.parametrize('tool_mib', [512, 4096])
def test_live_borrow_roundtrip_executes_during_and_after_rebind(
    tmp_path: Path, monkeypatch, residual, tool_mib,
) -> None:
    import clawbox.experiments.qualification as module

    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
        "sandbox": spec.sandbox.model_copy(update={"memory_mib": tool_mib}),
        "resources": spec.resources.__class__.model_validate({
            **spec.resources.model_dump(),
            "pool_memory_budget_mib": 32768,
            "local_memory_low_watermark_mib": 28672,
            "local_memory_high_watermark_mib": 32768,
            "local_memory_capacity_mib": 36864,
            "shared_memory_borrow_limit_mib": 65536,
            "warm_memory_capacity_mib": 131072,
            "local_numa_node": 0,
            "warm_numa_node": 1,
            "local_memory_cgroup": "/sys/fs/cgroup/cube_sandbox/sandbox",
        }),
    })
    calls = []

    class Result:
        exit_code = 0

        def __init__(self, stdout):
            self.stdout = stdout

    class Commands:
        def run(self, command, timeout):
            calls.append(command)
            if command.startswith('free='):
                return Result('CLAWBOX_ALLOCATION_MIB=8')
            return Result(command.removeprefix("printf "))

    class Client:
        def __init__(self, **kwargs):
            pass

        def kill_owned_sandboxes(self, task_uid):
            calls.append("cleanup")

        def list_owned_sandboxes(self, task_uid):
            return []

    class Borrower:
        placements = []
        shared_node = 1
        def borrowed(self):
            return ()
        def __init__(self, *args, shared_pool, **kwargs):
            self.shared_pool = shared_pool

    def record(source, target, local_before, local_after, shared_before, shared_after,
               *, reservation_reused=False):
        return NumaBorrowRecord(
            sandbox_id="sandbox", cgroup="/cgroup/leaf",
            reserved_bytes=4096 * 1024 ** 2, source_node=source,
            target_node=target, started_unix_s=1, completed_unix_s=2,
            service_seconds=.1, local_bytes_before=local_before,
            local_bytes_after=local_after, shared_bytes_before=shared_before,
            shared_bytes_after=shared_after,
            reservation_reused=reservation_reused,
            reservation_released=target == 0 and shared_after == 0,
        )

    class Lifecycle:
        sandbox_id = 'sandbox'
        def __init__(self, client, **kwargs):
            self.sandbox = type("Sandbox", (), {"commands": Commands()})()
            self.borrower = kwargs["numa_borrower"]
            self.reserved = False

        def start(self):
            calls.append("start")

        def borrow_shared(self):
            reused = self.reserved
            if not self.reserved:
                self.borrower.shared_pool.reserve_borrow("sandbox", 4096 * 1024 ** 2)
                self.reserved = True
            return record(0, 1, 5, 1, 0, 4, reservation_reused=reused)

        def return_local_memory(self):
            if not residual:
                self.borrower.shared_pool.release_borrow("sandbox")
                self.reserved = False
            return record(1, 0, 1, 5, 4, residual)

        def close(self):
            calls.append("close")
            if residual:
                self.borrower.shared_pool.release_borrow("sandbox")

    monkeypatch.setattr(module, "CubeSandboxClient", Client)
    monkeypatch.setattr(module, "CubeSandboxLifecycle", Lifecycle)
    monkeypatch.setattr(module, "SandboxNumaBorrower", Borrower)
    monkeypatch.setattr(module, '_capture_borrow_residency',
                        lambda *args: {'resident_bytes': {0: 1, 1: 8}})

    result = live_borrow_roundtrip_test(spec, artifact_root=tmp_path / "borrow")

    assert result["passed"] is True
    assert calls[:3] == ["start", "printf local-ready", "printf shared-alive"]
    assert 'df -Pm /dev/shm' in calls[3]
    bound = min(128, tool_mib // 16)
    assert f'count={bound};' in calls[3]
    assert result['allocation_probe_mib'] == 8
    assert calls[4] == 'printf local-again'
    assert calls[-2:] == ["close", "cleanup"]


@pytest.mark.skipif(__import__('os').name == 'nt', reason='guest command requires a POSIX shell')
@pytest.mark.parametrize('free,maximum,expected', [(256, 32, 32), (20, 128, 16), (5, 32, 1), (4, 32, None)])
def test_borrow_probe_shell_enforces_bound_and_stops_before_dd(tmp_path, free, maximum, expected):
    import os
    import subprocess
    from clawbox.experiments.qualification import _borrow_allocation_command

    # Fake only the guest's df/dd; execute the actual shell control flow.
    df = tmp_path / 'df'
    df.write_text(f'#!/bin/sh\nprintf "header\\ntmpfs 1024 0 {free} 0 /dev/shm\\n"\n')
    dd = tmp_path / 'dd'
    witness = tmp_path / 'dd-args'
    dd.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$PROBE_WITNESS"\n')
    df.chmod(0o755)
    dd.chmod(0o755)
    result = subprocess.run(['sh', '-c', _borrow_allocation_command('test-probe', maximum)],
        env={**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
             'PROBE_WITNESS': str(witness)}, text=True, capture_output=True)
    if expected is None:
        assert result.returncode != 0 and not witness.exists()
    else:
        assert result.returncode == 0
        assert result.stdout.strip() == f'CLAWBOX_ALLOCATION_MIB={expected}'
        assert f'count={expected}\n' in witness.read_text()
