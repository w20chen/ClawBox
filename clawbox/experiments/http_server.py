"""Local listeners must not wait for external reverse DNS during startup."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer
from concurrent.futures import ThreadPoolExecutor


class LocalHTTPServer(ThreadingHTTPServer):
    # Experiment recovery can restart the fixed host control endpoints while
    # connections from the previous worker remain in TCP TIME_WAIT.
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Starting a fresh Python thread for every connection makes accept()
        # wait for each thread's bootstrap handshake. Reuse handlers across
        # connections; idle keep-alive sockets still expire in the handler.
        self._handlers = ThreadPoolExecutor(
            max_workers=max(32, self.request_queue_size * 2),
            thread_name_prefix='experiment-http',
        )

    def process_request(self, request, client_address):
        self._handlers.submit(self.process_request_thread, request, client_address)

    def server_close(self):
        super().server_close()
        if hasattr(self, '_handlers'):
            self._handlers.shutdown(wait=False)

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]
