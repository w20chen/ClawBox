from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "clawbox-arm64-contract-v1"


@dataclass(frozen=True)
class ImageRecipe:
    instance_id: str
    original_image: str
    context: Path
    dockerfile: Path
    build_args: dict[str, str] = field(default_factory=dict)
    dependency_command: str = "true"
    test_command: str = "true"


def _records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = raw if isinstance(raw, list) else raw.get("tasks", raw.get("instances", []))
    if not isinstance(values, list):
        raise ValueError("task definition must contain a tasks/instances list")
    return values


def load_recipes(path: Path) -> list[ImageRecipe]:
    base = path.resolve().parent
    recipes: list[ImageRecipe] = []
    for value in _records(path):
        instance_id = str(value.get("instance_id") or value.get("task_id") or value.get("id") or "").strip()
        original = str(value.get("image") or value.get("docker_image") or value.get("image_name") or "").strip()
        context_value = value.get("build_context") or value.get("context")
        dockerfile_value = value.get("dockerfile")
        if not instance_id or not original or not context_value or not dockerfile_value:
            raise ValueError(
                "each native recipe requires instance_id, original image, build_context, and dockerfile"
            )
        context = Path(str(context_value))
        dockerfile = Path(str(dockerfile_value))
        context = context if context.is_absolute() else base / context
        dockerfile = dockerfile if dockerfile.is_absolute() else base / dockerfile
        args = value.get("build_args") or {}
        if not isinstance(args, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in args.items()):
            raise ValueError(f"{instance_id}: build_args must be string-to-string")
        recipes.append(ImageRecipe(
            instance_id=instance_id,
            original_image=original,
            context=context.resolve(),
            dockerfile=dockerfile.resolve(),
            build_args=args,
            dependency_command=str(value.get("dependency_command") or "true"),
            test_command=str(value.get("test_command") or "true"),
        ))
    return recipes


def _run(command: list[str], *, timeout: int = 3600, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_native_arm64() -> None:
    machine = platform.machine().lower()
    if machine not in {"aarch64", "arm64"}:
        raise RuntimeError(f"native arm64 builder required; host architecture is {machine}")
    docker_arch = _run(["docker", "info", "--format", "{{.Architecture}}"], capture=True).stdout.strip()
    if docker_arch not in {"aarch64", "arm64"}:
        raise RuntimeError(f"Docker engine must be native arm64; reported {docker_arch}")
    foreign = Path("/proc/sys/fs/binfmt_misc")
    if foreign.is_dir():
        enabled = [item.name for item in foreign.iterdir() if item.name.startswith(("qemu-", "rosetta"))]
        if enabled:
            raise RuntimeError(f"foreign-architecture binfmt handlers must be disabled: {', '.join(sorted(enabled))}")


def recipe_revision(recipe: ImageRecipe) -> str:
    digest = hashlib.sha256()
    digest.update(CONTRACT_VERSION.encode())
    digest.update(recipe.dockerfile.read_bytes())
    digest.update(json.dumps(recipe.build_args, sort_keys=True).encode())
    try:
        revision = _run(
            ["git", "-C", str(recipe.context), "rev-parse", "HEAD"], timeout=30, capture=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        revision = "unversioned"
    digest.update(revision.encode())
    return f"{revision}:{digest.hexdigest()}"


def safe_tag(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.lower()).strip("-.")
    return (normalized or "task")[:100]


def build_image(recipe: ImageRecipe, image: str) -> None:
    if not recipe.context.is_dir():
        raise RuntimeError(f"build context is missing: {recipe.context}")
    if not recipe.dockerfile.is_file() or recipe.context not in recipe.dockerfile.parents:
        raise RuntimeError("dockerfile must exist inside its declared build context")
    command = [
        "docker", "build", "--platform", "linux/arm64", "--pull",
        "--file", str(recipe.dockerfile), "--tag", image,
    ]
    for key, value in sorted(recipe.build_args.items()):
        command += ["--build-arg", f"{key}={value}"]
    command.append(str(recipe.context))
    _run(command, timeout=7200)
    architecture = _run(
        ["docker", "image", "inspect", "--format", "{{.Architecture}}", image], capture=True
    ).stdout.strip()
    if architecture not in {"arm64", "aarch64"}:
        raise RuntimeError(f"built image has foreign architecture: {architecture}")


def verify_contract(recipe: ImageRecipe, image: str, bridge_binary: Path) -> None:
    if not bridge_binary.is_file():
        raise RuntimeError(f"Tool Bridge binary is missing: {bridge_binary}")
    contract = " && ".join((
        "test -d /testbed",
        "test -w /testbed",
        "command -v sh",
        "command -v git",
        "command -v patch",
        "test -x /clawbox-contract/tool-bridge",
        "/clawbox-contract/tool-bridge --self-test",
        f"cd /testbed && ({recipe.dependency_command})",
        f"cd /testbed && ({recipe.test_command})",
    ))
    _run([
        "docker", "run", "--rm", "--network", "none", "--platform", "linux/arm64", "--user", "10001:10001",
        "--mount", f"type=bind,src={bridge_binary.resolve()},dst=/clawbox-contract/tool-bridge,readonly",
        "--entrypoint", "/bin/sh", image, "-ec", contract,
    ], timeout=1800)


def push_immutable(image: str) -> str:
    _run(["docker", "push", image], timeout=7200)
    output = _run(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], capture=True
    ).stdout.strip()
    digests = json.loads(output or "[]")
    if not digests:
        raise RuntimeError(f"registry did not return an immutable digest for {image}")
    repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    immutable = next((str(item) for item in digests if str(item).split("@", 1)[0] == repository), str(digests[0]))
    inspected = _run(["docker", "buildx", "imagetools", "inspect", immutable], capture=True).stdout
    if not re.search(r"(?i)(platform:\s*linux/arm64|linux/arm64)", inspected):
        raise RuntimeError(f"published manifest does not prove linux/arm64: {immutable}")
    return immutable


def load_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("image mapping must be a JSON object")
    return raw


def write_mapping(path: Path, mapping: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(mapping, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    os.replace(temporary, path)


def build_all(
    recipes: list[ImageRecipe], *, registry: str, mapping_path: Path,
    bridge_binary: Path, push: bool, fail_fast: bool,
) -> dict[str, Any]:
    require_native_arm64()
    mapping = load_mapping(mapping_path)
    for recipe in recipes:
        revision = recipe_revision(recipe)
        existing = mapping.get(recipe.original_image)
        if existing and existing.get("status") == "supported":
            if existing.get("recipe_revision") != revision:
                raise RuntimeError(
                    f"immutable mapping for {recipe.original_image} already uses a different recipe revision"
                )
            if existing.get("platform") != "linux/arm64" or not re.fullmatch(
                r".+@sha256:[a-f0-9]{64}", str(existing.get("arm64_image") or ""),
            ):
                raise RuntimeError(f"supported mapping for {recipe.original_image} lacks arm64 digest provenance")
            continue
        image = f"{registry.rstrip('/')}/swe-rebench-arm64:{safe_tag(recipe.instance_id)}"
        try:
            build_image(recipe, image)
            verify_contract(recipe, image, bridge_binary)
            if not push:
                mapping[recipe.original_image] = {
                    "arm64_image": image,
                    "platform": "linux/arm64",
                    "recipe_revision": revision,
                    "status": "built-unpublished",
                }
            else:
                mapping[recipe.original_image] = {
                    "arm64_image": push_immutable(image),
                    "platform": "linux/arm64",
                    "recipe_revision": revision,
                    "status": "supported",
                }
        except Exception as exc:
            mapping[recipe.original_image] = {
                "arm64_image": None,
                "platform": "linux/arm64",
                "recipe_revision": revision,
                "status": "unsupported-arm64",
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
            write_mapping(mapping_path, mapping)
            if fail_fast:
                raise
        write_mapping(mapping_path, mapping)
    return mapping


def main() -> None:
    parser = argparse.ArgumentParser(description="Build SWE-ReBench task images natively for linux/arm64")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--tool-bridge-binary", type=Path, required=True)
    parser.add_argument("--push", action="store_true", help="publish and emit supported digest mappings")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    result = build_all(
        load_recipes(args.tasks), registry=args.registry, mapping_path=args.mapping,
        bridge_binary=args.tool_bridge_binary, push=args.push, fail_fast=args.fail_fast,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if any(item.get("status") == "unsupported-arm64" for item in result.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
