"""The socket-owning half of ws_client: the RFC 6455 handshake and send/recv over a real (local,
TLS-free) TCP connection to the fake WS server double — so this is the real client's real I/O path,
not a mock.
"""

from __future__ import annotations

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
    import socket
    import threading

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    host, port = listener.getsockname()

    def _serve_one():
        conn, _ = listener.accept()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    threading.Thread(target=_serve_one, daemon=True).start()
    with pytest.raises(HandshakeError):
        connect(host, port, "/", use_tls=False, timeout=2.0)
    listener.close()


def test_recv_transparently_answers_a_ping(server) -> None:
    from selly_agent.channel.discord.ws_client import OPCODE_PING, encode_frame

    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_raw(encode_frame(OPCODE_PING, b"ping-payload"))
    server.send_text('{"after": "ping"}')
    assert ws.recv_text() == '{"after": "ping"}'
    ws.close()


def test_recv_raises_connection_closed_on_a_close_frame(server) -> None:
    from selly_agent.channel.discord.ws_client import OPCODE_CLOSE, encode_frame

    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_raw(encode_frame(OPCODE_CLOSE, (1000).to_bytes(2, "big")))
    with pytest.raises(ConnectionClosed):
        ws.recv_text()


def test_wait_readable_times_out_with_nothing_pending(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    assert ws.wait_readable(0.2) is False
    ws.close()


def test_wait_readable_true_once_the_server_sends(server) -> None:
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_text('{"x": 1}')
    assert ws.wait_readable(2.0) is True
    assert ws.recv_text() == '{"x": 1}'
    ws.close()
