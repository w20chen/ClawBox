"""Export ClawTune as a consistent Docker build context."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import shutil
import zipfile
from pathlib import Path


def prepare(root: Path, output: Path, *, working_tree: bool = False) -> str:
    if output.exists():
        raise FileExistsError(f"choose a new output directory: {output}")
    if working_tree:
        names = subprocess.check_output([
            "git", "-C", str(root), "ls-files", "--cached", "--others",
            "--exclude-standard", "-z",
        ]).split(b"\0")
        digest = hashlib.sha256()
        output.mkdir(parents=True)
        for raw_name in sorted(name for name in names if name):
            name = raw_name.decode("utf-8")
            source = (root / name).resolve()
            destination = (output / name).resolve()
            if not source.is_relative_to(root) or not destination.is_relative_to(output):
                raise ValueError(f"unsafe working-tree path: {name}")
            if not source.is_file():
                continue
            data = source.read_bytes()
            digest.update(raw_name + b"\0" + hashlib.sha256(data).digest())
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        head_revision = subprocess.check_output([
            "git", "-C", str(root), "rev-parse", "HEAD",
        ], text=True).strip()
        # Keep the public revision shape compatible with ClawBox provenance
        # (a 40-hex revision) while changing whenever tracked or untracked
        # source content changes.
        revision = hashlib.sha1(
            f"{head_revision}:{digest.hexdigest()}".encode("ascii")
        ).hexdigest()
        (output / "CLAWTUNE_REVISION").write_text(revision + "\n", encoding="ascii")
        return revision
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
    parser.add_argument("--working-tree", action="store_true")
    args = parser.parse_args()
    revision = prepare(args.clawtune_root.resolve(), args.output.resolve(),
                       working_tree=args.working_tree)
    print(json.dumps({"source": "working-tree" if args.working_tree else "main",
                      "revision": revision, "context": str(args.output.resolve())}))
