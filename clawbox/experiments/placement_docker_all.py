"""Run every placement in a frozen five-round Docker experiment.

Run this on the experiment host after placing placement_docker.py and plan.json
in the same directory (or set PLACEMENT_FORMAL_ROOT).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from . import placement_docker as runner
except ImportError:  # copied beside placement_docker.py on the remote host
    import placement_docker as runner


def main() -> None:
    root = Path(os.environ.get("PLACEMENT_FORMAL_ROOT", str(runner.ROOT)))
    rows = []
    for round_id, order in enumerate(runner.PLAN["schedule"], 1):
        for placement in order:
            existing = sorted(root.glob(f"r{round_id}-{placement}-*/trial.json"))
            if existing:
                rows.append(json.loads(existing[-1].read_text()))
                continue
            last_error: Exception | None = None
            for attempt in range(1, 4):
                if (root / f"r{round_id}-{placement}-{attempt}").exists():
                    continue
                try:
                    row = runner.run_trial(round_id, placement, attempt)
                    rows.append(row)
                    print(json.dumps({
                        "round": round_id,
                        "placement": placement,
                        "attempt": attempt,
                        "makespan_s": round(row["makespan_s"], 3),
                    }), flush=True)
                    break
                except Exception as error:  # retry only a failed fresh trial
                    last_error = error
                    print(f"failed {round_id} {placement} attempt {attempt}: {error}", flush=True)
            else:
                raise RuntimeError(f"no verified trial for {round_id} {placement}") from last_error
    rows.sort(key=lambda row: (row["round"], row["placement"]))
    (root / "rounds.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
