import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


class MarketFaultProxy:
    def __init__(self, front):
        parsed = urlsplit(front)
        if parsed.scheme != "tcp" or not parsed.hostname or not parsed.port or parsed.username or parsed.password:
            raise ValueError("只读行情代理目标无效")
        self.target = (parsed.hostname, parsed.port)
        self.lock = threading.Lock()
        self.connections = set()
        self.accepted = 0
        self.forced_closes = 0
        self.transferred = 0
        self.closed = False
        owner = self

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    upstream = socket.create_connection(owner.target, timeout=5)
                except OSError:
                    return
                pair = (self.request, upstream)
                with owner.lock:
                    if owner.closed:
                        upstream.close()
                        return
                    owner.connections.add(pair)
                    owner.accepted += 1
                try:
                    while not owner.closed:
                        readable, _, _ = select.select(pair, [], [], 1)
                        for source in readable:
                            payload = source.recv(65536)
                            if not payload:
                                return
                            target = upstream if source is self.request else self.request
                            target.sendall(payload)
                            with owner.lock:
                                owner.transferred += len(payload)
                except (OSError, ValueError):
                    return
                finally:
                    with owner.lock:
                        owner.connections.discard(pair)
                    upstream.close()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.front = f"tcp://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, name="market-fault-proxy", daemon=True)
        self.thread.start()

    def drop(self):
        with self.lock:
            pairs = list(self.connections)
            self.forced_closes += len(pairs)
        for pair in pairs:
            for connection in pair:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        return len(pairs)

    def report(self):
        with self.lock:
            return dict(target=f"tcp://{self.target[0]}:{self.target[1]}", accepted_connections=self.accepted, forced_connections_closed=self.forced_closes, forwarded_bytes=self.transferred, packet_contents_recorded=False)

    def close(self):
        self.closed = True
        self.drop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
