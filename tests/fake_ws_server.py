"""A minimal, test-only server-side WebSocket endpoint, built on the same `websockets` library the
real client (`ws_client.py`) uses — so these tests drive the real client against a real protocol
implementation on both ends, over a real local socket, rather than a hand-rolled double of either
side. No TLS (tests connect with use_tls=False) and no concurrency: one connection at a time is
all these tests need.
"""

from __future__ import annotations

import json
import queue
import threading

from websockets.sync.server import ServerConnection, serve


class FakeWebSocketServer:
    def __init__(self):
        self._accepted: queue.Queue[ServerConnection] = queue.Queue()
        # serve()'s handler must stay on the stack for the connection's whole lifetime — once it
        # returns, the library closes the connection out from under whoever is still using it —
        # so the handler thread blocks here until the test is done (`close()` releases it), while
        # the connection itself is handed to the test via the queue above.
        self._release = threading.Event()
        self._server = serve(self._handle, "127.0.0.1", 0)
        self.host, self.port = self._server.socket.getsockname()
        self._serve_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._serve_thread.start()
        self._conn: ServerConnection | None = None

    def _handle(self, conn: ServerConnection) -> None:
        self._accepted.put(conn)
        self._release.wait()

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        try:
            self._conn = self._accepted.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("no client connected in time") from None

    def send_text(self, text: str) -> None:
        self._conn.send(text)

    def recv_text(self, timeout: float | None = None) -> str:
        return self._conn.recv(timeout=timeout)

    def close_connection(self, code: int = 1000, reason: str = "") -> None:
        """Close the current client connection with a specific close code, leaving the server up to
        accept the next one — how a test drives the client's reconnect policy."""
        self._conn.close(code, reason)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._release.set()  # let the handler return so shutdown()'s join doesn't hang
        self._server.shutdown()
        self._serve_thread.join(timeout=2.0)


class FakeGatewayServer(FakeWebSocketServer):
    """Adds Discord Gateway op-code framing on top of the raw text send/recv the base class gives:
    send_op/recv_op work in parsed dicts, matching what GatewaySession actually exchanges."""

    def send_op(self, op: int, d=None, *, t=None, s=None) -> None:
        payload: dict = {"op": op, "d": d}
        if t is not None:
            payload["t"] = t
        if s is not None:
            payload["s"] = s
        self.send_text(json.dumps(payload))

    def recv_op(self) -> dict:
        return json.loads(self.recv_text())

    def send_hello(self, heartbeat_interval_ms: int) -> None:
        self.send_op(10, {"heartbeat_interval": heartbeat_interval_ms})
