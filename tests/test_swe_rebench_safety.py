from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tarfile
import threading
from types import SimpleNamespace

import docker
import pytest

from clawbox.images import swerebench as sw


def fake_registry(monkeypatch: pytest.MonkeyPatch, extra: tarfile.TarInfo | None = None) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in {
            "./testbed/.git/HEAD": b"fake head",
            "./testbed/source.py": b"print('original checkout')\n",
        }.items():
            item = tarfile.TarInfo(name)
            item.size = len(content)
            archive.addfile(item, io.BytesIO(content))
        if extra is not None:
            archive.addfile(extra, io.BytesIO(b"x" * extra.size))
    payload = stream.getvalue()
    resolved = {
        "repository": "example/repo", "manifest_digest": "sha256:" + "a" * 64,
        "token": "dummy", "layers": [{
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(), "size": len(payload),
        }],
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload

    monkeypatch.setattr(sw, "resolve_original_image", lambda *args: resolved)
    monkeypatch.setattr(sw, "_registry_session", lambda *args: SimpleNamespace(
        get=lambda *args, **kwargs: Response(),
    ))
    monkeypatch.setattr(sw.shutil, "disk_usage", lambda *args: SimpleNamespace(free=10**12))
    monkeypatch.setattr(sw.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="b" * 40))


@pytest.mark.parametrize("kind", ["traversal", "symlink", "absolute-symlink", "hardlink", "device"])
def test_archive_rejects_unsafe_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    member = tarfile.TarInfo("testbed/unsafe")
    if kind == "traversal":
        member.name = "testbed/../../../escaped.txt"
        member.size = 1
    elif kind in {"symlink", "absolute-symlink", "hardlink"}:
        member.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
        member.linkname = "/outside" if kind == "absolute-symlink" else "../../escaped.txt"
    else:
        member.type = tarfile.CHRTYPE
    fake_registry(monkeypatch, member)

    with pytest.raises(tarfile.FilterError):
        sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1)

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "task").exists()


def test_extraction_preserves_checkout_and_internal_hardlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    member = tarfile.TarInfo("testbed/source-link.py")
    member.type = tarfile.LNKTYPE
    member.linkname = "testbed/source.py"
    fake_registry(monkeypatch, member)

    workspace, receipt = sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1)

    assert (workspace / "source-link.py").read_bytes() == (workspace / "source.py").read_bytes()
    assert receipt["git_head"] == "b" * 40
    assert (workspace.parent / "receipt.json").is_file()
    assert not (workspace.parent / "workspace-layer.tgz.part").exists()
    assert not (workspace.parent / "unpack").exists()
    assert sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1) == (workspace, receipt)


@pytest.mark.parametrize("newer_config", [None, b"[user]\nname = Later\n", "deleted"])
def test_source_git_config_from_earlier_layer_respects_replacement_and_whiteout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newer_config,
) -> None:
    original = b"[user]\nname = Recorded\nemail = recorded@example.test\n[safe]\ndirectory = /testbed\n"
    layers = []
    for files in [
        {"root/.gitconfig": original},
        {"testbed/.git/HEAD": b"head", "testbed/source.py": b"original\n",
         **({"root/.wh..gitconfig": b""} if newer_config == "deleted" else
            {"root/.gitconfig": newer_config} if newer_config is not None else {})},
    ]:
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, content in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        payload = stream.getvalue()
        layers.append(("sha256:" + hashlib.sha256(payload).hexdigest(), payload))

    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, size): yield self.payload

    resolved = {"repository": "example/repo", "manifest_digest": "sha256:" + "a" * 64,
                "token": "dummy", "layers": [{"digest": digest, "size": len(payload)}
                                               for digest, payload in layers]}
    session = SimpleNamespace(get=lambda url, **kwargs: Response(dict(layers)[url.rsplit("/", 1)[1]]))
    monkeypatch.setattr(sw.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="b" * 40))
    workspace, receipt = sw._extract_testbed_layer("example/repo", resolved, session, tmp_path)
    expected = b"" if newer_config == "deleted" else newer_config if newer_config is not None else original
    assert (workspace.parent / "source.gitconfig").read_bytes() == expected
    assert receipt["workspace_layer_digest"] == layers[-1][0]
    assert receipt["git_config_sha256"] == hashlib.sha256(expected).hexdigest()
    assert (workspace / "source.py").read_bytes() == b"original\n"


def test_workspace_build_installs_exact_recorded_git_configuration(tmp_path: Path) -> None:
    workspace = tmp_path / "testbed"
    workspace.mkdir()
    original = b"[user]\nname = Recorded\nemail = recorded@example.test\n"
    (tmp_path / "source.gitconfig").write_bytes(original)

    class Images:
        def build(self, **kwargs):
            context = Path(kwargs["path"])
            assert (context / "source.gitconfig").read_bytes() == original
            assert "COPY source.gitconfig /root/.gitconfig" in (context / "Dockerfile").read_text()
            return object(), []

    spec = SimpleNamespace(install_config={}, env_image_key="env", instance_image_key="task")
    assert sw.build_workspace_image(spec, workspace, SimpleNamespace(images=Images())) == "task"


@pytest.mark.parametrize("phase", ["unpack", "git-check"])
def test_retry_recovers_from_failed_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    fake_registry(monkeypatch)
    with monkeypatch.context() as patch:
        def fail_extract(*args, **kwargs):
            raise OSError("interrupted extraction")
        if phase == "unpack":
            patch.setattr(tarfile.TarFile, "extractall", fail_extract)
        else:
            patch.setattr(sw.subprocess, "run", fail_extract)
        with pytest.raises(OSError, match="interrupted extraction"):
            sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1)
    assert not (tmp_path / "task").exists()

    workspace, _ = sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1)
    assert (workspace / "source.py").is_file()


@pytest.mark.parametrize("state", ["unpack", "checkout", "partial-receipt"])
def test_retry_discards_state_left_by_killed_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    fake_registry(monkeypatch)
    task = tmp_path / "task"
    stale = task / ("unpack" if state == "unpack" else "testbed")
    stale.mkdir(parents=True)
    (stale / "stale.txt").write_text("partial checkout")
    if state == "partial-receipt":
        (task / "receipt.json").write_text('{"manifest_digest":')

    workspace, _ = sw.extract_original_testbed("example/repo", "task", tmp_path, proxy="", min_free_gib=1)
    assert (workspace / "source.py").is_file()
    assert not (workspace / "stale.txt").exists()


def test_changed_common_base_invalidates_cached_environment(tmp_path: Path) -> None:
    built = []
    cache = set()

    class Images:
        def build(self, **kwargs):
            return SimpleNamespace(attrs={"Architecture": "arm64"}), []

    def make_spec(record, namespace, base_image_tag, env_image_tag, instance_image_tag, arch):
        # The pinned harness keys environments by script hash and env_image_tag.
        return SimpleNamespace(arch=arch, base_image_key=f"base:{base_image_tag}",
                               env_image_key=f"same-script-hash:{env_image_tag}",
                               instance_image_key=f"task:{instance_image_tag}")

    def build_env(client, specs, force_rebuild=False, max_workers=1):
        for spec in specs:
            if spec.env_image_key not in cache:
                cache.add(spec.env_image_key)
                built.append(spec.env_image_key)
        return [], []

    client = SimpleNamespace(images=Images())
    recipe = tmp_path / "Dockerfile"

    def prepare():
        return sw.prepare_common_base_images(
            {"task": {}}, {"task": {}}, make_test_spec=make_spec,
            build_base_images=lambda *args, **kwargs: None, client=client,
            common_base_dockerfile=recipe,
        )["task"]

    recipe.write_text("FROM base:v1\nRUN echo old\n")
    before = prepare()
    sw.harness_build(build_env, before, client)
    sw.harness_build(build_env, prepare(), client)
    assert len(built) == 1
    recipe.write_text("FROM base:v1\nRUN echo new\n")
    after = prepare()
    sw.harness_build(build_env, after, client)
    assert len(built) == 2
    assert before.base_image_key != after.base_image_key
    assert before.instance_image_key != after.instance_image_key


def test_parallel_tasks_retain_shared_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    specs = {name: SimpleNamespace(instance_id=name, env_image_key="shared-env:v2", instance_image_key=name)
             for name in ("fast", "slow")}
    slow_ready = threading.Event()
    fast_cleaned = threading.Event()
    removed = []

    class Images:
        def get(self, name):
            return SimpleNamespace(attrs={"Architecture": "arm64"})

        def remove(self, image, force=False):
            removed.append(image)
            if image == "fast":
                fast_cleaned.set()

    monkeypatch.setattr(docker, "from_env", lambda: SimpleNamespace(images=Images()))
    monkeypatch.setattr(sw, "prepare_common_base_images", lambda *args, **kwargs: specs)
    monkeypatch.setattr(sw, "resolve_original_image", lambda *args: {"manifest_digest": "sha256:" + "a" * 64})

    def extract(image, task_id, root, **kwargs):
        workspace = root / task_id / "testbed"
        workspace.mkdir(parents=True)
        return workspace, {"manifest_digest": "sha256:" + "a" * 64,
                           "workspace_layer_digest": "sha256:" + "b" * 64, "git_head": "c" * 40}

    def build_workspace(spec, workspace, client):
        if spec.instance_id == "slow":
            slow_ready.set()
            assert fast_cleaned.wait(5)
        else:
            assert slow_ready.wait(5)
        return spec.instance_image_key

    monkeypatch.setattr(sw, "extract_original_testbed", extract)
    monkeypatch.setattr(sw, "harness_build", lambda *args: None)
    monkeypatch.setattr(sw, "build_workspace_image", build_workspace)
    monkeypatch.setattr(sw, "normalize_harness_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(sw, "verify_harness_image", lambda *args: None)
    monkeypatch.setattr(sw, "push_immutable", lambda image: image + "@sha256:" + "d" * 64)
    artifact = tmp_path / "artifact"
    artifact.write_text("test")
    result = sw.build_selected(
        [{"instance_id": name} for name in specs],
        {name: {"original_image": name} for name in specs},
        make_test_spec=None, build_base_images=None, build_env_images=None,
        registry="registry", bridge=artifact, mapping_path=tmp_path / "mapping.json", push=True,
        dataset_revision="dataset", harness_revision="harness", fail_fast=False,
        cube_tool_base_image="registry/tool@sha256:" + "a" * 64,
        cube_tool_entrypoint=artifact, common_base_dockerfile=artifact,
        workspace_root=tmp_path / "workspaces", registry_proxy="", min_free_gib=1,
        workers=2, keep_workspaces=False,
    )
    assert all(item["status"] == "supported" for item in result.values()), result
    assert "fast" in removed and "slow" in removed
    assert "shared-env:v2" not in removed
