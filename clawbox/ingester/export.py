from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath


def _request(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def export_traces(base_url: str, token: str, task_id: str, output_root: Path) -> list[Path]:
    encoded_task = urllib.parse.quote(task_id, safe="")
    archive_url = f"{base_url.rstrip('/')}/v1/archive/{encoded_task}"
    manifest_data = _request(f"{archive_url}/traces", token)
    manifest = json.loads(manifest_data)
    paths = manifest.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("trace manifest does not contain a paths object")

    output_root.mkdir(parents=True, exist_ok=True)
    resolved_root = output_root.resolve()
    _atomic_write(output_root / "manifest.json", manifest_data)
    exported: list[Path] = []
    for relative, metadata in sorted(paths.items()):
        if not isinstance(relative, str):
            raise ValueError("trace manifest contains a non-string path")
        parts = PurePosixPath(relative).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"unsafe trace path: {relative!r}")
        output = output_root.joinpath(*parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.resolve(strict=False).is_relative_to(resolved_root):
            raise ValueError(f"trace path escapes output directory: {relative!r}")
        encoded_path = urllib.parse.quote(relative, safe="/")
        try:
            data = _request(f"{archive_url}/traces/{encoded_path}", token)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            print(
                f"warning: skipped incomplete archived trace file: {relative}",
                file=sys.stderr,
            )
            continue
        expected = int(metadata["bytes"])
        if len(data) != expected:
            raise RuntimeError(
                f"trace size mismatch for {relative}: expected {expected}, got {len(data)}"
            )
        _atomic_write(output, data)
        exported.append(output)
    return exported


def main() -> None:
    parser = argparse.ArgumentParser(description="Export all archived task trace files")
    parser.add_argument("task_id", help="task archive identifier")
    parser.add_argument("--output", type=Path, required=True, help="destination directory")
    args = parser.parse_args()
    base_url = os.environ.get("CLAWBOX_ARCHIVE_URL", "")
    token = os.environ.get("CLAWBOX_ARCHIVE_TOKEN", "")
    if not base_url or not token:
        parser.error("CLAWBOX_ARCHIVE_URL and CLAWBOX_ARCHIVE_TOKEN are required")
    exported = export_traces(base_url, token, args.task_id, args.output)
    if not exported:
        raise SystemExit("trace archive contains no files")
    for path in exported:
        print(path)
    print(f"Exported {len(exported)} trace file(s) to {args.output}")


if __name__ == "__main__":
    main()
