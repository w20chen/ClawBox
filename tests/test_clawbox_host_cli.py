from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="host operator is a Linux bash entrypoint",
)


def _command(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _env(mock_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}{os.pathsep}{env['PATH']}"
    env.pop("CLAWBOX_TOKEN", None)
    env.pop("CLAWBOX_API_URL", None)
    return env


def test_doctor_reports_complete_host_concisely(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    _command(
        mock_bin / "uname",
        'if [[ "${1:-}" == "-m" ]]; then echo aarch64; else echo Linux; fi\n',
    )
    _command(mock_bin / "curl", "exit 0\n")
    _command(
        mock_bin / "kubectl",
        r'''
case "$*" in
  "get nodes --no-headers") echo "kunpeng Ready" ;;
  "get nodes -l clawbox.openai.com/firecracker-ready=true --no-headers") echo "kunpeng Ready" ;;
  "get runtimeclass "*) ;;
  *"jsonpath={.status.readyReplicas}") echo 1 ;;
  *"jsonpath={.spec.replicas}") echo 1 ;;
  *) exit 1 ;;
esac
''',
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/clawbox-host.sh"), "doctor"],
        cwd=ROOT,
        env=_env(mock_bin),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "deploy/managed-api     PASS" in result.stdout
    assert "ClawBox is ready" in result.stdout


def test_cli_automatically_uses_cluster_token_and_local_api(tmp_path):
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    _command(mock_bin / "curl", "exit 0\n")
    _command(
        mock_bin / "kubectl",
        r'''
case "$*" in
  "get nodes") ;;
  "-n clawbox-system get deployment clawbox-managed-api") ;;
  "-n clawbox-system get secret clawbox-managed -o jsonpath={.data.service-token}") printf dG9rZW4= ;;
  *) exit 1 ;;
esac
''',
    )
    _command(
        mock_bin / "python3",
        'printf "token=%s api=%s args=%s\\n" "$CLAWBOX_TOKEN" "$CLAWBOX_API_URL" "$*"\n',
    )

    result = subprocess.run(
        ["bash", str(ROOT / "scripts/clawbox"), "submit", "--input-ref", "task-a"],
        cwd=ROOT,
        env=_env(mock_bin),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "token=token api=http://127.0.0.1:8085" in result.stdout
    assert "args=-m clawbox.cli submit --input-ref task-a" in result.stdout


def test_up_script_cannot_invoke_destructive_bootstrap():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "bootstrap-openeuler-arm64" not in source
