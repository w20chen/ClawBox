#!/usr/bin/env python3
"""Build Tool image overlays containing evidence-pinned native Conda caches.

The input JSON lists cases with case_id, source_image_reference, conda_specs,
channels, and validation_command. Nothing is installed into a workload-created
environment until that workload executes its own recorded command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess


def run(command: list[str], *, log=None) -> str:
    result = subprocess.run(command, check=False, text=True, stdout=log or subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    if result.returncode:
        detail = "see " + str(log.name) if log else (result.stdout or "")[-8000:]
        raise RuntimeError(f"{command[0]} failed with exit {result.returncode}: {detail}")
    return result.stdout or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--registry", required=True, help="Registry/repository for the new overlay images")
    parser.add_argument("--proxy", default="", help="Optional HTTP(S) proxy used only during the Docker build")
    parser.add_argument("--workload", type=Path, help="Optional workload; write image replacements for collector refresh/registration")
    args = parser.parse_args()
    records = json.loads(args.manifest.read_text())["cases"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for record in records:
        case = record["case_id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", case):
            raise ValueError(f"unsafe case_id: {case!r}")
        specs = record["conda_specs"]
        if not specs or any(not re.fullmatch(r"[A-Za-z0-9_.-]+=[A-Za-z0-9_.-]+", s) for s in specs):
            raise ValueError(f"{case}: conda_specs must contain explicit package=version pins")
        channels = record["channels"]
        if not channels or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", c) for c in channels):
            raise ValueError(f"{case}: provide the recorded named channels")
        source = record["source_image_reference"]
        if "@sha256:" not in source or not re.fullmatch(r"[A-Za-z0-9_./:@-]+", source):
            raise ValueError(f"{case}: source image must be digest pinned")
        identity = hashlib.sha256(json.dumps({"recipe": 2, "case": record}, sort_keys=True).encode()).hexdigest()[:16]
        context = args.output_dir / (case.lower() + "-" + identity)
        context.mkdir(exist_ok=True)
        condarc = ("channels:\n" + "".join(f"  - {c}\n" for c in channels) + "offline: true\n"
                   + "pinned_packages:\n" + "".join(f"  - {spec}\n" for spec in specs))
        (context / "condarc").write_text(condarc)
        channel_args = " ".join("-c " + shlex.quote(c) for c in channels)
        pins = " ".join(shlex.quote(spec) for spec in specs)
        (context / "Dockerfile").write_text(
            f"FROM {source} AS cache\nUSER root\n"
            f"RUN /opt/conda/bin/conda create -y --download-only --override-channels {channel_args} "
            f"-p /tmp/clawbox-cache-download {pins}\n"
            f"FROM {source}\nUSER root\n"
            "COPY --from=cache /opt/conda/pkgs /opt/conda/pkgs\n"
            "COPY condarc /root/.condarc\n"
        )
        tag = args.registry.rstrip(":/") + ":" + case.lower() + "-" + identity
        command = ["docker", "build", "--platform", "linux/arm64", "--network", "host", "--pull=false", "--progress", "plain"]
        if args.proxy:
            command += ["--build-arg", "HTTP_PROXY=" + args.proxy, "--build-arg", "HTTPS_PROXY=" + args.proxy]
        command += ["-t", tag, str(context)]
        with (context / "build.log").open("w") as log:
            run(command, log=log)
        # Exercise the original create command with networking disabled. This
        # container is disposable; successful validation cannot precreate its
        # environment inside the exported image.
        with (context / "validation.log").open("w") as log:
            run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/bash",
                 tag, "-lc", record["validation_command"]], log=log)
        with (context / "push.log").open("w") as log:
            run(["docker", "push", tag], log=log)
        info = json.loads(run(["docker", "image", "inspect", tag]))[0]
        digest = next(d for d in info["RepoDigests"] if d.startswith(args.registry.rstrip(":/") + "@"))
        result = {**record, "source_image_reference": digest, "previous_source_image_reference": source,
                  "image_id": info["Id"], "context": str(context), "validation": "disposable Docker, network none"}
        results.append(result)
        (args.output_dir / "overlays.json").write_text(json.dumps({"cases": results}, indent=2) + "\n")
        print(json.dumps({"case_id": case, "source_image_reference": digest}), flush=True)
    if args.workload:
        workload = json.loads(args.workload.read_text())
        replacements = {row["case_id"]: row for row in results}
        found = set()
        for task in workload["tasks"]:
            replacement = replacements.get(task["case_id"])
            if replacement is None:
                continue
            if task["sandbox"]["source_image_reference"] != replacement["previous_source_image_reference"]:
                raise ValueError(f"{task['case_id']}: workload source differs from the manifest")
            image = replacement["source_image_reference"]
            task["sandbox"]["source_image_reference"] = image
            task["sandbox"]["image_digest"] = image.split("@", 1)[1]
            found.add(task["case_id"])
        if found != set(replacements):
            raise ValueError("workload does not contain every manifest case")
        (args.output_dir / "workload-for-registration.json").write_text(json.dumps(workload, indent=2) + "\n")


if __name__ == "__main__":
    main()
