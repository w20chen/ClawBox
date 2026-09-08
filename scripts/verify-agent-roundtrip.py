#!/usr/bin/env python3
"""Run live OpenClaw agents, then replay their untouched ClawTune recordings."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clawbox.experiments.baselines import BASELINES
from clawbox.replay.trace import find_recordings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Task and installed CubeSandbox templates")
    parser.add_argument("--clawtune-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New result directory")
    parser.add_argument("--estimate", choices=("fixed", "capacity"), action="append",
                        help="Limit the verification matrix to these reservation estimates")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    base = yaml.safe_load(args.spec.read_text())
    if len(base["workload"].get("cases", [])) != 1:
        parser.error("supply exactly one task; concurrency replicates that task")
    base["agent"] = {"driver": "openclaw"}
    base["inference"]["configuration"]["clawtune_config"] = str(args.clawtune_config.resolve())
    base["workload"]["repetitions"] = 1
    base["resources"]["full_tool_memory_mib"] = base["sandbox"].get("memory_mib", 4096)
    outcomes = []

    def run(spec, name):
        path = root / (name + ".yaml")
        path.write_text(yaml.safe_dump(spec, sort_keys=False))
        print(f"Starting {name}", flush=True)
        with (root / (name + ".log")).open("w") as log:
            result = subprocess.run([sys.executable, "-m", "clawbox.cli", "--output-root", str(root),
                                     "experiment", "run", str(path), "--run-id", name],
                                    stdout=log, stderr=subprocess.STDOUT)
        arms = [json.loads(p.read_text()) for p in (root / name / "arms").glob("*.json")]
        passed = result.returncode == 0 and len(arms) == 1 and arms[0]["status"] == "succeeded"
        outcomes.append({"run": name, "passed": passed, "exit_code": result.returncode})
        (root / "verification.json").write_text(json.dumps(outcomes, indent=2) + "\n")
        print(f"{name}: {'PASS' if passed else 'FAIL'}", flush=True)
        return passed

    baselines = [name for name in ("tool-static-resident", "tool-static-eager-reactive", "tool-full-resident")
                 if not args.estimate or ("capacity" if name == "tool-full-resident" else "fixed") in args.estimate]
    for baseline in baselines:
        for concurrency in (1, 4):
            live = copy.deepcopy(base)
            live["policies"] = [BASELINES[baseline].as_policy().model_dump(mode="json")]
            live["execution"]["concurrency_levels"] = [concurrency]
            live["execution"]["randomized_order"] = False
            live["workload"]["session_assignment"] = "single_case"
            live["inference"]["backend"] = "api"
            live_name = f"{baseline}-c{concurrency}-live"
            if not run(live, live_name):
                continue
            traces = []
            for directory in sorted((root / live_name / "runtime-traces").iterdir()):
                candidates = find_recordings(directory.rglob("*.jsonl"), directory.name)
                if len(candidates) != 1:
                    raise RuntimeError(f"Expected one native agent recording in {directory}")
                traces.extend(candidates)
            if len(traces) != concurrency:
                raise RuntimeError(f"Expected {concurrency} native recordings, found {len(traces)}")
            replay = copy.deepcopy(live)
            replay["inference"]["backend"] = "replay"
            replay["inference"]["configuration"]["time_scale"] = 1.0
            replay["workload"]["source"] = "recorded_trace"
            replay["workload"]["input"] = str(traces[0])
            replay["workload"]["session_assignment"] = "round_robin" if concurrency > 1 else "single_case"
            task = live["workload"]["cases"][0]
            replay["workload"]["cases"] = [dict(task, case_id=f"{task['case_id']}-{index}",
                source="recorded_trace", source_reference=str(trace), replay_trace_reference=str(trace))
                for index, trace in enumerate(traces)]
            run(replay, f"{baseline}-c{concurrency}-replay")
    if len(outcomes) != len(baselines) * 4 or not all(item["passed"] for item in outcomes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
