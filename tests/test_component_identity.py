from pathlib import Path

from clawbox.experiments.component_identity import lifecycle_component_identity


def _write_executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def test_lifecycle_component_identity_accepts_identical_builds(tmp_path: Path) -> None:
    service = tmp_path / "service"
    versioned = tmp_path / "versioned"
    component = versioned / "pinned" / "bin"
    for name, data in (
        (
            "containerd-shim-cube-rs",
            b"same-shim memory.restore_lineage_load memory.lineage_parent_rejected",
        ),
        ("cube-runtime", b"same-runtime"),
    ):
        _write_executable(service / name, data)
        _write_executable(component / name, data)

    result = lifecycle_component_identity(
        service_bin_dir=service,
        versioned_root=versioned,
        component_version="pinned",
    )

    assert result["ok"] is True
    assert result["errors"] == []
    assert all(record["match"] for record in result["files"].values())


def test_lifecycle_component_identity_rejects_shim_split_build(
    tmp_path: Path,
) -> None:
    service = tmp_path / "service"
    versioned = tmp_path / "versioned"
    component = versioned / "pinned" / "bin"
    _write_executable(
        service / "containerd-shim-cube-rs",
        b"lineage-shim memory.restore_lineage_load memory.lineage_parent_rejected",
    )
    _write_executable(component / "containerd-shim-cube-rs", b"legacy-shim")
    _write_executable(service / "cube-runtime", b"same-runtime")
    _write_executable(component / "cube-runtime", b"same-runtime")

    result = lifecycle_component_identity(
        service_bin_dir=service,
        versioned_root=versioned,
        component_version="pinned",
    )

    assert result["ok"] is False
    assert any("containerd-shim-cube-rs build mismatch" in error
               for error in result["errors"])
    assert not result["files"]["containerd-shim-cube-rs"]["match"]
    assert result["files"]["cube-runtime"]["match"]


def test_lifecycle_component_identity_rejects_missing_component(
    tmp_path: Path,
) -> None:
    service = tmp_path / "service"
    _write_executable(
        service / "containerd-shim-cube-rs",
        b"shim memory.restore_lineage_load memory.lineage_parent_rejected",
    )
    _write_executable(service / "cube-runtime", b"runtime")

    result = lifecycle_component_identity(
        service_bin_dir=service,
        versioned_root=tmp_path / "versioned",
        component_version="missing",
    )

    assert result["ok"] is False
    assert any("component containerd-shim-cube-rs unavailable" in error
               for error in result["errors"])


def test_lifecycle_component_identity_rejects_matching_legacy_shim(
    tmp_path: Path,
) -> None:
    service = tmp_path / "service"
    versioned = tmp_path / "versioned"
    component = versioned / "pinned" / "bin"
    for directory in (service, component):
        _write_executable(
            directory / "containerd-shim-cube-rs",
            b"legacy-shim-without-restored-parent-support",
        )
        _write_executable(directory / "cube-runtime", b"same-runtime")

    result = lifecycle_component_identity(
        service_bin_dir=service,
        versioned_root=versioned,
        component_version="pinned",
    )

    assert result["ok"] is False
    assert result["files"]["containerd-shim-cube-rs"]["match"] is True
    assert result["files"]["containerd-shim-cube-rs"][
        "service_lineage_capable"
    ] is False
    assert any("lacks incremental lineage restore markers" in error
               for error in result["errors"])


def test_lifecycle_component_identity_rejects_path_traversal(
    tmp_path: Path,
) -> None:
    result = lifecycle_component_identity(
        service_bin_dir=tmp_path / "service",
        versioned_root=tmp_path / "versioned",
        component_version="../outside",
    )

    assert result["ok"] is False
    assert result["errors"] == ["invalid component version: '../outside'"]
