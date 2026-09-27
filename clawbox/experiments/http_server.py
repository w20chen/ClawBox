"""Local listeners must not wait for external reverse DNS during startup."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]
