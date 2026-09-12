"""Export the latest remote main as a clean, consistent Docker build context."""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import zipfile
from pathlib import Path


def prepare(root: Path, output: Path) -> str:
    if output.exists():
        raise FileExistsError(f"choose a new output directory: {output}")
    # Fetch without switching branches or modifying the operator's working tree.
    subprocess.run(["git", "-C", str(root), "fetch", "origin", "refs/heads/main"], check=True)
    revision = subprocess.check_output(["git", "-C", str(root), "rev-parse", "FETCH_HEAD"], text=True).strip()
    archive = subprocess.check_output(["git", "-C", str(root), "archive", "--format=zip", revision])
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        for name in bundle.namelist():
            if not (output / name).resolve().is_relative_to(output.resolve()):
                raise ValueError(f"unsafe archive path: {name}")
        output.mkdir(parents=True)
        bundle.extractall(output)
        for entry in bundle.infolist():
            mode = (entry.external_attr >> 16) & 0o777
            if mode:
                (output / entry.filename).chmod(mode)
    (output / "CLAWTUNE_REVISION").write_text(revision + "\n", encoding="ascii")
    return revision


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clawtune-root", type=Path, default=Path(__file__).resolve().parents[2] / "ClawTune")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    revision = prepare(args.clawtune_root.resolve(), args.output.resolve())
    print(json.dumps({"source_branch": "main", "revision": revision, "context": str(args.output.resolve())}))
