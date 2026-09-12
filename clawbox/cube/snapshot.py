"""Read native CubeSandbox snapshot manifests; never inspect Guest RAM."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_snapshot_metadata(path: str, *, require_lineage: bool = False) -> dict[str, Any]:
    memory = Path(path)
    sidecar = Path(path + ".lineage.json")
    if not sidecar.exists():
        if require_lineage:
            raise RuntimeError("incremental checkpoint did not publish a native RAM lineage")
        stat = memory.stat()
        result = {"logical_bytes": stat.st_size, "allocated_bytes": stat.st_blocks * 512}
        metrics = Path(path + ".metrics.json")
        if metrics.exists():
            result.update(json.loads(metrics.read_text(encoding="utf-8")))
            result["allocated_bytes"] = (stat.st_blocks + metrics.stat().st_blocks) * 512
        return result
    manifest = json.loads(sidecar.read_text(encoding="utf-8"))
    layers = manifest.get("layers")
    if manifest.get("schema_version") != 1 or not isinstance(layers, list) or not layers:
        raise RuntimeError("invalid native RAM lineage")
    logical = manifest.get("logical_bytes")
    if type(logical) is not int or logical <= 0:
        raise RuntimeError("invalid native RAM logical size")
    files = [sidecar, memory]
    for index, layer in enumerate(layers):
        name = layer.get("name")
        if name != f"{index:06d}.mem":
            raise RuntimeError("invalid native RAM layer name")
        file = Path(path + ".layers") / name
        stat = file.stat()
        if file.is_symlink() or stat.st_size != logical:
            raise RuntimeError("missing or truncated native RAM layer")
        files.append(file)
    if not os.path.samefile(memory, files[-1]):
        raise RuntimeError("native RAM manifest does not identify the current delta")
    unique = {}
    for file in files:
        stat = file.stat()
        unique[(stat.st_dev, stat.st_ino)] = stat.st_blocks * 512
    return {
        "snapshot_mechanism": "incremental-cow",
        "logical_bytes": logical,
        "allocated_bytes": sum(unique.values()),
        "transferred_bytes": manifest["transferred_bytes"],
        "dirty_bytes": manifest.get("dirty_bytes"),
        "full_base": manifest["full_base"],
        "lineage_layers": len(layers),
        "lineage_manifest_path": str(sidecar),
    }
