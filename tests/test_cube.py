from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from trace_fixtures import llm_spans, write_spans

from clawbox.cube import (
    CubeCommandExecutor,
    CubeSandboxClient,
    CubeSandboxLifecycle,
    CubeSandboxTcpEndpoint,
    OwnedSandboxJournal,
    Ownership,
)
from clawbox.experiments import BASELINES, ExperimentSpec
import clawbox.experiments.worker as worker_module
from clawbox.experiments.policy import PolicyCoordinator
from clawbox.experiments.snapshot_pool import WarmSnapshotPool
from clawbox.experiments.spec_types import SnapshotTier
from clawbox.experiments.worker import ExperimentWorker


class _Files:
    def __init__(self) -> None:
        self.content: dict[str, str] = {}

    def read(self, path: str) -> str:
        return self.content[path]

    def write(self, path: str, data: str) -> None:
        self.content[path] = data


def test_snapshot_retention_value_uses_logical_state_not_private_delta() -> None:
    lifecycle = object.__new__(CubeSandboxLifecycle)
    lifecycle.snapshot_reuse_probability = 0.5
    lifecycle.warm_restore_seconds_per_gib = 0.1
    lifecycle.cold_restore_seconds_per_gib = 0.7

    assert lifecycle._retention_value_seconds(4 * 1024**3) == pytest.approx(1.2)


class _Commands:
    def __init__(self, owner) -> None:
        self.owner = owner

    def run(self, command: str, **kwargs):
        self.owner.command_calls.append((command, kwargs))
        return SimpleNamespace(exit_code=0, stdout="ok", stderr="")


class _Sandbox:
    items: dict[str, "_Sandbox"] = {}
    create_kwargs: dict = {}

    def __init__(self, data=None, config=None) -> None:
        data = data or {}
        self.sandbox_id = data.get("sandboxID", "sb-1")
        self.template_id = data.get("templateID", "tpl-1")
        self.metadata = data.get("metadata", {})
        self.state = data.get("state", "running")
        self.files = _Files()
        self.command_calls = []
        self.commands = _Commands(self)
        self.network_updates = []

    @classmethod
    def create(cls, **kwargs):
        cls.create_kwargs = kwargs
        item = cls({"sandboxID": f"sb-{len(cls.items) + 1}", "templateID": kwargs["template"],
                    "metadata": kwargs["metadata"]})
        cls.items[item.sandbox_id] = item
        return item

    @classmethod
    def connect(cls, sandbox_id, **kwargs):
        item = cls.items[sandbox_id]
        item.state = "running"
        return item

    @classmethod
    def list_v2(cls, **kwargs):
        return [{"sandboxID": item.sandbox_id, "templateID": item.template_id,
                 "metadata": item.metadata, "state": item.state} for item in cls.items.values()]

    def pause(self, wait=True, **kwargs):
        self.state = "paused"
        if kwargs:
            if kwargs.get("snapshot_mechanism") == "incremental-cow":
                memory = Path(kwargs["memory_snapshot_path"])
                memory.parent.mkdir(parents=True, exist_ok=True)
                memory.write_bytes(bytes(4096))
                layers = Path(str(memory) + ".layers")
                layers.mkdir()
                (layers / "000000.mem").hardlink_to(memory)
                Path(str(memory) + ".lineage.json").write_text(json.dumps({
                    "schema_version": 1, "logical_bytes": 4096,
                    "transferred_bytes": 4096, "full_base": True,
                    "layers": [{"name": "000000.mem"}],
                }))
            return {
                "memory_snapshot_path": kwargs["memory_snapshot_path"],
                "logical_bytes": 1024, "allocated_bytes": 1024,
                "transferred_bytes": 1024,
                "generation": kwargs["snapshot_generation"],
                "tier": kwargs["snapshot_tier"],
            }

    def relocate_snapshot(self, **kwargs):
        return {
            "memory_snapshot_path": kwargs["memory_snapshot_path"],
            "generation": kwargs["snapshot_generation"],
            "tier": kwargs["snapshot_tier"],
        }

    def get_tcp_endpoint(self, container_port):
        ordinal = int(self.sandbox_id.rsplit("-", 1)[-1]) if "-" in self.sandbox_id else 1
        return SimpleNamespace(
            sandbox_id=self.sandbox_id,
            container_port=container_port,
            address=f"192.0.2.{ordinal}:20{ordinal:03d}",
        )

    def update_network(self, network):
        self.network_updates.append(network)

    def kill(self):
        type(self).items.pop(self.sandbox_id, None)


def _owner() -> Ownership:
    return Ownership("run", "attempt", "task", "experiment", "session", "policy")


def test_cube_client_pins_node_disables_automatic_lifecycle_and_journals(tmp_path: Path) -> None:
    _Sandbox.items = {}
    journal = OwnedSandboxJournal(tmp_path / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal, sandbox_class=_Sandbox)
    sandbox = client.create_sandbox(template="tpl-arm64", node_name="node-a", ownership=_owner())
    assert _Sandbox.create_kwargs["timeout"] == -1
    assert _Sandbox.create_kwargs["lifecycle"] == {"on_timeout": "kill", "auto_resume": False}
    assert _Sandbox.create_kwargs["distribution_scope"] == ["node-a"]
    assert journal.sandbox_ids(task_uid="task") == [sandbox.sandbox_id]


def test_cube_client_adds_only_explicit_narrow_egress_allowlist() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    client.create_sandbox(
        template="tpl", node_name="node-a", ownership=_owner(),
        network_allow_out=["10.244.1.23/32"],
        network_deny_out=["0.0.0.0/0"],
    )
    assert _Sandbox.create_kwargs["network"] == {
        "allow_out": ["10.244.1.23/32"], "deny_out": ["0.0.0.0/0"],
    }


def test_cube_client_consumes_semantic_tcp_endpoint_and_checks_identity() -> None:
    sandbox = _Sandbox({"sandboxID": "sb-7"})
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    endpoint = client.get_tcp_endpoint(sandbox)
    assert endpoint == CubeSandboxTcpEndpoint(
        sandbox_id="sb-7", container_port=2222, address="192.0.2.7:20007",
    )

    class WrongEndpoint(_Sandbox):
        def get_tcp_endpoint(self, container_port):
            return SimpleNamespace(
                sandbox_id="another-sandbox", container_port=container_port,
                address="192.0.2.99:20099",
            )

    try:
        client.get_tcp_endpoint(WrongEndpoint({"sandboxID": "sb-8"}))
    except RuntimeError as exc:
        assert "identity mismatch" in str(exc)
    else:
        raise AssertionError("endpoint identity mismatch was not rejected")


def test_cube_client_retries_endpoint_publication_after_create() -> None:
    class EventuallyPublishedSandbox(_Sandbox):
        endpoint_calls = 0

        def get_tcp_endpoint(self, container_port):
            type(self).endpoint_calls += 1
            if type(self).endpoint_calls < 3:
                raise RuntimeError("HTTP 404: route not published yet")
            return super().get_tcp_endpoint(container_port)

    EventuallyPublishedSandbox.items = {}
    client = CubeSandboxClient(
        sandbox_class=EventuallyPublishedSandbox,
        tcp_endpoint_attempts=3,
        tcp_endpoint_initial_delay_s=0,
        tcp_endpoint_max_delay_s=0,
    )
    sandbox = EventuallyPublishedSandbox({"sandboxID": "sb-7"})
    endpoint = client.get_tcp_endpoint(sandbox)
    assert endpoint.address == "192.0.2.7:20007"
    assert EventuallyPublishedSandbox.endpoint_calls == 3


def test_lifecycle_refreshes_runtime_network_only_for_a_new_endpoint_host() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        allow_internet_access=False,
        network_allow_out=["192.0.2.10/32"],
        network_deny_out=["0.0.0.0/0"],
    )
    lifecycle.start()
    assert lifecycle.ensure_network_allow_out("192.0.2.10/32") is False
    assert lifecycle.ensure_network_allow_out("192.0.2.11/32") is True
    assert lifecycle.sandbox.network_updates == [{
        "allow_internet_access": False,
        "allow_out": ["192.0.2.10/32", "192.0.2.11/32"],
        "deny_out": ["0.0.0.0/0"],
    }]
    assert lifecycle.ensure_network_allow_out("192.0.2.11/32") is False
    lifecycle.close()


def test_cube_client_rejects_ready_template_with_wrong_pinned_image() -> None:
    expected = "sha256:" + "a" * 64

    class Template:
        status = "READY"
        image_info = "http://registry.example/clawbox/runtime@sha256:" + "b" * 64

        @classmethod
        def get(cls, _reference: str):
            return cls

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    with pytest.raises(RuntimeError, match="digest mismatch"):
        client.validate_template_image("tpl-runtime", expected)


def test_cube_client_accepts_ready_template_with_pinned_image() -> None:
    digest = "sha256:" + "a" * 64

    class Template:
        status = "READY"
        image_info = "http://registry.example/clawbox/runtime@" + digest

        @classmethod
        def get(cls, _reference: str):
            return cls

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    result = client.validate_template_image("tpl-runtime", digest)
    assert result["image_digest"] == digest


def test_cube_client_uses_official_list_when_detail_omits_image_info() -> None:
    digest = "sha256:" + "a" * 64

    class Item:
        template_id = "tpl-runtime"
        image_info = "http://registry.example/clawbox/runtime@" + digest

    class Template:
        status = "READY"
        image_info = ""

        @classmethod
        def get(cls, _reference: str):
            return cls

        @classmethod
        def list(cls):
            return [Item()]

    client = CubeSandboxClient(sandbox_class=_Sandbox, template_class=Template)
    result = client.validate_template_image("tpl-runtime", digest)
    assert result["image_digest"] == digest


def test_cube_client_bounds_a_stalled_command_stream() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox, command_stream_grace_s=0.01)
    sandbox = _Sandbox({"sandboxID": "stalled"})
    release = threading.Event()

    def stalled_run(_command: str, **_kwargs):
        release.wait()
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    sandbox.commands = SimpleNamespace(run=stalled_run)
    started = time.monotonic()
    try:
        client.run_command(sandbox, "true", timeout_s=0.02)
    except TimeoutError as exc:
        assert "command stream exceeded deadline" in str(exc)
    else:
        raise AssertionError("stalled command stream was not bounded")
    finally:
        release.set()
    assert time.monotonic() - started < 0.5


def test_incremental_pause_cannot_silently_use_legacy_api() -> None:
    sandbox = _Sandbox()
    with pytest.raises(ValueError, match="explicit snapshot tier"):
        CubeSandboxClient.pause_sandbox(sandbox)
    assert sandbox.state == "running"
    CubeSandboxClient.pause_sandbox(sandbox, snapshot_mechanism="full-copy")
    assert sandbox.state == "paused"


def test_missing_cold_destination_fails_before_pausing_vm() -> None:
    _Sandbox.items = {}
    lifecycle = CubeSandboxLifecycle(CubeSandboxClient(sandbox_class=_Sandbox),
                                    template="tpl", node_name="node-a", ownership=_owner())
    lifecycle.start()
    with pytest.raises(ValueError, match="cold_snapshot_root"):
        lifecycle.checkpoint_and_evict()
    assert lifecycle.resident and lifecycle.sandbox.state == "running"
    lifecycle.close()


def test_warm_only_never_falls_back_to_cold():
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(client, template="tpl", node_name="node-a", ownership=_owner(),
                                    warm_snapshot_root="/warm", cold_snapshot_root="/must-not-write",
                                    snapshot_storage="warm-only", snapshot_pool=WarmSnapshotPool(512),
                                    snapshot_reservation_bytes=1024)
    lifecycle.start()
    with pytest.raises(RuntimeError, match="disk fallback is disabled"):
        lifecycle.checkpoint_and_evict()
    assert lifecycle.resident and lifecycle.sandbox.state == "running"
    with pytest.raises(ValueError, match="COLD snapshots are disabled"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    lifecycle.close()


def test_lifecycle_preserves_id_across_pause_restore_and_executor(tmp_path) -> None:
    _Sandbox.items = {}
    calls=[]
    class Routed(_Sandbox):
        @classmethod
        def create(cls, **kwargs):
            calls.append('create')
            return super().create(**kwargs)
        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            calls.append('connect')
            return super().connect(sandbox_id, **kwargs)
    client = CubeSandboxClient(sandbox_class=Routed)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        cold_snapshot_root=str(tmp_path / "cold"),
        cgroup_token='a'*32, prepare_cgroup=lambda:calls.append('prepare'),
        verify_cgroup=lambda sandbox_id:calls.append(('verify',sandbox_id)),
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id
    assert Routed.create_kwargs['env_vars']['CLAWBOX_CGROUP_TOKEN']=='a'*32
    assert calls==['prepare','create',('verify',sandbox_id)]
    executor = CubeCommandExecutor(client, lambda: lifecycle.sandbox)
    assert executor.execute("printf ok", 5).stdout == "ok"
    lifecycle.checkpoint_and_evict()
    assert lifecycle.tier is SnapshotTier.COLD
    assert lifecycle.timings[-1]["snapshot_metrics"]["snapshot_mechanism"] == "incremental-cow"
    assert not lifecycle.resident
    assert lifecycle.checkpoint_and_evict() is None
    lifecycle.restore()
    assert calls[-3:]==['prepare','connect',('verify',sandbox_id)]
    assert lifecycle.sandbox_id == sandbox_id and lifecycle.resident
    lifecycle.close()
    assert sandbox_id not in _Sandbox.items


def test_lifecycle_passes_verified_cgroup_to_numa_borrower(tmp_path) -> None:
    _Sandbox.items = {}
    calls = []
    class Borrower:
        def pin_local(self, sandbox_id, *, verified_cgroup=None):
            calls.append((sandbox_id, verified_cgroup))
        def release_destroyed(self, sandbox_id):
            pass
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    leaf = tmp_path/'leaf'
    lifecycle = CubeSandboxLifecycle(
        client, template='tpl', node_name='node-a', ownership=_owner(),
        cold_snapshot_root=str(tmp_path/'cold'), numa_borrower=Borrower(),
        verify_cgroup=lambda sandbox_id: {'sandbox_id': sandbox_id, 'cgroup': str(leaf)},
    )
    lifecycle.start()
    assert calls == [(lifecycle.sandbox_id, str(leaf))]
    lifecycle.close()


def test_checkpoint_returns_borrowed_tombstone_to_local_before_restore(tmp_path) -> None:
    _Sandbox.items = {}
    calls = []

    class Borrower:
        def pin_local(self, sandbox_id, *, verified_cgroup=None):
            calls.append(('pin', sandbox_id))
        def borrowed(self):
            return (lifecycle.sandbox_id,)
        def return_local(self, sandbox_id):
            calls.append(('return', sandbox_id))
        def release_destroyed(self, sandbox_id):
            calls.append(('destroy', sandbox_id))

    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template='tpl',
        node_name='node-a', ownership=_owner(),
        cold_snapshot_root=str(tmp_path / 'cold'), numa_borrower=Borrower(),
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id
    lifecycle.checkpoint_and_evict()
    assert ('return', sandbox_id) in calls
    assert ('destroy', sandbox_id) not in calls
    lifecycle.restore()
    lifecycle.close()
    assert ('destroy', sandbox_id) in calls


def test_checkpoint_manifest_failure_does_not_mark_paused_vm_running() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
    )
    lifecycle.start()
    sandbox_id = lifecycle.sandbox_id

    def fail_after_pause(sandbox, **kwargs):
        sandbox.state = "paused"
        raise RuntimeError("lineage unreadable after remote pause")

    client.pause_sandbox = fail_after_pause
    with pytest.raises(RuntimeError, match="lineage unreadable"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    assert lifecycle.state.value == "unknown"
    assert not lifecycle.resident
    with pytest.raises(Exception, match="not resident"):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.COLD)
    lifecycle.close()
    assert sandbox_id not in _Sandbox.items


def test_restore_failure_after_remote_resume_does_not_mark_vm_swapped(tmp_path) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        cold_snapshot_root=str(tmp_path / "cold"),
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict()

    def fail_after_resume(sandbox_id, **kwargs):
        _Sandbox.items[sandbox_id].state = "running"
        raise RuntimeError("response lost after resume")

    client.connect_sandbox = fail_after_resume
    with pytest.raises(RuntimeError, match="response lost"):
        lifecycle.restore()
    assert lifecycle.state.value == "unknown"
    assert not lifecycle.resident
    lifecycle.close()
    assert not _Sandbox.items


def test_failed_warm_manifest_retains_inflight_capacity_until_destroy():
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(2048)
    lifecycle = CubeSandboxLifecycle(client, template='tpl', node_name='node-a',
        ownership=_owner(), warm_snapshot_root='/warm', cold_snapshot_root='/cold',
        snapshot_pool=pool, snapshot_reservation_bytes=1024)
    lifecycle.start()
    def lost_manifest(sandbox, **kwargs):
        sandbox.state = 'paused'
        raise RuntimeError('manifest lost after writing')
    client.pause_sandbox = lost_manifest
    with pytest.raises(RuntimeError, match='manifest lost'):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert pool.reserved_bytes == 1024
    assert lifecycle.state.value == 'unknown'
    lifecycle.close()
    assert pool.reserved_bytes == pool.committed_bytes == 0


def test_failed_warm_cleanup_keeps_capacity_until_files_are_removed(tmp_path):
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(2048)
    lifecycle = CubeSandboxLifecycle(client, template='tpl', node_name='node-a',
        ownership=_owner(), warm_snapshot_root=str(tmp_path), cold_snapshot_root='/cold',
        snapshot_pool=pool, snapshot_reservation_bytes=1024)
    lifecycle.start()
    written = []
    def lost_manifest(sandbox, **kwargs):
        path = Path(kwargs['memory_snapshot_path'])
        path.parent.mkdir(parents=True)
        path.write_bytes(b'owned snapshot')
        written.append(path)
        sandbox.state = 'paused'
        raise RuntimeError('manifest lost after writing')
    client.pause_sandbox = lost_manifest
    with pytest.raises(RuntimeError, match='manifest lost'):
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    with pytest.raises(Exception, match='snapshot files remain'):
        lifecycle.close()
    assert pool.reserved_bytes == 1024
    assert not _Sandbox.items
    written[0].unlink()
    lifecycle.close()
    assert pool.reserved_bytes == pool.committed_bytes == 0


@pytest.mark.parametrize("capacity", [0, 512])
def test_disabled_or_undersized_warm_pool_checkpoints_to_ssd(capacity) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.tier is SnapshotTier.COLD
    assert pool.committed_bytes == pool.reserved_bytes == 0
    lifecycle.restore()
    assert lifecycle.resident
    lifecycle.close()


@pytest.mark.parametrize("capacity, expected_tier", [(1024, SnapshotTier.COLD), (2048, SnapshotTier.WARM)])
def test_lazy_restore_retains_warm_until_next_commit_or_close(capacity, expected_tier):
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(capacity)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024, lazy_restore=True,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    lifecycle.restore()
    assert lifecycle.tier is SnapshotTier.LOCAL
    assert pool.committed_bytes == 1024
    assert pool.snapshot()["manifests"][0]["retained_by_vm"]
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycle.tier is expected_tier
    assert pool.committed_bytes == (1024 if expected_tier is SnapshotTier.WARM else 0)
    lifecycle.restore()
    lifecycle.close()
    assert pool.committed_bytes == pool.reserved_bytes == 0


def test_warm_overflow_spills_authoritative_lru_to_cold() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1024, clock=lambda: 1.0)
    owners = [
        Ownership("run", "attempt", "task", "experiment", f"session-{index}", "policy")
        for index in (1, 2)
    ]
    lifecycles = [
        CubeSandboxLifecycle(
            client, template="tpl", node_name="node-a", ownership=owner,
            warm_snapshot_root="/warm", cold_snapshot_root="/cold",
            snapshot_pool=pool, snapshot_reservation_bytes=1024,
            snapshot_mechanism="full-copy",
        )
        for owner in owners
    ]
    for lifecycle in lifecycles:
        lifecycle.start()
        lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    assert lifecycles[0].tier is SnapshotTier.COLD
    assert lifecycles[1].tier is SnapshotTier.WARM
    assert pool.committed_bytes == 1024
    spill = next(item for item in lifecycles[0].timings if item["operation"] == "spill")
    assert spill["tier_from"] == "warm" and spill["tier_to"] == "cold"


@pytest.mark.parametrize("operation", ["restore", "close"])
def test_lifecycle_consumption_waits_for_selected_spill(operation: str) -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    pool = WarmSnapshotPool(1024)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
        warm_snapshot_root="/warm", cold_snapshot_root="/cold",
        snapshot_pool=pool, snapshot_reservation_bytes=1024,
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict(tier=SnapshotTier.WARM)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    relocate = client.relocate_snapshot

    def delayed_relocate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return relocate(*args, **kwargs)

    client.relocate_snapshot = delayed_relocate

    def consume():
        getattr(lifecycle, operation)()
        finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        spilling = executor.submit(pool.spill_for_admission, 1024)
        assert entered.wait(5)
        consuming = executor.submit(consume)
        try:
            assert not finished.wait(0.1)
        finally:
            release.set()
        spilling.result(timeout=5)
        consuming.result(timeout=5)
    assert pool.committed_bytes == 0
    if operation == "restore":
        assert lifecycle.resident and lifecycle.tier is SnapshotTier.LOCAL
        lifecycle.close()
    assert not _Sandbox.items


@pytest.mark.parametrize("fail_runtime_create", [False, True])
def test_openclaw_snapshot_pauses_runtime_and_restores_it_before_model_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_runtime_create: bool,
) -> None:
    coordinators = []
    create_claims_at_cleanup = []
    real_coordinator = worker_module.PolicyCoordinator

    class ObservedCoordinator(real_coordinator):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            coordinators.append(self)

        def blocked_demand_pressure(self):
            return True  # exercise supported wait-aware eviction under pressure

        def unregister(self, session_id):
            create_claims_at_cleanup.append((session_id, "create") in self.demand.claims)
            super().unregister(session_id)

    monkeypatch.setattr(worker_module, "PolicyCoordinator", ObservedCoordinator)

    class SnapshotCommands(_Commands):
        def run(self, command: str, **kwargs):
            self.owner.command_calls.append((command, kwargs))
            if "kill -0 $pid" in command:
                return SimpleNamespace(exit_code=0, stdout="4242", stderr="")
            if 'printf %s "$TASK_ID"' in command:
                match = re.search(r"/state/openclaw/([^/]+)/ssh/id_ed25519", command)
                assert match is not None
                return SimpleNamespace(
                    exit_code=0, stdout=match.group(1), stderr="",
                )
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class SnapshotSandbox(_Sandbox):
        sequence = 0
        created: list["SnapshotSandbox"] = []

        def __init__(self, data=None, config=None) -> None:
            super().__init__(data, config)
            self.pause_calls = 0
            self.resume_calls = 0
            self.commands = SnapshotCommands(self)

        @classmethod
        def create(cls, **kwargs):
            if fail_runtime_create and kwargs["template"] == "runtime-tpl":
                raise RuntimeError("injected Runtime creation failure")
            cls.sequence += 1
            item = cls({
                "sandboxID": f"snapshot-{cls.sequence}",
                "templateID": kwargs["template"],
                "metadata": kwargs["metadata"],
            })
            cls.items[item.sandbox_id] = item
            cls.created.append(item)
            item.create_kwargs = kwargs
            return item

        @classmethod
        def connect(cls, sandbox_id, **kwargs):
            item = cls.items[sandbox_id]
            item.state = "running"
            item.resume_calls += 1
            return item

        def pause(self, wait=True, **kwargs):
            self.pause_calls += 1
            return super().pause(wait=wait, **kwargs)

    SnapshotSandbox.items = {}
    SnapshotSandbox.sequence = 0
    SnapshotSandbox.created = []
    # This simulated lifecycle has no host microVM processes. Keep the real
    # sampler, but isolate its procfs discovery from the test host OS.
    from clawbox.experiments.memory import SandboxRSSSampler
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(worker_module, "SandboxRSSSampler",
                        lambda sandbox_id, **kw: SandboxRSSSampler(sandbox_id, proc_root=proc_root, **kw))
    # Emulate only cgroup I/O; exercise the real demand ledger and independent
    # OFF runtime controller through the complete worker lifecycle.
    cg = tmp_path/'cg'
    cg.mkdir()
    def files(path):
        path.mkdir(parents=True, exist_ok=True)
        for key, value in {'memory.max': str(100000*1024**2), 'memory.current': '0',
                           'cgroup.procs': '',
                           'memory.stat': 'anon 0\nfile 0\npgscan 0\npgsteal 0\n',
                           'memory.low': '0', 'memory.high': 'max', 'cpu.weight': '100',
                           'memory.events': 'high 0\noom_kill 0\n'}.items():
            (path/key).write_text(value)
    files(cg)
    from clawbox.experiments.priority import PriorityDomain
    class FakePriorityDomain(PriorityDomain):
        def start(self):
            for branch in (self.high, self.low):
                files(branch)
            for i in self.priorities:
                for role in ('tool', 'runtime'):
                    branch = self.high if self.priorities[i] == 'high' else self.low
                    leaf = branch/f'session-{i}'/role
                    files(leaf.parent); files(leaf)
                    self.leaves[i, role] = leaf
        def prepare(self, *args): pass
        def verify(self, index, role, sandbox_id):
            return {'priority': self.priorities[index], 'cgroup': str(self.leaves[index, role])}
        def close(self): pass
    monkeypatch.setattr(worker_module, 'PriorityDomain', FakePriorityDomain)
    def unexpected_process_scan(*args):
        raise AssertionError('Tool admission rescanned /proc despite a verified VM leaf')
    monkeypatch.setattr(worker_module, 'locate_cgroup', unexpected_process_scan)
    trace = tmp_path / "openclaw-snapshot.jsonl"
    write_spans(trace, llm_spans(
        [{"role": "user", "content": "hello"}],
        {"content": "", "tool_calls": [{"id": "call-1", "type": "function",
         "function": {"name": "exec", "arguments": '{"command":"true"}'}}]},
        duration_ms=500,
    ))

    def post_policy(policy_control, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            policy_control.url + path,
            data=json.dumps(body).encode(), method="POST",
            headers={
                "Authorization": f"Bearer {policy_control.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            pytest.fail(f"policy {path} returned {exc.code}: {exc.read().decode()}")

    def fake_run_openclaw(*, prompt, session_id, configuration, ssh,
                          policy_control, runtime_executor, output_dir,
                          timeout_seconds, model_gateway, prediction_manifest=None,
                          resident_poll=None, checkpoint_relay=False):
        # Pair creation must release its own claim before the Agent runs,
        # even when demand-backed Guest RAM uses less than configured capacity.
        # Otherwise the unused footprint remains pending until session teardown.
        assert coordinators
        assert all((session_id, "create") not in c.demand.claims for c in coordinators)
        assert resident_poll is not None
        assert checkpoint_relay is True
        assert model_gateway.gateway.timeout_s == 120
        assert (
            SnapshotSandbox.created[1].create_kwargs["env_vars"]
            ["CLAWBOX_MODEL_GATEWAY_TOKEN"]
            == model_gateway.token
        )
        payload = {
            "model": "recorded-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
        status, _content_type, _body, request_id = model_gateway.gateway.complete_http(payload)
        assert status == 200
        # Response preparation restores only Runtime. Tool remains
        # swapped until the following Tool admission reserves memory for it.
        assert SnapshotSandbox.created[0].resume_calls == 0
        model_gateway.mark_delivery(request_id, delivered=True)
        command = "true"
        command_sha256 = hashlib.sha256(command.encode()).hexdigest()
        admission = post_policy(policy_control, "/v1/tool/admit", {
            "session_id": session_id, "execution_id": "exec-1",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": time.time(),
        })
        assert admission["decision"] == "ADMIT"
        started = time.time()
        post_policy(policy_control, "/v1/tool/complete", {
            "session_id": session_id, "execution_id": "exec-1",
            "operation": "exec", "command_sha256": command_sha256,
            "runtime_request_at": started,
            "execution_started_at": started,
            "ssh_reaped_at": started + 0.01,
            "execution_completed_at": started + 0.01,
            "exit_code": 0,
            "endpoint_sandbox_id": admission["sandbox_id"],
            "endpoint_epoch": admission["epoch"],
            "endpoint_host": admission["host"],
            "endpoint_port": admission["port"],
        })
        return {
            "agent_pid_file": f"/state/openclaw/{session_id}/agent.pid",
            "tool_latencies": [0.01], "runtime_traces": [],
            "agent_replay_invalid": True, "agent_tool_failures": 0,
            "policy_control_records": policy_control.records(),
        }

    monkeypatch.setattr(worker_module, "run_openclaw", fake_run_openclaw)
    monkeypatch.setattr(
        worker_module, "collect_and_validate_native_tool_artifacts",
        lambda **_kwargs: SimpleNamespace(
            root=tmp_path / "native", validation={"ok": True},
            cgroup_artifacts={"exec-1": {
                "memory_rss_peak_bytes": 2 * 1024 * 1024,
                "ts_start": 1.0, "ts_end": 1.01,
                "cpu_utilization_avg_cores": 0.25,
            }},
        ),
    )
    monkeypatch.setenv("CLAWBOX_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAWBOX_MODEL_GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAWBOX_POLICY_PORT", "0")
    monkeypatch.setenv("CLAWBOX_MODEL_GATEWAY_PORT", "0")
    spec = ExperimentSpec.model_validate({
        "schema_version": 3, "experiment_id": "openclaw-snapshot-test",
        "workload": {"source": "recorded_trace", "input": str(trace), "cases": [{
            "case_id": "snapshot-case", "source": "recorded_trace",
            "source_reference": str(trace), "replay_trace_reference": str(trace),
            "prompt": "hello",
        }]},
        "agent": {"driver": "openclaw"},
        "inference": {"backend": "replay", "configuration": {
            "model": "recorded-model", "model_wait_prediction_seconds": 0.3,
            "model_wait_prediction_source": "frozen-test-predictor",
        }},
        "runtime": {"template_alias": "runtime-tpl", "memory_mib": 2048},
        "sandbox": {"template_alias": "tool-tpl", "memory_mib": 4096},
        "execution": {"concurrency_levels": [1], "randomized_order": False,
                       "arm_timeout_seconds": 10, "command_timeout_seconds": 5,
                       "stabilization_seconds": 0},
        "resources": {"target_node": "node-a", "pool_memory_budget_mib": 100000,
                       "local_memory_cgroup": str(cg),
                       "emergency_free_memory_mib": 1, "checkpoint_restore_headroom_mib": 1,
                       "static_tool_memory_mib": 1,
                       "cold_snapshot_root": str(tmp_path / "cold")},
        "policies": [{"name": "snapshot", "admission": "fixed",
                      "runtime_control": "off",
                      "reclamation": "snapshot_pause", "eviction": "wait_aware_pressure",
                      "restore": "reactive"}],
    })
    result = ExperimentWorker(
        spec, run_id="openclaw-snapshot", attempt_id="attempt", task_uid="task",
        output_root=tmp_path / "results",
        client=CubeSandboxClient(sandbox_class=SnapshotSandbox),
    ).run()[0]

    assert create_claims_at_cleanup and not any(create_claims_at_cleanup), result.correctness
    if fail_runtime_create:
        assert result.status.value == "failed"
        assert len(SnapshotSandbox.created) == 1
        assert SnapshotSandbox.items == {}
        assert all(not c.demand.claims for c in coordinators)
        return
    assert result.status.value == "succeeded", result.model_dump()
    assert result.provenance["evidence_class"] == "deterministic-managed-replay"
    assert result.provenance["configured_trace_sha256"] == hashlib.sha256(
        trace.read_bytes()
    ).hexdigest()
    observation = result.performance["tool_execution_observations"][0]
    assert observation["actual_measured_memory_mib"] == 2.0
    assert observation["admitted_reservation_mib"] == 1
    assert observation["telemetry_validity"] == "valid"
    # Worker creates the Tool before the Runtime, even though the Runtime is
    # the long-lived OpenClaw process whose PID is witnessed.
    tool, runtime = SnapshotSandbox.created
    assert runtime.pause_calls >= 1
    assert runtime.resume_calls >= 1
    assert tool.pause_calls >= 1
    assert SnapshotSandbox.items == {}
    event_path = next((tmp_path / "results" / "events").glob("*.jsonl"))
    event_rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    pid_phases = {
        row["phase"] for row in event_rows
        if row.get("event") == "openclaw_agent_pid_observed"
    }
    assert "before_runtime_pause" in pid_phases
    assert {
        "after_runtime_restore_scheduled", "after_runtime_restore_response",
    } & pid_phases
    pause_roles = {
        row.get("role") for row in event_rows if row.get("event") == "sandbox_paused"
    }
    restore_roles = {
        row.get("role") for row in event_rows if row.get("event") == "sandbox_restored"
    }
    assert {"runtime", "tool"} <= pause_roles
    assert {"runtime", "tool"} <= restore_roles
    gateway_path = next((tmp_path / "results" / "model-gateway").glob("*.json"))
    gateway_record = json.loads(gateway_path.read_text())[0]
    admission = gateway_record["admission"]
    assert admission["runtime_snapshot_enabled"] is True
    assert admission["tool_snapshot_performed"] is True
    assert admission["runtime_snapshot_performed"] is True
    assert admission["runtime_pause_started_at"] <= admission["runtime_pause_completed_at"]
    assert admission["runtime_restore_started_at"] <= admission["runtime_restore_completed_at"]
    assert (
        admission["runtime_pause_completed_at"]
        <= admission["runtime_restore_started_at"]
        <= gateway_record["response_released_unix_s"]
    )
    assert admission["runtime_agent_pid_before_pause"] == 4242
    assert admission["runtime_agent_pid_after_restore"] == 4242
    session_spans = result.performance["session_time_spans"][0]
    span_names = {span["name"] for span in session_spans}
    assert {
        "model.wait", "model.response_hold", "model.response_delivery",
        "tool.operation", "sandbox.tool.create", "sandbox.tool.checkpoint",
        "sandbox.tool.restore", "sandbox.tool.destroy",
        "sandbox.runtime.create", "sandbox.runtime.checkpoint",
        "sandbox.runtime.restore", "sandbox.runtime.destroy",
    } <= span_names
    for span in session_spans:
        assert span["end_unix_s"] >= span["start_unix_s"]
        assert span["duration_seconds"] >= 0
        if "start_monotonic_s" in span:
            assert span["end_monotonic_s"] >= span["start_monotonic_s"]


def test_lifecycle_records_failed_create_with_state_and_error_type() -> None:
    class FailingClient:
        def create_sandbox(self, **_kwargs):
            raise TimeoutError("create timed out")

    lifecycle = CubeSandboxLifecycle(
        FailingClient(), template="tpl", node_name="node-a", ownership=_owner(),
    )
    with pytest.raises(TimeoutError, match="timed out"):
        lifecycle.start()
    assert lifecycle.state.value == "new"
    assert lifecycle.timings[-1]["operation"] == "create"
    assert lifecycle.timings[-1]["status"] == "error"
    assert lifecycle.timings[-1]["error_type"] == "TimeoutError"
    assert lifecycle.timings[-1]["state_before"] == "new"
    assert lifecycle.timings[-1]["state_after"] == "new"


def test_lifecycle_records_host_physical_memory_reclamation() -> None:
    _Sandbox.items = {}
    samples = iter([
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 100},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 120},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 120},
        {"metric": "host_meminfo_memavailable", "host_used_bytes": 70},
    ])
    lifecycle = CubeSandboxLifecycle(
        CubeSandboxClient(sandbox_class=_Sandbox), template="tpl",
        node_name="node-a", ownership=_owner(),
        physical_observation=lambda: next(samples),
        snapshot_mechanism="full-copy",
    )
    lifecycle.start()
    lifecycle.checkpoint_and_evict()
    checkpoint = lifecycle.timings[-1]
    assert checkpoint["host_memory_before"]["host_used_bytes"] == 120
    assert checkpoint["host_memory_after"]["host_used_bytes"] == 70
    assert checkpoint["host_observed_reclaimed_bytes"] == 50
    assert checkpoint["host_observed_growth_bytes"] == 0
    assert checkpoint["host_observed_net_change_bytes"] == -50
    assert checkpoint["host_reclamation_evidence"] == "observed_whole_host_reclamation"
    assert checkpoint["lifecycle_mechanism"] == "cubesandbox_pause_snapshot_destroy"
    assert checkpoint["host_memory_effect"]["host_used_bytes"] == {
        "before_bytes": 120, "after_bytes": 70, "net_change_bytes": -50,
        "reclaimed_bytes": 50, "growth_bytes": 0,
    }


def test_observed_command_preserves_execution_id_and_reads_cgroup_artifact() -> None:
    _Sandbox.items = {}
    client = CubeSandboxClient(sandbox_class=_Sandbox)
    lifecycle = CubeSandboxLifecycle(
        client, template="tpl", node_name="node-a", ownership=_owner(),
    )
    lifecycle.start()
    sandbox = lifecycle.sandbox
    execution_id = "call:123"
    cgroup = {
        "schema": "cgroup_resource_v1", "execution_id": execution_id,
        "source": "cgroup-v2", "memory_rss_peak_bytes": 4096,
    }
    sandbox.files.content[
        "/var/lib/clawtune/artifacts/tool-resource/cgroup-resource-call_123.json"
    ] = json.dumps(cgroup)
    pmu = {"schema": "pmu_profile_v1", "execution_id": execution_id}
    pmu_path = "/var/lib/clawtune/artifacts/tool-resource/pmu-profile-call_123.json"
    sandbox.files.content[pmu_path] = json.dumps(pmu)

    def run(command: str, **_kwargs):
        encoded = command.rsplit(" ", 1)[-1]
        envelope = base64.b64decode(encoded).decode()
        assert envelope.endswith("\nprintf observed")
        record = {
            "execution_id": execution_id, "exit_code": 0,
            "telemetry_state": "complete", "telemetry_artifact": "",
            "pmu_state": "reliable", "pmu_artifact": pmu_path,
        }
        return SimpleNamespace(
            exit_code=0, stdout="observed", stderr=(
                "diagnostic\nCLAWBOX_TELEMETRY_RECORD=" + json.dumps(record) + "\n"
            ),
        )

    sandbox.commands = SimpleNamespace(run=run)
    observed = CubeCommandExecutor(
        client, lambda: lifecycle.sandbox,
    ).execute_observed("printf observed", 5, execution_id=execution_id)
    assert observed.result.stdout == "observed"
    assert observed.result.stderr == "diagnostic"
    assert observed.bridge_record["execution_id"] == execution_id
    assert json.loads(observed.artifacts["cgroup_resource_v1"])[
        "memory_rss_peak_bytes"
    ] == 4096
    assert json.loads(observed.artifacts["pmu_profile_v1"])["execution_id"] == execution_id
    assert observed.telemetry_unavailable_reason is None
    lifecycle.close()


def test_cleanup_uses_journal_and_metadata_fallback(tmp_path: Path) -> None:
    _Sandbox.items = {}
    journal = OwnedSandboxJournal(tmp_path / "owned-sandboxes.jsonl")
    client = CubeSandboxClient(journal=journal, sandbox_class=_Sandbox)
    first = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    second = _Sandbox.create(template="tpl", metadata=_owner().metadata())
    client._handles.clear()
    client.kill_owned_sandboxes("task")
    assert first.sandbox_id not in _Sandbox.items
    assert second.sandbox_id not in _Sandbox.items


def test_cleanup_retries_transient_kill_and_removes_every_owned_sandbox() -> None:
    class PartiallyFailingSandbox(_Sandbox):
        failed = False

        def kill(self):
            if not type(self).failed:
                type(self).failed = True
                raise RuntimeError("transient kill failure")
            return super().kill()

    PartiallyFailingSandbox.items = {}
    client = CubeSandboxClient(sandbox_class=PartiallyFailingSandbox)
    first = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    second = client.create_sandbox(template="tpl", node_name="node", ownership=_owner())
    client.kill_owned_sandboxes("task")
    assert second.sandbox_id not in PartiallyFailingSandbox.items
    assert first.sandbox_id not in PartiallyFailingSandbox.items
    assert PartiallyFailingSandbox.items == {}
