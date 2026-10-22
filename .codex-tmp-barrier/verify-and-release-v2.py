#!/usr/bin/env python3
"""Verify simultaneous ClawBox acceptance state, then release barrier v2."""

import collections
import hashlib
import json
import os
import pathlib
import shlex
import sys
import time
import urllib.request


GOAL = pathlib.Path("/data/clawbox-acceptance-20261022T102744Z")
run = pathlib.Path(json.loads((GOAL / "current-run.json").read_text())["root"])
barrier = json.loads((GOAL / "current-barrier-v2.json").read_text())
target = int(barrier["target"])
state = json.loads((run / "results" / run.name / "run-state.json").read_text())
assert state["state"] == "running", state["state"]
assert len(state["arms"]) == 1
arm_id, arm = next(iter(state["arms"].items()))
expected = {f"{arm_id}-{index:04d}" for index in range(target)}
events = next((run / "results" / run.name).glob("attempts/*/*/events/*.jsonl"))
live: dict[tuple[str, str], str] = {}
verified: dict[str, dict] = {}
native: dict[str, dict] = {}
counts: collections.Counter = collections.Counter()
for line in events.open():
    try:
        event = json.loads(line)
    except ValueError:
        continue
    name = event.get("event")
    session_id = event.get("session_id")
    counts[name] += 1
    assert name not in ("session_failed", "session_error"), event
    if name == "sandbox_created":
        live[session_id, event["role"]] = event["sandbox_id"]
    elif name == "sandbox_destroyed":
        live.pop((session_id, event["role"]), None)
    elif name == "task_environment_checked":
        assert event["exit_code"] == 0
        value = json.loads(event["stdout"])
        assert value["environment_verified"] and value["arch"] == "aarch64"
        verified[session_id] = value
    elif name == "native_tool_artifacts_collected":
        value = event["validation"]
        assert value["valid"] and value["exact_id_join_rate"] == 1.0
        assert value["telemetry_loss_total"] == value["wrong_session_routing"] == 0
        assert value["duplicate_tool_execution_count"] == 0
        native[session_id] = value

headers = {"Authorization": barrier["key"], "Content-Type": "application/json"}
health_request = urllib.request.Request(
    f"http://127.0.0.1:{barrier['port']}/health", headers=headers
)
health = json.load(urllib.request.urlopen(health_request, timeout=15))
assert health == {"arrived": target, "released": False, "target": target}, health
barrier_root = pathlib.Path(barrier["root"])
arrivals = [json.loads(line) for line in (barrier_root / "arrivals.jsonl").read_text().splitlines()]
assert len(arrivals) == target
assert {value["session_id"] for value in arrivals} == expected
assert set(verified) == set(native) == expected
assert len(live) == len(set(live.values())) == 2 * target
assert set(live) == {
    (session_id, role)
    for session_id in expected
    for role in ("runtime", "tool")
}
assert len({value["isolation_token"] for value in verified.values()}) == target
assert all(
    value["token"] == verified[value["session_id"]]["isolation_token"]
    for value in arrivals
)
assert counts["session_complete"] == counts["sandbox_destroyed"] == 0

for line in pathlib.Path("/home/weitianc/.config/clawbox/machine.env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.removeprefix("export ").split("=", 1)
        parts = shlex.split(value)
        if key.strip().replace("_", "").isalnum():
            os.environ[key.strip()] = parts[0] if parts else ""

from cubesandbox import Config, Sandbox

items = [
    item
    for item in Sandbox.list_v2(config=Config(request_timeout=130))
    if (item.get("metadata") or {}).get("clawbox.task-uid") == arm["task_uid"]
]
assert len(items) == 2 * target
assert all(item["state"] == "running" for item in items)
assert {item["sandboxID"] for item in items} == set(live.values())
assert {item["metadata"]["clawbox.session"] for item in items} == {
    session_id + "-" + role
    for session_id in expected
    for role in ("runtime", "tool")
}

sys.path.insert(0, str(run / "source"))
from clawbox.experiments.spec import expand_matrix, load_experiment
from clawbox.experiments.worker import session_case_for

spec = load_experiment(run / "experiment.yaml")
arms = expand_matrix(spec)
assert len(arms) == 1 and arms[0].arm_id == arm_id
cases = [session_case_for(arms[0], index) for index in range(target)]
by_id = {item["sandboxID"]: item for item in items}
for index, case in enumerate(cases):
    session_id = f"{arm_id}-{index:04d}"
    assert by_id[live[session_id, "tool"]]["templateID"] == case.sandbox.template_id
unique_cases = len({case.case_id for case in cases})
assert unique_cases == min(503, target)

cgroup = pathlib.Path("/sys/fs/cgroup/cube_sandbox/sandbox")
memory_events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
assert int(memory_events["oom"]) == int(memory_events["oom_kill"]) == 0
receipt = {
    "verified_at": time.time(),
    "run_id": run.name,
    "arm_id": arm_id,
    "live_running_vms": 2 * target,
    "simultaneous_verified_task_pairs": target,
    "unique_environment_cases": unique_cases,
    "unique_writable_markers": target,
    "native_first_rounds_validated": target,
    "new_session_failures": 0,
    "telemetry_loss_total": 0,
    "wrong_session_routing": 0,
    "barrier": health,
    "memory_events": memory_events,
    "memory_current_bytes": int((cgroup / "memory.current").read_text()),
    "events": str(events),
    "event_bytes_observed": events.stat().st_size,
    "workload_sha256": hashlib.sha256((run / "workload.json").read_bytes()).hexdigest(),
    "sdk_inventory": [
        {key: item[key] for key in ("sandboxID", "templateID", "state", "metadata")}
        for item in items
    ],
    "case_counts": dict(collections.Counter(case.case_id for case in cases)),
}
receipt_path = run / ("concurrency-barrier-v2-receipt-" + str(time.time_ns()) + ".json")
receipt_path.write_text(json.dumps(receipt, indent=2))
(run / "verified-barrier-v2-receipt.txt").write_text(str(receipt_path))

release_request = urllib.request.Request(
    f"http://127.0.0.1:{barrier['port']}/release",
    data=json.dumps({"receipt": str(receipt_path)}).encode(),
    headers=headers,
)
release = json.load(urllib.request.urlopen(release_request, timeout=15))
assert release == {"arrived": target, "released": True, "target": target}, release
(run / "barrier-v2-release-response.json").write_text(json.dumps(release, indent=2))
print(json.dumps({
    "receipt": str(receipt_path),
    "target": target,
    "live_running_vms": 2 * target,
    "unique_environment_cases": unique_cases,
    "release": release,
}))
