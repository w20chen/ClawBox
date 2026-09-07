#!/usr/bin/env python3
"""Capture new request metadata without changing a v4 trace's recorded workload.

The prepare output is a diagnostic capture input, NOT a strict replay gate.
Finalize requires all original responses to have been delivered exactly once.
The resulting reference must pass a separate strict replay and task validation.
"""
import argparse
import base64
import copy
import json
from pathlib import Path

import yaml

from clawbox.replay.model_gateway import _replay_response, _response_message
from clawbox.replay.trace import load_trace


def read_original(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows or any(row.get("type") != "action" or row.get("action_type") != "llm_call" for row in rows):
        raise ValueError("this metadata-capture helper requires an LLM-only v4 trace")
    return rows


def write_rows(path, rows):
    if path.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "finalize"])
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--gateway", type=Path)
    args = parser.parse_args()
    original = read_original(args.original)
    rows = copy.deepcopy(original)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.phase == "prepare":
        if args.spec is None:
            parser.error("prepare requires --spec")
        for row in rows:
            row["data"].pop("raw_request", None)
            row["data"].pop("messages_in", None)
        capture = args.output_dir / "rec-a-request-capture.jsonl"
        write_rows(capture, rows)
        spec = yaml.safe_load(args.spec.read_text())
        if spec["execution"]["concurrency_levels"] != [1]:
            raise ValueError("capture is only supported at c1")
        spec["experiment_id"] = "rec-a-request-reference-capture"
        spec["workload"]["input"] = str(capture)
        for case in spec["workload"]["cases"]:
            case["replay_trace_reference"] = str(capture)
            case["source_reference"] = str(capture)
        output_spec = args.output_dir / "capture-c1.yaml"
        if output_spec.exists():
            raise FileExistsError(output_spec)
        output_spec.write_text(yaml.safe_dump(spec, sort_keys=False))
        print(f"Diagnostic capture spec: {output_spec}. Strict completeness will remain false.")
        return
    if args.gateway is None:
        parser.error("finalize requires --gateway")
    records = sorted(json.loads(args.gateway.read_text()), key=lambda row: row["replay_index"])
    actions = [action for action in load_trace(args.original) if action.kind == "llm"]
    if [record["replay_index"] for record in records] != list(range(len(actions))):
        raise ValueError("capture must contain every original model step exactly once")
    by_id = {row["action_id"]: row for row in rows}
    for action, record in zip(actions, records):
        if (not record["ready"] or not record["delivered"] or record["error"]
                or record["status_code"] != 200):
            raise ValueError(f"undelivered/failed model step: {action.action_id}")
        _, content_type, body = _replay_response(action, False)
        if _response_message(content_type, body) != _response_message(
                record["content_type"], base64.b64decode(record["response_b64"])):
            raise ValueError(f"recorded response changed: {action.action_id}")
        row = by_id[action.action_id]
        row["data"].pop("messages_in", None)
        row["data"]["raw_request"] = record["request_payload"]
    for before, after in zip(original, rows):
        before, after = copy.deepcopy(before), copy.deepcopy(after)
        for value in (before, after):
            value["data"].pop("raw_request", None)
            value["data"].pop("messages_in", None)
        if before != after:
            raise ValueError("non-request trace fields changed")
    reference = args.output_dir / "rec-a-healthy-reference.jsonl"
    write_rows(reference, rows)
    report = {"original": str(args.original), "gateway": str(args.gateway),
              "reference": str(reference), "model_steps": len(records),
              "changed_fields_only": ["data.raw_request", "data.messages_in"],
              "responses_commands_timestamps_and_waits_preserved": True,
              "strict_validation_required": True}
    (args.output_dir / "reference-provenance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
