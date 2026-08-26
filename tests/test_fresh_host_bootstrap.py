from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASH_UNAVAILABLE = sys.platform == "win32" or shutil.which("bash") is None


def _command(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_kata_install_separates_staged_and_installed_host_audits() -> None:
    audit = (ROOT / "scripts/audit-kata-firecracker-arm64.sh").read_text(encoding="utf-8")
    builder = (ROOT / "scripts/build-kata-firecracker-arm64.sh").read_text(encoding="utf-8")
    staged, installed = builder.split('if [[ "${MODE}" == build ]]', maxsplit=1)

    assert '--artifact-only) ARTIFACT_ONLY=true' in audit
    assert 'if [[ "${ARTIFACT_ONLY}" == true ]]' in audit
    assert "installed-host Kata shim wrapper validation is deferred" in audit
    assert "--artifact-only" in staged
    wrapper_call = 'install-shim-nofile-wrapper.sh" --real-shim "${shim}"'
    full_audit = "--root /opt/kata --kata-version"
    assert wrapper_call in installed
    assert full_audit in installed
    assert installed.index(wrapper_call) < installed.index(full_audit)


def test_post_install_host_gate_requires_label_but_prelabel_gate_does_not() -> None:
    check_host = (ROOT / "deploy/check-host.sh").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap-openeuler-arm64.sh").read_text(encoding="utf-8")

    assert '--require-ready-label) REQUIRE_READY_LABEL=true' in check_host
    assert 'elif [[ "${REQUIRE_READY_LABEL}" == true ]]' in check_host
    assert "add it only after this pre-label gate passes" in check_host
    assert 'check-host.sh" --runtime-class "${RUNTIME_CLASS}" --require-ready-label' in bootstrap


@pytest.mark.skipif(BASH_UNAVAILABLE, reason="bootstrap operators are Linux bash scripts")
def test_artifact_only_audit_does_not_require_an_installed_host_wrapper(tmp_path: Path) -> None:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    _command(mock_bin / "file", "echo 'ELF 64-bit LSB executable, ARM aarch64'\n")

    kata = tmp_path / "kata"
    _command(kata / "bin/firecracker", "echo 'Firecracker v1.12.1'\n")
    _command(kata / "bin/jailer", "echo 'jailer v1.12.1'\n")
    _command(
        kata / "runtime-rs/bin/containerd-shim-kata-v2",
        "echo 'Kata Containers version 3.31.0'\n",
    )
    share = kata / "share/kata-containers"
    share.mkdir(parents=True)
    (share / "vmlinux").write_bytes(b"kernel")
    (share / "kata-containers.img").write_bytes(b"rootfs")
    config = kata / "share/defaults/kata-containers/configuration-fc-arm64.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[hypervisor.firecracker]
path = "/opt/kata/bin/firecracker"
jailer_path = "/opt/kata/bin/jailer"
kernel = "/opt/kata/share/kata-containers/vmlinux"
image = "/opt/kata/share/kata-containers/kata-containers.img"
default_vcpus = 2
default_maxvcpus = 32

[runtime]
static_sandbox_resource_mgmt = true
disable_guest_empty_dir = false

[agent.kata]
dial_timeout_ms = 1000
reconnect_timeout_ms = 30000
""".strip()
        + "\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}{os.pathsep}{env['PATH']}"
    env["CLAWBOX_SHIM_WRAPPER"] = str(tmp_path / "missing-host-wrapper")
    command = [
        "bash",
        str(ROOT / "scripts/audit-kata-firecracker-arm64.sh"),
        "--root",
        str(kata),
        "--kata-version",
        "3.31.0",
        "--firecracker-version",
        "1.12.1",
    ]

    staged = subprocess.run(
        [*command, "--artifact-only"], env=env, text=True, capture_output=True, check=False
    )
    installed = subprocess.run(command, env=env, text=True, capture_output=True, check=False)

    assert staged.returncode == 0, staged.stdout + staged.stderr
    assert "installed-host Kata shim wrapper validation is deferred" in staged.stdout
    assert installed.returncode != 0
    assert "Kata shim RLIMIT_NOFILE wrapper is not installed" in installed.stdout


@pytest.mark.skipif(BASH_UNAVAILABLE, reason="shim wrapper installer is a Linux bash script")
def test_explicit_real_shim_install_repairs_partial_state_and_is_idempotent(tmp_path: Path) -> None:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    _command(mock_bin / "id", '[[ "${1:-}" == -u ]] && { echo 0; exit; }\nexec /usr/bin/id "$@"\n')
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    shim_path = shim_dir / "containerd-shim-kata-v2"
    real_path = shim_dir / "containerd-shim-kata-v2.real"
    kata_v1 = tmp_path / "kata-v1"
    kata_v2 = tmp_path / "kata-v2"
    _command(kata_v1, "echo kata-v1\n")
    _command(kata_v2, "echo kata-v2\n")
    shim_path.symlink_to(kata_v1)

    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}{os.pathsep}{env['PATH']}"
    env["CLAWBOX_SHIM_DIR"] = str(shim_dir)
    installer = ["bash", str(ROOT / "scripts/install-shim-nofile-wrapper.sh"), "--real-shim"]

    first = subprocess.run(
        [*installer, str(kata_v1)], env=env, text=True, capture_output=True, check=False
    )
    second = subprocess.run(
        [*installer, str(kata_v2)], env=env, text=True, capture_output=True, check=False
    )
    version = subprocess.run(
        [str(shim_path), "--version"], env=env, text=True, capture_output=True, check=False
    )

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert "clawbox-shim-nofile-wrapper-v1" in shim_path.read_text(encoding="utf-8")
    assert real_path.resolve() == kata_v2.resolve()
    assert kata_v1.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
    assert version.returncode == 0
    assert version.stdout.strip() == "kata-v2"


@pytest.mark.skipif(BASH_UNAVAILABLE, reason="shim wrapper installer is a Linux bash script")
def test_wrapper_installer_recovers_after_real_shim_was_saved(tmp_path: Path) -> None:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    _command(mock_bin / "id", '[[ "${1:-}" == -u ]] && { echo 0; exit; }\nexec /usr/bin/id "$@"\n')
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir()
    real_path = shim_dir / "containerd-shim-kata-v2.real"
    _command(real_path, "echo recovered-kata\n")

    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}{os.pathsep}{env['PATH']}"
    env["CLAWBOX_SHIM_DIR"] = str(shim_dir)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-shim-nofile-wrapper.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    shim_path = shim_dir / "containerd-shim-kata-v2"
    version = subprocess.run(
        [str(shim_path), "--version"], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "repairing interrupted wrapper install" in result.stdout
    assert "clawbox-shim-nofile-wrapper-v1" in shim_path.read_text(encoding="utf-8")
    assert version.returncode == 0
    assert version.stdout.strip() == "recovered-kata"
