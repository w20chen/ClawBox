from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawbox.experiments.baselines import BASELINES
from clawbox.experiments.qualification import (
    QUALIFICATION_SCHEMA_VERSION, implementation_digest,
    live_borrow_roundtrip_test, qualification_spec, snapshot_roundtrip_test,
    validate_receipt,
)
from clawbox.experiments.numa_borrow import NumaBorrowRecord
from clawbox.experiments.spec import load_experiment, spec_digest


EXAMPLE = Path("examples/experiments/getting-started.yaml")


def _receipt(spec, *, concurrency: int | None = None) -> dict:
    qualified = qualification_spec(spec, concurrency=concurrency)
    return {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "source_spec_digest": spec_digest(spec),
        "implementation_digest": implementation_digest(),
        "qualification_spec_digest": spec_digest(qualified),
        "qualified_unix_s": 1.0,
        "concurrency": max(qualified.execution.concurrency_levels),
        "policy_names": [policy.name for policy in qualified.policies],
        "target_node": spec.resources.target_node,
        "runtime_image_digest": spec.runtime.image_digest,
        "tool_image_digest": spec.sandbox.image_digest,
        "run_root": "/results/qualification-test",
        "supervision_fault_test": {"passed": True},
        "cleanup_verified": True,
        "status": "succeeded",
    }


def test_qualification_uses_all_policies_at_target_concurrency() -> None:
    spec = load_experiment(EXAMPLE)
    qualified = qualification_spec(spec, concurrency=3)

    assert qualified.inference.configuration["max_model_steps"] == 1
    assert qualified.execution.concurrency_levels == (3,)
    assert qualified.execution.randomized_order is False
    assert qualified.workload.repetitions == 1
    assert qualified.validation.command == "true"
    assert [item.name for item in qualified.policies] == [
        item.name for item in spec.policies
    ]


def test_receipt_is_bound_to_exact_spec_and_required_concurrency(tmp_path: Path) -> None:
    spec = load_experiment(EXAMPLE)
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps(_receipt(spec)), encoding="utf-8")

    assert validate_receipt(spec, path)["status"] == "succeeded"

    changed = spec.model_copy(update={
        "execution": spec.execution.model_copy(update={
            "concurrency_levels": (1, 2),
        }),
    })
    with pytest.raises(ValueError, match="source_spec_digest|concurrency"):
        validate_receipt(changed, path)

    stale = _receipt(spec)
    stale["implementation_digest"] = "0" * 64
    path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="implementation_digest"):
        validate_receipt(spec, path)


def test_snapshot_roundtrip_uses_configured_mechanism_and_cleans(
    tmp_path: Path, monkeypatch,
) -> None:
    import clawbox.experiments.qualification as module

    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
        "policies": (BASELINES["tool-p50-wait-reactive"].as_policy(),),
        "resources": spec.resources.model_copy(update={
            "prediction_artifact": "/data/p50.json",
            "snapshot_storage": "warm-only",
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
            result.stdout = command.removeprefix("printf ")
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
            assert kwargs["snapshot_storage"] == "warm-only"
            self.sandbox = type(
                "Sandbox", (), {"commands": Commands(), "files": Files()},
            )()
            self.timings = []
            self.generation = 0

        def start(self):
            calls.append("start")

        def checkpoint_and_evict(self):
            calls.append("checkpoint")
            self.generation += 1
            self.timings.append({
                "operation": "checkpoint", "status": "ok",
                "snapshot_metrics": {
                    "full_base": self.generation == 1,
                    "lineage_layers": self.generation,
                    "logical_bytes": 4096,
                    "transferred_bytes": 4096 if self.generation == 1 else 128,
                },
            })

        def restore(self):
            calls.append("restore")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "CubeSandboxClient", Client)
    monkeypatch.setattr(module, "CubeSandboxLifecycle", Lifecycle)
    result = snapshot_roundtrip_test(spec, artifact_root=tmp_path / "probe")

    assert result["passed"] is True
    assert result["incremental_delta_verified"] is True
    assert calls[0:5] == [
        "start", "printf qualification-before", "checkpoint", "restore",
        "checkpoint",
    ]
    assert calls[5] == "restore"
    assert calls[6] == "close"
    assert calls[7].startswith("cleanup:qualification-snapshot-")


def test_live_borrow_roundtrip_executes_during_and_after_rebind(
    tmp_path: Path, monkeypatch,
) -> None:
    import clawbox.experiments.qualification as module

    spec = load_experiment(EXAMPLE)
    spec = spec.model_copy(update={
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
            return Result(command.removeprefix("printf "))

    class Client:
        def __init__(self, **kwargs):
            pass

        def kill_owned_sandboxes(self, task_uid):
            calls.append("cleanup")

        def list_owned_sandboxes(self, task_uid):
            return []

    class Borrower:
        def __init__(self, *args, shared_pool, **kwargs):
            self.shared_pool = shared_pool

    def record(source, target, local_before, local_after, shared_before, shared_after):
        return NumaBorrowRecord(
            sandbox_id="sandbox", cgroup="/cgroup/leaf",
            reserved_bytes=4096 * 1024 ** 2, source_node=source,
            target_node=target, started_unix_s=1, completed_unix_s=2,
            service_seconds=.1, local_bytes_before=local_before,
            local_bytes_after=local_after, shared_bytes_before=shared_before,
            shared_bytes_after=shared_after,
        )

    class Lifecycle:
        def __init__(self, client, **kwargs):
            self.sandbox = type("Sandbox", (), {"commands": Commands()})()
            self.borrower = kwargs["numa_borrower"]

        def start(self):
            calls.append("start")

        def borrow_shared(self):
            self.borrower.shared_pool.reserve_borrow("sandbox", 4096 * 1024 ** 2)
            return record(0, 1, 5, 1, 0, 4)

        def return_local_memory(self):
            self.borrower.shared_pool.release_borrow("sandbox")
            return record(1, 0, 1, 5, 4, 0)

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "CubeSandboxClient", Client)
    monkeypatch.setattr(module, "CubeSandboxLifecycle", Lifecycle)
    monkeypatch.setattr(module, "SandboxNumaBorrower", Borrower)

    result = live_borrow_roundtrip_test(spec, artifact_root=tmp_path / "borrow")

    assert result["passed"] is True
    assert calls[:4] == ["start", "printf local-ready", "printf shared-alive", "printf local-again"]
    assert calls[-2:] == ["close", "cleanup"]
