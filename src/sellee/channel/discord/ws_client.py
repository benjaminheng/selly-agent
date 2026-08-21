"""A thin wrapper around the `websockets` library's synchronous client: the Gateway is the one
thing in this codebase that needs a persistent, bidirectional connection — long-poll (used
everywhere else) has no Discord equivalent for receiving DMs. `websockets` owns the RFC 6455
handshake, framing, masking, fragmentation, and ping/pong (chosen over `aiohttp`, which brings a
full HTTP client/server stack this project doesn't need).

This module's only job is translating between the library's API and the two calls
`GatewaySession` actually needs: `send_text`/`recv_text` (the latter taking an optional read
timeout, since the session races "a message arrived" against "the heartbeat is due"), plus our
own exception types so callers depend on this module's contract rather than the library's.
"""

from __future__ import annotations

from websockets import ConnectionClosed as _LibConnectionClosed
from websockets import InvalidHandshake
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as _connect


class HandshakeError(Exception):
    """The opening handshake did not complete — a non-101 response, or another RFC 6455
    handshake-negotiation failure."""


class ConnectionClosed(Exception):
    """The peer closed the connection, or the socket dropped mid-read/write.

    `code` is the WebSocket close code the peer sent, or None when there was no close frame (a
    dropped socket) or the close was raised locally. The Gateway's reconnect policy keys off it:
    some Discord close codes mean "never reconnect with this token"."""

    def __init__(self, message: str, *, code: int | None = None, reason: str = ""):
        super().__init__(message)
        self.code = code
        self.reason = reason


def _closed(exc: _LibConnectionClosed) -> ConnectionClosed:
    """Our exception carrying the *received* close code — what the peer said, not what we sent, so
    a close we initiated is never mistaken for the server rejecting us."""
    rcvd = exc.rcvd
    if rcvd is None:
        return ConnectionClosed(str(exc))
    return ConnectionClosed(str(exc), code=rcvd.code, reason=rcvd.reason)


class WebSocket:
    """A connected client: send/receive text frames. `recv_text`'s `timeout` lets the Gateway
    session race an inbound message against its own heartbeat deadline without ever starting a
    read that can't be bounded."""

    def __init__(self, conn: ClientConnection):
        self._conn = conn

    def send_text(self, text: str) -> None:
        try:
            self._conn.send(text)
        except _LibConnectionClosed as exc:
            raise _closed(exc) from exc

    def recv_text(self, timeout: float | None = None) -> str:
        """Block for the next text message. With `timeout` set, raises the builtin
        `TimeoutError` if none arrives in time — the caller (the Gateway session's heartbeat
        loop) treats that as "nothing to read yet", not a failure."""
        try:
            message = self._conn.recv(timeout=timeout)
        except _LibConnectionClosed as exc:
            raise _closed(exc) from exc
        assert isinstance(message, str)  # Gateway payloads are JSON text, never binary
        return message

    def close(self, code: int = 1000) -> None:
        self._conn.close(code)


def connect(
    host: str, port: int, path: str, *, use_tls: bool = True, timeout: float = 10.0
) -> WebSocket:
    scheme = "wss" if use_tls else "ws"
    uri = f"{scheme}://{host}:{port}{path}"
    try:
        # ping_interval=None: Discord's own app-level Heartbeat opcode is this connection's only
        # liveness signal — no need for the library's separate WS-level keepalive ping too.
        # proxy=None: opt out of the library's default of honoring system proxy env vars, so
        # behavior doesn't depend on the host's environment.
        conn = _connect(uri, open_timeout=timeout, ping_interval=None, proxy=None)
    except InvalidHandshake as exc:
        raise HandshakeError(str(exc)) from exc
    return WebSocket(conn)
