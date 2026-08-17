from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
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


def records_from_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".parquet":
        try:
            from datasets import Dataset
        except ImportError as exc:
            raise RuntimeError("reading Parquet requires `pip install datasets`") from exc
        return [dict(item) for item in Dataset.from_parquet(str(path))]
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


def load_harness(root: Path, revision: str) -> tuple[Any, Any, str]:
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
    except ModuleNotFoundError as exc:
        requirements = root / "requirements.txt"
        hint = f"python3 -m pip install -r '{requirements}'" if requirements.is_file() else \
            "python3 -m pip install ghapi docker datasets"
        raise RuntimeError(
            "SWE-bench fork dependencies are missing from this Python environment "
            f"(missing module: {exc.name}). Install them first, e.g.:\n"
            f"  {hint}"
        ) from exc
    # Restricted networks: github.com is often unreachable from inside the
    # docker build even when the host can reach it through a proxy.  When
    # CLAWBOX_GIT_PROXY is set, inject a git per-host proxy into every
    # generated instance Dockerfile (right after FROM, so it applies before
    # setup_repo.sh clones the repo) and force host networking so the proxy's
    # loopback address is reachable. Gated: unset env keeps stock behaviour.
    git_proxy = os.environ.get("CLAWBOX_GIT_PROXY", "")
    if git_proxy:
        _original_dockerfile_instance = test_spec_module.get_dockerfile_instance

        def _dockerfile_instance_with_git_proxy(platform: str, language: str, env_image_key: str) -> str:
            dockerfile = _original_dockerfile_instance(platform, language, env_image_key)
            lines = dockerfile.splitlines()
            patched: list[str] = []
            inserted = False
            for line in lines:
                patched.append(line)
                if not inserted and line.startswith("FROM "):
                    patched.append(
                        f"RUN git config --global http.https://github.com.proxy {git_proxy}"
                    )
                    inserted = True
            return "\n".join(patched) + "\n"

        test_spec_module.get_dockerfile_instance = _dockerfile_instance_with_git_proxy
    return (
        test_spec_module.make_test_spec,
        getattr(build_module, "build_env_images", None),
        build_module.build_instance_image,
        actual,
    )


def recipe_revision(record: dict[str, Any], dataset_revision: str, harness_revision: str) -> str:
    relevant = {
        key: record.get(key) for key in (
            "instance_id", "repo", "base_commit", "environment_setup_commit",
            "install_config", "requirements", "environment",
        )
    }
    digest = hashlib.sha256(json.dumps(relevant, sort_keys=True, default=str).encode()).hexdigest()
    return (
        f"dataset:{dataset_revision}:harness:{harness_revision}:"
        f"contract:{CONTRACT_VERSION}:recipe:{digest}"
    )


def verify_harness_image(image: str, bridge: Path, test_command: str) -> None:
    test_command = test_command or "true"
    contract = " && ".join((
        "test -d /testbed", "test -w /testbed", "command -v sh", "command -v git", "command -v patch",
        "test -x /clawbox-contract/tool-bridge", "/clawbox-contract/tool-bridge --self-test",
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


def normalize_harness_image(local_image: str, output: str) -> None:
    """Make the harness output compatible with the unprivileged Tool Pod."""
    with tempfile.TemporaryDirectory(prefix="clawbox-arm64-wrapper-") as directory:
        dockerfile = Path(directory) / "Dockerfile"
        dockerfile.write_text(
            "ARG BASE_IMAGE\n"
            "FROM ${BASE_IMAGE}\n"
            "USER 0\n"
            "RUN test -d /testbed && chown -R 10001:10001 /testbed\n"
            "USER 10001:10001\n"
            "WORKDIR /testbed\n",
            encoding="utf-8",
        )
        subprocess.run([
            "docker", "build", "--platform", "linux/arm64", "--pull=false",
            "--build-arg", f"BASE_IMAGE={local_image}", "--tag", output,
            "--file", str(dockerfile), directory,
        ], check=True, timeout=7200)


def harness_build(
    build_env_images: Any, build_instance_image: Any, spec: Any,
    client: Any, logger: logging.Logger,
) -> None:
    # The pinned fork builds base + environment images through the batch
    # helpers and only consumes them in the singular build_instance_image.
    # Introspect signatures so a future harness change fails loudly instead
    # of silently altering the image recipe.
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
    parameters = inspect.signature(build_instance_image).parameters
    options = {
        key: value for key, value in {
            "nocache": False,
            "force_rebuild": False,
        }.items() if key in parameters
    }
    result = build_instance_image(spec, client, logger, **options)
    if result is False:
        raise RuntimeError("SWE-bench harness reported an instance image build failure")


def build_selected(
    records: list[dict[str, Any]], selection: dict[str, dict[str, str]], *,
    make_test_spec: Any, build_env_images: Any, build_instance_image: Any,
    registry: str, bridge: Path, mapping_path: Path, push: bool,
    dataset_revision: str, harness_revision: str, fail_fast: bool,
) -> dict[str, Any]:
    import docker

    by_id = {str(item.get("instance_id")): item for item in records}
    missing = sorted(set(selection) - set(by_id))
    if missing:
        raise RuntimeError(f"selected tasks are absent from the full dataset: {', '.join(missing)}")
    mapping = load_mapping(mapping_path)
    client = docker.from_env()
    if os.environ.get("CLAWBOX_GIT_PROXY"):
        # Build containers share the host network so the git proxy bound to the
        # host loopback (e.g. 127.0.0.1:1080) is reachable inside the build.
        _original_build = client.api.build

        def _build_with_host_network(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("network_mode", "host")
            return _original_build(*args, **kwargs)

        client.api.build = _build_with_host_network
    logger = logging.getLogger("clawbox.swe-rebench-arm64")
    logging.basicConfig(level=logging.INFO)
    # The pinned SWE-bench harness writes build logs through a custom logger
    # exposing `.log_file`; attach it so error reporting doesn't mask failures.
    if not hasattr(logger, "log_file"):
        logger.log_file = str(mapping_path.parent / "swe-rebench-harness.log")

    for task_id, selected in selection.items():
        record = by_id[task_id]
        original = selected["original_image"]
        revision = recipe_revision(record, dataset_revision, harness_revision)
        existing = mapping.get(original)
        if existing and existing.get("status") == "supported":
            if existing.get("recipe_revision") != revision:
                raise RuntimeError(f"immutable mapping drift for {original}")
            if existing.get("platform") != "linux/arm64" or not re.fullmatch(
                r".+@sha256:[a-f0-9]{64}", str(existing.get("arm64_image") or ""),
            ):
                raise RuntimeError(f"supported mapping for {original} lacks arm64 digest provenance")
            continue
        try:
            # The pinned fork predates the harness's native `arch` parameter.
            # Pass only the keyword arguments the installed harness accepts;
            # without `arch`, building on the native arm64 daemon still yields
            # arm64 images, which the architecture check below enforces.
            spec_kwargs = {
                key: value for key, value in {
                    "namespace": "clawbox-arm64",
                    "base_image_tag": "v1",
                    "env_image_tag": "v1",
                    "instance_image_tag": "v1",
                    "arch": "arm64",
                }.items() if key in inspect.signature(make_test_spec).parameters
            }
            spec = make_test_spec(record, **spec_kwargs)
            spec_arch = str(getattr(spec, "arch", "arm64"))
            if spec_arch not in {"arm64", "aarch64"}:
                raise RuntimeError(f"harness produced a foreign-architecture spec: {spec_arch}")
            harness_build(build_env_images, build_instance_image, spec, client, logger)
            local_image = str(spec.instance_image_key)
            architecture = client.images.get(local_image).attrs.get("Architecture")
            if architecture not in {"arm64", "aarch64"}:
                raise RuntimeError(f"harness image architecture is {architecture}")
            output = f"{registry.rstrip('/')}/swe-rebench-arm64:{safe_tag(task_id)}"
            normalize_harness_image(local_image, output)
            install_config = record.get("install_config") or {}
            if isinstance(install_config, str):
                install_config = json.loads(install_config)
            verify_harness_image(output, bridge, str(install_config.get("test_cmd") or "true"))
            if push:
                mapping[original] = {
                    "arm64_image": push_immutable(output), "platform": "linux/arm64",
                    "recipe_revision": revision, "status": "supported",
                }
            else:
                mapping[original] = {
                    "arm64_image": output, "platform": "linux/arm64",
                    "recipe_revision": revision, "status": "built-unpublished",
                }
        except Exception as exc:
            mapping[original] = {
                "arm64_image": None, "platform": "linux/arm64", "recipe_revision": revision,
                "status": "unsupported-arm64", "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            write_mapping(mapping_path, mapping)
            if fail_fast:
                raise
        write_mapping(mapping_path, mapping)
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the selected SWE-ReBench images natively for arm64")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dataset", type=Path)
    source.add_argument("--dataset-id", default=None)
    parser.add_argument("--dataset-revision", default=DEFAULT_DATASET_REVISION)
    parser.add_argument("--selection", type=Path, required=True, help="ClawTune's 128-task tasks.json")
    parser.add_argument("--expected-count", type=int, default=128)
    parser.add_argument("--swebench-root", type=Path, required=True)
    parser.add_argument("--swebench-revision", default=DEFAULT_HARNESS_REVISION)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--tool-bridge-binary", type=Path, required=True)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    require_native_arm64()
    selection = selected_tasks(args.selection)
    if len(selection) != args.expected_count:
        raise SystemExit(f"selection count is {len(selection)}, expected {args.expected_count}")
    records = records_from_file(args.dataset) if args.dataset else fetch_dataset(args.dataset_id, args.dataset_revision)
    make_test_spec, build_env_images, build_instance_image, harness_revision = load_harness(
        args.swebench_root.resolve(), args.swebench_revision,
    )
    result = build_selected(
        records, selection, make_test_spec=make_test_spec,
        build_env_images=build_env_images, build_instance_image=build_instance_image,
        registry=args.registry, bridge=args.tool_bridge_binary, mapping_path=args.mapping,
        push=args.push, dataset_revision=args.dataset_revision, harness_revision=harness_revision,
        fail_fast=args.fail_fast,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if any(value.get("status") == "unsupported-arm64" for value in result.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
