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
    control_timeouts = (
        ROOT / "deploy" / "cubesandbox" / "sdk-control-timeouts.patch"
    ).read_text(encoding="utf-8")
    create_timeout = (
        ROOT / "deploy" / "cubesandbox" / "cube-api-create-timeout.patch"
    ).read_text(encoding="utf-8")
    active_irq = (
        ROOT / "deploy" / "cubesandbox" / "irq-routing-active-legacy.patch"
    ).read_text(encoding="utf-8")
    envd_retry = (
        ROOT / "deploy" / "cubesandbox" / "envd-init-retry.patch"
    ).read_text(encoding="utf-8")
    cgroup_reclaim = (
        ROOT / "deploy" / "cubesandbox" / "cgroup-reuse-reclaim.patch"
    ).read_text(encoding="utf-8")
    rustup_url = (
        ROOT / "deploy" / "cubesandbox" / "builder-rustup-url.patch"
    ).read_text(encoding="utf-8")

    assert "CUBE_SOURCE_TAG=${CUBE_SOURCE_TAG:-v0.7.0}" in helper
    assert "apply --check" in helper
    assert "refusing to overwrite" in helper
    assert ".clawbox-patchset.sha256" in helper
    assert "different or unrecorded ClawBox patch set" in helper
    assert "pointing to a new empty directory" in helper
    assert "/sandboxes/{sandboxID}/ports/{containerPort}" in patch
    assert "SandboxTcpEndpoint" in patch
    assert "get_tcp_endpoint" in patch
    assert "HAIRPIN_PATCH_FILE" in helper
    assert "tcp_hairpin_proxy" in hairpin
    assert "remote_port_mapping" in hairpin
    assert "mvmip_to_ifindex" in hairpin
    assert "TIER_API_PATCH_FILE" in helper
    assert "CONTROL_TIMEOUT_PATCH_FILE" in helper
    assert "timeout=cfg.request_timeout" in control_timeouts
    assert "CREATE_TIMEOUT_PATCH_FILE" in helper
    assert 'apply_once "$CREATE_TIMEOUT_PATCH_FILE"' in helper
    assert '.route("/sandboxes", post(sandboxes::create_sandbox))' in create_timeout
    assert "Create / Pause / Resume / Connect use the 120s lifecycle budget" in create_timeout
    lifecycle_timeouts = (ROOT / "deploy/cubesandbox/sdk-lifecycle-timeouts.patch").read_text()
    assert "timeout=cfg.request_timeout" in lifecycle_timeouts
    assert "timeout=self._config.request_timeout" in lifecycle_timeouts
    assert 'apply_once "$LIFECYCLE_TIMEOUT_PATCH_FILE"' in helper
    assert "IRQ_ACTIVE_PATCH_FILE" in helper
    assert 'apply_once "$IRQ_ACTIVE_PATCH_FILE"' in helper
    assert "enable_requested" in active_irq
    assert "self.requested.store(true" in active_irq
    assert "ENVD_INIT_RETRY_PATCH_FILE" in helper
    assert 'apply_once "$ENVD_INIT_RETRY_PATCH_FILE"' in helper
    assert "envdInitAttemptTimeout" in envd_retry
    assert "envdInitMaxAttempts" in envd_retry
    assert "CGROUP_RECLAIM_PATCH_FILE" in helper
    assert 'apply_once "$CGROUP_RECLAIM_PATCH_FILE"' in helper
    assert '"memory.reclaim"' in cgroup_reclaim
    assert "fileBytes - shmemBytes" in cgroup_reclaim
    assert "syscall.Write" in cgroup_reclaim
    assert "syscall.EAGAIN" in cgroup_reclaim
    assert "os.WriteFile(path.Join(groupPath, \"memory.reclaim\")" not in cgroup_reclaim
    assert "RUSTUP_URL_PATCH_FILE" in helper
    assert 'apply_once "$RUSTUP_URL_PATCH_FILE"' in helper
    assert "RUSTUP_INIT_URL" in rustup_url
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
