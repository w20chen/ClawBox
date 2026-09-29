from types import SimpleNamespace
from pathlib import Path
import json
import os
import pytest
from clawbox.lab import prepare_spec, template_record


def args(**changes):
    values = dict(spec=Path("examples/experiments/getting-started.yaml"), baseline=None,
                  reserve_during=None, estimate=None, idle=None, resume=None,
                  concurrency=[1], storage="memory", pool_gib=None, trace=None)
    values.update(changes)
    return SimpleNamespace(**values)


def profile():
    template = dict(template_id="tpl-test", source_image_reference="registry/image@sha256:" + "a"*64,
                    image_digest="sha256:"+"a"*64, memory_mib=2048, vcpu=2)
    return dict(
        node="node-a", runtime=template, sandbox=template,
        local_memory_cgroup="/sys/fs/cgroup/cube_sandbox/sandbox",
        local_memory_capacity_mib=65536, local_numa_node=0,
        warm_root="/mnt/warm", warm_capacity_mib=32768, warm_numa_node=1,
    )


def test_default_runs_resident_without_disk_snapshots():
    spec = prepare_spec(args(), profile())
    assert spec.policies[0].name == "tool-static-resident"
    assert spec.resources.snapshot_storage == "warm-only"
    assert spec.resources.cold_snapshot_root is None
    assert spec.resources.local_memory_cgroup == "/sys/fs/cgroup/cube_sandbox/sandbox"
    assert spec.resources.local_numa_node == 0
    assert spec.resources.warm_numa_node == 1


def test_memory_baseline_preserves_warm_capacity_through_expansion():
    from clawbox.experiments.spec import expand_matrix
    spec = prepare_spec(args(baseline=["tool-p50-wait-reactive"]), profile())
    arm, = expand_matrix(spec)
    assert arm.resources.warm_memory_capacity_mib == 32768
    assert arm.resources.snapshot_storage == "warm-only"


def test_run_can_override_static_tool_reservation():
    spec = prepare_spec(args(static_tool_memory_mib=512,
                             non_command_tool_memory_mib=16), profile())
    assert spec.resources.static_tool_memory_mib == 512
    assert spec.resources.non_command_tool_memory_mib == 16


def test_run_accepts_fractional_gib_pool_at_mib_precision():
    spec = prepare_spec(args(pool_gib=14.25), profile())
    assert spec.resources.pool_memory_budget_mib == 14592


def test_run_can_supply_wait_prediction_for_final_baseline():
    spec = prepare_spec(args(
        baseline=["tool-p50-wait-reactive"],
        model_wait_prediction_seconds=3.0,
        model_wait_prediction_source="separate-training-run",
    ), profile())
    assert spec.inference.configuration["model_wait_prediction_seconds"] == 3.0
    assert spec.inference.configuration["model_wait_prediction_source"] == (
        "separate-training-run"
    )


def test_run_can_repeat_and_randomize_formal_arms():
    spec = prepare_spec(args(
        baseline=["tool-static-resident", "tool-p50-resident"],
        repetitions=3, randomized_order=True, random_seed=20260928,
    ), profile())
    assert spec.workload.repetitions == 3
    assert spec.execution.randomized_order is True
    assert spec.execution.random_seed == 20260928


def test_c16_final_baseline_requires_room_for_both_vm_snapshots():
    roomy = profile()
    roomy["warm_capacity_mib"] = 128 * 1024
    spec = prepare_spec(args(
        baseline=["tool-p50-wait-reactive"], concurrency=[16], pool_gib=64,
        checkpoint_headroom_gib=8,
        model_wait_prediction_seconds=3.0,
        model_wait_prediction_source="separate-training-run",
    ), roomy)
    assert spec.execution.concurrency_levels == (16,)
    assert spec.resources.pool_memory_budget_mib == 64 * 1024
    assert spec.resources.warm_memory_capacity_mib == 128 * 1024
    assert spec.resources.checkpoint_restore_headroom_mib == 8 * 1024

    undersized = profile()
    undersized["warm_capacity_mib"] = 64 * 1024
    with pytest.raises(ValueError, match="WARM tmpfs is too small.*c16"):
        prepare_spec(args(
            baseline=["tool-p50-wait-reactive"], concurrency=[16], pool_gib=64,
        ), undersized)


def test_cold_dependent_policy_rejected_without_explicit_disk_permission():
    from clawbox.experiments.baselines import BASELINES
    name = next(n for n,p in BASELINES.items() if p.eviction_policy.value == "tiered_time_oracle")
    with pytest.raises(ValueError, match="require COLD"):
        prepare_spec(args(baseline=[name]), profile())


def test_bad_template_digest_rejected(monkeypatch):
    import clawbox.lab as lab
    class Template:
        @staticmethod
        def get(_):
            return SimpleNamespace(status="READY", image_info="image:latest")
    monkeypatch.setattr(lab, "sdk", lambda: (None, Template))
    with pytest.raises(ValueError, match="immutable"):
        template_record("tpl-test")


def test_local_listener_does_not_require_reverse_dns(monkeypatch):
    import socket
    from http.server import BaseHTTPRequestHandler
    from clawbox.experiments.http_server import LocalHTTPServer
    def forbidden(*_):
        raise AssertionError("control listener must not call external DNS")
    monkeypatch.setattr(socket, "getfqdn", forbidden)
    server = LocalHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert server.server_port > 0
    finally:
        server.server_close()


def test_setup_waits_for_definite_resource_rejection_and_destroys_probe(monkeypatch):
    import sys
    import clawbox.lab as lab
    calls = []
    class ApiError(Exception):
        pass
    class Probe:
        commands = SimpleNamespace(run=lambda *a, **k: SimpleNamespace(exit_code=0, stdout="clawbox-ready"))
        def kill(self):
            calls.append("kill")
    class Sandbox:
        @staticmethod
        def create(**kwargs):
            calls.append("create")
            if calls == ["create"]:
                raise ApiError("CubeMaster returned error code 130597: no more resource")
            return Probe()
    monkeypatch.setitem(sys.modules, "cubesandbox", SimpleNamespace(Sandbox=Sandbox, ApiError=ApiError))
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)
    lab.wait_for_vm_ready(profile())
    assert calls == ["create", "create", "kill"]


def test_setup_does_not_retry_ambiguous_create_failure(monkeypatch):
    import sys
    import clawbox.lab as lab
    class ApiError(Exception):
        pass
    def create(**kwargs):
        raise ApiError("request timed out")
    monkeypatch.setitem(sys.modules, "cubesandbox", SimpleNamespace(Sandbox=SimpleNamespace(create=create), ApiError=ApiError))
    monkeypatch.setattr(lab.time, "sleep", lambda _: pytest.fail("ambiguous create must not retry"))
    with pytest.raises(ApiError, match="timed out"):
        lab.wait_for_vm_ready(profile())


def test_setup_waits_for_storage_heartbeat_before_probing(monkeypatch):
    import clawbox.lab as lab
    ready = iter([False, False, True])
    sleeps = []
    monkeypatch.setattr(lab, "refresh_snapshot_storage", lambda _: next(ready))
    monkeypatch.setattr(lab.time, "sleep", sleeps.append)
    lab.wait_for_snapshot_storage(profile(), timeout=10)
    assert len(sleeps) == 2


def test_storage_wait_is_bounded_and_explains_failure(monkeypatch):
    import clawbox.lab as lab
    monkeypatch.setattr(lab, "refresh_snapshot_storage", lambda _: False)
    with pytest.raises(RuntimeError, match="heartbeat.*Profile not saved"):
        lab.wait_for_snapshot_storage(profile(), timeout=0)


@pytest.mark.parametrize("writable_mode", ["healthy", "warn"])
def test_snapshot_storage_refresh_requires_target_node_and_writable_mode(
    monkeypatch, writable_mode,
):
    import clawbox.lab as lab

    monkeypatch.setattr(lab.Path, "is_file", lambda self: True)
    monkeypatch.setattr(lab, "command", lambda *args, **kwargs: json.dumps({
        "ret": {"ret_code": 200, "ret_msg": "success"},
        "data": [{
            "node_id": "node-a",
            "node_ip": "10.0.0.1",
            "usage_pct": 70,
            "mode": writable_mode,
        }],
    }))
    assert lab.refresh_snapshot_storage({"node": "node-a"}) is True

    monkeypatch.setattr(lab, "command", lambda *args, **kwargs: json.dumps({
        "ret": {"ret_code": 200, "ret_msg": "success"},
        "data": [{"node_id": "node-a", "mode": "delete_only"}],
    }))
    assert lab.refresh_snapshot_storage({"node": "node-a"}) is False


def test_snapshot_sdk_ready_checkout_is_inferred(tmp_path, monkeypatch):
    import clawbox.lab as lab
    source = tmp_path / "CubeSandbox"
    module = source / "sdk/python/cubesandbox/sandbox.py"
    module.parent.mkdir(parents=True)
    module.write_text("# ready\n", encoding="utf-8")
    monkeypatch.setattr(lab, "_snapshot_sdk_info", lambda: {
        "ready": True, "file": str(module),
    })
    assert lab.ensure_snapshot_sdk() == source.resolve()


def test_snapshot_sdk_applies_only_sdk_hunks_and_reinstalls(tmp_path, monkeypatch):
    import clawbox.lab as lab
    monkeypatch.setattr(lab.sys, "path", list(lab.sys.path))
    source = tmp_path / "CubeSandbox"
    (source / ".git").mkdir(parents=True)
    module = source / "sdk/python/cubesandbox/sandbox.py"
    module.parent.mkdir(parents=True)
    module.write_text("class Sandbox:\n    pass\n", encoding="utf-8")
    inspections = iter((
        {"ready": False, "file": None},
        {"ready": True, "file": str(module)},
    ))
    calls = []
    monkeypatch.setattr(lab, "_snapshot_sdk_info", lambda: next(inspections))
    monkeypatch.setattr(
        lab, "command",
        lambda *argv, **kwargs: calls.append((argv, kwargs)) or "",
    )

    assert lab.ensure_snapshot_sdk(source) == source.resolve()
    apply_calls = [argv for argv, _ in calls if argv[:3] == ("git", "-C", str(source.resolve()))]
    assert len(apply_calls) == 4
    assert all("--include=sdk/python/cubesandbox/sandbox.py" in argv for argv in apply_calls)
    assert calls[-1][0][:5] == (
        os.sys.executable, "-m", "pip", "install", "--no-deps",
    )
    assert lab.sys.path[0] == str((source / "sdk/python").resolve())


def test_atomic_profile_write_preserves_previous_file_on_publish_failure(tmp_path, monkeypatch):
    import clawbox.lab as lab
    target = tmp_path / "host.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    monkeypatch.setattr(lab.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        lab._write_json_atomic(target, {"new": True})
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_warm_cleanup_rejects_path_escape(tmp_path):
    from clawbox.lab import cleanup_warm_snapshots
    with pytest.raises(ValueError, match="Invalid sandbox ID"):
        cleanup_warm_snapshots(tmp_path, ["../unrelated"])
    outside = tmp_path.parent/"unrelated"
    try:
        (tmp_path/("a"*32)).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows account cannot create directory symlinks")
        raise
    with pytest.raises(ValueError, match="escapes root"):
        cleanup_warm_snapshots(tmp_path, ["a"*32])


def test_warm_cleanup_only_removes_owned_directory(tmp_path, monkeypatch):
    import shutil
    import clawbox.lab as lab
    owned = tmp_path/("a"*32)
    unrelated = tmp_path/("b"*32)
    owned.mkdir(); unrelated.mkdir()
    def command(*args, **kwargs):
        if args[0] == "findmnt":
            return "tmpfs"
        assert args == ("sudo", "-n", "rm", "-rf", "--", str(owned))
        shutil.rmtree(owned)
        return ""
    monkeypatch.setattr(lab, "command", command)
    lab.cleanup_warm_snapshots(tmp_path, ["a"*32])
    assert unrelated.exists() and not owned.exists()
