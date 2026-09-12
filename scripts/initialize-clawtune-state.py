"""Initialize or resume one Runtime's native ClawTune working knowledge base."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def initialize(path: Path, owner: str, seed: Path | None = None) -> None:
    from clawtune_kb import StateStore, initialize_state
    from clawtune_kb.contracts import data_root

    if (path / "state.json").is_file():
        with StateStore(path):
            state = json.loads((path / "state.json").read_text(encoding="utf-8"))
            if state["owner"] != owner:
                raise ValueError("ClawTune working state belongs to another Runtime")
        return
    if path.exists():
        # Only remove an empty directory created by guest setup.
        path.rmdir()
    selected_seed = seed or (Path(os.environ["CLAWTUNE_COLD_START_DIR"])
                             if os.getenv("CLAWTUNE_COLD_START_DIR") else data_root() / "seeds/bootstrap-v1")
    initialize_state(path, selected_seed, owner=owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--seed", type=Path)
    args = parser.parse_args()
    initialize(args.state, args.owner, args.seed)
