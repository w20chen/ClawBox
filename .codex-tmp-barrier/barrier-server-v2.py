#!/usr/bin/env python3
"""Authenticated high-concurrency acceptance barrier."""

import http.server
import json
import os
import pathlib
import secrets
import socketserver
import threading
import time


EVIDENCE_ROOT = pathlib.Path(
    os.environ.get(
        "CLAWBOX_BARRIER_EVIDENCE_ROOT",
        "/data/clawbox-acceptance-20261022T102744Z",
    )
)
TARGET = int(os.environ.get("CLAWBOX_BARRIER_TARGET", "1280"))
root = EVIDENCE_ROOT / ("barrier-v2-" + str(time.time_ns()))
root.mkdir()
key = secrets.token_hex(24)
lock = threading.Lock()
arrivals: dict[str, dict] = {}
tokens: set[str] = set()
released = False


class BarrierServer(http.server.ThreadingHTTPServer):
    request_queue_size = 4096
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind performs a reverse DNS lookup for the bind
        # address. DNS on this experimental host can stall or spin; neither
        # the HTTP protocol nor the acceptance receipt needs that hostname.
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # Client retries are idempotent. A disconnected response must not
            # hold the state lock or affect other clients.
            return

    def authorized(self) -> bool:
        if self.headers.get("Authorization") == key:
            return True
        self.reply(403, {"error": "unauthorized"})
        return False

    @staticmethod
    def status_payload() -> dict:
        with lock:
            return {
                "arrived": len(arrivals),
                "released": released,
                "target": TARGET,
            }

    def do_GET(self) -> None:
        if not self.authorized():
            return
        if self.path != "/health":
            self.reply(404, {"error": "unknown path"})
            return
        self.reply(200, self.status_payload())

    def do_POST(self) -> None:
        global released
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size < 4096:
                raise ValueError("invalid content length")
            value = json.loads(self.rfile.read(size))
        except Exception:
            self.reply(400, {"error": "invalid request"})
            return

        if self.path == "/release":
            with lock:
                if len(arrivals) != TARGET:
                    payload = {"error": f"all {TARGET} arrivals required"}
                    status = 409
                else:
                    released = True
                    release_record = {
                        "released_at": time.time(),
                        "arrived": len(arrivals),
                        "evidence": value,
                    }
                    (root / "released.json").write_text(json.dumps(release_record))
                    payload = {
                        "arrived": len(arrivals),
                        "released": released,
                        "target": TARGET,
                    }
                    status = 200
            self.reply(status, payload)
            return

        if self.path != "/arrive":
            self.reply(404, {"error": "unknown path"})
            return

        sid = value.get("session_id", "")
        token = value.get("token", "")
        try:
            index = int(sid.rsplit("-", 1)[-1])
            if not 0 <= index < TARGET or len(token) != 32:
                raise ValueError("invalid session identity")
        except Exception:
            self.reply(400, {"error": "invalid session identity"})
            return

        with lock:
            if sid in arrivals:
                if arrivals[sid]["token"] != token:
                    payload = {"error": "session token changed"}
                    status = 409
                else:
                    payload = {
                        "arrived": len(arrivals),
                        "released": released,
                        "target": TARGET,
                    }
                    status = 200
            elif token in tokens:
                payload = {"error": "shared writable marker"}
                status = 409
            else:
                record = {**value, "arrived_at": time.time()}
                arrivals[sid] = record
                tokens.add(token)
                with (root / "arrivals.jsonl").open("a") as stream:
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                payload = {
                    "arrived": len(arrivals),
                    "released": released,
                    "target": TARGET,
                }
                status = 200
        self.reply(status, payload)


server = BarrierServer(("0.0.0.0", 0), Handler)
record = {
    "root": str(root),
    "host": "193.124.7.2",
    "port": server.server_port,
    "key": key,
    "target": TARGET,
    "version": 2,
}
descriptor = root / "descriptor.json"
descriptor.write_text(json.dumps(record, indent=2))
descriptor.chmod(0o600)
current = EVIDENCE_ROOT / "current-barrier-v2.json"
current.write_text(json.dumps(record, indent=2))
current.chmod(0o600)
print(json.dumps({"root": str(root), "port": server.server_port, "target": TARGET}), flush=True)
server.serve_forever()
