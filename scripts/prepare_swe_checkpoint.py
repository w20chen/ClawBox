"""Replay a SWE-ReBench edit/write prefix and commit a fresh pre-tool image."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path


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


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--checkpoint-tag", required=True)
    parser.add_argument("--cpus", default="8,10,12,14")
    parser.add_argument("--mems", default="0")
    parser.add_argument("--memory", default="6g")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    target = next(item for item in plan["targets"] if item["name"] == args.candidate)
    container = f"placement-prep-{args.candidate}-{int(time.time())}"
    prefix_log = []
    try:
        run([
            "docker", "run", "--platform", "linux/amd64", "-d", "--name", container,
            "--network", "none", "--cpuset-cpus", args.cpus, "--cpuset-mems", args.mems,
            "--memory", args.memory, "--memory-swap", args.memory,
            target["source_image"], "sleep", "infinity",
        ])
        run(["docker", "exec", container, "bash", "-lc", "if [ -d /testbed ] && [ ! -e /workspace ]; then ln -s /testbed /workspace; fi"])
        for index, item in enumerate(target["prefix"]):
            kind = item["kind"]
            if kind == "exec":
                result = run(["docker", "exec", "-w", target.get("workdir", "/workspace"), container, "bash", "-lc", item["command"]], timeout=1200, check=False)
            elif kind == "write":
                path = item["path"]
                content = b64(item.get("content", ""))
                script = (
                    "import base64, pathlib, os; "
                    f"p=pathlib.Path({path!r}); p.parent.mkdir(parents=True, exist_ok=True); "
                    f"p.write_bytes(base64.b64decode({content!r}))"
                )
                result = run(["docker", "exec", "-w", target.get("workdir", "/workspace"), container, "python3", "-c", script], check=False)
            elif kind == "edit":
                path = item["path"]
                script_lines = [
                    "from pathlib import Path",
                    f"p=Path({path!r})",
                    "s=p.read_text()",
                ]
                for edit in item["edits"]:
                    old = b64(edit["oldText"])
                    new = b64(edit["newText"])
                    script_lines.append(
                        f"old=__import__('base64').b64decode({old!r}).decode(); new=__import__('base64').b64decode({new!r}).decode(); "
                        "assert old in s, 'oldText not found'; s=s.replace(old,new,1)"
                    )
                script_lines.append("p.write_text(s)")
                result = run(["docker", "exec", "-w", target.get("workdir", "/workspace"), container, "python3", "-c", "; ".join(script_lines)], check=False)
            elif kind == "patch":
                patch_data = b64(item["input"])
                script = f"""
import base64
from pathlib import Path

lines = base64.b64decode({patch_data!r}).decode().splitlines(keepends=True)
i = 1 if lines and lines[0].startswith('*** Begin Patch') else 0
while i < len(lines):
    line = lines[i]
    if line.startswith('*** Update File: '):
        path = Path(line.split(': ', 1)[1].rstrip('\\n'))
        text = path.read_text()
        i += 1
        while i < len(lines) and not lines[i].startswith('*** '):
            if not lines[i].startswith('@@'):
                i += 1
                continue
            i += 1
            old, new = [], []
            while i < len(lines) and not lines[i].startswith('@@') and not lines[i].startswith('*** '):
                h = lines[i]
                if h.startswith(' ') or h.startswith('-'):
                    old.append(h[1:])
                if h.startswith(' ') or h.startswith('+'):
                    new.append(h[1:])
                i += 1
            old_text, new_text = ''.join(old), ''.join(new)
            assert old_text in text, 'patch context not found: ' + str(path)
            text = text.replace(old_text, new_text, 1)
        path.write_text(text)
        continue
    if line.startswith('*** Add File: '):
        path = Path(line.split(': ', 1)[1].rstrip('\\n'))
        i += 1
        added = []
        while i < len(lines) and not lines[i].startswith('*** '):
            if lines[i].startswith('+'):
                added.append(lines[i][1:])
            i += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(added))
        continue
    i += 1
"""
                result = run(["docker", "exec", "-w", target.get("workdir", "/workspace"), container, "python3", "-c", script], check=False)
            else:
                raise RuntimeError(f"unsupported prefix kind: {kind}")
            prefix_log.append({
                "index": index,
                "sequence_no": item["sequence_no"],
                "kind": kind,
                "returncode": result.returncode,
            })

        run(["docker", "commit", container, args.checkpoint_tag], timeout=900)
        check = run([
            "docker", "run", "--platform", "linux/amd64", "--rm", "--network", "none",
            "--cpuset-cpus", args.cpus, "--cpuset-mems", args.mems,
            "--memory", args.memory, "--memory-swap", args.memory,
            "-w", target.get("workdir", "/workspace"), args.checkpoint_tag,
            "bash", "-lc", target["command"],
        ], timeout=900, check=False)
        print(json.dumps({
            "candidate": args.candidate,
            "checkpoint": args.checkpoint_tag,
            "target_returncode": check.returncode,
            "target_stdout_tail": check.stdout[-1000:],
            "prefix": prefix_log,
        }, indent=2, ensure_ascii=False))
        if check.returncode != target["expected_exit_code"]:
            raise RuntimeError(f"target exit mismatch: {check.returncode} != {target['expected_exit_code']}")
    finally:
        run(["docker", "rm", "-f", container], check=False)


if __name__ == "__main__":
    main()
