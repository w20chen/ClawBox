import os
import sys
from pathlib import Path

import pytest


# Native tuning tests intentionally exercise the explicitly selected ClawTune
# implementation when one is configured, then the main-branch sibling used by
# a developer checkout.  This must match the source selected by ClawBox itself;
# otherwise one process can mix an older checkout with a newer installed wheel.
_configured_clawtune_src = os.getenv("CLAWTUNE_SIDECAR_SRC")
_clawtune_src = (
    Path(_configured_clawtune_src).expanduser()
    if _configured_clawtune_src
    else Path(__file__).resolve().parents[2] / "ClawTune" / "services" / "sidecar" / "src"
)
if _configured_clawtune_src and not _clawtune_src.is_dir():
    raise RuntimeError(f"CLAWTUNE_SIDECAR_SRC does not exist: {_clawtune_src}")
if _clawtune_src.is_dir():
    sys.path.insert(0, str(_clawtune_src.resolve()))


@pytest.fixture(scope="session")
def clawtune_shell_parser() -> Path:
    """Require ClawTune's native parser on Linux; unsupported clients may skip."""
    if sys.platform != "linux":
        pytest.skip("ClawTune's pinned mvdan adapter is exercised on Linux")

    from tool_resource.mvdan_client import MvdanClientError, ensure_compatible_adapter

    try:
        return ensure_compatible_adapter()
    except MvdanClientError:
        raise
