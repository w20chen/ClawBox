"""Locate ClawTune and reuse its native seed/state interfaces."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def use_clawtune() -> None:
    explicit = os.getenv("CLAWTUNE_SIDECAR_SRC") or os.getenv("CLAWTUNE_SCHEDULER_SRC")
    root = Path(os.getenv("CLAWTUNE_ROOT", str(Path(__file__).resolve().parents[2] / "ClawTune")))
    candidates = [explicit, str(root / "services/sidecar/src"), "/opt/clawtune/services/sidecar/src"]
    if explicit and not Path(explicit).is_dir():
        raise RuntimeError(f"CLAWTUNE_SIDECAR_SRC does not exist: {explicit}")
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            if candidate in sys.path:
                sys.path.remove(candidate)
            sys.path.insert(0, candidate)
            return
    # A wheel installed from main is also supported.


def seed_directory() -> Path:
    use_clawtune()
    from clawtune_kb.contracts import data_root
    from clawtune_kb import validate_seed

    path = Path(os.environ["CLAWTUNE_COLD_START_DIR"]) if os.getenv("CLAWTUNE_COLD_START_DIR") else data_root() / "seeds/bootstrap-v1"
    validate_seed(path)
    return path


def source_revision(root: Path) -> str:
    """Read export provenance without accidentally resolving an enclosing repo."""
    marker = root / "CLAWTUNE_REVISION"
    if marker.is_file():
        revision = marker.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("invalid ClawTune export revision")
        return revision
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
        return "unknown"
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()

