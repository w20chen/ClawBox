"""Local listeners must not wait for external reverse DNS during startup."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LocalHTTPServer(ThreadingHTTPServer):
    # Experiment recovery can restart the fixed host control endpoints while
    # connections from the previous worker remain in TCP TIME_WAIT.
    allow_reuse_address = True

    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]
