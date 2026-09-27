from types import SimpleNamespace
from pathlib import Path
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
    return dict(node="node-a", runtime=template, sandbox=template,
                warm_root="/mnt/warm", warm_capacity_mib=32768)


def test_default_runs_resident_without_disk_snapshots():
    spec = prepare_spec(args(), profile())
    assert spec.policies[0].name == "tool-full-resident"
    assert spec.resources.snapshot_storage == "warm-only"
    assert spec.resources.cold_snapshot_root is None


def test_memory_baseline_preserves_warm_capacity_through_expansion():
    from clawbox.experiments.spec import expand_matrix
    spec = prepare_spec(args(baseline=["tool-static-eager-reactive"]), profile())
    arm, = expand_matrix(spec)
    assert arm.resources.warm_memory_capacity_mib == 32768
    assert arm.resources.snapshot_storage == "warm-only"


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


def test_warm_cleanup_rejects_path_escape(tmp_path):
    from clawbox.lab import cleanup_warm_snapshots
    with pytest.raises(ValueError, match="Invalid sandbox ID"):
        cleanup_warm_snapshots(tmp_path, ["../unrelated"])
    outside = tmp_path.parent/"unrelated"
    (tmp_path/("a"*32)).symlink_to(outside, target_is_directory=True)
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
