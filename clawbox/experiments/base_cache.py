"""Fixed, prewarmed template RAM cache for the standalone experiment workflow.

The SSD inode remains the snapshot identity. Only native read mappings use the
cache. Preparation happens before arrivals; optional creation observations read
only the known VM process mappings against the already validated registry.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

import yaml
from pydantic import Field, model_validator

from .spec import StrictFrozenModel

REGISTRY = Path("/run/clawbox/base-cache.json")
CAPABILITY = b"CLAWBOX_BASE_CACHE_V1"
MIB = 1024 * 1024


class BaseCachePlan(StrictFrozenModel):
    warm_root: str
    numa_node: int = Field(ge=0)
    capacity_mib: int = Field(gt=0)
    vmm_binary: str
    templates: dict[str, str] = Field(min_length=1)
    aliases: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_paths(self):
        if set(self.aliases) - set(self.templates):
            raise ValueError("base cache aliases must name selected template identities")
        for value in (self.warm_root, self.vmm_binary, *self.templates.values(),
                      *(p for paths in self.aliases.values() for p in paths)):
            if not value.startswith("/") or ".." in Path(value).parts:
                raise ValueError(f"base cache paths must be absolute without '..': {value}")
        if any(not key.strip() for key in self.templates):
            raise ValueError("template identities must not be empty")
        return self


def load_plan(path: Path) -> BaseCachePlan:
    return BaseCachePlan.model_validate(yaml.safe_load(path.read_text()))


def init_plan(output: Path, *, spec_path: Path, templates: list[str],
              capacity_mib: int, vmm_binary: str | None = None) -> dict:
    """Resolve selected RAM paths from actual native XFS catalogs, not guesses."""
    from . import load_experiment
    spec = load_experiment(spec_path)
    resources = spec.resources
    if not resources.warm_snapshot_root or resources.warm_numa_node is None:
        raise ValueError("configure the experiment WARM root/node before base-cache init")
    if capacity_mib > resources.warm_memory_capacity_mib:
        raise ValueError("cache capacity_mib cannot exceed the experiment WARM budget")
    storage = Path(resources.cubelet_storage_root) / 'xfs'
    files = {}
    aliases = {}
    binaries = set()
    peer = resources.snapshot_scheduling_storage
    roots = ([Path(root) for _, root in sorted(peer.local_roots.items())]
             if peer is not None and peer.topology == 'peer-local-ssd' else [storage])
    for template in templates:
        if Path(template).name != template or template in {'.', '..'}:
            raise ValueError("template must be a native catalog identity")
        catalog = roots[0] / 'snapshots' / template / 'metadata' / 'catalog.json'
        value = json.loads(catalog.read_text())
        volume = value['memory_vol']
        def memory_path(root, metadata):
            vol = metadata['memory_vol']
            if metadata['snapshot_id'] != template:
                raise ValueError(f"mismatched template RAM catalog: {template}")
            if metadata['memory_kind'] == 'volume' and Path(vol).name == vol and vol not in {'.', '..'}:
                return root / 'objects' / 'volumes' / vol / vol
            if metadata['memory_kind'] == 'clawbox-direct-file':
                direct = Path(vol)
                if direct.is_absolute() and direct.resolve().is_relative_to(root.resolve()):
                    return direct
            raise ValueError(f"unsupported/out-of-domain template RAM catalog: {template}")
        memory = memory_path(roots[0], value)
        identity(memory)
        if Path(str(memory) + '.lineage.json').exists():
            raise ValueError("select an initial template RAM image, not a layered pause snapshot")
        version = value.get('component_versions', {}).get('cube-shim', '')
        if not version or Path(version).name != version or version in {'.', '..'}:
            raise ValueError(f"template catalog has no valid cube-shim version: {catalog}")
        binary = (Path(vmm_binary) if vmm_binary else
                  storage.parent.parent / 'root/component_versions/cube-shim' / version / 'bin/containerd-shim-cube-rs')
        binaries.add(str(binary.resolve(strict=True)))
        files[template] = str(memory)
        if len(roots) > 1:
            aliases[template] = [str(memory_path(root, json.loads(
                (root / 'snapshots' / template / 'metadata/catalog.json').read_text()))) for root in roots[1:]]
    if len(binaries) != 1:
        raise ValueError("selected templates use different Shim binaries; prepare a set using one compatible native version")
    plan = BaseCachePlan(warm_root=resources.warm_snapshot_root,
        numa_node=resources.warm_numa_node, capacity_mib=capacity_mib,
        vmm_binary=binaries.pop(), templates=files, aliases=aliases)
    with output.open('x') as stream:
        yaml.safe_dump(plan.model_dump(mode='json'), stream, sort_keys=False)
    return {'config': str(output), 'plan': plan.model_dump(mode='json'),
            'next': 'Review the plan, then base-cache prepare CONFIG before --apply'}


def identity(path: Path) -> dict:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f"base cache requires a regular file, not a symlink: {path}")
    return {"dev": value.st_dev, "ino": value.st_ino, "size": value.st_size,
            "mtime_sec": value.st_mtime_ns // 1_000_000_000,
            "mtime_nsec": value.st_mtime_ns % 1_000_000_000}


def allocated(path: Path) -> int:
    return path.stat().st_blocks * 512


def digest(path: Path, *, discard_source_cache: bool = False) -> str:
    with path.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256").hexdigest()
        if discard_source_cache and hasattr(os, "posix_fadvise"):
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        return result


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_idle() -> None:
    # A paused snapshot has no cache dependency: its lineage names the SSD base.
    # Do not rely just on the experiment cgroup; other Cube users may be live.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().split(b"\0")
            name = Path(os.fsdecode(command[0])).name if command else ""
            if name in {"cloud-hypervisor", "cube-hypervisor", "containerd-shim-cube-rs"}:
                raise RuntimeError(f"VM process {entry.name} is still present; finish/destroy VMs before changing the base cache")
        except (FileNotFoundError, ProcessLookupError):
            pass


@contextmanager
def _maintenance():
    import fcntl
    if os.geteuid() != 0:
        raise RuntimeError("Run base-cache prepare/cleanup with bash scripts/clawbox --sudo")
    # Shared with the native cgroup registry, which requires root-only access.
    REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with REGISTRY.with_suffix(".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("an experiment or cache maintenance holds the cache lease; wait for it to finish") from exc
        _assert_idle()
        yield


@contextmanager
def experiment_lease():
    """Prevent changing an active registry between an arm's VM creations."""
    if os.name != "posix" or not REGISTRY.with_suffix(".lock").exists():
        yield
        return
    import fcntl
    with REGISTRY.with_suffix(".lock").open("r") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        yield


def _mount(plan: BaseCachePlan) -> dict:
    result = subprocess.run(
        ["findmnt", "--json", "--target", plan.warm_root, "-o", "TARGET,FSTYPE,OPTIONS"],
        check=True, capture_output=True, text=True,
    )
    mount = json.loads(result.stdout)["filesystems"][0]
    options = mount["options"].split(",")
    if (mount["fstype"] != "tmpfs" or "noswap" not in options
            or f"mpol=bind:{plan.numa_node}" not in options):
        raise RuntimeError("WARM root must be tmpfs with noswap and mpol=bind:<numa_node>; run experiment host apply first")
    return mount


def _capable(path: str) -> None:
    previous = b""
    with open(path, "rb") as stream:
        while block := stream.read(MIB):
            if CAPABILITY in previous + block:
                return
            previous = block[-len(CAPABILITY):]
    raise RuntimeError("VMM binary lacks CLAWBOX_BASE_CACHE_V1; build/install the matching patched CubeSandbox release")


def preview(plan: BaseCachePlan) -> dict:
    _mount(plan)
    _capable(plan.vmm_binary)
    files = {}
    warm_dev = Path(plan.warm_root).stat().st_dev
    for template, raw in sorted(plan.templates.items()):
        path = Path(raw)
        info = identity(path)
        if not info["size"] or info["dev"] == warm_dev:
            raise ValueError(f"base must be a nonempty persistent SSD file: {path}")
        key = (info["dev"], info["ino"])
        item = files.setdefault(key, {"source": str(path), "source_identity": info,
                                      "templates": [], "source_allocated_bytes": allocated(path)})
        item["templates"].append(template)
        for raw_alias in plan.aliases.get(template, []):
            alias = Path(raw_alias)
            alias_info = identity(alias)
            if alias_info['size'] != info['size'] or alias_info['dev'] == warm_dev:
                raise ValueError(f"base alias must be persistent and match its base length: {alias}")
            if (alias_info['dev'], alias_info['ino']) != key:
                item.setdefault('aliases', []).append({'source': str(alias), 'source_identity': alias_info})
    required = sum(((row['source_identity']['size'] + 65535) // 65536) * 65536
                   for row in files.values())
    if required > plan.capacity_mib * MIB:
        raise RuntimeError("base cache capacity exceeded; select fewer templates or increase capacity_mib")
    return {"plan": plan.model_dump(mode="json"), "entries": list(files.values()),
            "capacity_bytes": plan.capacity_mib * MIB,
            "required_bytes_upper_bound": required,
            "source_allocated_bytes": sum(row["source_allocated_bytes"] for row in files.values()),
            "registry": str(REGISTRY)}


def _copy_base(source: Path, target: Path, *, budget: int) -> str:
    """Materialize once, including zero pages, before any timed VM mappings."""
    checksum = hashlib.sha256()
    written = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while block := reader.read(MIB):
            checksum.update(block)
            # tmpfs mmap faults allocate sparse holes even for a read-only base.
            # Materialize them here to keep allocation off the VM creation path.
            written += ((len(block) + 65535) // 65536) * 65536
            if written > budget:
                raise RuntimeError("base cache capacity exceeded; select fewer templates or increase capacity_mib")
            writer.write(block)
        writer.truncate(reader.tell())
        writer.flush()
        os.fsync(writer.fileno())
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(reader.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    return checksum.hexdigest()


def _validate_registry(receipt: dict, *, full: bool = False) -> None:
    if receipt.get("schema_version") != 1 or not receipt.get("entries"):
        raise ValueError("invalid base cache registry; rerun base-cache prepare")
    plan = BaseCachePlan.model_validate(receipt["plan"])
    if identity(Path(plan.vmm_binary)) != receipt["vmm_identity"]:
        raise RuntimeError("VMM binary changed since preparation; prepare the cache against the installed binary again")
    _mount(plan)
    seen = set()
    cache_seen = set()
    cache_checksums = {}
    total = 0
    for row in receipt["entries"]:
        source, cache = Path(row["source"]), Path(row["cache"])
        if identity(source) != row["source_identity"] or identity(cache) != row["cache_identity"]:
            raise RuntimeError("base cache identity changed; clean up on an idle host and prepare again")
        if cache.stat().st_mode & 0o222:
            raise RuntimeError("cache must remain read-only; prepare it again")
        if not cache.is_relative_to(Path(plan.warm_root) / ".clawbox-base-cache"):
            raise ValueError("cache file is outside the owned cache directory")
        key = (row["source_identity"]["dev"], row["source_identity"]["ino"])
        if key in seen:
            raise ValueError("duplicate base cache identity")
        seen.add(key)
        cache_key = (row['cache_identity']['dev'], row['cache_identity']['ino'])
        if cache_key in cache_checksums and cache_checksums[cache_key] != row['sha256']:
            raise RuntimeError('shared base cache aliases have inconsistent content identity')
        cache_checksums[cache_key] = row['sha256']
        if cache_key not in cache_seen:
            cache_seen.add(cache_key)
            total += allocated(cache)
        if full and (digest(source, discard_source_cache=True) != row["sha256"] or digest(cache) != row["sha256"]):
            raise RuntimeError("base cache byte verification failed; rebuild this cache on an idle host")
    if total != receipt["allocated_bytes"] or total > plan.capacity_mib * MIB:
        raise RuntimeError("base cache allocation differs from its receipt")


def prepare(plan: BaseCachePlan, *, apply: bool = False) -> dict:
    report = preview(plan)
    if not apply:
        return report
    with _maintenance():
        if REGISTRY.exists():
            current = json.loads(REGISTRY.read_text())
            if current["plan"] != report["plan"]:
                raise RuntimeError("another base cache plan is active; base-cache cleanup it before changing plans")
            _validate_registry(current, full=True)
            return {**current, "reused": True}
        space = os.statvfs(plan.warm_root) if hasattr(os, 'statvfs') else None
        required = sum(((row['source_identity']['size'] + 65535) // 65536) * 65536
                       for row in report['entries'])
        if space and required > space.f_bavail * space.f_frsize:
            raise RuntimeError("insufficient free WARM tmpfs capacity; select fewer bases or free owned snapshots before preparation")
        directory = Path(plan.warm_root) / ".clawbox-base-cache"
        if directory.is_symlink():
            raise RuntimeError("cache directory must not be a symlink")
        directory.mkdir(mode=0o755, exist_ok=True)
        # Per-plan immutable directory; leftover partial preparation is never adopted.
        import tempfile
        attempt = Path(tempfile.mkdtemp(prefix="prepared-", dir=directory))
        _write_json(attempt / "preparation.json", {
            "kind": "clawbox.base-cache.preparation", "plan": report["plan"],
        })
        created = []
        started = time.monotonic()
        try:
            entries, total = [], 0
            for index, row in enumerate(report["entries"]):
                target = attempt / f"{index:06d}.mem"
                created.append(target)
                source = Path(row["source"])
                checksum = _copy_base(source, target, budget=plan.capacity_mib * MIB - total)
                if identity(source) != row["source_identity"] or digest(target) != checksum:
                    raise RuntimeError("source changed during preparation or cache verification failed")
                target.chmod(0o444)
                total += allocated(target)
                entries.append({**row, "cache": str(target), "cache_identity": identity(target),
                                "sha256": checksum, "allocated_bytes": allocated(target)})
                seen_sources = {(entry['source_identity']['dev'], entry['source_identity']['ino'])
                                for entry in entries}
                for alias in row.get('aliases', []):
                    alias_path = Path(alias['source'])
                    alias_key = (alias['source_identity']['dev'], alias['source_identity']['ino'])
                    if alias_key in seen_sources:
                        continue
                    if (digest(alias_path, discard_source_cache=True) != checksum
                            or identity(alias_path) != alias['source_identity']):
                        raise RuntimeError(f"base alias content or identity differs: {alias_path}")
                    entries.append({**alias, 'templates': row['templates'],
                                    'cache': str(target), 'cache_identity': identity(target),
                                    'sha256': checksum, 'allocated_bytes': allocated(target)})
                    seen_sources.add(alias_key)
            receipt = {"schema_version": 1, "plan": report["plan"], "entries": entries,
                       "vmm_identity": identity(Path(plan.vmm_binary)),
                       "allocated_bytes": total, "prepared_unix_s": time.time(),
                       "preparation_seconds": time.monotonic() - started}
            _write_json(attempt / "receipt.json", receipt)
            _validate_registry(receipt)
            _write_json(REGISTRY, receipt)
            return receipt
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            (attempt / "receipt.json").unlink(missing_ok=True)
            (attempt / "preparation.json").unlink(missing_ok=True)
            attempt.rmdir()
            raise


def status(*, full: bool = False) -> dict:
    try:
        enabled = REGISTRY.exists()
    except PermissionError as exc:
        raise RuntimeError('base-cache host checks require root; run bash scripts/clawbox --sudo') from exc
    if not enabled:
        return {"enabled": False}
    receipt = json.loads(REGISTRY.read_text())
    _validate_registry(receipt, full=full)
    return {"enabled": True, **receipt, "registry_sha256": digest(REGISTRY)}


def cleanup(*, apply: bool = False, plan: BaseCachePlan | None = None) -> dict:
    if plan is not None:
        return _cleanup_preparations(plan, apply=apply)
    if not REGISTRY.exists():
        return {"enabled": False, "removed": False}
    receipt = json.loads(REGISTRY.read_text())
    # Cleanup must work after source changes, but may delete only owned cache files.
    root = Path(receipt["plan"]["warm_root"]) / ".clawbox-base-cache"
    unique_rows = {row['cache']: row for row in receipt['entries']}
    paths = [Path(name) for name in unique_rows]
    for path, row in zip(paths, unique_rows.values()):
        if (not path.is_relative_to(root) or ".." in path.parts
                or identity(path) != row["cache_identity"]):
            raise RuntimeError(f"refusing cleanup of changed/unowned cache: {path}")
    if apply:
        with _maintenance():
            if json.loads(REGISTRY.read_text()) != receipt:
                raise RuntimeError("base cache changed during cleanup; retry")
            REGISTRY.unlink()
            for path in paths:
                path.unlink()
            for directory in {path.parent for path in paths}:
                (directory / "receipt.json").unlink(missing_ok=True)
                (directory / "preparation.json").unlink(missing_ok=True)
                directory.rmdir()
    return {"removed": apply, "cache_files": [str(path) for path in paths],
            "allocated_bytes": receipt["allocated_bytes"], "source_files_preserved": True}


def _cleanup_preparations(plan: BaseCachePlan, *, apply: bool) -> dict:
    """Recover interrupted preparation without adopting unverified files."""
    root = Path(plan.warm_root) / ".clawbox-base-cache"
    candidates = []
    for marker in sorted(root.glob("prepared-*/preparation.json")):
        if marker.parent.is_symlink() or marker.is_symlink():
            raise RuntimeError("refusing symlink in cache preparation directory")
        owner = json.loads(marker.read_text())
        if owner != {"kind": "clawbox.base-cache.preparation", "plan": plan.model_dump(mode='json')}:
            continue
        paths = list(marker.parent.iterdir())
        for path in paths:
            if (not path.is_file() or path.is_symlink()
                    or not (path.name in {'preparation.json', 'receipt.json'}
                            or (len(path.name) == 10 and path.name[:6].isdigit() and path.suffix == '.mem'))):
                raise RuntimeError(f"unexpected file in owned preparation; preserve and inspect: {path}")
        candidates.append((marker.parent, paths))
    active = json.loads(REGISTRY.read_text()) if REGISTRY.exists() else None
    if apply:
        with _maintenance():
            if (json.loads(REGISTRY.read_text()) if REGISTRY.exists() else None) != active:
                raise RuntimeError("base cache changed during cleanup; retry")
            if active and active['plan'] == plan.model_dump(mode='json'):
                REGISTRY.unlink()
            elif active and any(Path(row['cache']).parent == directory
                                for directory, _ in candidates for row in active['entries']):
                raise RuntimeError("another active cache references this preparation")
            for directory, paths in candidates:
                for path in paths:
                    path.unlink()
                directory.rmdir()
    return {"removed": apply, "preparations": [str(path) for path, _ in candidates],
            "source_files_preserved": True}


def qualification_warm_bytes(resources) -> int:
    if resources.base_template_cache != 'required':
        return 0
    value = os.statvfs(resources.warm_snapshot_root)
    return (value.f_blocks - value.f_bfree) * value.f_frsize


def qualification_input(resources) -> dict:
    if resources.base_template_cache != 'required':
        return {}
    receipt = check_experiment(resources)
    return {'base_template_cache': {'registry_sha256': receipt['registry_sha256'],
                                    'vmm_identity': receipt['vmm_identity']}}


def verify_created_mapping(resources, sandbox_id: str, template: str, *,
                           receipt: dict | None = None, pids=None,
                           fail_on_miss: bool = True) -> dict:
    """Verify private mappings; timed callers supply one validated receipt/PIDs.

    Supplying these skips registry revalidation and the global process scan.
    Only the selected VM's small maps files are inspected; no file hashing.
    """
    if resources.base_template_cache != 'required':
        return {'required': False}
    receipt = check_experiment(resources) if receipt is None else receipt
    expected = [row for row in receipt['entries'] if template in row['templates']]
    if not expected:
        return {'required': False, 'template_selected': False, 'cache_hit': None}
    if len({row['cache'] for row in expected}) != 1:
        raise RuntimeError('template cache identity is ambiguous')
    row = expected[0]
    if pids is None:
        from .qualification import _sandbox_pids
        pids = _sandbox_pids(sandbox_id)
    matched = []
    for pid in pids:
        try:
            lines = Path(f'/proc/{pid}/maps').read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue
        for line in lines:
            fields = line.split(maxsplit=5)
            if len(fields) >= 5:
                major, minor = (int(value, 16) for value in fields[3].split(':'))
                matched_identity = (os.makedev(major, minor) == row['cache_identity']['dev']
                                    and int(fields[4]) == row['cache_identity']['ino'])
                if len(fields) == 6 and fields[5] == row['cache'] and not matched_identity:
                    raise RuntimeError('VM cache mapping differs from prepared inode identity')
                if not matched_identity:
                    continue
                if fields[1][-1] != 'p':
                    raise RuntimeError('VM cache mapping is not the prepared private file mapping')
                matched.append(pid)
    if not matched and fail_on_miss:
        raise RuntimeError('selected VM did not map its WARM base; check the template versioned Shim and private lazy restore configuration')
    return {'required': True, 'passed': bool(matched), 'cache_hit': bool(matched),
            'template_selected': True, 'template': template,
            'cache': row['cache'], 'cache_identity': row['cache_identity'],
            'pids': sorted(set(matched)), 'observation': 'private maps device/inode matches prepared cache'}


def observe_local_ssd_backing(domain_root: str, template: str, pids) -> dict:
    """Corroborate selected native catalog and actual open local rootfs backing."""
    root = Path(domain_root).resolve()
    catalog = root / 'snapshots' / template / 'metadata/catalog.json'
    metadata = json.loads(catalog.read_text())
    if metadata.get('snapshot_id') != template:
        raise RuntimeError('selected template catalog identity does not match creation')
    opened = []
    for pid in pids:
        try:
            descriptors = list(Path(f'/proc/{pid}/fd').iterdir())
        except (FileNotFoundError, ProcessLookupError):
            continue
        for descriptor in descriptors:
            try:
                target = descriptor.resolve(strict=True)
                if target.is_relative_to(root / 'objects/volumes'):
                    value = descriptor.stat()
                    opened.append({'pid': pid, 'fd': descriptor.name, 'path': str(target),
                                   'dev': value.st_dev, 'ino': value.st_ino})
                    break
            except (FileNotFoundError, ProcessLookupError):
                continue
    return {'local_ssd_template_hit': bool(opened), 'catalog': str(catalog),
            'catalog_identity': identity(catalog), 'open_rootfs_descriptors': opened,
            'observation': 'selected native template catalog present and VM rootfs descriptor opened inside current domain; not a page-cache or physical-I/O hit count'}


def check_experiment(resources) -> dict:
    receipt = status()
    expected = resources.base_template_cache == "required"
    if expected != receipt["enabled"]:
        raise RuntimeError("base_template_cache differs from host state; prepare/cleanup the host cache and use a new run ID")
    if expected:
        plan = receipt["plan"]
        if (Path(plan["warm_root"]).resolve() != Path(resources.warm_snapshot_root).resolve()
                or plan["numa_node"] != resources.warm_numa_node
                or plan["capacity_mib"] > resources.warm_memory_capacity_mib):
            raise RuntimeError("base cache root, NUMA node or budget differs from experiment resources")
    return receipt
