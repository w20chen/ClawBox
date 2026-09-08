from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kubernetes_install_and_launch_commands_are_retired() -> None:
    assert not (ROOT / "scripts" / "install-cubesandbox-kunpeng920.sh").exists()
    assert not (ROOT / "scripts" / "recover-cubesandbox-s3lvol-kunpeng920.sh").exists()
    project = (ROOT / "pyproject.toml").read_text()
    assert "clawbox-cell-controller =" not in project
    assert "clawbox-managed-dispatcher =" not in project


def test_template_helper_exposes_cube_command_ports() -> None:
    helper = (ROOT / "scripts" / "register-cube-template.py").read_text(
        encoding="utf-8"
    )

    assert 'action="append"' in helper
    assert "args.exposed_port or [49983]" in helper
    assert "default=49983" in helper
    assert '"--writable-layer-size", default="20G"' in helper
    assert "writable_layer_size=args.writable_layer_size" in helper


def test_semantic_source_prepare_is_pinned_and_non_destructive() -> None:
    helper = (ROOT / "deploy" / "cubesandbox" / "prepare-semantic-source.sh").read_text(
        encoding="utf-8"
    )
    patch = (ROOT / "deploy" / "cubesandbox" / "semantic-tcp-endpoint.patch").read_text(
        encoding="utf-8"
    )
    hairpin = (ROOT / "deploy" / "cubesandbox" / "hostport-hairpin.patch").read_text(
        encoding="utf-8"
    )
    lifecycle_timing = (
        ROOT / "deploy" / "cubesandbox" / "tiered-memory-api.patch"
    ).read_text(encoding="utf-8")

    assert "CUBE_SOURCE_TAG=${CUBE_SOURCE_TAG:-v0.7.0}" in helper
    assert "apply --check" in helper
    assert "refusing to overwrite" in helper
    assert "/sandboxes/{sandboxID}/ports/{containerPort}" in patch
    assert "SandboxTcpEndpoint" in patch
    assert "get_tcp_endpoint" in patch
    assert "HAIRPIN_PATCH_FILE" in helper
    assert "tcp_hairpin_proxy" in hairpin
    assert "remote_port_mapping" in hairpin
    assert "mvmip_to_ifindex" in hairpin
    assert "TIER_API_PATCH_FILE" in helper
    assert '"phase":       "snapshot_create"' in lifecycle_timing
    assert '"phase":       "swap_out"' in lifecycle_timing
    assert '"duration_ms"' in lifecycle_timing


def test_setup_docs_reject_pod_ip_native_ssh_and_link_from_readme() -> None:
    guide = (ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "semantic TCP endpoint" in guide
    assert "--count 1" in guide
    assert "--count 4" in guide
    assert "--count 8" not in guide
    assert "docs/installation.md" in readme
    assert "standalone CubeSandbox" in guide
