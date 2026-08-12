"""The Gateway session state machine — HELLO -> IDENTIFY -> READY, heartbeating with ACK tracking,
and RESUME on a dropped connection — driven against a fake Gateway server speaking real opcodes
over a real (local) WebSocket connection. No bind/ingest wiring here (Task 8).

A note on ordering: `server.recv_op()` and `ws.recv_text()` are *blocking* reads — the peer's bytes
must already be sent (into a kernel socket buffer, not necessarily "processed") before a blocking
read for them is issued, or the read hangs forever. Full-duplex TCP means sends never block on the
peer reading them (these payloads are a few hundred bytes, far under any default socket buffer), so
each test below queues a server response with `send_op(...)` *before* the client-side call that
consumes it, and only reads back what the client sent (`recv_op()`) *after* the client-side call
that sends it — with the single-threaded read/write ordering worked out by hand rather than by
guessing at scheduling. `_read_until_ready()` and `_pump_until_stopped()` are themselves opaque,
multi-step blocking calls, so this ordering is the only way to drive them from one thread; the
heartbeat test still needs a background thread once the loop must both send *and* receive to
observe ACK bookkeeping mid-flight.
"""

from __future__ import annotations

import threading
import time

import pytest

from fake_ws_server import FakeGatewayServer
from selly_agent.channel.discord.gateway import GatewaySession
from selly_agent.channel.discord.ws_client import connect


def _connect_and_serve_hello(server, heartbeat_ms=50):
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_hello(heartbeat_ms)
    return ws


def test_identify_sent_after_hello() -> None:
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        # Queued before _read_until_ready() runs: that call blocks inside its own read loop until
        # READY arrives, so READY must already be sent (buffered) before we invoke it here.
        server.send_op(0, {"session_id": "sess-x"}, t="READY", s=1)
        session._read_until_ready()
        identify = server.recv_op()  # the client's IDENTIFY, sent right after HELLO
        assert identify["op"] == 2
        assert identify["d"]["token"] == "fake-token"
        assert identify["d"]["intents"] == 4096  # DIRECT_MESSAGES only
        ws.close()
    finally:
        server.close()


def test_session_id_captured_from_ready() -> None:
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server)
        server.send_op(0, {"session_id": "sess-abc", "user": {"id": "1"}}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY (already sent by _read_until_ready, above)
        assert session._session_id == "sess-abc"
        assert session._seq == 1
        ws.close()
    finally:
        server.close()


def test_heartbeat_sent_on_schedule_and_acked() -> None:
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server, heartbeat_ms=50)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY
        session._heartbeat_interval_sec = 0.05

        stop = threading.Event()
        thread = threading.Thread(target=session._pump_until_stopped, args=(stop,), daemon=True)
        thread.start()
        heartbeat = server.recv_op()
        assert heartbeat["op"] == 1
        server.send_op(11)  # Heartbeat ACK
        time.sleep(0.02)
        assert session._ack_pending is False
        stop.set()
        thread.join(timeout=1.0)
        ws.close()
    finally:
        server.close()


def test_zombied_connection_without_ack_raises_for_reconnect() -> None:
    from selly_agent.channel.discord.ws_client import ConnectionClosed

    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server, heartbeat_ms=30)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY
        session._heartbeat_interval_sec = 0.03

        stop = threading.Event()
        with pytest.raises(ConnectionClosed):
            session._pump_until_stopped(stop)
        # By the time _pump_until_stopped raises (zombied: no ACK before the next heartbeat was
        # due) it has already sent the first heartbeat over the wire, so this read doesn't block.
        heartbeat = server.recv_op()  # first heartbeat — never ACKed by the test server
        assert heartbeat["op"] == 1
    finally:
        server.close()


def test_resume_payload_shape() -> None:
    from selly_agent.channel.discord.gateway import _resume_payload

    payload = _resume_payload(token="fake-token", session_id="sess-abc", seq=42)
    assert payload == {"op": 6, "d": {"token": "fake-token", "session_id": "sess-abc", "seq": 42}}


@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
def test_non_resumable_close_codes(code) -> None:
    from selly_agent.channel.discord.gateway import NON_RESUMABLE_CLOSE_CODES

    assert code in NON_RESUMABLE_CLOSE_CODES


@pytest.mark.parametrize("code", [1000, 4000, 4001, 4009])
def test_resumable_close_codes(code) -> None:
    from selly_agent.channel.discord.gateway import NON_RESUMABLE_CLOSE_CODES

    assert code not in NON_RESUMABLE_CLOSE_CODES
