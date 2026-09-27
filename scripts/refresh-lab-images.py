#!/usr/bin/env python3
"""Refresh existing task templates with current ClawBox/ClawTune, preserving their workspace."""
from __future__ import annotations
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from clawbox.clawtune_integration import source_revision
from clawbox.lab import template_record
from cubesandbox import Template, Sandbox, NEVER_TIMEOUT


def call(argv, **kwargs):
    subprocess.run(argv, check=True, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", type=Path, default=Path.home()/".config/clawbox/lab.json")
    p.add_argument("--registry", default="127.0.0.1:5000/clawbox")
    p.add_argument("--go", default="go")
    p.add_argument("--pip-index", default="https://pypi.org/simple")
    p.add_argument("--direct-network", action="store_true")
    p.add_argument("--role", choices=["runtime", "sandbox"], action="append")
    p.add_argument("--kernel-source", default=os.getenv("CLAWBOX_GUEST_KERNEL_SOURCE"))
    p.add_argument("--kernel-build", default=os.getenv("CLAWBOX_GUEST_KERNEL_BUILD"))
    args = p.parse_args()
    if "sandbox" in (args.role or ("runtime", "sandbox")):
        if not args.kernel_source or not args.kernel_build:
            p.error("Tool images require --kernel-source and --kernel-build from the actual guest kernel build")
        if not (Path(args.kernel_source)/"include/linux/sched.h").is_file() or not (Path(args.kernel_build)/"include/generated/autoconf.h").is_file():
            p.error("Guest kernel source/build headers are missing")
    os.environ.setdefault("BUILDX_CONFIG", str(Path.home()/".cache/clawbox/buildx"))
    if args.direct_network:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
    root = Path(__file__).resolve().parents[1]
    tune = Path(os.environ["CLAWTUNE_ROOT"])
    profile = json.loads(args.profile.read_text())
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
        env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "arm64",
               "GOPROXY": os.getenv("GOPROXY", "https://goproxy.cn,direct"),
               "GOCACHE": str(Path.home()/".cache/clawbox/go-build")}
        call([args.go, "build", "-mod=readonly", "-trimpath", "-o", str(context/"tool-bridge"), "."], cwd=root/"toolbridge", env=env)
        for role in args.role or ("runtime", "sandbox"):
            prior = Template.get(profile[role]["template_id"])
            image = profile[role]["source_image_reference"]
            tag = f"{args.registry}/lab-{role}:{stamp}"
            target = "/opt/clawtune" if role == "runtime" else "/opt/clawtune-guest"
            entry = "cube-runtime-entrypoint.sh" if role == "runtime" else "cube-tool-entrypoint.sh"
            dockerfile = f'''FROM {image}
USER root
COPY sidecar {target}/services/sidecar
COPY contracts {target}/contracts
COPY seeds {target}/seeds
RUN /opt/clawtune/venv/bin/pip install --index-url {args.pip_index} --upgrade jsonschema setuptools wheel && /opt/clawtune/venv/bin/pip install --index-url {args.pip_index} --no-build-isolation {target}/services/sidecar
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
                    if (Path(args.kernel_build)/".config").read_text() != config:
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
            kernel_contexts = (["--build-context", "kernel-source=" + str(Path(args.kernel_source).resolve()),
                                "--build-context", "kernel-build=" + str(Path(args.kernel_build).resolve())]
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
            args.profile.with_suffix(".images.json").write_text(json.dumps(report, indent=2)+"\n")
    profile.update(report["templates"])
    args.profile.write_text(json.dumps(profile, indent=2)+"\n")
    print(f"Updated templates: {', '.join(report['templates'])}. Profile: {args.profile}")


if __name__ == "__main__":
    main()
