"""Runtime-local relay that reconnects managed model requests after VM restore."""

from __future__ import annotations


RELAY_PORT = 8766
RELAY_CHECKPOINT_URL = f"http://127.0.0.1:{RELAY_PORT}/checkpoint"


# The relay and OpenClaw/ClawTune are checkpointed in the same Runtime VM, so
# their loopback TCP stream survives. Only the relay's host-facing request is
# retried after restore. ModelGateway provides the idempotency boundary and
# returns the already-produced response for the identical request.
RUNTIME_MODEL_RELAY_SCRIPT = r'''#!/usr/bin/env python3
import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ["CLAWBOX_RELAY_UPSTREAM"].rstrip("/") + "/chat/completions"
TOKEN = os.environ["CLAWBOX_RELAY_TOKEN"]
PENDING = {}
PENDING_LOCK = threading.Lock()


class Pending:
    def __init__(self, body, content_type):
        self.body = body
        self.content_type = content_type
        self.condition = threading.Condition()
        self.response = None
        self.attempts = 0

    def start(self):
        with self.condition:
            if self.response is not None:
                return
            self.attempts += 1
        threading.Thread(target=self.fetch, daemon=True).start()

    def fetch(self):
        request = urllib.request.Request(
            UPSTREAM, data=self.body, method="POST",
            headers={"Authorization": "Bearer " + TOKEN,
                     "Content-Type": self.content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=650) as response:
                result = (response.status,
                          response.headers.get("Content-Type", "application/json"),
                          response.read())
        except urllib.error.HTTPError as exc:
            result = (exc.code, exc.headers.get("Content-Type", "application/json"),
                      exc.read())
        except Exception:
            # A pre-checkpoint host connection can remain unreadable after the
            # guest resumes. The explicit /checkpoint notification starts a
            # fresh attempt; do not turn the stale attempt into an Agent error.
            return
        with self.condition:
            if self.response is None:
                self.response = result
                self.condition.notify_all()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/checkpoint":
            with PENDING_LOCK:
                pending = list(PENDING.values())
            for item in pending:
                item.start()
            body = json.dumps({"retried": len(pending)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.rstrip("/") not in {"/v1/chat/completions", "/chat/completions"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > 8 * 1024 * 1024:
            self.send_error(400)
            return
        body = self.rfile.read(length)
        pending_id = hashlib.sha256(body + os.urandom(16)).hexdigest()
        pending = Pending(body, self.headers.get("Content-Type", "application/json"))
        with PENDING_LOCK:
            PENDING[pending_id] = pending
        pending.start()
        try:
            with pending.condition:
                pending.condition.wait_for(lambda: pending.response is not None, timeout=660)
                response = pending.response
            if response is None:
                self.send_error(504)
                return
            status, content_type, response_body = response
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
        finally:
            with PENDING_LOCK:
                PENDING.pop(pending_id, None)

    def log_message(self, _format, *_args):
        return


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


Server(("127.0.0.1", int(os.environ.get("CLAWBOX_RELAY_PORT", "8766"))), Handler).serve_forever()
'''
