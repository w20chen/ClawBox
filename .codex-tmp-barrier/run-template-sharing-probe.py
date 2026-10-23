#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path

from clawbox.experiments.qualification import template_base_sharing_test
from clawbox.experiments.spec import load_experiment, load_workload_cases


parser = argparse.ArgumentParser()
parser.add_argument("spec", type=Path)
parser.add_argument("artifact_root", type=Path)
parser.add_argument("--limit", type=int)
args = parser.parse_args()

spec = load_experiment(args.spec)
if args.limit is not None:
    cases = tuple(load_workload_cases(spec.workload)[:args.limit])
    spec = spec.model_copy(update={
        "workload": spec.workload.model_copy(update={"cases": cases}),
        "execution": spec.execution.model_copy(update={
            "sandbox_create_concurrency": min(
                spec.execution.sandbox_create_concurrency, len(cases)
            ),
        }),
    })

started = time.time()
result = template_base_sharing_test(spec, artifact_root=args.artifact_root)
print(json.dumps({
    "wall_seconds": time.time() - started,
    "result": result,
}, indent=2, sort_keys=True))
