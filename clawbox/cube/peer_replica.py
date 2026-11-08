"""Complete native snapshot packages for the peer-local-SSD experiment.

Control uses Cubelet's root-only Unix socket. Formal payload movement uses
PeerReplicaStore TCP; only explicit template preparation copies original assets.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import threading
import time
import urllib.parse
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_hashes: dict[tuple, dict] = {}
_hash_lock = threading.Lock()
_hash_jobs: dict[tuple, threading.Lock] = {}
_cas_prime_lock = threading.Lock()
_primed_cas_roots: set[str] = set()
_prepare_object_locks: dict[tuple[str, str], threading.Lock] = {}


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=300)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def native_control(control_socket: str, path: str, payload: dict | None = None) -> dict:
    connection = _UnixHTTP(control_socket)
    try:
        connection.request("GET" if payload is None else "POST", path,
                           None if payload is None else json.dumps(payload),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(f"native replica operation failed: {result.get('error', result)}")
        return result
    finally:
        connection.close()


@contextmanager
def source_domain_unavailable(control_socket: str, source_domain: str):
    """Hide one task SSD only in Cube's existing slave mount namespace.

    Qualification enters after both roles pause and transfer completes. No host
    mount is changed; the target shim inherits the masked Cube namespace.
    """
    if os.geteuid() != 0:
        raise RuntimeError("source isolation qualification requires root")
    health = native_control(control_socket, "/health")
    source = Path(health["domains"][source_domain])
    namespace = Path("/usr/local/services/cubetoolbox/cubeletmnt/mnt")
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = Path((process / "cmdline").read_bytes().split(b"\0")[0].decode()).name
        except (FileNotFoundError, ProcessLookupError):
            continue
        if executable in {"containerd-shim-cube-rs", "cloud-hypervisor", "cube-hypervisor"}:
            raise RuntimeError(f"source isolation requires all VMs paused; process {process.name} is live")

    def command(*args, inside=False):
        argv = (["nsenter", "--mount=" + str(namespace)] if inside else []) + list(args)
        return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()

    before = command("findmnt", "-n", "-o", "TARGET,PROPAGATION,ID", "--target", str(source), inside=True).split()
    if len(before) != 3 or before[0] != str(source) or "shared" in before[1]:
        raise RuntimeError("source task mount must be private/slave in Cube namespace; refusing shared propagation")
    host_before = command("findmnt", "-n", "-o", "ID", "--target", str(source))
    host_objects = (source / "objects").is_dir()
    if not host_objects:
        raise RuntimeError("source isolation requires a prepared native domain")
    empty = Path(tempfile.mkdtemp(prefix="clawbox-source-hidden-", dir="/run"))
    mounted = False
    receipt = {"source_domain": source_domain, "source_root": str(source),
               "mount_namespace": str(namespace), "original_mount_id": before[2],
               "original_propagation": before[1], "host_mount_id": host_before,
               "restored": False}
    try:
        empty.chmod(0)
        command("mount", "--bind", str(empty), str(source), inside=True)
        mounted = True
        command("mount", "-o", "remount,bind,ro", str(source), inside=True)
        receipt["hidden_mount_id"] = command("findmnt", "-n", "-o", "ID", "--target", str(source), inside=True)
        hidden = subprocess.run(["nsenter", "--mount=" + str(namespace), "test", "-e", str(source / "objects")]).returncode
        if hidden != 1 or not (source / "objects").is_dir():
            raise RuntimeError("source masking did not hide only the Cube namespace payload")
        receipt["source_payload_inaccessible"] = True
        receipt["host_payload_preserved"] = True
        yield receipt
    finally:
        if mounted:
            command("umount", str(source), inside=True)
            restored_id = command("findmnt", "-n", "-o", "ID", "--target", str(source), inside=True)
            if restored_id != before[2] or command("findmnt", "-n", "-o", "ID", "--target", str(source)) != host_before:
                raise RuntimeError("source isolation cleanup mount identity changed; inspect task mounts")
            receipt["restored"] = True
        empty.chmod(0o700)
        empty.rmdir()


def assert_missing_target_rejected(control_socket: str, ready: dict, domain_root: str) -> dict:
    """Qualification fault injection before first import of a staged generation."""
    manifest = ready["manifest"]
    query = urllib.parse.urlencode({"backend": "xfs@" + ready["storage_domain"],
                                    "id": manifest["snapshot_id"]})
    try:
        native_control(control_socket, "/snapshot?" + query)
    except RuntimeError as exc:
        if "snapshot catalog not found" not in str(exc):
            raise
    else:
        raise RuntimeError("missing-target gate requires a new generation before import")
    relative = manifest["native"]["source_paths"][manifest["native"]["rootfs_path"]]
    target = Path(ready["object_paths"][relative])
    if not target.resolve().is_relative_to(Path(ready["target_root"]).resolve()):
        raise RuntimeError("negative gate target must be an owned staged payload")
    missing = target.with_name(target.name + ".qualification-withheld")
    if missing.exists():
        raise RuntimeError("withheld fixture exists; recover prior qualification first")
    os.rename(target, missing)
    failure = None
    try:
        try:
            import_replica(control_socket, ready, domain_root)
        except (RuntimeError, FileNotFoundError) as exc:
            failure = str(exc)
        if failure is None:
            raise RuntimeError("native import incorrectly accepted a missing target rootfs")
    finally:
        os.rename(missing, target)
    return {"snapshot_id": manifest["snapshot_id"], "target_object": str(target),
            "native_import_rejected": True, "error": failure, "target_object_restored": True}


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _prime_cas_metadata(path: Path) -> None:
    if ".clawbox-peer" not in path.parts:
        return
    index = path.parts.index(".clawbox-peer")
    cache_root = Path(*path.parts[:index + 1]) / "objects"
    with _cas_prime_lock:
        if str(cache_root) in _primed_cas_roots:
            return
        from clawbox.experiments.peer_transport import _identity, _same_content_identity
        found = {}
        for receipt_path in cache_root.glob("*.json"):
            try:
                receipt = json.loads(receipt_path.read_text())
                obj = receipt["object"]
                cache = cache_root / obj["object_id"]
                current = _identity(cache)
                if _same_content_identity(receipt["identity"], current):
                    found[tuple(current[:4])] = obj
            except (OSError, KeyError, ValueError):
                continue
        with _hash_lock:
            _hashes.update(found)
        _primed_cas_roots.add(str(cache_root))


def _metadata(path: Path) -> dict:
    from clawbox.experiments.peer_transport import sparse_metadata, _identity
    stat = path.stat()
    # Hardlink creation changes ctime but not immutable content. Native generations
    # are frozen while exported; inode, length and mtime identify this local alias.
    key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
    with _hash_lock:
        job = _hash_jobs.setdefault(key, threading.Lock())
    with job:
        with _hash_lock:
            found = _hashes.get(key)
        if found is None:
            _prime_cas_metadata(path)
            with _hash_lock:
                found = _hashes.get(key)
        if found is None:
            found = sparse_metadata(path)
            with _hash_lock:
                _hashes[key] = found
    # Content identity ignores hardlink-only ctime changes; transport still
    # verifies the transmitted SHA. Account actual local permissions/allocation.
    return {**found, "file_identity": _identity(path), "mode": stat.st_mode & 0o7777,
            "allocated_bytes": stat.st_blocks * 512}


def export_snapshot(control_socket: str, *, storage_domain: str,
                    domain_root: str, snapshot_id: str | None = None,
                    memory_path: str | None = None,
                    shared_dram_root: str | None = None,
                    preparation: bool = False) -> dict:
    query = {"backend": "xfs" if preparation else "xfs@" + storage_domain}
    if snapshot_id:
        query["id"] = snapshot_id
    elif memory_path:
        query["memory_path"] = memory_path
    else:
        raise ValueError("snapshot_id or memory_path is required")
    catalog = native_control(control_socket, "/snapshot?" + urllib.parse.urlencode(query))
    metadata_root = Path(catalog["meta_dir"])
    rootfs = Path(catalog["rootfs_dev"])
    memory = Path(catalog["memory_dev"]) if catalog.get("memory_dev") else None
    objects: list[dict] = []
    source_paths: dict[str, str] = {}
    directories: dict[str, str] = {}
    runtime_sources: set[str] = set()
    inspected: set[str] = set()
    shared_root = Path(shared_dram_root) if shared_dram_root else None
    virtual_paths: dict[str, str] = {}
    previous_runtime_path = metadata_root / "peer-runtime.json"
    previous_runtime = (json.loads(previous_runtime_path.read_text())
                        if previous_runtime_path.exists() else {})
    stable_parents: dict[str, set[str]] = {}
    for original, local in previous_runtime.items():
        # Keep the original asset directory identity across migrations. Imported
        # package paths are transient aliases and must never become new prefixes.
        if ".clawbox-peer" in Path(original).parts:
            continue
        if shared_root and Path(original).is_relative_to(shared_root):
            continue
        stable_parents.setdefault(str(Path(local).parent), set()).add(str(Path(original).parent))

    def relative_payload(path: Path) -> str:
        raw = str(path)
        if memory and (path == memory or raw.startswith(str(memory) + ".")):
            # Native and compressor discover these siblings by appending suffixes.
            return "ram/current.mem" + raw[len(str(memory)):]
        origins = stable_parents.get(str(path.parent))
        if origins:
            parent = min(origins, key=lambda value: (
                not value.startswith("/data/cubelet/root/component_versions/"), value))
        else:
            parent = str(path.resolve().parent)
        anchor = hashlib.sha256(parent.encode()).hexdigest()[:24]
        return "payload/" + anchor + "/" + path.name

    compression_path = Path(str(memory) + ".zstd-pages.json") if memory else None
    if compression_path and compression_path.exists():
        compression = json.loads(compression_path.read_text())
        for group in compression["groups"]:
            for raw in group["paths"]:
                virtual_paths[raw] = relative_payload(Path(raw))

    def add(path: Path, relative: str | None = None, *, runtime: bool = False):
        original = str(path)
        if original in source_paths:
            if runtime:
                runtime_sources.add(original)
            return
        if not path.is_file():
            raise RuntimeError(f"snapshot payload is missing or is not a regular file: {path}")
        shared = bool(shared_root and _inside(path, shared_root))
        if not preparation and not shared and not _inside(path, Path(domain_root)):
            raise RuntimeError(f"snapshot payload escapes owning storage domain {storage_domain}: {path}")
        info = _metadata(path)
        # Preserve sibling names: native discovers .lineage.json and the
        # compressor discovers .zstd-pages.json by appending to the RAM path.
        relative = relative or relative_payload(path)
        obj = {**info, "source_path": original, "relative_path": relative,
               "storage_domain": "shared_dram" if shared else storage_domain}
        objects.append(obj)
        source_paths[original] = relative
        # Symlink aliases from config/components must be relocated too.
        source_paths.setdefault(str(path.resolve()), relative)
        if runtime:
            runtime_sources.update((original, str(path.resolve())))
        if path.suffix == ".json" and original not in inspected:
            inspected.add(original)
            value = json.loads(path.read_text())
            scan(value, document=path)

    def scan(value: Any, key: str = "", *, document: Path | None = None, trail: str = ""):
        if isinstance(value, dict):
            for name, child in value.items():
                scan(child, name, document=document, trail=trail + "/" + name)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan(child, key, document=document, trail=trail + "/" + str(index))
        elif isinstance(value, str):
            raw = value[7:] if value.startswith("file://") else value
            if not raw.startswith("/") or raw.startswith(("/dev/", "/proc/", "/sys/", "/run/")):
                return
            path = Path(raw)
            if raw in virtual_paths:
                return
            # RunCubeSandboxRequest's container_path is a guest mount target.
            # Its generic path fields are HTTP routes, guest sandbox paths or
            # lifecycle hooks; VM backing files live in snapshot/config.json.
            if key == "container_path" or (key == "path" and document and document.name == "sandbox_spec.json"):
                return
            # Only actual host payload fields; guest mount destinations and argv
            # are not host dependencies. Lists of compressed paths keep their key.
            payload = key in {"path", "paths", "file", "kernel", "firmware", "initramfs",
                              "external_path", "container", "host_path", "shared_dir"}
            if not payload:
                return
            if key == "path" and raw in original_rootfs_paths:
                source_paths[raw] = source_paths[str(rootfs)]
                return
            if path.is_dir():
                if key == "shared_dir" and not any(path.iterdir()):
                    directories[raw] = "empty/" + hashlib.sha256(raw.encode()).hexdigest()[:16]
                    return
                raise RuntimeError(f"unsupported external directory dependency: {path} at {document}:{trail}")
            if path.is_file():
                add(path, runtime=key in {"file", "kernel", "firmware", "initramfs"})
            elif key not in {"path", "container_path"}:
                raise RuntimeError(f"missing native snapshot dependency {key}: {path}")

    # Original live disk paths in config are overridden by the newly allocated
    # target rootfs; they are not a second mutable object to transfer.
    original_rootfs_paths: set[str] = set()
    config_path = metadata_root / "snapshot" / "config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        disks = config.get("disks") or []
        if len(disks) != 1:
            raise RuntimeError("peer restore currently requires exactly one managed rootfs disk")
        original_rootfs_paths.update(d["path"] for d in disks if d.get("path"))
    add(rootfs, "rootfs/frozen.ext4")
    for path in sorted(metadata_root.rglob("*")):
        if path.is_file() and path.name not in {"catalog.json", "peer-runtime.json"}:
            add(path, "metadata/" + path.relative_to(metadata_root).as_posix())
    if memory:
        if memory.exists():
            add(memory)
        for suffix in (".lineage.json", ".compression.json", ".zstd.json"):
            candidate = Path(str(memory) + suffix)
            if candidate.exists():
                add(candidate)
        # Compressed containers and relocated per-layer files sit beside the
        # main path. Manifest JSON discovers exact external dependencies.
        for candidate in memory.parent.glob(memory.name + "*"):
            if candidate.is_file():
                add(candidate)
            elif candidate.is_dir() and candidate.name.endswith((".layers", ".zstd-pages")):
                for layer in candidate.rglob("*"):
                    if layer.is_file():
                        add(layer)
    # Builtin component identities point at deployed versioned files. Include
    # their exact bytes and aliases in preparation; normal snapshots already
    # carry rebound paths and inherit this map from the parent package.
    component_files = {"cube-kernel-scf": "vmlinux", "cube-image": "cube-guest-image-cpu.img",
                       "cube-agent": "cube-agent.ext4"}
    if preparation:
        for component, filename in component_files.items():
            version = (catalog.get("component_versions") or {}).get(component)
            if version:
                directory = Path("/data/cubelet/root/component_versions") / component / version
                # The shim reads version/version.json beside component payloads.
                # Preserve the complete pinned directory, including kernel aliases.
                for path in sorted(directory.rglob("*")):
                    if path.is_file():
                        add(path, runtime=True)
    else:
        if previous_runtime:
            # Native generation uses component identifiers from the original
            # template. Preserve their explicit aliases without reading source SSD.
            for original, local in previous_runtime.items():
                if local in source_paths:
                    source_paths[original] = source_paths[local]
                elif Path(local).is_file():
                    add(Path(local), runtime=True)
                    source_paths[original] = source_paths[local]
    return {"schema_version": 1, "snapshot_id": catalog["snapshot_id"],
            "source_domain": storage_domain, "source_root": domain_root,
            "objects": objects, "directories": directories,
            "virtual_paths": virtual_paths,
            "shared_dram_root": shared_dram_root,
            "native": {"catalog": catalog, "source_paths": source_paths,
                       "rootfs_path": str(rootfs), "memory_path": str(memory) if memory else None,
                       "runtime_sources": sorted(runtime_sources)},
            "logical_bytes": sum(x["logical_bytes"] for x in objects),
            "allocated_bytes": sum({x["object_id"]: x["allocated_bytes"] for x in objects}.values()),
            "transfer_payload_bytes": sum({x["object_id"]: x["transfer_payload_bytes"] for x in objects
                                            if x["storage_domain"] != "shared_dram"}.values())}


def import_replica(control_socket: str, ready: dict, domain_root: str,
                   compression_binary: str = "clawbox-zstd-page-snapshot") -> dict:
    started = time.monotonic()
    phases = {"catalog_lookup_s": 0.0, "metadata_alias_rewrite_s": 0.0,
              "shared_decompress_s": 0.0, "target_decompress_s": 0.0,
              "native_import_s": 0.0}
    if ready.get("state") != "ready":
        raise ValueError("native import requires a verified ready replica")
    manifest = ready["manifest"]
    query = urllib.parse.urlencode({"backend": "xfs@" + ready["storage_domain"],
                                    "id": manifest["snapshot_id"]})
    phase_started = time.monotonic()
    try:
        existing = native_control(control_socket, "/snapshot?" + query)
    except RuntimeError as exc:
        if "snapshot catalog not found" not in str(exc):
            raise
    else:
        phases["catalog_lookup_s"] = time.monotonic() - phase_started
        return {"storage_domain": ready["storage_domain"], "snapshot_id": existing["snapshot_id"],
                "memory_path": existing.get("memory_dev"), "rootfs_path": existing["rootfs_dev"],
                "catalog": existing, "manifest_id": ready.get("manifest_id"),
                "object_paths": ready["object_paths"], "already_imported": True,
                "import_phase_seconds": phases, "import_elapsed_seconds": time.monotonic() - started}
    phases["catalog_lookup_s"] = time.monotonic() - phase_started
    package = Path(ready["target_root"])
    if not _inside(package, Path(domain_root)):
        raise ValueError("ready package is outside native domain")
    native = manifest["native"]
    paths = dict(ready["object_paths"])
    shared_root = manifest.get("shared_dram_root")
    # Shared RAM keeps real tmpfs hardlinks: native incremental checkpoints
    # hardlink predecessor layers, so SSD symlinks would break successor lineage.
    # Only lineage JSON is private; its external SSD paths are rebound below.
    original_memory = native.get("memory_path")
    shared_memory = bool(original_memory and shared_root and _inside(Path(original_memory).parent, Path(shared_root)))
    shared_alias_root = (Path(shared_root) / ".peer-replicas" / ready["storage_domain"] /
                         str(ready.get("manifest_id") or hashlib.sha256(str(package).encode()).hexdigest())) if shared_root else None
    if shared_memory and Path(original_memory + ".zstd-pages.json").exists():
        from .snapshot_compression import decompress_snapshot
        phase_started = time.monotonic()
        decompress_snapshot(original_memory, binary=compression_binary)
        phases["shared_decompress_s"] = time.monotonic() - phase_started
    phase_started = time.monotonic()
    for obj in manifest["objects"]:
        if obj.get("storage_domain") != "shared_dram":
            continue
        source = Path(obj["source_path"])
        destination = shared_alias_root / obj["relative_path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            # Compressed containers/manifest consumed during shared decompression.
            continue
        if destination.suffix == ".json":
            shutil.copyfile(source, destination)
        elif not destination.exists():
            os.link(source, destination)
        paths[obj["relative_path"]] = str(destination)
    replacements = {source: paths[relative] for source, relative in native["source_paths"].items()}
    for source, relative in manifest.get("virtual_paths", {}).items():
        destination = package / relative
        if shared_root and _inside(Path(source).parent, Path(shared_root)):
            destination = shared_alias_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.link(source, destination)
        replacements[source] = str(destination)
    for source, relative in manifest.get("directories", {}).items():
        destination = package / relative
        destination.mkdir(parents=True, exist_ok=True)
        replacements[source] = str(destination)

    def rewrite(value):
        if isinstance(value, dict):
            return {key: rewrite(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rewrite(child) for child in value]
        if isinstance(value, str):
            if value in replacements:
                return replacements[value]
            if value.startswith("file://") and value[7:] in replacements:
                return "file://" + replacements[value[7:]]
        return value

    for obj in manifest["objects"]:
        if obj["relative_path"].endswith(".json"):
            path = Path(paths[obj["relative_path"]])
            if path.exists() and (_inside(path, package) or (shared_alias_root and _inside(path, shared_alias_root))):
                path.write_text(json.dumps(rewrite(json.loads(path.read_text()))), encoding="utf-8")
    memory_path = replacements.get(native["memory_path"], "")
    phases["metadata_alias_rewrite_s"] = time.monotonic() - phase_started
    if memory_path and Path(memory_path + ".zstd-pages.json").exists():
        from .snapshot_compression import decompress_snapshot
        phase_started = time.monotonic()
        decompress_snapshot(memory_path, binary=compression_binary)
        phases["target_decompress_s"] = time.monotonic() - phase_started
    phase_started = time.monotonic()
    result = native_control(control_socket, "/import", {
        "storage_domain": ready["storage_domain"], "catalog": native["catalog"],
        "rootfs_path": replacements[native["rootfs_path"]],
        "memory_path": memory_path,
        "metadata_path": str(package / "metadata"),
        # Include all file aliases: PMEM config and versioned component paths
        # are independently generated by native restore and need the same map.
        # Decompression consumes container/manifest paths. They are no longer
        # runtime dependencies; retain only the materialized local closure.
        "runtime_paths": {source: target for source, target in replacements.items()
                          if Path(target).exists()},
        "run_id": ready.get("run_id") or native["catalog"].get("clawbox_run_id", ""),
    })
    phases["native_import_s"] = time.monotonic() - phase_started
    return {"storage_domain": ready["storage_domain"], "snapshot_id": result["snapshot_id"],
            "memory_path": result.get("memory_dev"), "rootfs_path": result["rootfs_dev"],
            "catalog": result, "manifest_id": ready.get("manifest_id"),
            "object_paths": paths, "import_phase_seconds": phases,
            "import_elapsed_seconds": time.monotonic() - started}


def _complete_existing_components(result: dict, ready: dict, root: Path) -> list[str]:
    """Complete previously prepared component siblings without moving RAM bases."""
    if not result.get("already_imported"):
        return []
    from clawbox.experiments.peer_transport import _write_json
    mapping_path = Path(result["catalog"]["meta_dir"]) / "peer-runtime.json"
    mappings = json.loads(mapping_path.read_text())
    repaired = []
    prefix = "/data/cubelet/root/component_versions/"
    for obj in ready["manifest"]["objects"]:
        source = Path(obj["source_path"])
        if not str(source).startswith(prefix):
            continue
        existing = mappings.get(str(source))
        if existing and Path(existing).is_file():
            continue
        anchors = [Path(target) for original, target in mappings.items()
                   if Path(original).parent == source.parent and Path(target).is_file()]
        if not anchors:
            raise RuntimeError(f"prepared component directory lacks its original payload anchor: {source.parent}")
        destination = anchors[0].parent / source.name
        if not _inside(destination.parent, root / ".clawbox-peer" / "replicas"):
            raise RuntimeError("prepared component repair escaped owned SSD packages")
        if not destination.exists():
            staged = Path(ready["object_paths"][obj["relative_path"]])
            if destination.suffix == ".json":
                shutil.copyfile(staged, destination)
            else:
                os.link(staged, destination)
        mappings[str(source)] = str(destination)
        repaired.append(str(destination))
    if repaired:
        _write_json(mapping_path, mappings)
    return repaired


def prepare_templates(template_ids: list[str], domains: dict[str, str],
                      control_socket: str, shared_dram_root: str | None = None, *, workers: int = 1) -> dict:
    """Copy original immutable assets once, deduplicating within each local SSD.

    This preparation is outside the formal replay window. Every formal missing
    object transfer goes through PeerReplicaStore and its shaped TCP link.
    """
    from clawbox.experiments.peer_transport import _identity, _write_json, _digest_file
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("preparation workers must be a positive integer")
    started = time.monotonic()
    result: dict[str, Any] = {"imports": {}, "manifests": {}, "prepared_bytes": {},
                              "preparation_transport": "host sparse copy outside measurement"}
    def prepare_one(template):
        result: dict[str, Any] = {"imports": {}, "manifests": {}, "prepared_bytes": {}}
        manifest = export_snapshot(control_socket, storage_domain="preparation", domain_root="/",
                                   snapshot_id=template, preparation=True,
                                   shared_dram_root=shared_dram_root)
        result["manifests"][template] = manifest
        identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        for domain, root_text in domains.items():
            root = Path(root_text)
            package = root / ".clawbox-peer" / "replicas" / ("prepare-" + identity)
            package.mkdir(parents=True, exist_ok=True)
            paths = {}
            copied = 0
            for obj in manifest["objects"]:
                with _hash_lock:
                    object_lock = _prepare_object_locks.setdefault((str(root), obj["object_id"]), threading.Lock())
                with object_lock:
                    cache = root / ".clawbox-peer" / "objects" / obj["object_id"]
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    if not cache.exists():
                        usage = shutil.disk_usage(root)
                        if usage.used + obj["allocated_bytes"] >= usage.total * .85:
                            raise RuntimeError("template closure exceeds target SSD 85% limit; reduce case set or use a larger owned pool")
                        temporary = cache.with_suffix(".copying")
                        with open(obj["source_path"], "rb", buffering=0) as source, temporary.open("xb", buffering=0) as target:
                            target.truncate(obj["logical_bytes"])
                            for offset, length in obj["extents"]:
                                source.seek(offset)
                                target.seek(offset)
                                while length:
                                    chunk = source.read(min(length, 4 * 1024 * 1024))
                                    if not chunk:
                                        raise RuntimeError("short read preparing immutable object")
                                    view = memoryview(chunk)
                                    while view:
                                        view = view[target.write(view):]
                                    length -= len(chunk)
                            os.fsync(target.fileno())
                        if _digest_file(temporary, obj["logical_bytes"], obj["extents"]) != obj["sha256"]:
                            raise RuntimeError("prepared template sparse checksum mismatch")
                        temporary.chmod(obj["mode"] & ~0o222)
                        os.replace(temporary, cache)
                        copied += obj["transfer_payload_bytes"]
                    _write_json(cache.with_suffix(".json"), {"object": obj, "identity": _identity(cache)})
                destination = package / obj["relative_path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    if destination.suffix == ".json":
                        shutil.copyfile(cache, destination)
                    else:
                        os.link(cache, destination)
                paths[obj["relative_path"]] = str(destination)
            ready = {"state": "ready", "manifest": manifest, "object_paths": paths,
                     "storage_domain": domain, "target_root": str(package), "manifest_id": identity,
                     "run_ids": [], "preparation": True}
            imported = import_replica(control_socket, ready, root_text)
            imported["completed_component_files"] = _complete_existing_components(imported, ready, root)
            result["imports"].setdefault(domain, {})[template] = imported
            _write_json(package / "manifest.json", manifest)
            _write_json(package / "ready.json", ready)
            result["prepared_bytes"][domain] = result["prepared_bytes"].get(domain, 0) + copied
        return result

    templates = list(dict.fromkeys(template_ids))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prepare-template") as executor:
        # map yields input order even when copies complete out of order.
        for partial in executor.map(prepare_one, templates):
            result["manifests"].update(partial["manifests"])
            for domain, imports in partial["imports"].items():
                result["imports"].setdefault(domain, {}).update(imports)
            for domain, count in partial["prepared_bytes"].items():
                result["prepared_bytes"][domain] = result["prepared_bytes"].get(domain, 0) + count
    result["workers"] = workers
    result["elapsed_seconds"] = time.monotonic() - started
    return result
