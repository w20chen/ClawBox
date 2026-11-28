"""Exercise the real control transport and installed stdlib-only launcher."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('policy_ssh_transport', ROOT/'scripts/clawbox-policy-ssh.py')
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


@pytest.fixture
def control(monkeypatch):
    seen = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            seen.append((self.client_address, self.path, json.loads(body), self.headers.get('Authorization')))
            status = responses.pop(0) if responses else 200
            if status == 'drop':
                self.close_connection = True
                return
            self.send_response(200 if status == 'idle-close' else status)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
            if status == 'idle-close':
                self.close_connection = True

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('CLAWBOX_POLICY_CONTROL_URL', f'http://127.0.0.1:{server.server_port}/control')
    monkeypatch.setenv('CLAWBOX_POLICY_CONTROL_AUTH', 'test-only-token')
    monkeypatch.setenv('CLAWBOX_POLICY_TIMEOUT_SECONDS', '2')
    # Control traffic is direct, including when the surrounding shell has proxies.
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    try:
        yield seen, responses
    finally:
        shim._close_control_connection()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_admission_and_completion_reuse_authenticated_socket(control):
    seen, _ = control
    for path in ('/v1/tool/admit', '/v1/tool/complete'):
        assert shim._post(path, {'execution_id': 'same-operation'}, attempts=3) == {}
    assert len(seen) == 2
    assert seen[0][0] == seen[1][0]
    assert [r[1] for r in seen] == ['/control/v1/tool/admit', '/control/v1/tool/complete']
    assert all(r[2] == {'execution_id': 'same-operation'} and r[3] == 'Bearer test-only-token' for r in seen)


def test_dropped_response_retries_same_identity_on_new_socket(control):
    seen, responses = control
    responses.extend(['drop', 200])
    assert shim._post('/v1/tool/complete', {'execution_id': 'executed-once'}, attempts=3) == {}
    assert len(seen) == 2 and seen[0][0] != seen[1][0]
    assert seen[0][2] == seen[1][2] == {'execution_id': 'executed-once'}


def test_closed_keepalive_reconnects_without_retry_delay(control, monkeypatch):
    seen, responses = control
    responses.append('idle-close')
    shim._post('/v1/tool/admit', {}, attempts=3)
    assert shim.select.select([shim._CONTROL_CONNECTION.sock], [], [], 2)[0]
    def no_backoff(_seconds):
        raise AssertionError('known-closed keepalive must reconnect before sending')
    monkeypatch.setattr(shim.time, 'sleep', no_backoff)
    shim._post('/v1/tool/complete', {}, attempts=3)
    assert len(seen) == 2 and seen[0][0] != seen[1][0]


@pytest.mark.parametrize('status', [302, 401, 409, 500])
def test_http_rejection_is_not_retried_or_redirected(control, status):
    seen, responses = control
    responses.append(status)
    with pytest.raises(RuntimeError, match=f'HTTP {status}'):
        shim._post('/v1/tool/admit', {}, attempts=3)
    assert len(seen) == 1


def test_installed_launcher_preserves_args_exit_and_streams(tmp_path):
    source = tmp_path/'shim source.py'
    source.write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\nprint("stderr retained",file=sys.stderr)\nraise SystemExit(7)\n')
    launcher = tmp_path/'ssh launcher'
    subprocess.run([sys.executable, '-S', str(ROOT/'scripts/compile-policy-ssh.py'), str(source), str(launcher)], check=True)
    arguments = ['space argument', 'quote\'"', '$literal; not a command']
    command = ([str(launcher)] if os.name != 'nt' else
               [sys.executable, '-S', str(launcher)+'.pyc'])
    result = subprocess.run([*command, *arguments], capture_output=True, text=True)
    assert result.returncode == 7
    assert json.loads(result.stdout) == arguments
    assert result.stderr.strip() == 'stderr retained'
    # A repeated install replaces the bytecode using the same interpreter.
    source.write_text('raise SystemExit(3)\n')
    subprocess.run([sys.executable, '-S', str(ROOT/'scripts/compile-policy-ssh.py'), str(source), str(launcher)], check=True)
    assert subprocess.run(command).returncode == 3


def test_failed_compilation_does_not_publish_launcher(tmp_path):
    source = tmp_path/'invalid.py'; source.write_text('def broken(\n')
    launcher = tmp_path/'ssh'
    result = subprocess.run([sys.executable, '-S', str(ROOT/'scripts/compile-policy-ssh.py'), str(source), str(launcher)], capture_output=True)
    assert result.returncode != 0 and not launcher.exists()
