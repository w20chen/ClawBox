#!/usr/bin/env python3
"""Compare recorded and live tool messages without printing their contents.

Run from an installed ClawBox checkout. Exit 0 requires every model request
and every recorded tool result to be observed and canonically equivalent.
This audit does not replace final task validation or native telemetry joins.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from clawbox.replay.model_gateway import _canonical_replay_input
from clawbox.replay.trace import load_trace


def audit(trace: Path, gateway: Path) -> dict:
    actions = [action for action in load_trace(trace) if action.kind == "llm"]
    records = json.loads(gateway.read_text())
    observed = {record["replay_index"]: record for record in records}
    results = {}
    request_differences = []
    for index, action in enumerate(actions):
        expected = action.input
        if not isinstance(expected, dict) or not isinstance(expected.get("messages"), list):
            raise ValueError(f"model step {index} lacks recorded request messages")
        record = observed.get(index)
        actual = record["request_payload"] if record else {"messages": []}
        canonical_expected = _canonical_replay_input(expected)
        canonical_actual = _canonical_replay_input(actual)
        live_tools = {message["tool_call_id"]: message for message in actual["messages"]
                      if message.get("role") == "tool"}
        normalized_tools = {message["tool_call_id"]: message
                            for message in canonical_actual["messages"]
                            if message.get("role") == "tool"}
        normalized_expected = {message["tool_call_id"]: message
                               for message in canonical_expected["messages"]
                               if message.get("role") == "tool"}
        for message in expected["messages"]:
            if message.get("role") != "tool":
                continue
            call_id = message["tool_call_id"]
            status = "missing"
            if call_id in live_tools:
                if live_tools[call_id] == message:
                    status = "exact"
                elif normalized_tools[call_id] == normalized_expected[call_id]:
                    status = "normalized"
                else:
                    status = "different"
            previous = results.get(call_id)
            # Keep substantive mismatches visible across accumulated histories.
            if previous is None or (previous["status"] != "different" and status != "missing"):
                results[call_id] = {"tool_call_id": call_id, "status": status,
                                    "last_model_step": index}
        if record and record.get("replay_input_match") is not True:
            request_differences.append(index)
    counts = {status: sum(row["status"] == status for row in results.values())
              for status in ("exact", "normalized", "different", "missing")}
    complete = (len(observed) == len(actions) and not request_differences
                and counts["different"] == counts["missing"] == 0
                and all(record.get("delivered") for record in records))
    return {"complete": complete, "expected_model_steps": len(actions),
            "observed_model_steps": len(observed), "tool_output_counts": counts,
            "request_differences": request_differences,
            "tool_outputs": list(results.values()),
            "note": "Normalized is not byte-identical; raw requests remain in the gateway artifact. Final validation and eBPF joins are separate gates."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--gateway", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.trace, args.gateway)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "tool_outputs"}))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
