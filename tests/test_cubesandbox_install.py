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


def test_semantic_source_prepare_is_pinned_and_non_destructive() -> None:
    helper = (ROOT / "deploy" / "cubesandbox" / "prepare-semantic-source.sh").read_text(
        encoding="utf-8"
    )
    patch = (ROOT / "deploy" / "cubesandbox" / "semantic-tcp-endpoint.patch").read_text(
        encoding="utf-8"
    )

    assert "CUBE_SOURCE_TAG=${CUBE_SOURCE_TAG:-v0.7.0}" in helper
    assert "apply --check" in helper
    assert "refusing to overwrite" in helper
    assert "/sandboxes/{sandboxID}/ports/{containerPort}" in patch
    assert "SandboxTcpEndpoint" in patch
    assert "get_tcp_endpoint" in patch


def test_setup_docs_reject_pod_ip_native_ssh_and_link_from_readme() -> None:
    guide = (ROOT / "docs" / "cubesandbox-setup.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Pod IP" in guide
    assert "--count 1" in guide
    assert "--count 4" in guide
    assert "--count 8" in guide
    assert "docs/cubesandbox-setup.md" in readme
    assert "get_host(2222)" in guide
