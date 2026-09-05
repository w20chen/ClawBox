#!/usr/bin/env python3
"""Audit all checked-in schema-v2 experiment matrices without starting sandboxes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the checked-out package importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clawbox.experiments.audit import audit_experiments


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="experiment YAML files; defaults to examples/experiments/*.yaml",
    )
    args = parser.parse_args()
    paths = args.paths or sorted(Path("examples/experiments").glob("*.yaml"))
    try:
        result = {"valid": True, "experiments": audit_experiments(paths)}
    except (OSError, ValueError) as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
