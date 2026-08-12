"""A minimal, test-only server-side WebSocket endpoint: one TCP listener, one accepted connection,
speaking just enough RFC 6455 to drive the real client end to end. No TLS (tests connect with
use_tls=False) and no concurrency — one connection at a time is all `ws_client` tests need.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading

from selly_agent.channel.discord.ws_client import decode_frame_header, encode_frame

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeWebSocketServer:
    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.host, self.port = self._listener.getsockname()
        self._conn: socket.socket | None = None
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def _accept(self) -> None:
        conn, _ = self._listener.accept()
        self._handshake(conn)
        self._conn = conn

    def _handshake(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(1)
        head = buf.decode("iso-8859-1")
        key = next(
            line.split(":", 1)[1].strip()
            for line in head.split("\r\n")
            if line.lower().startswith("sec-websocket-key:")
        )
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode()
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n" + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii")
        )

    def wait_for_connection(self, timeout: float = 5.0) -> None:
        self._accept_thread.join(timeout)
        if self._conn is None:
            raise TimeoutError("no client connected in time")

    def send_text(self, text: str) -> None:
        # Server frames are never masked (RFC 6455 §5.1).
        self._conn.sendall(encode_frame(0x1, text.encode("utf-8")))

    def send_raw(self, data: bytes) -> None:
        self._conn.sendall(data)

    def recv_text(self) -> str:
        buf = b""
        while True:
            frame_and_len = decode_frame_header(buf)
            if frame_and_len is not None:
                frame, _ = frame_and_len
                return frame.payload.decode("utf-8")
            chunk = self._conn.recv(4096)
            if not chunk:
                raise ConnectionError("client closed")
            buf += chunk

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._listener.close()


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
