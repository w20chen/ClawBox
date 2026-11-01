import http.client
import threading
from http.server import BaseHTTPRequestHandler

from clawbox.experiments.http_server import LocalHTTPServer


def test_short_connections_reuse_handlers_and_close_sockets():
    handlers = set()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            handlers.add(threading.current_thread())
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass

    server = LocalHTTPServer(('127.0.0.1', 0), Handler)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    try:
        for _ in range(100):
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request('GET', '/')
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == b'ok'
            finally:
                connection.close()
        assert len(handlers) <= 4
    finally:
        server.shutdown()
        server.server_close()
        listener.join(3)
        for handler in handlers:
            handler.join(3)
    assert all(not handler.is_alive() for handler in handlers)
