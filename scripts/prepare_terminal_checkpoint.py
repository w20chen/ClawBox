"""Prepare and verify a fresh pre-tool Docker checkpoint on the experiment host."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time


def run(cmd: list[str], *, timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--checkpoint-tag", required=True)
    parser.add_argument("--cpus", default="8,10,12,14")
    parser.add_argument("--mems", default="0")
    parser.add_argument("--memory", default="6g")
    args = parser.parse_args()

    plan = json.load(open(args.plan, encoding="utf-8"))
    target = next(item for item in plan["targets"] if item["name"] == args.candidate)
    container = f"placement-prep-{args.candidate}-{int(time.time())}"
    image = target["base_image"]
    checkpoint = args.checkpoint_tag
    prefix_log = []
    try:
        run(
            [
                "docker",
                "run",
                "--platform",
                "linux/amd64",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "--cpuset-cpus",
                args.cpus,
                "--cpuset-mems",
                args.mems,
                "--memory",
                args.memory,
                "--memory-swap",
                args.memory,
                image,
                "sleep",
                "infinity",
            ]
        )
        for index, item in enumerate(target["prefix"]):
            command = item["command"]
            result = run(
                ["docker", "exec", container, "bash", "-lc", command],
                timeout=1200,
                check=False,
            )
            prefix_log.append(
                {
                    "index": index,
                    "sequence_no": item["sequence_no"],
                    "span_id": item["span_id"],
                    "returncode": result.returncode,
                    "command": command,
                }
            )

        run(["docker", "commit", container, checkpoint], timeout=900)
        check = run(
            [
                "docker",
                "run",
                "--platform",
                "linux/amd64",
                "--rm",
                "--network",
                "none",
                "--cpuset-cpus",
                args.cpus,
                "--cpuset-mems",
                args.mems,
                "--memory",
                args.memory,
                "--memory-swap",
                args.memory,
                "-w",
                target["workdir"],
                checkpoint,
                "bash",
                "-lc",
                target["command"],
            ],
            timeout=900,
            check=False,
        )
        actual_hash = hashlib.sha256(check.stdout.encode()).hexdigest()
        if check.returncode != target["expected_exit_code"] or actual_hash != target["expected_stdout_sha256"]:
            raise RuntimeError(
                "checkpoint target verification failed: "
                f"returncode={check.returncode} expected={target['expected_exit_code']} "
                f"stdout_sha256={actual_hash} expected={target['expected_stdout_sha256']}"
            )
        print(json.dumps({"candidate": args.candidate, "checkpoint": checkpoint, "prefix": prefix_log}, indent=2))
    finally:
        run(["docker", "rm", "-f", container], check=False)


if __name__ == "__main__":
    main()
