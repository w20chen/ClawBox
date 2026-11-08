"""Immutable sparse-object transfer over the reviewed peer TCP endpoints.

The helpers only serve registered immutable files below their domain root. A
receiver runs in the destination network namespace, so every remote byte crosses
the one aggregate egress queue of the source namespace. This module is a storage
helper for ``clawbox experiment``, not a separate experiment runner.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable
import uuid

CHUNK = 1024 * 1024
MAX_HEADER = 16 * CHUNK


def _identity(path: Path) -> list[int]:
    value = path.stat()
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _same_content_identity(saved, current) -> bool:
    # Adding/removing another immutable hardlink changes ctime without changing
    # content. Device, inode, logical length and mtime still identify this alias.
    return bool(saved) and list(saved[:4]) == list(current[:4])


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"unsafe object relative path: {value}")
    return str(path)


def _inside(root: Path, path: Path) -> Path:
    value = path.resolve(strict=True)
    if not value.is_relative_to(root.resolve()) or not value.is_file():
        raise ValueError(f"object is not a regular file in its storage domain: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _ranges(fd: int, size: int) -> list[list[int]]:
    result = []
    position = 0
    while position < size:
        try:
            start = os.lseek(fd, position, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
            return [[0, size]] if size else []
        end = min(size, os.lseek(fd, start, os.SEEK_HOLE))
        if end <= start:
            raise RuntimeError("filesystem returned invalid sparse extent")
        result.append([start, end - start])
        position = end
    return result


def _digest_file(path: Path, size: int, extents: list[list[int]]) -> str:
    digest = hashlib.sha256(size.to_bytes(8, "big"))
    with path.open("rb", buffering=0) as stream:
        for offset, length in extents:
            digest.update(struct.pack("!QQ", offset, length))
            stream.seek(offset)
            remaining = length
            while remaining:
                chunk = stream.read(min(CHUNK, remaining))
                if not chunk:
                    raise RuntimeError(f"short immutable object read: {path}")
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


def sparse_metadata(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    before = _identity(path)
    with path.open("rb") as stream:
        extents = _ranges(stream.fileno(), before[2])
    digest = _digest_file(path, before[2], extents)
    if not _same_content_identity(before, _identity(path)):
        raise RuntimeError(f"object changed during immutable export: {path}")
    value = path.stat()
    return {"object_id": digest, "sha256": digest, "logical_bytes": value.st_size,
            "allocated_bytes": value.st_blocks * 512,
            "transfer_payload_bytes": sum(row[1] for row in extents),
            "mode": stat.S_IMODE(value.st_mode), "extents": extents,
            "file_identity": before}


def _validate_object(obj: dict[str, Any]) -> None:
    if not isinstance(obj.get("object_id"), str) or len(obj["object_id"]) != 64:
        raise ValueError("object_id must be a SHA256 hex digest")
    int(obj["object_id"], 16)
    if obj.get("sha256") != obj["object_id"]:
        raise ValueError("object identity/checksum mismatch")
    size = obj["logical_bytes"]
    if not isinstance(size, int) or size < 0:
        raise ValueError("invalid logical length")
    previous = 0
    for row in obj["extents"]:
        if len(row) != 2 or not all(isinstance(x, int) for x in row):
            raise ValueError("invalid sparse extent")
        offset, length = row
        if offset < previous or length <= 0 or offset + length > size:
            raise ValueError("overlapping/out-of-bounds sparse extent")
        previous = offset + length


def register_manifest(domain_root: Path, manifest: dict[str, Any]) -> None:
    """Register native-exported immutable paths, verifying once at publication."""
    domain_root = domain_root.resolve()
    source_root = Path(manifest["source_root"])
    for obj in manifest["objects"]:
        _validate_object(obj)
        path = _inside(domain_root, Path(obj.get("source_path") or source_root / _relative(obj["relative_path"])))
        registry = domain_root / ".clawbox-peer" / "registry" / (obj["object_id"] + ".json")
        identity = _identity(path)
        if registry.exists():
            saved = json.loads(registry.read_text())
            if saved.get("path") == str(path) and _same_content_identity(saved.get("identity"), identity):
                continue
        # Native export already checksums the immutable inode. Reuse that proof
        # while its identity is unchanged; receiver always verifies stream bytes.
        changed = not _same_content_identity(obj.get("file_identity"), identity)
        if path.stat().st_size != obj["logical_bytes"] or (changed and _digest_file(path, obj["logical_bytes"], obj["extents"]) != obj["sha256"]):
            raise RuntimeError(f"native exported object does not match immutable manifest: {path}")
        _write_json(registry, {"path": str(path), "identity": identity, "object": obj})


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("incomplete peer object stream")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_header(connection: socket.socket, value: Any) -> int:
    data = json.dumps(value, separators=(",", ":")).encode()
    connection.sendall(struct.pack("!I", len(data)) + data)
    return len(data) + 4


def _recv_header(connection: socket.socket) -> tuple[Any, int]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if size > MAX_HEADER:
        raise ValueError("peer protocol header exceeds limit")
    return json.loads(_recv_exact(connection, size)), size + 4


class ObjectServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], root: Path, owner: str):
        self.root = root.resolve()
        self.owner = owner
        super().__init__(address, ObjectHandler)


def domain_command(netns: str, cpus: str, numa_node: int, *arguments: str) -> list[str]:
    return ["ip", "netns", "exec", netns, "numactl", "--physcpubind=" + cpus,
            "--membind=" + str(numa_node), sys.executable,
            "-m", "clawbox.experiments.peer_transport", *arguments]


def process_locality() -> dict:
    return {"cpus": sorted(os.sched_getaffinity(0)),
            "memory_policies": sorted({line.split()[1] for line in Path("/proc/self/numa_maps").read_text().splitlines()
                                       if len(line.split()) > 1})}


class ObjectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        connection = self.request
        connection.settimeout(300)
        header_sent = False
        try:
            request, _ = _recv_header(connection)
            if request.get("op") == "ping":
                _send_header(connection, {"root": str(self.server.root), "owner": self.server.owner,
                                          "locality": process_locality()})
                return
            oid = request.get("object_id", "")
            if len(oid) != 64 or any(c not in "0123456789abcdef" for c in oid):
                raise ValueError("invalid object ID")
            record = json.loads((self.server.root / ".clawbox-peer" / "registry" / (oid + ".json")).read_text())
            path = _inside(self.server.root, Path(record["path"]))
            if not _same_content_identity(record["identity"], _identity(path)):
                raise RuntimeError("registered immutable source changed")
            obj = record["object"]
            _send_header(connection, {"ok": True, "object": obj})
            header_sent = True
            with path.open("rb", buffering=0) as stream:
                for offset, length in obj["extents"]:
                    stream.seek(offset)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(CHUNK, remaining))
                        if not chunk:
                            raise RuntimeError("immutable source became short")
                        connection.sendall(chunk)
                        remaining -= len(chunk)
            if not _same_content_identity(record["identity"], _identity(path)):
                raise RuntimeError("source changed during transfer")
            _send_header(connection, {"complete": True, "object_id": oid})
        except Exception as exc:
            if not header_sent:
                _send_header(connection, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            print(json.dumps({"event": "peer_transfer_failed", "error": str(exc)}), flush=True)


@contextmanager
def _object_lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _cache_hit(root: Path, obj: dict[str, Any]) -> Path | None:
    oid = obj["object_id"]
    path = root / ".clawbox-peer" / "objects" / oid
    receipt = path.with_suffix(".json")
    try:
        record = json.loads(receipt.read_text())
        if record["object"]["sha256"] == obj["sha256"] and _same_content_identity(record["identity"], _identity(path)):
            return path
    except (OSError, ValueError, KeyError):
        pass
    return None


def _reflink_local(source: Path, target: Path, mode: int) -> None:
    """Create a private local inode without reading/copying all template bytes."""
    import fcntl
    with source.open("rb", buffering=0) as source_file:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            fcntl.ioctl(descriptor, 0x40049409, source_file.fileno())  # FICLONE
            os.fchmod(descriptor, mode)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)


def fetch_object(root: Path, obj: dict[str, Any], address: str, port: int, timeout: float = 300) -> dict[str, Any]:
    """Called inside destination netns; distinct objects may transfer concurrently."""
    _validate_object(obj)
    oid = obj["object_id"]
    cache = root / ".clawbox-peer" / "objects"
    started = time.monotonic()
    with _object_lock(cache / (oid + ".lock")):
        if _cache_hit(root, obj):
            return {"object_id": oid, "cache_hit": True, "payload_bytes": 0, "protocol_bytes": 0,
                    "queue_seconds": time.monotonic() - started, "transfer_seconds": 0,
                    "queue_start_monotonic_s": started,
                    "transfer_start_monotonic_s": None, "transfer_end_monotonic_s": None,
                    "payload_start_monotonic_s": None, "payload_end_monotonic_s": None}
        transfer_start = time.monotonic()
        temporary = cache / (oid + ".part-" + uuid.uuid4().hex)
        payload = protocol = 0
        try:
            with socket.create_connection((address, port), timeout=timeout) as connection:
                connection.settimeout(timeout)
                protocol += _send_header(connection, {"op": "get", "object_id": oid})
                reply, count = _recv_header(connection)
                protocol += count
                if not reply.get("ok"):
                    raise RuntimeError(f"peer rejected immutable object: {reply.get('error')}")
                remote = reply["object"]
                for key in ("object_id", "logical_bytes", "extents", "sha256"):
                    if remote.get(key) != obj.get(key):
                        raise RuntimeError(f"peer object manifest differs at {key}")
                digest = hashlib.sha256(obj["logical_bytes"].to_bytes(8, "big"))
                payload_start = time.monotonic()
                with temporary.open("xb", buffering=0) as stream:
                    stream.truncate(obj["logical_bytes"])
                    for offset, length in obj["extents"]:
                        digest.update(struct.pack("!QQ", offset, length))
                        stream.seek(offset)
                        remaining = length
                        while remaining:
                            chunk = _recv_exact(connection, min(CHUNK, remaining))
                            stream.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                            payload += len(chunk)
                    payload_end = time.monotonic()
                    os.fsync(stream.fileno())
                footer, count = _recv_header(connection)
                protocol += count
                if footer != {"complete": True, "object_id": oid} or digest.hexdigest() != obj["sha256"]:
                    raise RuntimeError("incomplete or corrupt peer object")
            os.chmod(temporary, obj["mode"])
            target = cache / oid
            os.replace(temporary, target)
            _write_json(target.with_suffix(".json"), {"object": obj, "identity": _identity(target)})
        finally:
            temporary.unlink(missing_ok=True)
        return {"object_id": oid, "cache_hit": False, "payload_bytes": payload,
                "protocol_bytes": protocol, "queue_seconds": transfer_start - started,
                "transfer_seconds": time.monotonic() - transfer_start,
                "queue_start_monotonic_s": started,
                "transfer_start_monotonic_s": transfer_start,
                "transfer_end_monotonic_s": time.monotonic(),
                "payload_start_monotonic_s": payload_start,
                "payload_end_monotonic_s": payload_end}


class PeerReplicaStore:
    def __init__(self, spec, *, emit: Callable[[dict[str, Any]], None] | None = None,
                 run_id: str = "peer", timeout_seconds: float | None = None):
        self.spec = spec
        self.emit = emit or (lambda event: None)
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else getattr(spec, "transfer_timeout_seconds", 300)
        receipt = json.loads(Path(spec.host_receipt).read_text())
        self.host_nodes = receipt["host_configuration"]["nodes"]
        if {key: node["local_mount"] for key, node in self.host_nodes.items()} != spec.local_roots:
            raise ValueError("peer receiver affinity receipt roots differ from runtime domains")
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._active: dict[tuple[str, str], int] = {}
        self._reserved: dict[str, int] = {}

    def active_transfers(self, source_domain: str, target_domain: str) -> int:
        with self._guard:
            return self._active.get((source_domain, target_domain), 0)

    def storage_sample(self) -> dict[str, dict[str, int]]:
        with self._guard:
            reserved = dict(self._reserved)
        result = {}
        for node, root in self.spec.local_roots.items():
            usage = shutil.disk_usage(root)
            result[node] = {"filesystem_used_bytes": usage.used,
                            "filesystem_total_bytes": usage.total,
                            "filesystem_free_bytes": usage.free,
                            "reserved_transfer_payload_bytes": reserved.get(node, 0)}
        return result

    def _shared_object(self, obj: dict[str, Any]) -> Path | None:
        if obj.get("storage_domain") != "shared_dram":
            return None
        path = _inside(Path(self.spec.warm_root), Path(obj["source_path"]))
        if not _same_content_identity(obj.get("file_identity"), _identity(path)):
            raise RuntimeError("shared WARM object identity changed after export; re-export the paused generation")
        return path

    def estimate_missing(self, manifest: dict[str, Any], target_domain: str) -> dict[str, Any]:
        root = Path(self.spec.local_roots[target_domain])
        missing: dict[str, dict] = {}
        hits = 0
        shared_hits = 0
        for obj in manifest["objects"]:
            _validate_object(obj)
            if self._shared_object(obj):
                shared_hits += 1
            elif _cache_hit(root, obj):
                hits += 1
            else:
                missing[obj["object_id"]] = obj
        return {"payload_bytes": sum(sum(r[1] for r in o["extents"]) for o in missing.values()),
                "logical_bytes": sum(o["logical_bytes"] for o in missing.values()),
                "missing_objects": len(missing), "cache_hits": hits, "shared_warm_hits": shared_hits,
                "source": "verified-target-object-inventory"}

    def stage_replica(self, manifest: dict[str, Any], target_domain: str) -> dict[str, Any]:
        source = manifest["source_domain"]
        if source not in self.spec.local_roots or target_domain not in self.spec.local_roots:
            raise ValueError("manifest source/target is not a configured storage domain")
        source_root = Path(self.spec.local_roots[source])
        target_root = Path(self.spec.local_roots[target_domain])
        registration_started = time.monotonic()
        transferred_objects = [obj for obj in manifest["objects"] if self._shared_object(obj) is None]
        register_manifest(source_root, {**manifest, "objects": transferred_objects})
        registration_seconds = time.monotonic() - registration_started
        self.emit({"event": "peer_manifest_registered", "source_domain": source,
                   "target_domain": target_domain, "snapshot_id": manifest["snapshot_id"],
                   "seconds": registration_seconds, "monotonic_s": time.monotonic()})
        manifest_id = hashlib.sha256((self.run_id + "\0" + json.dumps(manifest, sort_keys=True)).encode()).hexdigest()
        with self._guard:
            lock = self._locks.setdefault((target_domain, manifest_id), threading.Lock())
        with lock:
            package = target_root / ".clawbox-peer" / "replicas" / manifest_id
            ready = package / "ready.json"
            if ready.exists():
                # Local hits use trusted inode identity, never rehash the whole template.
                saved = json.loads(ready.read_text())
                if all(self._shared_object(obj) or _cache_hit(target_root, obj) for obj in manifest["objects"]):
                    owners = set(saved.get("run_ids") or [saved.get("run_id")])
                    owners.discard(None)
                    if self.run_id not in owners:
                        saved["run_ids"] = sorted(owners | {self.run_id})
                        _write_json(ready, saved)
                    self.emit({"event": "peer_replica_hit", "source_domain": source,
                               "target_domain": target_domain, "snapshot_id": manifest["snapshot_id"],
                               "monotonic_s": time.monotonic(),
                               "shared_warm_hits": sum(obj.get("storage_domain") == "shared_dram" for obj in manifest["objects"]),
                               "payload_bytes": 0, "protocol_bytes": 0})
                    return {**saved, "cache_hit": True,
                            "run_id": self.run_id,
                            "stage_seconds": time.monotonic() - registration_started - registration_seconds,
                            "registration_seconds": registration_seconds,
                            "transfer": {"objects": [], "payload_bytes": 0, "protocol_bytes": 0,
                                         "cache_hits": sum(obj.get("storage_domain") != "shared_dram" for obj in manifest["objects"]),
                                         "shared_warm_hits": sum(obj.get("storage_domain") == "shared_dram" for obj in manifest["objects"])}}
                raise RuntimeError("ready replica backing changed; refuse stale local hit")
            estimate = self.estimate_missing(manifest, target_domain)
            endpoint = self.spec.transfer_endpoints[source]
            destination = self.spec.transfer_endpoints[target_domain]
            endpoint = endpoint.model_dump() if hasattr(endpoint, "model_dump") else endpoint
            destination = destination.model_dump() if hasattr(destination, "model_dump") else destination
            started = time.monotonic()
            self.emit({"event": "peer_transfer_start", "source_domain": source,
                       "target_domain": target_domain, "snapshot_id": manifest["snapshot_id"],
                       "monotonic_s": started, **estimate})
            # stdin carries the manifest only; actual file payload is exclusively TCP.
            request = {"root": str(target_root), "objects": transferred_objects,
                       "address": endpoint["address"], "port": endpoint["port"],
                       "timeout": self.timeout_seconds}
            host_node = self.host_nodes[target_domain]
            command = domain_command(destination["netns"], host_node["cpus"], host_node["numa_node"], "fetch")
            with self._guard:
                usage = shutil.disk_usage(target_root)
                reserved = self._reserved.get(target_domain, 0)
                if usage.used + reserved + estimate["payload_bytes"] >= usage.total * 0.85:
                    raise RuntimeError("target SSD exceeds 85% with missing payload and in-flight reservations; reduce workload")
                self._reserved[target_domain] = reserved + estimate["payload_bytes"]
                pair = (source, target_domain)
                self._active[pair] = self._active.get(pair, 0) + 1
            try:
                completed = subprocess.run(command, input=json.dumps(request), text=True,
                                           capture_output=True, timeout=self.timeout_seconds * max(1, len(manifest["objects"])))
            finally:
                with self._guard:
                    self._active[pair] -= 1
                    self._reserved[target_domain] -= estimate["payload_bytes"]
            if completed.returncode:
                raise RuntimeError(f"peer stage failed: {completed.stderr.strip()}")
            metrics = json.loads(completed.stdout)
            metrics["shared_warm_hits"] = estimate["shared_warm_hits"]
            staging = package.with_name(package.name + ".staging-" + uuid.uuid4().hex)
            staging.mkdir(parents=True)
            try:
                object_paths = {}
                for obj in manifest["objects"]:
                    relative = _relative(obj["relative_path"])
                    shared = self._shared_object(obj)
                    if shared is not None:
                        object_paths[relative] = str(shared)
                        continue
                    target = staging / relative
                    object_paths[relative] = str(package / relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    cached = _cache_hit(target_root, obj)
                    if cached is None:
                        raise RuntimeError("target immutable object vanished before publish")
                    # Relocatable JSON belongs to this package; its rewrite must not mutate CAS.
                    if target.suffix == ".json":
                        shutil.copyfile(cached, target)
                        os.chmod(target, obj["mode"])
                    elif stat.S_IMODE(cached.stat().st_mode) != obj["mode"]:
                        _reflink_local(cached, target, obj["mode"])
                    else:
                        with _object_lock(cached.with_suffix(".lock")):
                            os.link(cached, target)
                            # A new hardlink changes ctime, so renew the trusted alias identity.
                            _write_json(cached.with_suffix(".json"), {"object": obj, "identity": _identity(cached)})
                _write_json(staging / "manifest.json", manifest)
                result = {"state": "ready", "target_root": str(package),
                          "storage_domain": target_domain, "manifest": manifest,
                          "run_id": self.run_id, "run_ids": [self.run_id],
                          "cache_hit": False, "transfer": metrics,
                          "object_paths": object_paths,
                          "stage_seconds": time.monotonic() - started,
                          "registration_seconds": registration_seconds,
                          "manifest_id": manifest_id}
                _write_json(staging / "ready.json", result)
                os.replace(staging, package)
            except BaseException:
                shutil.rmtree(staging)
                raise
            self.emit({"event": "peer_transfer_end", "source_domain": source,
                       "target_domain": target_domain, "monotonic_s": time.monotonic(),
                       "snapshot_id": manifest["snapshot_id"], "stage_seconds": result["stage_seconds"],
                       "logical_bytes": sum(o["logical_bytes"] for o in {o["object_id"]: o for o in manifest["objects"]}.values()),
                       "missing_logical_bytes": estimate["logical_bytes"],
                       "shared_warm_hits": estimate["shared_warm_hits"],
                       "interface_bytes": None, **metrics})
            return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("serve", "ping", "fetch"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--address")
    parser.add_argument("--port", type=int)
    parser.add_argument("--owner")
    args = parser.parse_args()
    if args.operation == "serve":
        with ObjectServer((args.address, args.port), args.root, args.owner) as server:
            server.serve_forever()
    elif args.operation == "ping":
        with socket.create_connection((args.address, args.port), timeout=2) as connection:
            _send_header(connection, {"op": "ping"})
            reply, _ = _recv_header(connection)
            print(json.dumps(reply))
    else:
        request = json.load(sys.stdin)
        rows = []
        unique = {o["object_id"]: o for o in request["objects"]}
        for obj in unique.values():
            rows.append(fetch_object(Path(request["root"]), obj, request["address"], request["port"], request["timeout"]))
        print(json.dumps({"objects": rows, "payload_bytes": sum(r["payload_bytes"] for r in rows),
                          "protocol_bytes": sum(r["protocol_bytes"] for r in rows),
                          "cache_hits": sum(r["cache_hit"] for r in rows), "locality": process_locality()}))


if __name__ == "__main__":
    main()
