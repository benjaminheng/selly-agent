"""The Gateway session state machine — HELLO -> IDENTIFY -> READY, heartbeating with ACK tracking,
and RESUME on a dropped connection — driven against a fake Gateway server speaking real opcodes
over a real (local) WebSocket connection. No bind/ingest wiring here (Task 8).

A note on ordering: `server.recv_op()` and `ws.recv_text()` are *blocking* reads — the peer's bytes
must already be sent (into a kernel socket buffer, not necessarily "processed") before a blocking
read for them is issued, or the read hangs forever. Full-duplex TCP means sends never block on the
peer reading them (these payloads are a few hundred bytes, far under any default socket buffer), so
most tests below queue a server response with `send_op(...)` *before* the client-side call that
consumes it, and only read back what the client sent (`recv_op()`) *after* the client-side call
that sends it — with the single-threaded read/write ordering worked out by hand rather than by
guessing at scheduling.

`test_identify_sent_after_hello` is the one exception, deliberately: pre-queuing READY before
calling `_read_until_ready()` would make the test pass even for a broken `GatewaySession` that
waits for READY before ever sending IDENTIFY (which would hang forever against the real Gateway,
since Discord only sends READY *in response to* IDENTIFY) — the pre-queued READY would just be
sitting there ready to read whenever the broken client got around to its first read. So that test
runs `_read_until_ready()` on a background thread instead and only sends READY *after* the main
thread has already read IDENTIFY back off the wire, proving the send-before-read order by
construction, not just by the final assertions. The heartbeat tests use the same background-thread
approach for the same reason — `_pump_until_stopped()` must send and receive concurrently with the
main thread's own send/recv calls — while `test_session_id_captured_from_ready` and the two
heartbeat tests' *initial* handshake don't need this because their properties don't depend on which
of two ambiguous internal orderings occurred.
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
    """IDENTIFY must be sent *before* READY is ever available to read — not just "eventually
    sent". Proved by construction, not just by the final assertions: READY is only sent by the
    fake server (from the main thread) *after* the main thread has already read IDENTIFY off the
    wire, which — since `server.recv_op()` is a blocking read — can only have happened if
    `_read_until_ready()` (running on a background thread) actually sent it first. A broken
    `GatewaySession` that waited for READY before sending IDENTIFY would hang here forever (the
    real failure mode against Discord's Gateway, which only sends READY in response to IDENTIFY),
    rather than passing vacuously the way pre-queuing READY before the call would."""
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws

        thread = threading.Thread(target=session._read_until_ready, daemon=True)
        thread.start()
        identify = server.recv_op()  # can only arrive after _read_until_ready sent it
        assert identify["op"] == 2
        assert identify["d"]["token"] == "fake-token"
        assert identify["d"]["intents"] == 4096  # DIRECT_MESSAGES only

        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        thread.join(timeout=1.0)
        assert session._session_id == "sess-abc"
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
        ws = _connect_and_serve_hello(server, heartbeat_ms=120)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY
        session._heartbeat_interval_sec = 0.12

        stop = threading.Event()
        thread = threading.Thread(target=session._pump_until_stopped, args=(stop,), daemon=True)
        thread.start()
        heartbeat = server.recv_op()
        assert heartbeat["op"] == 1
        server.send_op(11)  # Heartbeat ACK
        time.sleep(0.05)
        assert session._ack_pending is False
        stop.set()
        thread.join(timeout=1.0)
        ws.close()
    finally:
        server.close()


def test_heartbeat_not_delayed_by_frequent_messages() -> None:
    """The heartbeat deadline is a fixed wall-clock schedule, not something inbound traffic resets.
    A naive loop that recomputes `wait_for` from the full interval on every message would never
    reach the timeout branch (where the heartbeat is actually sent) on a connection receiving
    messages faster than the heartbeat interval — plausible during an active DM conversation,
    since Discord's real interval is ~41.25s — so the heartbeat would never fire and Discord would
    eventually close the connection as a zombie.

    Proved by flooding faster-than-interval Dispatch messages, from a *separate* thread, for
    longer than one heartbeat interval, while the main thread's `server.recv_op()` — a blocking
    read — waits for the heartbeat concurrently with that flood. `elapsed` therefore measures the
    real time-to-heartbeat, not "however long our own flood loop happened to take": consuming the
    already-buffered heartbeat only *after* the flood loop finishes (as an earlier version of this
    test did, by calling `send_op` and `recv_op` from the same sequential loop) would floor the
    measurement at the flood's own duration regardless of when the heartbeat actually went out,
    making it unable to distinguish a fixed implementation from a starved one."""
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server, heartbeat_ms=100)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY
        session._heartbeat_interval_sec = 0.1

        stop = threading.Event()
        pump_thread = threading.Thread(
            target=session._pump_until_stopped, args=(stop,), daemon=True
        )

        def _flood() -> None:
            # 12 messages, 20ms apart (240ms of traffic) — faster than, and longer than, the
            # 100ms heartbeat interval.
            for i in range(2, 14):
                server.send_op(0, {"n": i}, t="MESSAGE_CREATE", s=i)
                time.sleep(0.02)

        flood_thread = threading.Thread(target=_flood, daemon=True)

        started_at = time.monotonic()
        pump_thread.start()
        flood_thread.start()

        heartbeat = server.recv_op()  # blocks until the pump actually sends one
        elapsed = time.monotonic() - started_at
        assert heartbeat["op"] == 1
        # A "reset the wait on every message" bug would push the heartbeat out past the entire
        # 240ms flood (to ~240ms + a further 100ms interval, since messages never stop arriving
        # faster than the interval); the fix keeps it pinned near the original ~0-100ms deadline
        # regardless of the concurrent traffic. 0.2s sits with a wide margin on both sides.
        assert elapsed < 0.2

        server.send_op(11)  # ACK, so the pump thread exits cleanly instead of zombie-raising
        stop.set()
        flood_thread.join(timeout=1.0)
        pump_thread.join(timeout=1.0)
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
