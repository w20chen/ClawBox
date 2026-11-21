from types import SimpleNamespace
from pathlib import Path
import json
import os
import pytest
from clawbox.experiments.host_runtime import template_record


def args(**changes):
    values = dict(spec=Path("tests/fixtures/experiments/getting-started.yaml"), baseline=None,
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


















@pytest.mark.parametrize('image_revision', ['current', 'old'])
def test_doctor_checks_current_clawtune_images(monkeypatch, tmp_path, image_revision):
    from clawbox import clawtune_integration
    from clawbox.experiments import host_runtime

    revisions = []
    templates = []
    monkeypatch.setenv('CLAWTUNE_ROOT', str(tmp_path))
    monkeypatch.setattr(clawtune_integration, 'source_revision',
                        lambda root: revisions.append(root) or 'current')
    monkeypatch.setattr(host_runtime, 'template_record', lambda _: {'ready': True})
    monkeypatch.setattr(host_runtime, 'command',
                        lambda *args: 'active' if args[0] == 'systemctl' else 'true')

    def get_template(template_id):
        templates.append(template_id)
        return SimpleNamespace(create_request={'containers': [{'envs': [
            {'key': 'CLAWTUNE_REVISION', 'value': image_revision},
        ]}]})

    monkeypatch.setattr(host_runtime, 'sdk',
                        lambda: (None, SimpleNamespace(get=get_template)))
    failures = host_runtime.doctor({
        'runtime': {'template_id': 'runtime'},
        'sandbox': {'template_id': 'tool'},
    }, current_images=True)
    assert revisions == [tmp_path]
    assert templates == ['runtime']
    image_failures = [name for name in failures if 'current ClawTune' in name]
    assert len(image_failures) == (0 if image_revision == 'current' else 1)


def test_bad_template_digest_rejected(monkeypatch):
    import clawbox.experiments.host_runtime as lab
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
        assert server.allow_reuse_address is True
    finally:
        server.server_close()


def test_setup_waits_for_definite_resource_rejection_and_destroys_probe(monkeypatch):
    import sys
    import clawbox.experiments.host_runtime as lab
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
    import clawbox.experiments.host_runtime as lab
    class ApiError(Exception):
        pass
    def create(**kwargs):
        raise ApiError("request timed out")
    monkeypatch.setitem(sys.modules, "cubesandbox", SimpleNamespace(Sandbox=SimpleNamespace(create=create), ApiError=ApiError))
    monkeypatch.setattr(lab.time, "sleep", lambda _: pytest.fail("ambiguous create must not retry"))
    with pytest.raises(ApiError, match="timed out"):
        lab.wait_for_vm_ready(profile())


def test_setup_waits_for_storage_heartbeat_before_probing(monkeypatch):
    import clawbox.experiments.host_runtime as lab
    ready = iter([False, False, True])
    sleeps = []
    monkeypatch.setattr(lab, "refresh_snapshot_storage", lambda _: next(ready))
    monkeypatch.setattr(lab.time, "sleep", sleeps.append)
    lab.wait_for_snapshot_storage(profile(), timeout=10)
    assert len(sleeps) == 2


def test_storage_wait_is_bounded_and_explains_failure(monkeypatch):
    import clawbox.experiments.host_runtime as lab
    monkeypatch.setattr(lab, "refresh_snapshot_storage", lambda _: False)
    with pytest.raises(RuntimeError, match="heartbeat.*Profile not saved"):
        lab.wait_for_snapshot_storage(profile(), timeout=0)


@pytest.mark.parametrize("writable_mode", ["healthy", "warn"])
def test_snapshot_storage_refresh_requires_target_node_and_writable_mode(
    monkeypatch, writable_mode,
):
    import clawbox.experiments.host_runtime as lab
    from io import BytesIO
    from types import SimpleNamespace
    payload = {
        "ret": {"ret_code": 200, "ret_msg": "success"},
        "data": [{
            "node_id": "node-a",
            "node_ip": "10.0.0.1",
            "usage_pct": 70,
            "mode": writable_mode,
        }],
    }
    def open_storage(url, timeout):
        assert url == 'http://127.0.0.1:8089/cube/snapshot/storage?refresh=true'
        assert timeout == 10
        return BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(lab, 'build_opener', lambda *args: SimpleNamespace(open=open_storage))
    monkeypatch.setattr(lab, 'command', lambda *a, **kw: pytest.fail('must not spawn CLI'))
    assert lab.refresh_snapshot_storage({"node": "node-a"}) is True
    assert lab.refresh_snapshot_storage({"node": "other-node"}) is False
    payload['data'][0]['mode'] = 'delete_only'
    assert lab.refresh_snapshot_storage({"node": "node-a"}) is False


def test_snapshot_storage_refresh_reports_connection_failure(monkeypatch):
    import clawbox.experiments.host_runtime as lab
    from types import SimpleNamespace
    def unavailable(*args, **kwargs):
        raise TimeoutError('control-plane timed out')
    monkeypatch.setattr(lab, 'build_opener', lambda *a: SimpleNamespace(open=unavailable))
    assert lab.refresh_snapshot_storage({'node': 'node-a'}) is False


def test_snapshot_sdk_ready_checkout_is_inferred(tmp_path, monkeypatch):
    import clawbox.experiments.host_runtime as lab
    source = tmp_path / "CubeSandbox"
    module = source / "sdk/python/cubesandbox/sandbox.py"
    module.parent.mkdir(parents=True)
    module.write_text("# ready\n", encoding="utf-8")
    monkeypatch.setattr(lab, "_snapshot_sdk_info", lambda: {
        "ready": True, "file": str(module),
    })
    assert lab.ensure_snapshot_sdk() == source.resolve()


def test_snapshot_sdk_applies_only_sdk_hunks_and_reinstalls(tmp_path, monkeypatch):
    import clawbox.experiments.host_runtime as lab
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
    assert len(apply_calls) == 8
    assert any("sdk-control-timeouts.patch" in argv[-1] for argv in apply_calls)
    assert any("sdk-lifecycle-timeouts.patch" in argv[-1] for argv in apply_calls)
    assert all("--include=sdk/python/cubesandbox/sandbox.py" in argv for argv in apply_calls)
    assert calls[-1][0][:5] == (
        os.sys.executable, "-m", "pip", "install", "--no-deps",
    )
    assert lab.sys.path[0] == str((source / "sdk/python").resolve())


def test_atomic_profile_write_preserves_previous_file_on_publish_failure(tmp_path, monkeypatch):
    import clawbox.experiments.host_runtime as lab
    target = tmp_path / "host.json"
    target.write_text('{"old": true}\n', encoding="utf-8")
    monkeypatch.setattr(lab.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        lab._write_json_atomic(target, {"new": True})
    assert target.read_text(encoding="utf-8") == '{"old": true}\n'
    assert list(tmp_path.iterdir()) == [target]
