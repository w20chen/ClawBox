#!/usr/bin/env python3
import collections
import json
from pathlib import Path
import re
import subprocess


ROOT = Path("/home/weitianc/clawbox-swe-images-20261021")
collectors = json.loads((ROOT / "build/collector-state.json").read_text())
image_ids = sorted({value["image_id"] for value in collectors.values()})
images = json.loads(subprocess.check_output(["docker", "image", "inspect", *image_ids], text=True))
by_id = {image["Id"]: image for image in images}


def env_map(entries):
    result = {}
    for entry in entries or []:
        key, _, value = entry.partition("=")
        result[key] = value
    return result


missing_source_env = collections.Counter()
changed_source_env = collections.Counter()
missing_source_env_tasks = collections.defaultdict(list)
changed_source_env_tasks = collections.defaultdict(list)
source_env_keys = collections.Counter()
current_extra_env_keys = collections.Counter()
source_workdirs = collections.Counter()
current_workdirs = collections.Counter()
source_users = collections.Counter()
current_users = collections.Counter()
current_entrypoints = collections.Counter()

for task, collector in collectors.items():
    safe = re.sub(r"[^a-z0-9_.-]+", "-", task.lower())
    prepared = json.loads((ROOT / "workspaces" / safe / "prepared.json").read_text())
    source = prepared["source_config"]
    current = by_id[collector["image_id"]]["Config"]
    old_env = env_map(source.get("Env"))
    new_env = env_map(current.get("Env"))
    source_env_keys.update(old_env.keys())
    current_extra_env_keys.update(set(new_env) - set(old_env))
    for key, value in old_env.items():
        expected = value.replace("x86_64", "arm64").replace("amd64", "arm64")
        if key not in new_env:
            missing_source_env[key] += 1
            missing_source_env_tasks[key].append({"task": task, "source": value})
        elif new_env[key] != expected:
            changed_source_env[(key, value, new_env[key])] += 1
            changed_source_env_tasks[key].append(task)
    source_workdirs[source.get("WorkingDir") or "<empty>"] += 1
    current_workdirs[current.get("WorkingDir") or "<empty>"] += 1
    source_users[source.get("User") or "<default-root>"] += 1
    current_users[current.get("User") or "<default-root>"] += 1
    current_entrypoints[str(current.get("Entrypoint"))] += 1

summary = {
    "cases": len(collectors),
    "distinct_collector_images": len(image_ids),
    "source_env_keys": dict(source_env_keys),
    "missing_source_env_keys": dict(missing_source_env),
    "missing_source_env_examples": {
        key: values[:5] for key, values in missing_source_env_tasks.items()
    },
    "changed_source_env_values": [
        {"key": key[0], "source": key[1], "current": key[2], "cases": count}
        for key, count in changed_source_env.items()
    ],
    "changed_source_env_task_examples": {
        key: values[:5] for key, values in changed_source_env_tasks.items()
    },
    "current_extra_env_keys": dict(current_extra_env_keys),
    "source_workdirs": dict(source_workdirs),
    "current_workdirs": dict(current_workdirs),
    "source_users": dict(source_users),
    "current_users": dict(current_users),
    "current_entrypoints": dict(current_entrypoints),
}
print(json.dumps(summary, indent=2, sort_keys=True))
