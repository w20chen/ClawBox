from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_preserves_helm_database_templates() -> None:
    script = (ROOT / "scripts" / "install-cubesandbox-kunpeng920.sh").read_text(
        encoding="utf-8"
    )

    assert 'include "cube.dbHost"' in script
    assert 'include "cube.redisNodes"' in script
    assert "target.write_text(source.read_text())" not in script
    assert "overcommit_ratio" in script
    assert "rewrite stop name regex (.*)[.]cube[.]local[.]?" in script
    assert "answer auto" in script


def test_template_helper_exposes_cube_command_ports() -> None:
    helper = (ROOT / "scripts" / "register-cube-template.py").read_text(
        encoding="utf-8"
    )

    assert 'action="append"' in helper
    assert "args.exposed_port or [49983]" in helper
    assert "default=49983" in helper


def test_installer_reproduces_semantic_api_and_reboot_services() -> None:
    script = (ROOT / "scripts" / "install-cubesandbox-kunpeng920.sh").read_text(
        encoding="utf-8"
    )
    patch = (
        ROOT / "deploy" / "cubesandbox" / "semantic-tcp-endpoint-v0.7.0.patch"
    ).read_text(encoding="utf-8")

    assert 'git -C "$SOURCE_DIR" apply "$SEMANTIC_ENDPOINT_PATCH"' in script
    assert "images.api.repository" in script
    assert "clawbox-registry-mirror.service" in script
    assert "--set cubeNode.enabled=false" in script
    assert "recover-cubesandbox-s3lvol-kunpeng920.sh" in script
    assert "/sandboxes/:sandboxID/ports/:containerPort" in patch
    assert "def get_tcp_endpoint" in patch


def test_pair_provisioner_emits_immutable_template_manifest() -> None:
    script = (ROOT / "scripts" / "provision-kunpeng-openclaw.sh").read_text(
        encoding="utf-8"
    )

    assert "runtime-cube-arm64" in script
    assert "tool-cube-arm64" in script
    assert "digest_ref" in script
    assert "--exposed-port 2222" in script
    assert "validate-cubesandbox-tcp-endpoints.py" in script
    assert "smoke-cubesandbox-agent-pair.py" in script
    assert "audit-cube-sandboxes.py" in script
