import json
import os
from pathlib import Path
import threading

import pytest

from clawbox.experiments.peer_transport import (
    ObjectServer, _digest_file, _recv_header, _send_header,
    fetch_object, register_manifest, sparse_metadata,
)
from clawbox.experiments.snapshot_host import SnapshotStorageHostSpec, init, load


def test_host_plan_requires_independent_reviewed_storage(tmp_path):
    path = tmp_path / "host.yaml"
    init(path)
    value = load(path).model_dump()
    value["nodes"]["node1"]["filesystem_uuid"] = value["nodes"]["node0"]["filesystem_uuid"]
    with pytest.raises(ValueError, match="proven distinct physical devices"):
        SnapshotStorageHostSpec.model_validate(value)
    for index, node in enumerate(value["nodes"].values()):
        node["backing_mount"] = "/"
        node["physical_device"] = f"/dev/nvme{index}n1"
        node["image_allocation_receipt"] = f"/owned/node{index}.json"
    SnapshotStorageHostSpec.model_validate(value)
    value["nodes"]["node1"]["physical_device"] = "/dev/nvme0n1"
    with pytest.raises(ValueError, match="proven distinct physical devices"):
        SnapshotStorageHostSpec.model_validate(value)
    value["schema_version"] = 1
    with pytest.raises(ValueError):
        SnapshotStorageHostSpec.model_validate(value)


@pytest.mark.skipif(os.name != "posix", reason="Linux sparse files and flock")
def test_sparse_tcp_roundtrip_dedup_and_source_mutation(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    path = source / "state.mem"
    with path.open("wb") as stream:
        stream.write(b"start")
        stream.seek(32 * 1024 * 1024)
        stream.write(b"end")
    obj = {**sparse_metadata(path), "relative_path": "state.mem"}
    manifest = {"source_root": str(source), "objects": [obj]}
    register_manifest(source, manifest)
    os.link(path, source / "alias.mem")
    server = ObjectServer(("127.0.0.1", 0), source, "test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = fetch_object(target, obj, "127.0.0.1", server.server_address[1])
        restored = target / ".clawbox-peer" / "objects" / obj["object_id"]
        assert result["payload_bytes"] < obj["logical_bytes"]
        assert restored.stat().st_size == path.stat().st_size
        assert _digest_file(restored, obj["logical_bytes"], obj["extents"]) == obj["sha256"]
        os.link(restored, target / "lazy-backing-alias.mem")
        assert fetch_object(target, obj, "127.0.0.1", server.server_address[1])["payload_bytes"] == 0
        path.write_bytes(b"changed")
        with pytest.raises(RuntimeError, match="source changed"):
            fetch_object(tmp_path / "other-target", obj, "127.0.0.1", server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "posix", reason="Linux sparse files")
def test_export_rejects_foreign_paths_and_overlapping_extents(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"data")
    obj = {**sparse_metadata(foreign), "relative_path": "foreign", "source_path": str(foreign)}
    with pytest.raises(ValueError, match="storage domain"):
        register_manifest(source, {"source_root": str(source), "objects": [obj]})
    obj["extents"] = [[0, 3], [2, 2]]
    with pytest.raises(ValueError, match="overlapping"):
        register_manifest(source, {"source_root": str(source), "objects": [obj]})


@pytest.mark.skipif(os.name != "posix", reason="Linux sparse files and flock")
def test_incomplete_stream_never_publishes_ready_object(tmp_path):
    import socketserver
    path = tmp_path / "source"
    path.write_bytes(b"x" * 8192)
    obj = sparse_metadata(path)
    class Incomplete(socketserver.BaseRequestHandler):
        def handle(self):
            _recv_header(self.request)
            _send_header(self.request, {"ok": True, "object": obj})
            self.request.sendall(b"x" * 100)
    server = socketserver.TCPServer(("127.0.0.1", 0), Incomplete)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    target = tmp_path / "target"
    try:
        with pytest.raises(ConnectionError, match="incomplete"):
            fetch_object(target, obj, "127.0.0.1", server.server_address[1])
        cache = target / ".clawbox-peer" / "objects"
        assert not (cache / obj["object_id"]).exists()
        assert not list(cache.glob("*.part-*"))
        assert not list(cache.glob("*.json"))
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(os.name != "posix", reason="Linux cleanup flock and paths")
def test_cleanup_requires_native_release_and_preserves_prepared_objects(tmp_path, monkeypatch):
    from clawbox.experiments import snapshot_host as host
    from clawbox.cube import peer_replica
    plan_path = tmp_path / "plan.yaml"
    init(plan_path)
    plan = load(plan_path).model_dump()
    plan.update(state_root=str(tmp_path / "state"), receipt=str(tmp_path / "receipt.json"))
    plan["shared_dram"].update(root=str(tmp_path / "warm"), receipt=str(tmp_path / "host.json"))
    for key, node in plan["nodes"].items():
        node.update(backing_mount=str(tmp_path / key), image_path=str(tmp_path / key / "image.xfs"),
                    local_mount=str(tmp_path / key / "pool"))
    spec = SnapshotStorageHostSpec.model_validate(plan)
    host._write_json(host._state_path(spec), {"configuration": host._configuration(spec)})
    root = Path(spec.nodes["node0"].local_mount)
    cache = root / ".clawbox-peer" / "objects"
    cache.mkdir(parents=True)
    ids = [str(i) * 64 for i in (1, 2, 3)]
    for oid in ids:
        (cache / oid).write_bytes(b"payload")
        host._write_json(cache / (oid + ".json"), {})
    package = root / ".clawbox-peer" / "replicas" / ("4" * 64)
    package.mkdir(parents=True)
    os.link(cache / ids[0], package / "own-payload")
    host._write_json(package / "ready.json", {"run_id": "run", "run_ids": ["run"],
        "manifest_id": "4" * 64, "manifest": {"objects": [{"object_id": oid} for oid in ids[:2]]}})
    prepared = package.parent / "prepare-reference"
    prepared.mkdir()
    host._write_json(prepared / "ready.json", {"run_ids": [], "preparation": True,
        "manifest": {"objects": [{"object_id": ids[1]}]}})
    monkeypatch.setattr(host.os, "geteuid", lambda: 0)
    monkeypatch.setattr(host, "_mount", lambda _: {"source": "/dev/loop-test"})
    monkeypatch.setattr(host, "_run", lambda *args: "/dev/loop-test: owned")
    memory = Path(spec.shared_dram.root) / "run.mem"
    compressed = Path(str(memory) + ".zstd-pages")
    compressed.mkdir(parents=True)
    container = compressed / "pages.zstd"
    container.write_bytes(b"compressed generation")
    host._write_json(Path(str(memory) + ".zstd-pages.json"), {"groups": [{"container": str(container)}]})
    allow = False
    def native(socket, path, payload=None):
        if path == "/health":
            return {"domains": {key: node.local_mount for key, node in spec.nodes.items()}}
        if payload["apply"] and not allow:
            raise RuntimeError("active VMM")
        return {"native_references_released": payload["apply"], "catalogs": {"node0": [{
            "clawbox_run_id": "run", "memory_kind": "clawbox-direct-file", "memory_vol": str(memory)}]}}
    monkeypatch.setattr(peer_replica, "native_control", native)
    preview = host.cleanup_replicas(spec, "run", "/control.sock")
    assert not preview["applied"] and package.exists()
    assert preview["compressed_generations"] == [str(memory)] and container.exists()
    with pytest.raises(RuntimeError, match="active VMM"):
        host.cleanup_replicas(spec, "run", "/control.sock", execute=True)
    assert package.exists() and (cache / ids[0]).exists()
    allow = True
    report = host.cleanup_replicas(spec, "run", "/control.sock", execute=True)
    assert report["applied"] and not package.exists() and not (cache / ids[0]).exists()
    assert not compressed.exists() and not Path(str(memory) + ".zstd-pages.json").exists()
    assert prepared.exists() and (cache / ids[1]).exists() and (cache / ids[2]).exists()


@pytest.mark.skipif(os.name != "posix", reason="Linux sparse files and flock")
def test_replica_stage_preserves_shared_backing_and_private_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import subprocess
    from clawbox.experiments import peer_transport as peer
    roots = {node: str(tmp_path / node) for node in ("node0", "node1")}
    for root in roots.values():
        Path(root).mkdir()
    warm = tmp_path / "warm"
    warm.mkdir()
    memory = warm / "base.mem"
    memory.write_bytes(b"shared-memory")
    metadata = Path(roots["node0"]) / "metadata.json"
    metadata.write_text('{"value":"source"}')
    objects = [
        {**sparse_metadata(memory), "relative_path": "memory/base.mem", "source_path": str(memory), "storage_domain": "shared_dram"},
        {**sparse_metadata(metadata), "relative_path": "metadata/config.json", "source_path": str(metadata), "storage_domain": "node0"},
    ]
    manifest = {"snapshot_id": "fixture", "source_domain": "node0", "source_root": roots["node0"], "objects": objects}
    server = ObjectServer(("127.0.0.1", 0), Path(roots["node0"]), "test")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = {"netns": "test", "address": "127.0.0.1", "port": server.server_address[1]}
    receipt = tmp_path / "host-receipt.json"
    receipt.write_text(json.dumps({"host_configuration": {"nodes": {
        node: {"local_mount": root, "cpus": "0", "numa_node": index}
        for index, (node, root) in enumerate(roots.items())}}}))
    spec = SimpleNamespace(local_roots=roots, warm_root=str(warm), host_receipt=str(receipt),
                           transfer_endpoints={node: endpoint for node in roots})
    def run(command, *, input, **kwargs):
        assert command[:7] == ["ip", "netns", "exec", "test", "numactl", "--physcpubind=0", "--membind=1"]
        request = json.loads(input)
        rows = [fetch_object(Path(request["root"]), obj, request["address"], request["port"]) for obj in request["objects"]]
        return subprocess.CompletedProcess(command, 0, json.dumps({"objects": rows,
            "payload_bytes": sum(row["payload_bytes"] for row in rows),
            "protocol_bytes": sum(row["protocol_bytes"] for row in rows),
            "cache_hits": sum(row["cache_hit"] for row in rows)}), "")
    monkeypatch.setattr(peer.subprocess, "run", run)
    try:
        store = peer.PeerReplicaStore(spec, run_id="run-one")
        estimate = store.estimate_missing(manifest, "node1")
        assert estimate["shared_warm_hits"] == 1 and estimate["missing_objects"] == 1
        ready = store.stage_replica(manifest, "node1")
        assert ready["object_paths"]["memory/base.mem"] == str(memory)
        copied_json = Path(ready["object_paths"]["metadata/config.json"])
        cached_json = Path(roots["node1"]) / ".clawbox-peer" / "objects" / objects[1]["object_id"]
        assert copied_json.stat().st_ino != cached_json.stat().st_ino
        copied_json.write_text('{"value":"relocated"}')
        again = store.stage_replica(manifest, "node1")
        assert again["cache_hit"] and again["transfer"]["payload_bytes"] == 0
        assert json.loads(cached_json.read_text())["value"] == "source"
        assert ready["run_id"] == "run-one"
    finally:
        server.shutdown()
        server.server_close()
