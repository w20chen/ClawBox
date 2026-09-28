#!/usr/bin/env python3
"""Refresh existing task templates with current ClawBox/ClawTune, preserving their workspace."""
from __future__ import annotations
import argparse
import json
import os
import re
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from clawbox.clawtune_integration import source_revision
from clawbox.lab import template_record


def call(argv, **kwargs):
    subprocess.run(argv, check=True, **kwargs)


def normalize_unix_script(path: Path) -> None:
    """Make a copied host script directly executable by a Linux guest."""
    data = path.read_bytes()
    if b"\0" in data:
        raise ValueError(f"refusing to normalize binary file as a script: {path}")
    path.write_bytes(data.replace(b"\r\n", b"\n"))


def atomic_json(path: Path, value: Any) -> None:
    """Publish a complete JSON document, or leave the previous one untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_build_inputs(args: argparse.Namespace, profile: dict[str, Any]) -> dict[str, Any]:
    """Resolve repeatable image inputs from flags, saved profile, then environment."""
    saved = profile.get("image_build") or {}
    if not isinstance(saved, dict):
        raise ValueError("host profile image_build must be an object")
    direct_network = args.direct_network
    if direct_network is None:
        direct_network = bool(saved.get("direct_network", False))
    return {
        "registry": args.registry or saved.get("registry") or "127.0.0.1:5000/clawbox",
        "go": args.go or saved.get("go") or os.getenv("CLAWBOX_GO") or "go",
        "kernel_source": (
            args.kernel_source or saved.get("kernel_source")
            or os.getenv("CLAWBOX_GUEST_KERNEL_SOURCE")
        ),
        "kernel_build": (
            args.kernel_build or saved.get("kernel_build")
            or os.getenv("CLAWBOX_GUEST_KERNEL_BUILD")
        ),
        "direct_network": direct_network,
    }


def flattened_base(image: str) -> str:
    """Render a one-layer rootfs base so repeated refreshes cannot hit depth limits."""
    inspected = json.loads(subprocess.check_output(
        ["docker", "image", "inspect", image], text=True,
    ))
    if len(inspected) != 1:
        raise ValueError(f"cannot inspect exactly one base image: {image}")
    config = inspected[0].get("Config") or {}
    lines = [f"FROM {image} AS previous", "FROM scratch", "COPY --from=previous / /"]
    for entry in config.get("Env") or []:
        key, separator, value = str(entry).partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid inherited image environment entry: {entry!r}")
        lines.append(f"ENV {key}={json.dumps(value)}")
    for key, value in sorted((config.get("Labels") or {}).items()):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(key)):
            raise ValueError(f"invalid inherited image label: {key!r}")
        lines.append(f"LABEL {key}={json.dumps(str(value))}")
    if working_dir := str(config.get("WorkingDir") or "").strip():
        if not working_dir.startswith("/"):
            raise ValueError(f"base image has a non-absolute working directory: {working_dir}")
        lines.append(f"WORKDIR {json.dumps(working_dir)}")
    lines.append("USER root")
    return "\n".join(lines) + "\n"


def main():
    from cubesandbox import Template, Sandbox, NEVER_TIMEOUT

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, default=Path.home()/".config/clawbox/host.json")
    p.add_argument("--registry")
    p.add_argument("--go")
    p.add_argument("--pip-index", default="https://pypi.org/simple")
    network = p.add_mutually_exclusive_group()
    network.add_argument("--direct-network", action="store_true", dest="direct_network")
    network.add_argument("--proxy-network", action="store_false", dest="direct_network")
    p.set_defaults(direct_network=None)
    p.add_argument("--role", choices=["runtime", "sandbox"], action="append")
    p.add_argument("--kernel-source")
    p.add_argument("--kernel-build")
    args = p.parse_args()
    if not args.profile.is_file():
        p.error(f"host profile does not exist: {args.profile}; run 'clawbox experiment setup' first")
    try:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        if not isinstance(profile, dict):
            raise ValueError("root value is not an object")
        build = resolve_build_inputs(args, profile)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        p.error(f"cannot read host profile {args.profile}: {exc}")
    if "sandbox" in (args.role or ("runtime", "sandbox")):
        if not build["kernel_source"] or not build["kernel_build"]:
            p.error(
                "Tool images require --kernel-source and --kernel-build on the first build; "
                "successful values are saved in the host profile"
            )
        if not (Path(build["kernel_source"])/"include/linux/sched.h").is_file() or not (Path(build["kernel_build"])/"include/generated/autoconf.h").is_file():
            p.error("Guest kernel source/build headers are missing")
    go_executable = shutil.which(str(build["go"]))
    if not go_executable:
        p.error(f"Go executable does not exist: {build['go']}")
    build["go"] = str(Path(go_executable).resolve())
    if build["kernel_source"]:
        build["kernel_source"] = str(Path(build["kernel_source"]).resolve())
    if build["kernel_build"]:
        build["kernel_build"] = str(Path(build["kernel_build"]).resolve())
    os.environ.setdefault("BUILDX_CONFIG", str(Path.home()/".cache/clawbox/buildx"))
    if build["direct_network"]:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
    root = Path(__file__).resolve().parents[1]
    tune = Path(os.environ.get("CLAWTUNE_ROOT", root.parent / "ClawTune")).resolve()
    if not (tune / "services" / "sidecar" / "pyproject.toml").is_file():
        p.error(
            "ClawTune checkout is missing; set CLAWTUNE_ROOT or place it next "
            "to ClawBox"
        )
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "aarch64", "arm64",
    }:
        p.error("image refresh must run on the target ARM64 Linux host")
    plugin = tune/"packages/clawtune-plugin"
    if not (plugin/"node_modules/.bin/tsc").exists():
        call(["npm", "ci", "--ignore-scripts"], cwd=plugin)
    call(["npm", "run", "build"], cwd=plugin)
    revision = source_revision(tune)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report = {"clawtune_revision": revision, "templates": {}}
    with tempfile.TemporaryDirectory(prefix="clawbox-image-") as temp:
        context = Path(temp)
        shutil.copytree(tune/"services/sidecar", context/"sidecar", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(tune/"contracts", context/"contracts")
        shutil.copytree(tune/"seeds", context/"seeds")
        shutil.copytree(plugin/"dist", context/"plugin/dist")
        for name in ("package.json", "openclaw.plugin.json"):
            shutil.copy(plugin/name, context/"plugin"/name)
        shutil.copy(tune/"tools/guest_collector_server.py", context)
        shutil.copytree(root/"scripts", context/"scripts", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(root/"docker/cube-runtime-entrypoint.sh", context/"entrypoint.sh")
        for script in (
            context/"entrypoint.sh",
            context/"guest_collector_server.py",
            *(context/"scripts").glob("*.py"),
            *(context/"scripts").glob("*.sh"),
        ):
            normalize_unix_script(script)
        env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64",
               "GOPROXY": os.getenv("GOPROXY", "https://goproxy.cn,direct"),
               "GOCACHE": str(Path.home()/".cache/clawbox/go-build")}
        call([build["go"], "build", "-mod=readonly", "-trimpath", "-o", str(context/"tool-bridge"), "."], cwd=root/"toolbridge", env=env)
        mvdan_source = tune/"services/sidecar/src/tool_resource/_mvdan_adapter"
        mvdan_binary_name = "mvdan-clause-adapter-protocol-3-mvdan-v3.13.1-linux-arm64"
        mvdan_binary = context/mvdan_binary_name
        call(
            [build["go"], "build", "-mod=readonly", "-buildvcs=false", "-trimpath",
             "-o", str(mvdan_binary), "."],
            cwd=mvdan_source, env=env,
        )
        for role in args.role or ("runtime", "sandbox"):
            prior = Template.get(profile[role]["template_id"])
            image = profile[role]["source_image_reference"]
            tag = f"{build['registry']}/lab-{role}:{stamp}"
            target = "/opt/clawtune" if role == "runtime" else "/opt/clawtune-guest"
            entry = "cube-runtime-entrypoint.sh" if role == "runtime" else "cube-tool-entrypoint.sh"
            dockerfile = flattened_base(image) + f'''
COPY sidecar {target}/services/sidecar
COPY contracts {target}/contracts
COPY seeds {target}/seeds
RUN /opt/clawtune/venv/bin/pip install --index-url {args.pip_index} --upgrade jsonschema setuptools wheel && /opt/clawtune/venv/bin/pip install --index-url {args.pip_index} --no-build-isolation {target}/services/sidecar
ENV XDG_CACHE_HOME=/opt/clawtune/cache
COPY {mvdan_binary_name} /opt/clawtune/cache/agent-sched-bench/{mvdan_binary_name}
RUN chmod 755 /opt/clawtune/cache/agent-sched-bench/{mvdan_binary_name} && /opt/clawtune/cache/agent-sched-bench/{mvdan_binary_name} </dev/null >/dev/null
COPY entrypoint.sh /usr/local/bin/{entry}
ENV CLAWTUNE_REVISION={revision}
LABEL org.opencontainers.image.source.clawtune.revision="{revision}"
'''
            if role == "runtime":
                dockerfile += '''COPY plugin /opt/clawtune/packages/clawtune-plugin
COPY seeds/bootstrap-v1 /opt/clawtune/cold-start/tool-resource
COPY scripts/clawbox-policy-ssh.py /usr/local/bin/ssh
COPY scripts/initialize-clawtune-state.py /usr/local/bin/initialize-clawtune-state.py
COPY scripts/runtime-entrypoint.sh /usr/local/bin/runtime-entrypoint
COPY scripts/clawtune-sidecar-entrypoint.sh /usr/local/bin/clawtune-sidecar-entrypoint
COPY scripts/native-kb-pull.py /usr/local/bin/native-kb-pull.py
COPY scripts/kb-flush.py /usr/local/bin/kb-flush.py
RUN chmod 755 /usr/local/bin/ssh /usr/local/bin/runtime-entrypoint /usr/local/bin/clawtune-sidecar-entrypoint
'''
            else:
                # BCC reads kernel structures: a matching version string alone
                # is insufficient. Use the actual build's generated headers.
                probe = Sandbox.create(template=profile[role]["template_id"],
                                       timeout=NEVER_TIMEOUT,
                                       distribution_scope=[profile["node"]],
                                       metadata={"clawbox.owner": "lab-image-kernel-probe-" + stamp})
                try:
                    result = probe.commands.run("uname -r; zcat /proc/config.gz", timeout=30)
                    if result.exit_code:
                        raise RuntimeError("Cannot read guest kernel configuration: " + result.stderr)
                    release, config = result.stdout.split("\n", 1)
                    if not config.startswith("#") or "CONFIG_ARM64=y" not in config:
                        raise ValueError("Guest did not return an ARM64 kernel configuration")
                    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", release):
                        raise ValueError("Invalid guest kernel release")
                    if (Path(build["kernel_build"])/".config").read_text() != config:
                        raise ValueError("Provided kernel build configuration differs from the running guest")
                finally:
                    probe.kill()
                dockerfile += f'''COPY --from=kernel-source / /usr/src/linux-{release}
COPY --from=kernel-build /include /usr/src/linux-{release}/include
COPY --from=kernel-build /arch/arm64/include /usr/src/linux-{release}/arch/arm64/include
COPY --from=kernel-build /.config /usr/src/linux-{release}/.config
RUN ln -sfn /usr/src/linux-{release} /lib/modules/{release}/build
'''
                dockerfile += '''COPY tool-bridge /usr/local/bin/tool-bridge
COPY guest_collector_server.py /opt/clawtune-guest/tools/guest_collector_server.py
RUN chmod 755 /usr/local/bin/tool-bridge
'''
            dockerfile += f'RUN chmod 755 /usr/local/bin/{entry}\nENTRYPOINT ["/usr/local/bin/{entry}"]\n'
            (context/"Dockerfile").write_text(dockerfile)
            kernel_contexts = (["--build-context", "kernel-source=" + build["kernel_source"],
                                "--build-context", "kernel-build=" + build["kernel_build"]]
                               if role == "sandbox" else [])
            call(["docker", "build", "--network=host", *kernel_contexts, "-t", tag, str(context)])
            call(["docker", "push", tag])
            image = json.loads(subprocess.check_output(["docker", "image", "inspect", tag]))[0]["RepoDigests"][0]
            kernels = {r.get("kernel_version") for r in prior.replicas or [] if r.get("node_id") == profile["node"]}
            if len(kernels) != 1:
                raise ValueError("Template kernel identity is ambiguous")
            alias = f"clawbox-lab-{role}-{stamp}"
            register = [sys.executable, str(root/"scripts/register-cube-template.py"), image,
                        "--alias", alias, "--node", profile["node"],
                        "--cpu-millicores", str(profile[role]["vcpu"]*1000),
                        "--memory-mib", str(profile[role]["memory_mib"]),
                        "--command", f"/usr/local/bin/{entry}",
                        "--expected-kernel-version", next(iter(kernels)), "--exposed-port", "49983"]
            if role == "sandbox":
                register += ["--exposed-port", "2222", "--writable-layer-size", "40G"]
            result = subprocess.check_output(register, text=True)
            identity = json.loads(result.strip().splitlines()[-1])
            record = template_record(identity["template_id"])
            record.pop("nodes")
            report["templates"][role] = record
            # Persist each created identity so an interrupted build remains reviewable.
            atomic_json(args.profile.with_suffix(".images.json"), report)
    profile.update(report["templates"])
    profile["image_build"] = build
    atomic_json(args.profile, profile)
    print(f"Updated templates: {', '.join(report['templates'])}. Profile: {args.profile}")


if __name__ == "__main__":
    main()
