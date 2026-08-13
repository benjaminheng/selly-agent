"""This module's own contract on top of `websockets`: the host/port/path -> URI translation, our
exception types (`HandshakeError`, `ConnectionClosed`) in place of the library's, and `recv_text`'s
timeout — driven against a real (local, TLS-free) fake WS server, not a mock. RFC 6455 protocol
correctness itself (framing, masking, ping/pong, fragmentation) is the `websockets` library's own
test suite's job, not this one's.
"""

from __future__ import annotations

import socket
import threading

import pytest

from fake_ws_server import FakeWebSocketServer
from selly_agent.channel.discord.ws_client import ConnectionClosed, HandshakeError, connect


@pytest.fixture
def server():
    srv = FakeWebSocketServer()
    yield srv
    srv.close()


def test_handshake_succeeds_and_can_exchange_text(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_text('{"hello": "world"}')
    assert ws.recv_text() == '{"hello": "world"}'
    ws.send_text('{"reply": true}')
    assert server.recv_text() == '{"reply": true}'
    ws.close()


def test_handshake_fails_against_a_plain_http_server() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def _serve_one():
        conn, _ = listener.accept()
        # Read (some of) the request before responding — the client library's background reader
        # thread starts as soon as the socket connects, before the handshake request is even
        # sent, so a response arriving unprompted races the client's own send-then-check-state
        # ordering and fails with the wrong exception. A real HTTP server always reads a request
        # first; this fake one has to as well to be a faithful enough double of one.
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    threading.Thread(target=_serve_one, daemon=True).start()
    with pytest.raises(HandshakeError):
        connect(host, port, "/", use_tls=False, timeout=2.0)
    listener.close()


def test_recv_text_raises_timeout_error_with_nothing_pending(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    with pytest.raises(TimeoutError):
        ws.recv_text(timeout=0.2)
    ws.close()


def test_recv_text_returns_once_the_server_sends(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_text('{"x": 1}')
    assert ws.recv_text(timeout=2.0) == '{"x": 1}'
    ws.close()


def test_recv_text_raises_connection_closed_after_the_peer_closes(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.close()
    with pytest.raises(ConnectionClosed):
        ws.recv_text(timeout=2.0)
