"""Extract exact SWE-ReBench candidate calls and replay prefixes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


CALLS = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\placement-first-pass\calls.json")
OUT = Path(r"C:\Users\29068\Desktop\ClawBox\.artifacts\llc-placement-discovery-20260914\extra_candidate_plans.json")

TARGETS = [
    {
        "name": "scim2-flake8",
        "task_id": "d5b3ab7233dd9f436750",
        "sequence_no": 32,
        "source_image": "swerebench/sweb.eval.x86_64.15five_1776_scim2-filter-parser-13:latest",
        "checkpoint_image": "placement-replica-scim2-before-32:20260914",
        "checkpoint_image_id": "sha256:3f9eeb31deb3b921ee59faa9ba0d839468fb7d9eb0a6c09563ed1767ba76518e",
        "workdir": "/workspace",
    },
    {
        "name": "mbed-tools-parse-url",
        "task_id": "d74471b12c62091c0318",
        "sequence_no": 30,
        "source_image": "swerebench/sweb.eval.x86_64.armmbed_1776_mbed-tools-190:latest",
        "workdir": "/workspace",
    },
]


def output_for(call: dict) -> str:
    for line in Path(call["trace_path"]).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") != "span_end" or row.get("span_id") != call["span_id"]:
            continue
        content = row.get("output", {}).get("result", {}).get("content", [])
        for item in content:
            if item.get("type") != "text":
                continue
            try:
                payload = json.loads(item.get("text", ""))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and "stdout" in payload:
                return payload["stdout"]
    raise RuntimeError(call["span_id"])


def prefix_for(calls: list[dict], task_id: str, seq: int) -> list[dict]:
    rows = []
    for call in sorted((x for x in calls if x["task_id"] == task_id and x["sequence_no"] < seq), key=lambda x: x["sequence_no"]):
        args = call.get("requested_args")
        if not isinstance(args, dict):
            continue
        if args.get("command"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "exec", "command": args["command"]})
        elif args.get("edits"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "edit", "path": args.get("path"), "edits": args["edits"]})
        elif args.get("input", "").lstrip().startswith("*** Begin Patch"):
            rows.append({"sequence_no": call["sequence_no"], "kind": "patch", "input": args["input"]})
        elif args.get("content") is not None:
            rows.append({"sequence_no": call["sequence_no"], "kind": "write", "path": args.get("path"), "content": args["content"]})
    return rows


def main() -> None:
    calls = json.loads(CALLS.read_text(encoding="utf-8"))
    by_key = {(x["task_id"], x["sequence_no"]): x for x in calls}
    targets = []
    for spec in TARGETS:
        call = by_key[(spec["task_id"], spec["sequence_no"])]
        stdout = output_for(call)
        target = {
            "name": spec["name"],
            "trace": call["trace_id"],
            "span": call["span_id"],
            "task_id": spec["task_id"],
            "sequence_no": spec["sequence_no"],
            "repo": call["repo"],
            "duration_s": call["duration_s"],
            "llc_read_M_per_CPU_s": None,
            "source_image": spec["source_image"],
            "checkpoint_image": spec.get("checkpoint_image"),
            "checkpoint_image_id": spec.get("checkpoint_image_id"),
            "workdir": spec["workdir"],
            "command": call["command"],
            "expected_exit_code": call["exit_code"] if call["exit_code"] is not None else 0,
            "expected_stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "expected_stdout_tail": stdout[-500:],
            "prefix": prefix_for(calls, spec["task_id"], spec["sequence_no"]),
        }
        targets.append(target)
    OUT.write_text(json.dumps({"schema": "llc-placement-extra-candidates-v1", "targets": targets}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for target in targets:
        print(target["name"], target["duration_s"], len(target["prefix"]), target["expected_stdout_sha256"])


if __name__ == "__main__":
    main()
