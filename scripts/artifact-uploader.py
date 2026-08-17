#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


TASK_ID = os.environ.get("TASK_ID", "")
TOKEN = os.environ.get("TRACE_UPLOAD_TOKEN", "")
BASE_URL = os.environ.get("TRACE_INGESTER_URL", "").rstrip("/")
STATE = Path(os.environ.get("CLAWBOX_STATE_DIR", "/state"))
TRACE = Path(os.environ.get("CLAWTUNE_TRACE_DIR", str(STATE / "traces")))
OFFSETS = STATE / ".upload-offsets.json"
LOCK = STATE / ".artifact-uploader.lock"
STOP = False


def request(path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    message = urllib.request.Request(
        f"{BASE_URL}{path}", data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(message, timeout=30) as response:
        return json.loads(response.read())


def load_offsets() -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in json.loads(OFFSETS.read_text(encoding="utf-8")).items()}
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def save_offsets(offsets: dict[str, int]) -> None:
    temporary = OFFSETS.with_suffix(".tmp")
    temporary.write_text(json.dumps(offsets, sort_keys=True), encoding="utf-8")
    os.replace(temporary, OFFSETS)


def upload_traces(*, final: bool) -> int:
    offsets = load_offsets()
    count = 0
    skipped = 0
    if TRACE.exists():
        for path in sorted(item for item in TRACE.rglob("*") if item.is_file()):
            relative = path.relative_to(TRACE).as_posix()
            offset = offsets.get(relative, 0)
            size = path.stat().st_size
            if offset > size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while data := stream.read(512 * 1024):
                    digest = hashlib.sha256(data).hexdigest()
                    chunk_id = hashlib.sha256(f"{TASK_ID}\0{relative}\0{offset}\0{digest}".encode()).hexdigest()
                    try:
                        request(f"/v1/tasks/{TASK_ID}/traces", {
                            "chunk_id": chunk_id, "relative_path": relative, "offset": offset,
                            "sha256": digest, "data_base64": base64.b64encode(data).decode(), "final": False,
                        })
                        offset += len(data)
                        offsets[relative] = offset
                        save_offsets(offsets)
                        count += 1
                    except urllib.error.HTTPError as exc:
                        if exc.code != 409:
                            raise
                        # The trace file was rewritten with different content at
                        # this offset (observed: OpenClaw rewrites session JSONL
                        # files), so the immutable chunk at this offset can never
                        # be reconciled. Traces are best-effort: mark the file as
                        # consumed and keep the pipeline moving. result.json and
                        # the .final marker are uploaded separately and strictly.
                        print(f"trace chunk conflict on {relative}@{offset}; skipping file", file=sys.stderr)
                        offsets[relative] = size
                        save_offsets(offsets)
                        skipped += 1
                        break
    if final:
        digest = hashlib.sha256(b"").hexdigest()
        marker = hashlib.sha256(f"{TASK_ID}\0.final\0{digest}".encode()).hexdigest()
        request(f"/v1/tasks/{TASK_ID}/traces", {
            "chunk_id": marker, "relative_path": ".final", "offset": 0,
            "sha256": digest, "data_base64": "", "final": True,
        })
    return count


def upload_result(required: bool) -> bool:
    path = STATE / "result.json"
    if not path.exists():
        if required:
            raise RuntimeError(f"required result is missing: {path}")
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    request(f"/v1/tasks/{TASK_ID}/result", payload)
    return True


def once(require_result: bool) -> None:
    upload_traces(final=require_result)
    upload_result(require_result)
    if require_result:
        receipt_request = urllib.request.Request(
            f"{BASE_URL}/v1/tasks/{TASK_ID}/receipt",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with urllib.request.urlopen(receipt_request, timeout=30) as response:
            receipt = json.loads(response.read())
        if not receipt.get("complete"):
            raise RuntimeError(f"central ingestion is incomplete: {receipt}")


def locked_upload(*, require_result: bool) -> None:
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        once(require_result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--require-result", action="store_true")
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    if not TASK_ID or not TOKEN or not BASE_URL:
        raise SystemExit("TASK_ID, TRACE_UPLOAD_TOKEN and TRACE_INGESTER_URL are required")
    STATE.mkdir(parents=True, exist_ok=True)
    if args.once:
        locked_upload(require_result=args.require_result)
        return

    def stop(*_: object) -> None:
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not STOP:
        try:
            locked_upload(require_result=False)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"trace upload retry: {type(exc).__name__}", flush=True)
        time.sleep(max(1, args.interval))
    try:
        locked_upload(require_result=False)
    except (OSError, ValueError, urllib.error.URLError):
        pass


if __name__ == "__main__":
    main()
