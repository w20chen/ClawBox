"""Host identity checks for the Cube shim/runtime selected by pinned templates."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


DEFAULT_SERVICE_BIN_DIR = Path("/usr/local/services/cubetoolbox/cube-shim/bin")
DEFAULT_VERSIONED_ROOT = Path("/data/cubelet/root/component_versions/cube-shim")
DEFAULT_COMPONENT_VERSION = "v0.7.0-clawbox-standalone"
COMPONENT_FILES = ("containerd-shim-cube-rs", "cube-runtime")
LIFECYCLE_SHIM_MARKERS = (
    b"memory.restore_lineage_load",
    b"memory.lineage_parent_rejected",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _contains_markers(path: Path, markers: tuple[bytes, ...]) -> bool:
    found: set[bytes] = set()
    overlap = max(map(len, markers)) - 1
    tail = b""
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            window = tail + chunk
            found.update(marker for marker in markers if marker in window)
            tail = window[-overlap:]
    return found == set(markers)


def lifecycle_component_identity(
    *,
    service_bin_dir: Path = DEFAULT_SERVICE_BIN_DIR,
    versioned_root: Path = DEFAULT_VERSIONED_ROOT,
    component_version: str | None = None,
) -> dict:
    """Compare the default and template-pinned Cube lifecycle binaries.

    Cubelet starts restored VMs from ``component_versions/<version>/bin`` when
    a template pins that version, while service qualification may inspect the
    default ``cube-shim/bin`` path.  A split build lets a VM run an older shim
    that cannot inherit the restored RAM parent, silently turning the next COLD
    checkpoint into a full base.  The two locations must therefore be byte
    identical before any WARM/COLD experiment starts.
    """
    version = (
        component_version
        or os.environ.get("CLAWBOX_COMPONENT_SHIM_VERSION")
        or DEFAULT_COMPONENT_VERSION
    ).strip()
    if not version or "/" in version or version in {".", ".."}:
        return {
            "ok": False,
            "component_version": version,
            "errors": [f"invalid component version: {version!r}"],
            "files": {},
        }

    service_dir = Path(service_bin_dir)
    component_dir = Path(versioned_root) / version / "bin"
    errors: list[str] = []
    files: dict[str, dict] = {}
    for name in COMPONENT_FILES:
        service = service_dir / name
        component = component_dir / name
        record = {
            "service_path": str(service),
            "component_path": str(component),
            "service_sha256": None,
            "component_sha256": None,
            "match": False,
        }
        files[name] = record
        for label, path in (("service", service), ("component", component)):
            try:
                if not path.is_file() or not os.access(path, os.X_OK):
                    raise OSError("missing or not executable")
                record[f"{label}_sha256"] = _sha256(path)
            except OSError as exc:
                errors.append(f"{label} {name} unavailable at {path}: {exc}")
        if (
            record["service_sha256"] is not None
            and record["component_sha256"] is not None
        ):
            record["match"] = (
                record["service_sha256"] == record["component_sha256"]
            )
            if not record["match"]:
                errors.append(
                    f"{name} build mismatch: service={record['service_sha256']} "
                    f"component={record['component_sha256']} "
                    f"version={version}"
                )
        if name == "containerd-shim-cube-rs":
            for label in ("service", "component"):
                path = Path(record[f"{label}_path"])
                if not path.is_file() or not os.access(path, os.X_OK):
                    continue
                capable = _contains_markers(path, LIFECYCLE_SHIM_MARKERS)
                record[f"{label}_lineage_capable"] = capable
                if not capable:
                    errors.append(
                        f"{label} {name} lacks incremental lineage restore markers: "
                        f"{path}"
                    )
    return {
        "ok": not errors,
        "component_version": version,
        "service_bin_dir": str(service_dir),
        "component_bin_dir": str(component_dir),
        "errors": errors,
        "files": files,
    }
