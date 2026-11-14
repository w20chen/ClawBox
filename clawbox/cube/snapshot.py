"""Read native CubeSandbox snapshot manifests; never inspect Guest RAM."""
from __future__ import annotations

from clawbox.experiments.control_timing import operation

import json
import os
from pathlib import Path
from typing import Any


def _allocated_bytes(path: Path, stat: os.stat_result | None = None) -> int:
    """Return physical allocation without substituting logical file length."""
    value = stat or path.stat()
    blocks = getattr(value, "st_blocks", None)
    if blocks is not None:
        return int(blocks) * 512
    if os.name == "nt":
        # Windows does not expose st_blocks. GetCompressedFileSizeW returns
        # the physical bytes allocated for sparse and compressed files too.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = kernel32.GetCompressedFileSizeW
        function.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        function.restype = wintypes.DWORD
        high = wintypes.DWORD()
        ctypes.set_last_error(0)
        low = function(str(path), ctypes.byref(high))
        if low == 0xFFFFFFFF and ctypes.get_last_error():
            raise OSError(ctypes.get_last_error(), f"cannot read allocation for {path}")
        return (int(high.value) << 32) | int(low)
    raise RuntimeError(f"physical allocation is unavailable for {path}")


@operation('native.snapshot_metadata')
def read_snapshot_metadata(path: str, *, require_lineage: bool = False) -> dict[str, Any]:
    from .snapshot_compression import read_compressed_snapshot_metadata
    if compressed := read_compressed_snapshot_metadata(path):
        return compressed
    memory = Path(path)
    sidecar = Path(path + ".lineage.json")
    if not sidecar.exists():
        if require_lineage:
            raise RuntimeError("incremental checkpoint did not publish a native RAM lineage")
        stat = memory.stat()
        result = {"logical_bytes": stat.st_size,
                  "allocated_bytes": _allocated_bytes(memory, stat)}
        metrics = Path(path + ".metrics.json")
        if metrics.exists():
            result.update(json.loads(metrics.read_text(encoding="utf-8")))
            result["allocated_bytes"] = (
                _allocated_bytes(memory, stat) + _allocated_bytes(metrics)
            )
        return result
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    layers = manifest.get("layers")
    if manifest.get("schema_version") not in {1, 2} or not isinstance(layers, list) or not layers:
        raise RuntimeError("invalid native RAM lineage")
    logical = manifest.get("logical_bytes")
    if type(logical) is not int or logical <= 0:
        raise RuntimeError("invalid native RAM logical size")
    # External layers are immutable dependencies. Legacy schema 1 has only a
    # template base at index zero; schema 2 may also reference managed COLD
    # layers at any position. None are charged to the WARM snapshot pool.
    owned_files = [sidecar, memory]
    layer_files = []
    external_base_identity = None
    external_base_allocated_bytes = 0
    external_layer_paths = []
    external_layer_allocations = []
    owned_layer_allocated_bytes = 0
    owned_layer_allocations: dict[str, int] = {}
    owned_ranges: list[tuple[int, int]] = []
    layer_allocations = []
    for index, layer in enumerate(layers):
        name = layer.get("name")
        if name != f"{index:06d}.mem":
            raise RuntimeError("invalid native RAM layer name")
        external_path = layer.get("external_path")
        if external_path is not None:
            managed = bool(layer.get("managed"))
            layer_id = layer.get("layer_id")
            if not isinstance(external_path, str) or not Path(external_path).is_absolute():
                raise RuntimeError("invalid external native RAM base")
            if not managed and index != 0:
                raise RuntimeError("legacy external native RAM base must be layer zero")
            if managed and (
                not isinstance(layer_id, str) or not layer_id
                or "/" in layer_id or "\\" in layer_id
                or Path(external_path).parent.name != layer_id
            ):
                raise RuntimeError("invalid managed native RAM layer identity")
            file = Path(external_path)
        else:
            file = Path(path + ".layers") / name
        stat = file.stat()
        if file.is_symlink() or stat.st_size != logical:
            raise RuntimeError("missing or truncated native RAM layer")
        layer_files.append(file)
        layer_allocations.append({
            "layer_index": index,
            "identity": f"{stat.st_dev}:{stat.st_ino}",
            "allocated_bytes": _allocated_bytes(file, stat),
            "external": external_path is not None,
        })
        if external_path is None:
            owned_files.append(file)
            owned_layer_allocated_bytes += _allocated_bytes(file, stat)
            owned_layer_allocations[f"{stat.st_dev}:{stat.st_ino}"] = _allocated_bytes(file, stat)
            for item in layer.get("ranges") or []:
                offset = item.get("offset")
                length = item.get("length")
                if (type(offset) is int and type(length) is int
                        and offset >= 0 and length > 0):
                    owned_ranges.append((offset, offset + length))
        else:
            identity = f"{stat.st_dev}:{stat.st_ino}"
            allocated = _allocated_bytes(file, stat)
            external_layer_paths.append(external_path)
            external_layer_allocations.append({
                "layer_index": index,
                "path": external_path,
                "layer_id": layer_id if managed else None,
                "managed": managed,
                "identity": identity,
                "allocated_bytes": allocated,
            })
            external_base_allocated_bytes += allocated
            if index == 0:
                external_base_identity = identity
    if layers[-1].get("external_path") is None:
        if not os.path.samefile(memory, layer_files[-1]):
            raise RuntimeError("native RAM manifest does not identify the current delta")
    elif memory.is_symlink() or not memory.is_file() or memory.stat().st_size != logical:
        raise RuntimeError("zero-dirty native RAM snapshot placeholder is invalid")
    unique = {}
    for file in owned_files:
        stat = file.stat()
        unique[(stat.st_dev, stat.st_ino)] = _allocated_bytes(file, stat)
    reused_base_allocated_bytes = int(manifest.get("reused_base_allocated_bytes", 0))
    owned_logical_bytes = 0
    end = 0
    for start, stop in sorted(owned_ranges):
        start = max(start, end)
        if stop > start:
            owned_logical_bytes += stop - start
            end = stop
    return {
        "lineage_schema_version": manifest["schema_version"],
        "snapshot_mechanism": "incremental-cow",
        "logical_bytes": logical,
        "allocated_bytes": sum(unique.values()),
        "transferred_bytes": manifest["transferred_bytes"],
        "dirty_bytes": manifest.get("dirty_bytes"),
        "full_base": manifest["full_base"],
        "lineage_layers": len(layers),
        "lineage_manifest_path": str(sidecar),
        "external_base_path": layers[0].get("external_path"),
        "external_base_identity": external_base_identity,
        "external_base_allocated_bytes": external_base_allocated_bytes,
        "external_layer_paths": external_layer_paths,
        "external_layer_allocations": external_layer_allocations,
        "managed_external_bytes": sum(
            item["allocated_bytes"] for item in external_layer_allocations if item["managed"]
        ),
        "private_delta_bytes": max(0, owned_layer_allocated_bytes - reused_base_allocated_bytes),
        "owned_allocated_bytes": sum(owned_layer_allocations.values()),
        "owned_logical_bytes": min(logical, owned_logical_bytes),
        # A COLD lineage owns names, not necessarily exclusive data blocks.
        # Deduplicate identities across lineages when summing physical storage.
        "layer_allocations": layer_allocations,
        "reused_base_allocated_bytes": reused_base_allocated_bytes,
        "dependency_count": max(0, len(layers) - 1) + len(external_layer_allocations),
    }
