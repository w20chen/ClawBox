from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


BASH_UNAVAILABLE = sys.platform == "win32" or shutil.which("bash") is None


requires_bash = pytest.mark.skipif(
    BASH_UNAVAILABLE,
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
    env["CLAWBOX_PYTHON"] = "python3"
    return env


@requires_bash
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
IMAGE_DIGEST=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
case "$*" in
  "get nodes --no-headers") echo "kunpeng Ready" ;;
  "get nodes -l clawbox.openai.com/firecracker-ready=true --no-headers") echo "kunpeng Ready" ;;
  "get runtimeclass "*) ;;
  *"jsonpath={.status.readyReplicas}") echo 1 ;;
  *"jsonpath={.spec.replicas}") echo 1 ;;
  *"jsonpath={.spec.template.spec.containers[0].image}") echo "registry.example/clawbox/control@sha256:${IMAGE_DIGEST}" ;;
  "-n clawbox-system get deployment clawbox-cell-controller -o json")
    printf '{"spec":{"template":{"spec":{"containers":[{"env":[{"name":"RUNTIME_IMAGE","value":"registry.example/clawbox/runtime@sha256:%s"},{"name":"TOOL_BRIDGE_IMAGE","value":"registry.example/clawbox/bridge@sha256:%s"}]}]}}}}' "${IMAGE_DIGEST}" "${IMAGE_DIGEST}"
    ;;
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


@requires_bash
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
  "-n clawbox-system port-forward service/clawbox-managed-api :8085")
    echo "Forwarding from 127.0.0.1:49152 -> 8085"
    while true; do sleep 1; done
    ;;
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
    assert "token=token api=http://127.0.0.1:49152" in result.stdout
    assert "args=-m clawbox.cli submit --input-ref task-a" in result.stdout


def test_up_script_cannot_invoke_destructive_bootstrap():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "bootstrap-openeuler-arm64" not in source


def test_public_cli_routes_configure_to_host_workflow():
    source = (ROOT / "scripts/clawbox").read_text(encoding="utf-8")
    assert "up|doctor|configure|install|traces" in source


def test_trace_export_is_a_single_host_command():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'traces) export_traces "$@"' in source
    assert "service/clawbox-ingester :8084" in source
    assert "CLAWBOX_ARCHIVE_TOKEN" in source


def test_host_workflow_generates_secrets_without_persisting_plaintext_in_repo():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "create secret generic clawbox-control-plane" in source
    assert "create secret generic clawbox-llm" in source
    assert "create secret generic clawbox-managed" in source
    assert '--from-file="llm-api-key=' in source
    assert "resolve_image_digest" in source


def test_legacy_migration_emits_real_tab_separators():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'print("\\t".join(' in source
    assert 'print("\\\\t".join(' not in source


def test_migrations_run_inside_pinned_control_plane_image():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'local docker_args=(--rm --network host --env-file' in source
    assert 'docker run "${docker_args[@]}"' in source
    assert '"${control_image}" alembic upgrade head' in source
    assert "python3 -m alembic" not in source
    assert "/var/lib/clawbox/managed:/data" in source


def test_capacity_inventory_is_not_hidden_behind_a_pipeline():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'collect-node-capacity.py" --configmap |' not in source
    assert 'get configmap clawbox-node-capacity' in source


def test_upgrade_stops_only_the_two_superseded_managed_containers():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "for container in clawbox-m1-api clawbox-m1-dispatcher" in source
    assert 'docker stop "${container}"' in source
    assert "docker rm" not in source


def test_managed_secret_updates_restart_existing_consumers():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "restart_managed_deployments_if_present" in source
    assert 'rollout restart "deployment/${deployment}"' in source


def test_generated_tokens_never_include_openssl_trailing_newline():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'openssl rand -hex 32 >' not in source
    assert "openssl rand -hex 32 | tr -d '\\n'" in source


def test_install_combines_one_time_configuration_and_deployment():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert 'install) shift; install_host "$@"' in source
    assert 'parse_configure_args "$@"' in source
    assert "configure" in source


def test_existing_deployment_still_runs_upgrade_reconciliation():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    up = source.split("\nup() {", 1)[1].split("\ninstall_host() {", 1)[0]
    assert "Existing ClawBox deployment found; reconciling it in place" in up
    assert "preflight_reconcile" in up
    assert "run_migrations" in up
    assert "return" not in up


def test_platform_build_handoff_and_standard_image_env_names_are_supported():
    host = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    build = (ROOT / "scripts/build-kubernetes-images.sh").read_text(encoding="utf-8")
    assert '${CLAWBOX_CONTROL_IMAGE:-${CONTROL_IMAGE:-}}' in host
    assert '${CLAWBOX_RUNTIME_IMAGE:-${RUNTIME_IMAGE:-}}' in host
    assert '${CLAWBOX_TOOL_BRIDGE_IMAGE:-${TOOL_BRIDGE_IMAGE:-}}' in host
    assert "platform-images.env" in host
    assert "Saved platform image handoff" in build
    assert '"${candidate}" == "${repository}"@sha256:*' in build


def test_swe_overlay_build_fails_closed_on_clawtune_revision_drift():
    source = (ROOT / "scripts/rebuild-swe-rebench-tool-overlay.sh").read_text(encoding="utf-8")
    assert 'actual_clawtune_revision="$(git -C "${CLAWTUNE_ROOT}" rev-parse HEAD)"' in source
    assert 'EXPECTED_CLAWTUNE_REVISION:-76eab6fa5c6333f4e80901c030f10cab0e4ce605' in source
    assert 'does not match checkout' in source


def test_doctor_does_not_require_a_local_registry():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    doctor = source.split("\ndoctor() {", 1)[1].split("\nsecret_value() {", 1)[0]
    assert 'registry="SKIP (external registry supported)"' in doctor
    assert "failed=1" not in doctor.split("local-registry", 1)[0].rsplit("registry=", 1)[1]


def test_doctor_checks_init_container_images_too():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    doctor = source.split("\ndoctor() {", 1)[1].split("\nsecret_value() {", 1)[0]
    assert 'deployment_init_images "${deployment}"' in doctor
    assert ".spec.template.spec.initContainers[*]" in source


def test_task_cli_uses_dynamic_local_port_forward():
    source = (ROOT / "scripts/clawbox-host.sh").read_text(encoding="utf-8")
    assert "service/clawbox-managed-api :8085" in source
    assert 'CLAWBOX_API_URL="http://127.0.0.1:${local_port}"' in source
