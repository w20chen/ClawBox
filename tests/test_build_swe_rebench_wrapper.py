from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_build_swe_rebench_wrapper_exposes_dataset_workflow() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), env.get("PYTHONPATH")) if value
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build-swe-rebench-arm64.py"), "--help"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--dataset" in result.stdout
    assert "--selection" in result.stdout
    assert "--swebench-root" in result.stdout
