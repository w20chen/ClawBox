#!/usr/bin/env python3
"""Derive a c1/c4 gate without changing the formal trace, tools or policies."""
import argparse
from pathlib import Path
import yaml
from clawbox.experiments import ExperimentSpec

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, default=Path("examples/experiments/tiered-oracle-rec-a-c40.yaml"))
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--concurrency", type=int, choices=(1, 4), required=True)
parser.add_argument("--policy", action="append", required=True)
parser.add_argument("--local-mib", type=int, default=65536)
parser.add_argument("--warm-mib", type=int, default=5120)
parser.add_argument("--headroom-mib", type=int, default=8192)
args = parser.parse_args()
if args.output.exists():
    parser.error("output already exists; use a new gate name")
raw = yaml.safe_load(args.source.read_text())
policies = {item["name"]: item for item in raw["policies"]}
unknown = set(args.policy) - policies.keys()
if unknown:
    parser.error(f"unknown policies: {sorted(unknown)}")
raw["experiment_id"] = args.output.stem
raw["policies"] = [policies[name] for name in args.policy]
raw["execution"].update(concurrency_levels=[args.concurrency], randomized_order=False)
raw["resources"].update(
    local_memory_capacity_mib=args.local_mib, pool_memory_budget_mib=args.local_mib,
    warm_memory_capacity_mib=args.warm_mib,
    checkpoint_restore_headroom_mib=args.headroom_mib,
)
ExperimentSpec.model_validate(raw)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
print(args.output)
