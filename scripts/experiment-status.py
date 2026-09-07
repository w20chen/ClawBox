#!/usr/bin/env python3
"""Print compact progress/failure information from one experiment directory."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("directory", type=Path)
args = parser.parse_args()
root = args.directory
report = {"directory": str(root), "finished": (root / "summary.json").exists()}
if report["finished"]:
    summary = json.loads((root / "summary.json").read_text())
    report["arms"] = [{"policy": item["arm"]["policy"]["name"],
                       "concurrency": item["arm"]["concurrency"], "status": item["status"],
                       "correctness": item["correctness"]} for item in summary["arms"]]
report["sessions"] = []
for path in sorted((root / "model-gateway").glob("*.json")):
    if ".rejected-request-" in path.name:
        continue
    records = json.loads(path.read_text())
    report["sessions"].append({"session": path.stem, "model_steps": len(records),
                               "responses_delivered": sum(bool(row.get("delivered")) for row in records),
                               "rejections": [p.name for p in path.parent.glob(path.stem + ".rejected-request-*.json")]})
print(json.dumps(report, indent=2))
