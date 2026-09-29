"""Read native CubeSandbox snapshot manifests; never inspect Guest RAM."""
from __future__ import annotations

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


def read_snapshot_metadata(path: str, *, require_lineage: bool = False) -> dict[str, Any]:
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
    if manifest.get("schema_version") != 1 or not isinstance(layers, list) or not layers:
        raise RuntimeError("invalid native RAM lineage")
    logical = manifest.get("logical_bytes")
    if type(logical) is not int or logical <= 0:
        raise RuntimeError("invalid native RAM logical size")
    # External layer zero is the immutable template RAM image.  It is a
    # dependency of the lineage, but it is not owned by (or charged to) the
    # WARM snapshot pool.
    owned_files = [sidecar, memory]
    layer_files = []
    for index, layer in enumerate(layers):
        name = layer.get("name")
        if name != f"{index:06d}.mem":
            raise RuntimeError("invalid native RAM layer name")
        external_path = layer.get("external_path")
        if external_path is not None:
            if index != 0 or not isinstance(external_path, str) or not Path(external_path).is_absolute():
                raise RuntimeError("invalid external native RAM base")
            file = Path(external_path)
        else:
            file = Path(path + ".layers") / name
        stat = file.stat()
        if file.is_symlink() or stat.st_size != logical:
            raise RuntimeError("missing or truncated native RAM layer")
        layer_files.append(file)
        if external_path is None:
            owned_files.append(file)
    if not os.path.samefile(memory, layer_files[-1]):
        raise RuntimeError("native RAM manifest does not identify the current delta")
    unique = {}
    for file in owned_files:
        stat = file.stat()
        unique[(stat.st_dev, stat.st_ino)] = _allocated_bytes(file, stat)
    return {
        "snapshot_mechanism": "incremental-cow",
        "logical_bytes": logical,
        "allocated_bytes": sum(unique.values()),
        "transferred_bytes": manifest["transferred_bytes"],
        "dirty_bytes": manifest.get("dirty_bytes"),
        "full_base": manifest["full_base"],
        "lineage_layers": len(layers),
        "lineage_manifest_path": str(sidecar),
        "external_base_path": layers[0].get("external_path"),
    }
