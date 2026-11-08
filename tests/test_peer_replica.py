import json
import os
from pathlib import Path

import pytest

from clawbox.cube import peer_replica


pytestmark = pytest.mark.skipif(not hasattr(os, "SEEK_DATA"), reason="Linux sparse/native packages")


def _fixture(root):
    meta = root / "snapshots" / "snap-fixture" / "metadata"
    (meta / "snapshot").mkdir(parents=True)
    rootfs = root / "frozen.ext4"
    rootfs.write_bytes(b"frozen-rootfs")
    (meta / "snapshot" / "config.json").write_text(json.dumps({"disks": [{"path": "/old/live-rootfs"}]}))
    memory = root / "cold" / "guest.mem"
    memory.parent.mkdir()
    catalog = {"snapshot_id": "snap-fixture", "rootfs_dev": str(rootfs), "meta_dir": str(meta),
               "memory_dev": str(memory), "kind": "pause_snapshot", "spec_dir": "2C1024M"}
    return catalog, memory


def test_export_includes_every_lineage_layer_and_preserves_sibling_names(tmp_path, monkeypatch):
    catalog, memory = _fixture(tmp_path)
    memory.write_bytes(b"new-delta")
    layers = Path(str(memory) + ".layers")
    layers.mkdir()
    (layers / "000000.mem").write_bytes(b"old-valid-pages")
    external = tmp_path / "base.mem"
    external.write_bytes(b"external-base")
    Path(str(memory) + ".lineage.json").write_text(json.dumps({"layers": [
        {"name": "000000.mem", "ranges": [{"offset": 0, "length": 3}]},
        {"name": "base", "external_path": str(external), "ranges": [{"offset": 3, "length": 3}]}]}))
    (Path(catalog["meta_dir"]) / "sandbox_spec.json").write_text(json.dumps({
        "containers": [{"volume_mounts": [{"container_path": "/"}],
                        "readiness_probe": {"http_get": {"path": "/"}}}],
        "volumes": [{"sandbox_path": {"path": "/"}}],
    }))
    monkeypatch.setattr(peer_replica, "native_control", lambda *args: catalog)
    manifest = peer_replica.export_snapshot("socket", storage_domain="node0", domain_root=str(tmp_path), memory_path=str(memory))
    mapping = manifest["native"]["source_paths"]
    assert str(external) in mapping
    assert str(layers / "000000.mem") in mapping
    assert mapping[str(memory) + ".lineage.json"] == mapping[str(memory)] + ".lineage.json"
    assert manifest["native"]["source_paths"]["/old/live-rootfs"] == mapping[catalog["rootfs_dev"]]


def test_export_rejects_actual_external_host_directory(tmp_path, monkeypatch):
    catalog, memory = _fixture(tmp_path)
    memory.write_bytes(b"memory")
    host = tmp_path / "host-directory"
    host.mkdir()
    (host / "data").write_bytes(b"host payload")
    (Path(catalog["meta_dir"]) / "sandbox_spec.json").write_text(json.dumps({
        "volume_mounts": [{"host_path": str(host), "container_path": "/"}],
    }))
    monkeypatch.setattr(peer_replica, "native_control", lambda *args: catalog)
    with pytest.raises(RuntimeError, match="unsupported external directory dependency.*host_path"):
        peer_replica.export_snapshot("socket", storage_domain="node0", domain_root=str(tmp_path), memory_path=str(memory))


def test_export_compressed_snapshot_carries_virtual_paths_and_container(tmp_path, monkeypatch):
    catalog, memory = _fixture(tmp_path)
    container = Path(str(memory) + ".zstd-pages") / "000000.zpg"
    container.parent.mkdir()
    container.write_bytes(b"compressed-fixture")
    compression = {"snapshot_path": str(memory), "groups": [{"paths": [str(memory)],
                   "container": str(container), "mode": 0o600}]}
    Path(str(memory) + ".zstd-pages.json").write_text(json.dumps(compression))
    monkeypatch.setattr(peer_replica, "native_control", lambda *args: catalog)
    manifest = peer_replica.export_snapshot("socket", storage_domain="node0", domain_root=str(tmp_path), memory_path=str(memory))
    assert not memory.exists()
    assert str(memory) in manifest["virtual_paths"]
    assert str(container) in manifest["native"]["source_paths"]
    assert manifest["native"]["source_paths"][str(memory) + ".zstd-pages.json"] == manifest["virtual_paths"][str(memory)] + ".zstd-pages.json"


def test_shared_ram_import_uses_real_hardlink_and_private_relocated_lineage(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    catalog, memory = _fixture(source)
    warm = tmp_path / "warm"
    warm.mkdir()
    memory = warm / "guest.mem"
    memory.write_bytes(b"warm-inode")
    catalog["memory_dev"] = str(memory)
    base = source / "base.mem"
    base.write_bytes(b"base")
    Path(str(memory) + ".lineage.json").write_text(json.dumps({"layers": [{"external_path": str(base)}]}))
    monkeypatch.setattr(peer_replica, "native_control", lambda *args: catalog)
    manifest = peer_replica.export_snapshot("socket", storage_domain="node0", domain_root=str(source), memory_path=str(memory), shared_dram_root=str(warm))
    target = tmp_path / "target"
    package = target / ".clawbox-peer" / "replicas" / "fixture"
    object_paths = {}
    for obj in manifest["objects"]:
        if obj["storage_domain"] == "shared_dram":
            object_paths[obj["relative_path"]] = obj["source_path"]
        else:
            destination = package / obj["relative_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(Path(obj["source_path"]).read_bytes())
            object_paths[obj["relative_path"]] = str(destination)
    def native(socket, route, payload=None):
        if payload is None:
            raise RuntimeError("snapshot catalog not found")
        return {"snapshot_id": catalog["snapshot_id"], "memory_dev": payload["memory_path"], "rootfs_dev": payload["rootfs_path"]}
    monkeypatch.setattr(peer_replica, "native_control", native)
    ready = {"state": "ready", "manifest": manifest, "target_root": str(package),
             "storage_domain": "node1", "manifest_id": "fixture", "object_paths": object_paths}
    result = peer_replica.import_replica("socket", ready, str(target))
    alias = Path(result["memory_path"])
    assert not alias.is_symlink()
    assert os.path.samefile(alias, memory)
    assert str(target) in Path(str(alias) + ".lineage.json").read_text()
    assert str(source) in Path(str(memory) + ".lineage.json").read_text()


def test_prepared_cas_identity_reuses_verified_digest_without_payload_read(tmp_path, monkeypatch):
    from clawbox.experiments import peer_transport
    original = tmp_path / "original"
    original.write_bytes(b"verified payload" * 4096)
    obj = peer_transport.sparse_metadata(original)
    cache = tmp_path / ".clawbox-peer" / "objects" / obj["object_id"]
    cache.parent.mkdir(parents=True)
    cache.write_bytes(original.read_bytes())
    cache.chmod(0o444)
    peer_transport._write_json(cache.with_suffix(".json"), {
        "object": obj, "identity": peer_transport._identity(cache),
    })
    alias = tmp_path / ".clawbox-peer" / "replicas" / "prepared" / "base.mem"
    alias.parent.mkdir(parents=True)
    os.link(cache, alias)
    def unexpected_hash(path):
        raise AssertionError("immutable CAS alias reread its payload")
    monkeypatch.setattr(peer_transport, "sparse_metadata", unexpected_hash)
    actual = peer_replica._metadata(alias)
    assert actual["sha256"] == obj["sha256"]
    assert actual["mode"] == 0o444
    assert actual["file_identity"] == peer_transport._identity(alias)


def test_parallel_preparation_copies_shared_object_once_per_domain(tmp_path, monkeypatch):
    from clawbox.experiments import peer_transport
    source = tmp_path / "source.bin"
    source.write_bytes(b"shared immutable" * 16384)
    obj = {**peer_transport.sparse_metadata(source), "source_path": str(source),
           "relative_path": "payload/source.bin", "storage_domain": "preparation"}
    def export(socket, *, snapshot_id, **kwargs):
        return {"snapshot_id": snapshot_id, "objects": [obj], "native": {}}
    monkeypatch.setattr(peer_replica, "export_snapshot", export)
    monkeypatch.setattr(peer_replica, "import_replica", lambda socket, ready, root: {
        "snapshot_id": ready["manifest"]["snapshot_id"], "catalog": {},
    })
    domains = {name: str(tmp_path / name) for name in ("node0", "node1")}
    for root in domains.values():
        Path(root).mkdir()
    result = peer_replica.prepare_templates(["tpl-a", "tpl-b", "tpl-c"], domains, "socket", workers=3)
    assert result["prepared_bytes"] == {name: obj["transfer_payload_bytes"] for name in domains}
    assert list(result["manifests"]) == ["tpl-a", "tpl-b", "tpl-c"]
    for root in domains.values():
        aliases = list((Path(root) / ".clawbox-peer" / "replicas").glob("*/payload/source.bin"))
        assert len(aliases) == 3
        assert len({path.stat().st_ino for path in aliases}) == 1


def test_pending_tool_roundtrip_keeps_reactivated_source_backing(tmp_path, monkeypatch):
    from threading import RLock
    from types import SimpleNamespace
    from clawbox.cube.lifecycle import CubeSandboxLifecycle, SandboxState
    from clawbox.experiments.spec_types import SnapshotTier
    vm = CubeSandboxLifecycle.__new__(CubeSandboxLifecycle)
    original = str(tmp_path / "warm" / "original.mem")
    replica = str(tmp_path / "warm" / "replica.mem")
    vm._lock = RLock()
    vm._state = SandboxState.SWAPPED
    vm.peer_control_socket = "socket"
    vm.snapshot_compression_binary = "unused"
    vm._snapshot_tier = SnapshotTier.WARM
    vm._snapshot_key = "tool-generation-1"
    manifest = SimpleNamespace(path=replica)
    vm.snapshot_pool = SimpleNamespace(manifest=lambda key: manifest,
        rebind_path=lambda key, expected_path, path: setattr(manifest, "path", path))
    vm._warm_paths = {replica}
    vm._peer_retired_warm_paths = {original}
    monkeypatch.setattr(peer_replica, "import_replica", lambda *args, **kwargs: {"memory_path": original})
    vm.bind_replica({"storage_domain": "node0", "target_root": str(tmp_path / "node0" / ".clawbox-peer" / "replicas" / "roundtrip")})
    assert vm._warm_paths == {original}
    assert vm._peer_retired_warm_paths == {replica}


def test_twenty_package_generations_keep_bounded_paths_and_native_siblings(tmp_path, monkeypatch):
    import shutil
    roots = [tmp_path / "node0", tmp_path / "node1"]
    for root in roots:
        root.mkdir()
    catalog, memory = _fixture(roots[0])
    memory.write_bytes(b"RAM")
    layers = Path(str(memory) + ".layers")
    layers.mkdir()
    (layers / "000001.mem").write_bytes(b"ancestor")
    Path(str(memory) + ".lineage.json").write_text(json.dumps({"layers": [{"name": "000001.mem"}]}))
    asset = roots[0] / "assets" / "image.img"
    asset.parent.mkdir()
    asset.write_bytes(b"image")
    (asset.parent / "version").write_text("version-one")
    meta = Path(catalog["meta_dir"])
    config = meta / "snapshot" / "config.json"
    config.write_text(json.dumps({"disks": [{"path": catalog["rootfs_dev"]}], "pmem": [{"file": str(asset)}]}))
    (meta / "peer-runtime.json").write_text(json.dumps({
        "/original-component/image.img": str(asset),
        "/original-component/version": str(asset.parent / "version"),
    }))
    expected_asset_relative = None
    for generation in range(20):
        source_index = generation % 2
        target_index = 1 - source_index
        catalog["snapshot_id"] = f"snapshot-{generation}"
        monkeypatch.setattr(peer_replica, "native_control", lambda *args: catalog)
        manifest = peer_replica.export_snapshot("socket", storage_domain=f"node{source_index}",
            domain_root=str(roots[source_index]), snapshot_id=catalog["snapshot_id"])
        relative = manifest["native"]["source_paths"]["/original-component/image.img"]
        expected_asset_relative = expected_asset_relative or relative
        assert relative == expected_asset_relative
        assert Path(relative).parent == Path(manifest["native"]["source_paths"]["/original-component/version"]).parent
        package = roots[target_index] / ".clawbox-peer" / "replicas" / f"generation-{generation}"
        paths = {}
        for obj in manifest["objects"]:
            target = package / obj["relative_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(obj["source_path"], target)
            paths[obj["relative_path"]] = str(target)
            assert len(str(target)) < 512
        def native(socket, route, payload=None):
            if payload is None:
                raise RuntimeError("snapshot catalog not found")
            Path(payload["metadata_path"], "peer-runtime.json").write_text(json.dumps(payload["runtime_paths"]))
            return {**payload["catalog"], "meta_dir": payload["metadata_path"],
                    "rootfs_dev": payload["rootfs_path"], "memory_dev": payload["memory_path"]}
        monkeypatch.setattr(peer_replica, "native_control", native)
        imported = peer_replica.import_replica("socket", {"state": "ready", "manifest": manifest,
            "target_root": str(package), "storage_domain": f"node{target_index}",
            "manifest_id": str(generation), "object_paths": paths}, str(roots[target_index]))
        catalog = imported["catalog"]
        assert Path(catalog["memory_dev"] + ".lineage.json").is_file()
        assert Path(catalog["memory_dev"] + ".layers", "000001.mem").read_bytes() == b"ancestor"
