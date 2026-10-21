from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from clawbox.images.arm64 import (
    CONTRACT_VERSION,
    load_mapping,
    push_immutable,
    require_native_arm64,
    safe_tag,
    write_mapping,
)


DEFAULT_DATASET_REVISION = "4ece23ba02fe8b68858e430134adddfd64d6f0f4"
DEFAULT_HARNESS_REVISION = "980d0cca8aa4e73f1d9f894e906370bef8c4de8a"
PORTABLE_ENV_VERSION = "swe-rebench-original-testbed-v5-safe-extraction-base-cache"
STOCK_BASE_IMAGE_TAG = "v1"
COMMON_BASE_IMAGE_TAG = "v2"


def portable_requirements(instance: dict[str, Any]) -> str:
    """Translate the recorded x86 environment freeze into pip-portable input."""
    raw = str(instance.get("requirements") or "")
    repository = str(instance.get("repo") or "").lower().removesuffix(".git")
    result: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if re.fullmatch(r"(?i)rasterio\s*==\s*1\.4\.3", stripped):
            # PyPI has no linux/aarch64 wheel for 1.4.3, whose source requires
            # GDAL >= 3.5.  The Ubuntu ARM64 base provides GDAL 3.4, supported
            # by the nearest 1.3 maintenance release.
            line = "rasterio==1.3.11"
        if lowered.startswith("-e git+") and repository and (
            f"github.com/{repository}.git" in lowered
            or f"github.com/{repository}#" in lowered
        ):
            # The instance layer installs this exact checkout after cloning it.
            continue
        if " @ file://" in line:
            # Conda exports host-local build-cache paths.  Keep the package
            # identity and let pip select the native ARM64 distribution.
            line = line.split(" @ file://", 1)[0].strip()
        result.append(line)
    return "\n".join(result).strip() + "\n"


def records_from_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:
            raise RuntimeError("reading Parquet requires `pip install pyarrow`") from exc
        return [
            dict(item)
            for batch in parquet.ParquetFile(path).iter_batches(batch_size=1024)
            for item in batch.to_pylist()
        ]
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
        values = raw if isinstance(raw, list) else raw.get("tasks", raw.get("instances", raw.get("data", [])))
        return [item for item in values if isinstance(item, dict)]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def fetch_dataset(dataset_id: str, revision: str) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("fetching Hugging Face data requires `pip install datasets`") from exc
    dataset = load_dataset(dataset_id, split="test", revision=revision)
    return [dict(item) for item in dataset]


def selected_tasks(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for item in records_from_file(path):
        task_id = str(item.get("instance_id") or item.get("task_id") or "")
        image = str(item.get("docker_image") or item.get("image") or item.get("image_name") or "")
        if not task_id or not image:
            raise ValueError("selection entries require instance_id and original image")
        result[task_id] = {"original_image": image}
    return result


def load_harness(root: Path, revision: str) -> tuple[Any, Any, Any, str]:
    actual = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    if actual != revision:
        raise RuntimeError(f"SWE-bench fork must be pinned to {revision}; found {actual}")
    sys.path.insert(0, str(root))
    try:
        test_spec_module = importlib.import_module("swebench.harness.test_spec.test_spec")
        build_module = importlib.import_module("swebench.harness.docker_build")
        python_spec_module = importlib.import_module("swebench.harness.test_spec.python")
        create_scripts_module = importlib.import_module("swebench.harness.test_spec.create_scripts")
    except ModuleNotFoundError as exc:
        requirements = root / "requirements.txt"
        hint = f"python3 -m pip install -r '{requirements}'" if requirements.is_file() else \
            "python3 -m pip install ghapi docker datasets"
        raise RuntimeError(
            "SWE-bench fork dependencies are missing from this Python environment "
            f"(missing module: {exc.name}). Install them first, e.g.:\n"
            f"  {hint}"
        ) from exc
    original_get_requirements = python_spec_module.get_requirements
    original_make_env_script_list = python_spec_module.make_env_script_list_py

    def _dataset_requirements(instance: dict[str, Any]) -> str:
        if instance.get("requirements"):
            return portable_requirements(instance)
        return original_get_requirements(instance)

    def _dataset_env_script(instance: dict[str, Any], specs: dict[str, Any], env_name: str) -> list[str]:
        portable_specs = dict(specs)
        if instance.get("requirements") and portable_specs.get("packages") in {
            "requirements.txt", "environment.yml", "Pipfile",
        }:
            portable_specs["packages"] = "requirements.txt"
        return original_make_env_script_list(instance, portable_specs, env_name)

    python_spec_module.get_requirements = _dataset_requirements
    python_spec_module.make_env_script_list_py = _dataset_env_script
    create_scripts_module.make_env_script_list_py = _dataset_env_script
    return (
        test_spec_module.make_test_spec,
        getattr(build_module, "build_base_images", None),
        getattr(build_module, "build_env_images", None),
        actual,
    )


def recipe_revision(
    record: dict[str, Any], dataset_revision: str, harness_revision: str,
    bridge: Path, cube_tool_base_image: str, cube_tool_entrypoint: Path,
    common_base_dockerfile: Path, original_manifest_digest: str,
    workspace_layer_digest: str,
) -> str:
    relevant = {
        key: record.get(key) for key in (
            "instance_id", "repo", "base_commit", "environment_setup_commit",
            "install_config", "requirements", "environment",
        )
    }
    digest = hashlib.sha256(
        json.dumps(relevant, sort_keys=True, default=str).encode()
        + b":" + PORTABLE_ENV_VERSION.encode()
        + b":" + hashlib.sha256(bridge.read_bytes()).hexdigest().encode()
        + b":" + cube_tool_base_image.encode()
        + b":" + hashlib.sha256(cube_tool_entrypoint.read_bytes()).hexdigest().encode()
        + b":" + hashlib.sha256(common_base_dockerfile.read_bytes()).hexdigest().encode()
        + b":" + original_manifest_digest.encode()
        + b":" + workspace_layer_digest.encode()
    ).hexdigest()
    return (
        f"dataset:{dataset_revision}:harness:{harness_revision}:"
        f"contract:{CONTRACT_VERSION}:recipe:{digest}"
    )


def verify_harness_image(image: str, bridge: Path, test_command: str) -> None:
    test_command = test_command or "true"
    contract = " && ".join((
        "test -d /testbed", "test -w /testbed", "command -v sh", "command -v git", "command -v patch",
        "test -x /clawbox-contract/tool-bridge", "/clawbox-contract/tool-bridge --self-test",
        "test -x /usr/bin/envd", "test -x /usr/local/bin/cube-tool-entrypoint.sh",
        "cd /testbed",
        # A benchmark base may intentionally fail its fail-to-pass tests.  The
        # gate rejects only inability to launch the configured test command.
        f"set +e; timeout 30s /bin/sh -lc {json.dumps(test_command)} >/tmp/clawbox-test.log 2>&1; s=$?; "
        "set -e; test $s -ne 126 -a $s -ne 127",
    ))
    subprocess.run([
        "docker", "run", "--rm", "--network", "none", "--platform", "linux/arm64",
        "--user", "10001:10001",
        "--mount", f"type=bind,src={bridge.resolve()},dst=/clawbox-contract/tool-bridge,readonly",
        "--entrypoint", "/bin/sh", image, "-ec", contract,
    ], check=True, timeout=1800)


def normalize_harness_image(
    local_image: str, output: str, bridge: Path, *,
    cube_tool_base_image: str, cube_tool_entrypoint: Path,
) -> None:
    """Make the harness output compatible with the unprivileged Tool Pod.

    Kata on the target has no shared filesystem, so a Tool Pod cannot use an
    init container to copy the bridge into a shared emptyDir (cross-container
    volume sharing fails with agent ENOENT); bake the binary into the image.
    """
    with tempfile.TemporaryDirectory(prefix="clawbox-arm64-wrapper-") as directory:
        shutil.copy2(bridge, Path(directory) / "tool-bridge")
        shutil.copy2(cube_tool_entrypoint, Path(directory) / "cube-tool-entrypoint.sh")
        dockerfile = Path(directory) / "Dockerfile"
        dockerfile.write_text(
            "ARG CUBE_TOOL_BASE_IMAGE\n"
            "ARG BASE_IMAGE\n"
            "FROM ${CUBE_TOOL_BASE_IMAGE} AS cube_tool_base\n"
            "FROM ${BASE_IMAGE}\n"
            "USER 0\n"
            "RUN test -d /testbed && chown -R 10001:10001 /testbed\n"
            "COPY --from=cube_tool_base /usr/bin/envd /usr/bin/envd\n"
            "COPY tool-bridge /usr/local/bin/tool-bridge\n"
            "COPY cube-tool-entrypoint.sh /usr/local/bin/cube-tool-entrypoint.sh\n"
            "RUN chmod 0555 /usr/local/bin/tool-bridge "
            "/usr/local/bin/cube-tool-entrypoint.sh /usr/bin/envd "
            "&& mkdir -p /workspace /run/clawtune /run/clawbox-ssh "
            "/var/lib/clawtune/artifacts/tool-resource "
            "&& touch /var/log/envd.log /var/log/tool-bridge.log "
            "&& chown -R 10001:10001 /workspace /run/clawtune /run/clawbox-ssh "
            "/var/lib/clawtune /var/log/envd.log /var/log/tool-bridge.log\n"
            # SWE-ReBench testbeds run tests with the `testbed` conda env, but
            # the fork only puts conda's base bin on PATH (and auto-activates
            # testbed only in root's .bashrc). Expose the testbed env bin so the
            # unprivileged tool user can launch pytest; a missing directory on
            # PATH is harmless for tasks without a conda env.
            "ENV PATH=/opt/miniconda3/envs/testbed/bin:$PATH\n"
            "ENV CLAWBOX_VM_ROLE=tool\n"
            "USER 10001:10001\n"
            "WORKDIR /testbed\n"
            "ENTRYPOINT [\"/usr/local/bin/cube-tool-entrypoint.sh\"]\n",
            encoding="utf-8",
        )
        subprocess.run([
            "docker", "build", "--platform", "linux/arm64", "--pull=false",
            "--build-arg", f"BASE_IMAGE={local_image}", "--tag", output,
            "--build-arg", f"CUBE_TOOL_BASE_IMAGE={cube_tool_base_image}",
            "--file", str(dockerfile), directory,
        ], check=True, timeout=7200)


def harness_build(build_env_images: Any, spec: Any, client: Any) -> None:
    # Build only architecture-specific base/environment layers.  The prepared
    # repository is copied from the original SWE-ReBench image below.
    if build_env_images is None:
        raise RuntimeError(
            "SWE-bench fork lacks build_env_images; the environment image must "
            "be built before the instance image"
        )
    env_options = {
        key: value for key, value in {
            "force_rebuild": False,
            "max_workers": 1,
        }.items() if key in inspect.signature(build_env_images).parameters
    }
    _, env_failed = build_env_images(client, [spec], **env_options)
    if env_failed:
        raise RuntimeError(
            "SWE-bench harness failed to build environment images: "
            + ", ".join(str(item) for item in env_failed)
        )


def _make_spec(make_test_spec: Any, record: dict[str, Any], image_tag: str) -> Any:
    options = {
        key: value for key, value in {
            "namespace": "clawbox-arm64",
            "base_image_tag": image_tag,
            "env_image_tag": image_tag,
            "instance_image_tag": image_tag,
            "arch": "arm64",
        }.items() if key in inspect.signature(make_test_spec).parameters
    }
    spec = make_test_spec(record, **options)
    architecture = str(getattr(spec, "arch", "arm64"))
    if architecture not in {"arm64", "aarch64"}:
        raise RuntimeError(f"harness produced a foreign-architecture spec: {architecture}")
    return spec


def prepare_common_base_images(
    records: dict[str, dict[str, Any]], selection: dict[str, dict[str, str]], *,
    make_test_spec: Any, build_base_images: Any, client: Any,
    common_base_dockerfile: Path,
) -> dict[str, Any]:
    """Build stock ARM64 bases, then add native build packages once per base."""
    if build_base_images is None:
        raise RuntimeError(
            "SWE-bench fork lacks build_base_images; cannot prepare ARM64 base images"
        )
    stock_specs: list[Any] = []
    common_specs: dict[str, Any] = {}
    base_pairs: dict[str, str] = {}
    common_recipe = common_base_dockerfile.read_bytes()
    # The harness caches environments by script hash and tag, without checking
    # their base image. Version every derived tag with the common-base recipe.
    common_tag = f"{COMMON_BASE_IMAGE_TAG}-{hashlib.sha256(common_recipe).hexdigest()}"
    for task_id in selection:
        record = records[task_id]
        stock = _make_spec(make_test_spec, record, STOCK_BASE_IMAGE_TAG)
        common = _make_spec(make_test_spec, record, common_tag)
        stock_specs.append(stock)
        common_specs[task_id] = common
        base_pairs[str(common.base_image_key)] = str(stock.base_image_key)

    options = {
        key: value for key, value in {"force_rebuild": False}.items()
        if key in inspect.signature(build_base_images).parameters
    }
    build_base_images(client, stock_specs, **options)
    with tempfile.TemporaryDirectory(prefix="clawbox-swe-common-base-") as directory:
        context = Path(directory)
        (context / "Dockerfile").write_bytes(common_recipe)
        for common_image, stock_image in sorted(base_pairs.items()):
            image, logs = client.images.build(
                path=str(context), dockerfile="Dockerfile", tag=common_image,
                platform="linux/arm64", pull=False, rm=True, forcerm=True,
                buildargs={"BASE_IMAGE": stock_image},
            )
            for entry in logs:
                if "error" in entry:
                    raise RuntimeError(str(entry["error"]))
            architecture = image.attrs.get("Architecture")
            if architecture not in {"arm64", "aarch64"}:
                raise RuntimeError(
                    f"common base image {common_image} has foreign architecture: {architecture}"
                )
    return common_specs


def _docker_hub_reference(image: str) -> tuple[str, str]:
    value = image.removeprefix("docker://").removeprefix("docker.io/")
    if "@" in value:
        repository, reference = value.rsplit("@", 1)
    elif ":" in value.rsplit("/", 1)[-1]:
        repository, reference = value.rsplit(":", 1)
    else:
        repository, reference = value, "latest"
    if "/" not in repository:
        repository = f"library/{repository}"
    return repository, reference


def _registry_session(proxy: str) -> Any:
    import requests
    from urllib3.util.retry import Retry

    session = requests.Session()
    if proxy:
        session.trust_env = False
        session.proxies.update({"http": proxy, "https": proxy})
    retries = Retry(
        total=4, connect=4, read=4, status=4, backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=retries))
    return session


def resolve_original_image(image: str, proxy: str) -> dict[str, Any]:
    """Resolve an x86 Docker Hub image and return its immutable manifest/layers."""
    repository, reference = _docker_hub_reference(image)
    session = _registry_session(proxy)
    token_response = session.get(
        "https://auth.docker.io/token",
        params={"service": "registry.docker.io", "scope": f"repository:{repository}:pull"},
        timeout=60,
    )
    token_response.raise_for_status()
    headers = {
        "Authorization": f"Bearer {token_response.json()['token']}",
        "Accept": ", ".join((
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json",
        )),
    }
    url = f"https://registry-1.docker.io/v2/{repository}/manifests/{reference}"
    response = session.get(url, headers=headers, timeout=120)
    response.raise_for_status()
    manifest = response.json()
    if "manifests" in manifest:
        candidates = [
            item for item in manifest["manifests"]
            if item.get("platform", {}).get("architecture") == "amd64"
            and item.get("platform", {}).get("os") == "linux"
        ]
        if len(candidates) != 1:
            raise RuntimeError(f"{image} has {len(candidates)} linux/amd64 manifests")
        reference = candidates[0]["digest"]
        response = session.get(
            f"https://registry-1.docker.io/v2/{repository}/manifests/{reference}",
            headers=headers, timeout=120,
        )
        response.raise_for_status()
        manifest = response.json()
    layers = manifest.get("layers") or []
    if not layers:
        raise RuntimeError(f"{image} manifest has no layers")
    digest = response.headers.get("Docker-Content-Digest") or reference
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise RuntimeError(f"registry did not return an immutable digest for {image}")
    return {
        "repository": repository, "manifest_digest": digest,
        "layers": [{"digest": x["digest"], "size": int(x["size"])} for x in layers],
        "manifest": manifest,
        "token": headers["Authorization"],
    }


def _testbed_member_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo:
    # Do not normalize away traversal before checking it. The data filter also
    # rejects devices, absolute link targets, and links escaping the destination.
    if ".." in PurePosixPath(member.name).parts or "\\" in member.name:
        raise tarfile.FilterError(f"unsafe testbed path: {member.name}")
    filtered = tarfile.data_filter(member, destination)
    workspace = (Path(destination) / "testbed").resolve()
    target = Path(destination) / filtered.name
    if not target.resolve().is_relative_to(workspace):
        raise tarfile.FilterError(f"path escapes testbed: {member.name}")
    if filtered.issym() or filtered.islnk():
        link_base = target.parent if filtered.issym() else Path(destination)
        link = (link_base / filtered.linkname).resolve()
        if not link.is_relative_to(workspace):
            raise tarfile.FilterError(f"link escapes testbed: {member.name}")
    return filtered


def extract_original_testbed(
    image: str, task_id: str, root: Path, *, proxy: str, min_free_gib: int,
) -> tuple[Path, dict[str, Any]]:
    """Extract the prepared /testbed from the original x86 image task layer."""
    resolved = resolve_original_image(image, proxy)
    session = _registry_session(proxy)
    free = shutil.disk_usage(root).free
    largest_candidate = max(item["size"] for item in resolved["layers"][-3:])
    required = max(min_free_gib * 1024**3, largest_candidate * 3)
    if free < required:
        raise RuntimeError(
            f"only {free / 1024**3:.1f} GiB free; need at least {required / 1024**3:.1f} GiB"
        )
    task_root = root.resolve() / safe_tag(task_id)
    if task_root.resolve().parent != root.resolve():
        raise RuntimeError(f"workspace path escapes its root: {task_root}")
    complete = task_root / "receipt.json"
    workspace = task_root / "testbed"
    if complete.is_file() and workspace.is_dir():
        try:
            receipt = json.loads(complete.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            receipt = {}
        if receipt.get("manifest_digest") == resolved["manifest_digest"]:
            return workspace, receipt
    # A killed process may leave a partial unpack, checkout, or receipt. Only a
    # matching completed receipt above is reusable.
    if task_root.exists():
        shutil.rmtree(task_root)
    task_root.mkdir(parents=True)
    try:
        return _extract_testbed_layer(image, resolved, session, task_root)
    except BaseException:
        # Also clean failures before a workspace can be returned to the caller.
        shutil.rmtree(task_root, ignore_errors=True)
        raise


def _extract_testbed_layer(
    image: str, resolved: dict[str, Any], session: Any, task_root: Path,
) -> tuple[Path, dict[str, Any]]:
    workspace = task_root / "testbed"
    complete = task_root / "receipt.json"
    staging = task_root / "unpack"
    digest = ""
    layer_size = 0
    for layer in reversed(resolved["layers"]):
        blob = task_root / "workspace-layer.tgz.part"
        digest = layer["digest"]
        url = f"https://registry-1.docker.io/v2/{resolved['repository']}/blobs/{digest}"
        for attempt in range(1, 5):
            try:
                with session.get(
                    url, headers={"Authorization": resolved["token"]},
                    stream=True, timeout=(60, 600),
                ) as response:
                    response.raise_for_status()
                    hasher = hashlib.sha256()
                    with blob.open("wb") as output:
                        for chunk in response.iter_content(8 * 1024 * 1024):
                            if chunk:
                                output.write(chunk)
                                hasher.update(chunk)
                if f"sha256:{hasher.hexdigest()}" != digest:
                    raise RuntimeError(f"workspace layer checksum mismatch for {image}")
                break
            except Exception:
                blob.unlink(missing_ok=True)
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        with tarfile.open(blob, mode="r:*") as archive:
            members = []
            for member in archive.getmembers():
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if name == "testbed" or name.startswith("testbed/"):
                    member.name = name
                    members.append(member)
            has_checkout = any(member.name == "testbed/.git/HEAD" for member in members)
            if has_checkout:
                staging.mkdir()
                archive.extractall(staging, members=members, filter=_testbed_member_filter)
        blob.unlink()
        if has_checkout:
            layer_size = layer["size"]
            break
    if not digest or not (staging / "testbed" / ".git" / "HEAD").is_file():
        raise RuntimeError(f"no image layer in {image} contains a complete /testbed Git checkout")
    os.replace(staging / "testbed", workspace)
    shutil.rmtree(staging)
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
        text=True, stdout=subprocess.PIPE,
    ).stdout.strip()
    receipt_data = {
        "original_image": image, "manifest_digest": resolved["manifest_digest"],
        "workspace_layer_digest": digest, "workspace_layer_size": layer_size,
        "git_head": head,
    }
    complete.write_text(json.dumps(receipt_data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return workspace, receipt_data


def build_workspace_image(spec: Any, workspace: Path, client: Any) -> str:
    """Build the ARM64 task image from the original image's prepared checkout."""
    install = spec.install_config or {}
    commands = ["source /opt/miniconda3/bin/activate", "conda activate testbed"]
    commands.extend(install.get("pre_install") or [])
    if install.get("install"):
        commands.append(str(install["install"]))
    script = "set -euxo pipefail\ncd /testbed\n" + "\n".join(commands) + "\n"
    with tempfile.TemporaryDirectory(prefix="clawbox-swe-workspace-") as directory:
        context = Path(directory)
        shutil.copytree(workspace, context / "testbed", symlinks=True)
        (context / "install.sh").write_text(script, encoding="utf-8")
        (context / "Dockerfile").write_text(
            f"FROM {spec.env_image_key}\n"
            "COPY testbed /testbed\n"
            "COPY install.sh /root/install-authoritative-testbed.sh\n"
            "RUN /bin/bash /root/install-authoritative-testbed.sh && "
            "rm /root/install-authoritative-testbed.sh\n"
            "WORKDIR /testbed\n",
            encoding="utf-8",
        )
        image, logs = client.images.build(
            path=str(context), tag=str(spec.instance_image_key), platform="linux/arm64",
            rm=True, forcerm=True,
        )
        for entry in logs:
            if "error" in entry:
                raise RuntimeError(str(entry["error"]))
    return str(spec.instance_image_key)


def build_selected(
    records: list[dict[str, Any]], selection: dict[str, dict[str, str]], *,
    make_test_spec: Any, build_base_images: Any, build_env_images: Any,
    registry: str, bridge: Path, mapping_path: Path, push: bool,
    dataset_revision: str, harness_revision: str, fail_fast: bool,
    cube_tool_base_image: str, cube_tool_entrypoint: Path,
    common_base_dockerfile: Path,
    workspace_root: Path, registry_proxy: str, min_free_gib: int,
    workers: int, keep_workspaces: bool,
) -> dict[str, Any]:
    import docker

    by_id = {str(item.get("instance_id")): item for item in records}
    missing = sorted(set(selection) - set(by_id))
    if missing:
        raise RuntimeError(f"selected tasks are absent from the full dataset: {', '.join(missing)}")
    mapping = load_mapping(mapping_path)
    logging.basicConfig(level=logging.INFO)
    workspace_root.mkdir(parents=True, exist_ok=True)
    mapping_lock = threading.Lock()
    specs = prepare_common_base_images(
        by_id, selection, make_test_spec=make_test_spec,
        build_base_images=build_base_images, client=docker.from_env(),
        common_base_dockerfile=common_base_dockerfile,
    )
    environment_locks: dict[str, Any] = {}

    def build_one(task_id: str, selected: dict[str, str]) -> None:
        record = by_id[task_id]
        original = selected["original_image"]
        revision = "unresolved"
        workspace: Path | None = None
        cleanup_images: list[str] = []
        client: Any = None
        failure: Exception | None = None
        try:
            resolved = resolve_original_image(original, registry_proxy)
            with mapping_lock:
                existing = mapping.get(original)
            if existing and existing.get("status") == "supported" and \
                    existing.get("original_manifest_digest") == resolved["manifest_digest"]:
                revision = recipe_revision(
                    record, dataset_revision, harness_revision, bridge,
                    cube_tool_base_image, cube_tool_entrypoint,
                    common_base_dockerfile, resolved["manifest_digest"],
                    str(existing.get("workspace_layer_digest")),
                )
                if existing.get("recipe_revision") == revision:
                    if existing.get("platform") != "linux/arm64" or not re.fullmatch(
                        r".+@sha256:[a-f0-9]{64}", str(existing.get("arm64_image") or ""),
                    ):
                        raise RuntimeError(
                            f"supported mapping for {original} lacks arm64 digest provenance"
                        )
                    return
            workspace, provenance = extract_original_testbed(
                original, task_id, workspace_root,
                proxy=registry_proxy, min_free_gib=min_free_gib,
            )
            revision = recipe_revision(
                record, dataset_revision, harness_revision, bridge,
                cube_tool_base_image, cube_tool_entrypoint,
                common_base_dockerfile, provenance["manifest_digest"],
                provenance["workspace_layer_digest"],
            )
            spec = specs[task_id]
            client = docker.from_env()
            with mapping_lock:
                environment_lock = environment_locks.setdefault(
                    str(spec.env_image_key), threading.Lock()
                )
            # Tasks with identical scripts share both an image and harness log
            # directory. Serialize their environment creation and retain the
            # cached environment for all consumers and subsequent runs.
            with environment_lock:
                harness_build(build_env_images, spec, client)
            local_image = build_workspace_image(spec, workspace, client)
            cleanup_images.append(local_image)
            architecture = client.images.get(local_image).attrs.get("Architecture")
            if architecture not in {"arm64", "aarch64"}:
                raise RuntimeError(f"harness image architecture is {architecture}")
            output = f"{registry.rstrip('/')}/swe-rebench-arm64:{safe_tag(task_id)}"
            cleanup_images.insert(0, output)
            normalize_harness_image(
                local_image, output, bridge,
                cube_tool_base_image=cube_tool_base_image,
                cube_tool_entrypoint=cube_tool_entrypoint,
            )
            install_config = record.get("install_config") or {}
            if isinstance(install_config, str):
                install_config = json.loads(install_config)
            verify_harness_image(output, bridge, str(install_config.get("test_cmd") or "true"))
            if push:
                result = {
                    "arm64_image": push_immutable(output), "platform": "linux/arm64",
                    "recipe_revision": revision, "status": "supported",
                    "cube_tool_base_image": cube_tool_base_image,
                    "original_manifest_digest": provenance["manifest_digest"],
                    "workspace_layer_digest": provenance["workspace_layer_digest"],
                    "workspace_git_head": provenance["git_head"],
                }
            else:
                result = {
                    "arm64_image": output, "platform": "linux/arm64",
                    "recipe_revision": revision, "status": "built-unpublished",
                    "cube_tool_base_image": cube_tool_base_image,
                    "original_manifest_digest": provenance["manifest_digest"],
                    "workspace_layer_digest": provenance["workspace_layer_digest"],
                    "workspace_git_head": provenance["git_head"],
                }
        except Exception as exc:
            result = {
                "arm64_image": None, "platform": "linux/arm64", "recipe_revision": revision,
                "status": "unsupported-arm64", "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            failure = exc
        finally:
            if workspace is not None and not keep_workspaces:
                shutil.rmtree(workspace.parent, ignore_errors=True)
        with mapping_lock:
            mapping[original] = result
            write_mapping(mapping_path, mapping)
        if failure is not None and fail_fast:
            raise failure
        if push and result.get("status") == "supported" and client is not None:
            for image_name in cleanup_images:
                try:
                    client.images.remove(image=image_name, force=False)
                except Exception as exc:
                    logging.getLogger("clawbox.swe-rebench-arm64").warning(
                        "could not remove local image %s after push: %s", image_name, exc,
                    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(build_one, task_id, selected): task_id
            for task_id, selected in selection.items()
        }
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception:
                if fail_fast:
                    for pending in futures:
                        pending.cancel()
                    raise
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the selected SWE-ReBench images natively for arm64")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--dataset-id", default=None)
    parser.add_argument("--dataset-revision", default=DEFAULT_DATASET_REVISION)
    parser.add_argument(
        "--selection", type=Path, required=True,
        help="JSON/JSONL/Parquet rows containing instance_id and original image",
    )
    parser.add_argument("--expected-count", type=int, default=128)
    parser.add_argument("--swebench-root", type=Path, required=True)
    parser.add_argument("--swebench-revision", default=DEFAULT_HARNESS_REVISION)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--tool-bridge-binary", type=Path, required=True)
    parser.add_argument(
        "--cube-tool-base-image", required=True,
        help="immutable ARM64 Cube Tool image supplying /usr/bin/envd",
    )
    parser.add_argument(
        "--cube-tool-entrypoint", type=Path,
        default=Path(__file__).resolve().parents[2] / "docker" / "cube-runtime-entrypoint.sh",
    )
    parser.add_argument(
        "--common-base-dockerfile", type=Path,
        default=(
            Path(__file__).resolve().parents[2]
            / "docker" / "Dockerfile.swe-rebench-arm64-common"
        ),
        help="Dockerfile layered over the stock ARM64 SWE-bench base",
    )
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--workers", type=int, default=4,
        help="concurrent image download/build pipelines (default: 4)",
    )
    parser.add_argument(
        "--registry-proxy", default="",
        help="HTTP/SOCKS proxy used only to read original Docker Hub images",
    )
    parser.add_argument(
        "--workspace-root", type=Path, required=True,
        help="temporary workspace extraction directory; completed tasks are removed",
    )
    parser.add_argument(
        "--min-free-gib", type=int, default=150,
        help="stop a new download when filesystem free space is below this value",
    )
    parser.add_argument("--keep-workspaces", action="store_true")
    args = parser.parse_args()

    require_native_arm64()
    if not re.fullmatch(r".+@sha256:[a-f0-9]{64}", args.cube_tool_base_image):
        parser.error("--cube-tool-base-image must be pinned as repository@sha256:...")
    if not args.cube_tool_entrypoint.is_file():
        parser.error(f"Cube Tool entrypoint does not exist: {args.cube_tool_entrypoint}")
    if not args.common_base_dockerfile.is_file():
        parser.error(f"common ARM64 base Dockerfile does not exist: {args.common_base_dockerfile}")
    if not args.tool_bridge_binary.is_file():
        parser.error(f"Tool Bridge binary does not exist: {args.tool_bridge_binary}")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.min_free_gib < 1:
        parser.error("--min-free-gib must be at least 1")
    selection = selected_tasks(args.selection)
    if len(selection) != args.expected_count:
        raise SystemExit(f"selection count is {len(selection)}, expected {args.expected_count}")
    records = records_from_file(args.dataset) if args.dataset else fetch_dataset(args.dataset_id, args.dataset_revision)
    make_test_spec, build_base_images, build_env_images, harness_revision = load_harness(
        args.swebench_root.resolve(), args.swebench_revision,
    )
    result = build_selected(
        records, selection, make_test_spec=make_test_spec,
        build_base_images=build_base_images, build_env_images=build_env_images,
        registry=args.registry, bridge=args.tool_bridge_binary, mapping_path=args.mapping,
        push=args.push, dataset_revision=args.dataset_revision, harness_revision=harness_revision,
        fail_fast=args.fail_fast, cube_tool_base_image=args.cube_tool_base_image,
        cube_tool_entrypoint=args.cube_tool_entrypoint,
        common_base_dockerfile=args.common_base_dockerfile,
        workspace_root=args.workspace_root, registry_proxy=args.registry_proxy,
        min_free_gib=args.min_free_gib, workers=args.workers,
        keep_workspaces=args.keep_workspaces,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if any(value.get("status") == "unsupported-arm64" for value in result.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
