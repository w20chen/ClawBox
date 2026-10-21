from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from clawbox.images import swerebench
from clawbox.images.swerebench import (
    _docker_hub_reference,
    portable_requirements,
    prepare_common_base_images,
    records_from_file,
)


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
    assert "--cube-tool-base-image" in result.stdout
    assert "--common-base-dockerfile" in result.stdout
    assert "--workers" in result.stdout
    assert "--workspace-root" in result.stdout


def test_records_from_parquet_ignores_huggingface_feature_metadata(tmp_path: Path) -> None:
    schema = pa.schema(
        [("instance_id", pa.string()), ("docker_image", pa.string())],
        metadata={
            b"huggingface": json.dumps(
                {"info": {"features": {"tags": {"_type": "List"}}}}
            ).encode("utf-8")
        },
    )
    table = pa.Table.from_pylist(
        [{"instance_id": "owner__repo-1", "docker_image": "example/image:tag"}],
        schema=schema,
    )
    path = tmp_path / "tasks.parquet"
    pq.write_table(table, path)

    assert records_from_file(path) == [
        {"instance_id": "owner__repo-1", "docker_image": "example/image:tag"}
    ]


def test_portable_requirements_removes_x86_cache_and_repo_self_install() -> None:
    instance = {
        "repo": "owner/project",
        "requirements": (
            "certifi @ file:///croot/certifi/work/certifi\n"
            "-e git+https://github.com/owner/project.git@abc#egg=project\n"
            "numpy==1.26.4\n"
            "rasterio==1.4.3\n"
        ),
    }

    assert portable_requirements(instance) == (
        "certifi\nnumpy==1.26.4\nrasterio==1.3.11\n"
    )


def test_docker_hub_reference_defaults_to_latest() -> None:
    assert _docker_hub_reference("swerebench/example") == ("swerebench/example", "latest")
    assert _docker_hub_reference("docker.io/sw/repo:v2") == ("sw/repo", "v2")


def test_prepare_common_base_builds_stock_once_and_layers_native_dependencies(
    tmp_path: Path,
) -> None:
    base_calls: list[list[str]] = []
    image_calls: list[dict[str, object]] = []

    def make_spec(
        record: dict[str, str], *, namespace: str, base_image_tag: str,
        env_image_tag: str, instance_image_tag: str, arch: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            arch=arch, base_image_key=f"sweb.base.py.arm64:{base_image_tag}",
            instance_id=record["instance_id"],
        )

    def build_base_images(client: object, specs: list[SimpleNamespace], **options: object) -> None:
        base_calls.append([spec.base_image_key for spec in specs])

    class Images:
        def build(self, **options: object) -> tuple[SimpleNamespace, list[dict[str, str]]]:
            image_calls.append(options)
            return SimpleNamespace(attrs={"Architecture": "arm64"}), []

    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n", encoding="utf-8")
    records = {
        "task-1": {"instance_id": "task-1"},
        "task-2": {"instance_id": "task-2"},
    }

    specs = prepare_common_base_images(
        records, {"task-1": {}, "task-2": {}}, make_test_spec=make_spec,
        build_base_images=build_base_images,
        client=SimpleNamespace(images=Images()), common_base_dockerfile=dockerfile,
    )

    assert set(specs) == {"task-1", "task-2"}
    assert set(base_calls[0]) == {"sweb.base.py.arm64:v1"}
    assert len(image_calls) == 1
    assert image_calls[0]["tag"] == "sweb.base.py.arm64:v2"
    assert image_calls[0]["buildargs"] == {"BASE_IMAGE": "sweb.base.py.arm64:v1"}


def test_fail_fast_persists_current_failure_before_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapping = tmp_path / "mapping.json"
    bridge = tmp_path / "tool-bridge"
    entrypoint = tmp_path / "entrypoint.sh"
    common_base = tmp_path / "Dockerfile"
    for path in (bridge, entrypoint, common_base):
        path.write_text("content\n", encoding="utf-8")
    spec = SimpleNamespace(arch="arm64")
    monkeypatch.setattr(
        swerebench, "prepare_common_base_images", lambda *args, **kwargs: {"task-1": spec}
    )
    monkeypatch.setattr(
        swerebench, "resolve_original_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("current failure")),
    )
    import docker
    monkeypatch.setattr(docker, "from_env", lambda: object())

    with pytest.raises(RuntimeError, match="current failure"):
        swerebench.build_selected(
            [{"instance_id": "task-1"}],
            {"task-1": {"original_image": "swerebench/example"}},
            make_test_spec=lambda record: spec, build_base_images=lambda *args: None,
            build_env_images=lambda *args: ([], []), registry="registry/clawbox",
            bridge=bridge, mapping_path=mapping, push=True,
            dataset_revision="dataset", harness_revision="harness", fail_fast=True,
            cube_tool_base_image="registry/tool@sha256:" + "a" * 64,
            cube_tool_entrypoint=entrypoint, common_base_dockerfile=common_base,
            workspace_root=tmp_path / "workspaces", registry_proxy="",
            min_free_gib=1, workers=1, keep_workspaces=False,
        )

    saved = json.loads(mapping.read_text(encoding="utf-8"))
    assert saved["swerebench/example"]["status"] == "unsupported-arm64"
    assert "current failure" in saved["swerebench/example"]["error"]
