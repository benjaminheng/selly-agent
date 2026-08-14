"""The Gateway session state machine — HELLO -> IDENTIFY -> READY, heartbeating with ACK tracking —
and `DiscordGateway.run()`'s close-code policy on top of it, driven against a fake Gateway server
speaking real opcodes over a real (local) WebSocket connection. Bind and ingest wiring is covered
by tests/test_channel_discord_gateway_dispatch.py.

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
from types import SimpleNamespace

import pytest

from fake_discord_api import FAKE_TOKEN
from fake_ws_server import FakeGatewayServer
from selly_agent import secrets
from selly_agent.channel.discord import gateway as gateway_mod
from selly_agent.channel.discord.gateway import DiscordGateway, GatewaySession
from selly_agent.channel.discord.ws_client import ConnectionClosed, connect
from selly_agent.config import Config


def _connect_and_serve_hello(server, heartbeat_ms=50):
    ws = connect(server.host, server.port, "/", use_tls=False, timeout=2.0)
    server.wait_for_connection()
    server.send_hello(heartbeat_ms)
    return ws


def _poll_until(predicate, timeout: float = 5.0) -> bool:
    """Wait for a background thread to reach an observable state. A fixed sleep would have to be
    long enough for the slowest machine and would still be a race on it; this fails fast on the
    quick path and only spends the full timeout when the property never holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _pin_first_heartbeat_jitter(monkeypatch) -> None:
    """The first heartbeat is deliberately jittered across the interval (`random.random()`), which
    leaves a timing test measuring the RNG as much as the code. Pinned to the full interval — the
    largest legitimate delay, so the gap to a starved implementation stays widest. Swapped in the
    gateway's own namespace rather than on the stdlib module, so nothing else sees a rigged RNG."""
    monkeypatch.setattr(gateway_mod, "random", SimpleNamespace(random=lambda: 1.0))


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


def test_heartbeat_sent_on_schedule_and_acked(monkeypatch) -> None:
    _pin_first_heartbeat_jitter(monkeypatch)
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server, heartbeat_ms=120)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY

        stop = threading.Event()
        thread = threading.Thread(target=session._pump_until_stopped, args=(stop,), daemon=True)
        thread.start()
        heartbeat = server.recv_op()
        assert heartbeat["op"] == 1
        server.send_op(11)  # Heartbeat ACK
        assert _poll_until(lambda: session._ack_pending is False)
        stop.set()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        ws.close()
    finally:
        server.close()


def test_heartbeat_not_delayed_by_frequent_messages(monkeypatch) -> None:
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
    _pin_first_heartbeat_jitter(monkeypatch)
    server = FakeGatewayServer()
    try:
        ws = _connect_and_serve_hello(server, heartbeat_ms=100)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
        session = GatewaySession(token="fake-token", on_dispatch=lambda t, d: None)
        session._ws = ws
        session._read_until_ready()
        server.recv_op()  # consume IDENTIFY

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
        # With the jitter pinned to the full interval the heartbeat is due at ~100ms. A "reset the
        # wait on every message" bug would push it out past the entire 240ms flood (to ~340ms,
        # since messages never stop arriving faster than the interval), so the bound below leaves
        # generous slack over the real deadline while staying far under the buggy one.
        assert elapsed < 0.22

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

        stop = threading.Event()
        with pytest.raises(ConnectionClosed):
            session._pump_until_stopped(stop)
        # By the time _pump_until_stopped raises (zombied: no ACK before the next heartbeat was
        # due) it has already sent the first heartbeat over the wire, so this read doesn't block.
        heartbeat = server.recv_op()  # first heartbeat — never ACKed by the test server
        assert heartbeat["op"] == 1
    finally:
        server.close()


def _ready_session(server, heartbeat_ms=50, on_dispatch=None):
    """A session through HELLO -> IDENTIFY -> READY, with IDENTIFY consumed off the wire."""
    ws = _connect_and_serve_hello(server, heartbeat_ms)
    server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=1)
    session = GatewaySession(
        token="fake-token", on_dispatch=on_dispatch or (lambda t, d: None), use_tls=False
    )
    session._ws = ws
    session._read_until_ready()
    server.recv_op()  # consume IDENTIFY
    return session, ws


def test_a_dispatch_before_ready_is_still_forwarded() -> None:
    """Discord can send Dispatch payloads between IDENTIFY and READY. Dropping them would lose a
    seller's message outright — the handshake is not a quiet period."""
    server = FakeGatewayServer()
    seen: list = []
    try:
        ws = _connect_and_serve_hello(server)
        session = GatewaySession(
            token="fake-token", on_dispatch=lambda t, d: seen.append((t, d)), use_tls=False
        )
        session._ws = ws

        thread = threading.Thread(target=session._read_until_ready, daemon=True)
        thread.start()
        server.recv_op()  # IDENTIFY
        server.send_op(0, {"content": "early"}, t="MESSAGE_CREATE", s=1)
        server.send_op(0, {"session_id": "sess-abc"}, t="READY", s=2)
        thread.join(timeout=5.0)
        assert not thread.is_alive()

        assert seen == [("MESSAGE_CREATE", {"content": "early"})]
        assert session._session_id == "sess-abc"
        ws.close()
    finally:
        server.close()


def test_a_server_initiated_heartbeat_is_answered_immediately() -> None:
    """Discord may ask for a heartbeat (op 1) off-schedule rather than waiting for ours. Ignoring
    it gets the connection dropped as unresponsive."""
    server = FakeGatewayServer()
    try:
        session, ws = _ready_session(server, heartbeat_ms=5000)  # far off, so ours can't race it
        stop = threading.Event()
        thread = threading.Thread(target=session._pump_until_stopped, args=(stop,), daemon=True)
        thread.start()

        server.send_op(1)  # server-initiated Heartbeat
        assert server.recv_op()["op"] == 1  # answered without waiting for the schedule

        stop.set()
        server.send_op(11)
        thread.join(timeout=5.0)
        ws.close()
    finally:
        server.close()


def test_a_reconnect_request_ends_the_session() -> None:
    """op 7 is Discord asking us to reconnect (it moves sessions between gateways routinely).
    `run()` treats the raised close as transient and dials again."""
    server = FakeGatewayServer()
    try:
        session, ws = _ready_session(server, heartbeat_ms=5000)
        server.send_op(7)
        with pytest.raises(ConnectionClosed, match="reconnect"):
            session._pump_until_stopped(threading.Event())
        ws.close()
    finally:
        server.close()


def test_a_non_resumable_invalid_session_clears_the_session_id() -> None:
    """Invalid Session with d=false means the session is gone for good, so its id must not be
    carried into the next connection."""
    server = FakeGatewayServer()
    try:
        session, ws = _ready_session(server, heartbeat_ms=5000)
        assert session._session_id == "sess-abc"

        server.send_op(9, False)
        with pytest.raises(ConnectionClosed, match="invalid session"):
            session._pump_until_stopped(threading.Event())
        assert session._session_id is None
        ws.close()
    finally:
        server.close()


def test_a_resumable_invalid_session_keeps_the_session_id() -> None:
    server = FakeGatewayServer()
    try:
        session, ws = _ready_session(server, heartbeat_ms=5000)
        server.send_op(9, True)
        with pytest.raises(ConnectionClosed):
            session._pump_until_stopped(threading.Event())
        assert session._session_id == "sess-abc"
        ws.close()
    finally:
        server.close()


def test_backoff_doubles_and_caps() -> None:
    """The reconnect delay grows from a cold 5s to a 60s ceiling, so a Gateway that is down stops
    being dialled every few seconds without the delay running away either."""
    gw = DiscordGateway(store=None, config=Config(), bus=None)
    delays = []
    for _ in range(8):
        gw._arm_backoff()
        delays.append(gw._backoff)
    assert delays[:4] == [5.0, 10.0, 20.0, 40.0]
    assert delays[-1] == gateway_mod.BACKOFF_CAP_SEC
    assert max(delays) == gateway_mod.BACKOFF_CAP_SEC


# --- run()'s close-code policy, end to end over a real socket ---------------------------------
#
# A close code Discord will never stop sending (4004 and friends: bad token, shard, API version or
# intent grant) must not be retried — the failure mode is a revoked token re-IDENTIFYing every 60s
# forever. Everything else, including a bare drop, reconnects. Both are properties of
# `DiscordGateway.run()` rather than of the constant, so they are driven against the fake Gateway.


def _point_gateway_at_fake(monkeypatch, server) -> None:
    """Make `DiscordGateway.run()` dial the local fake instead of gateway.discord.gg: the
    host/port/path constants, plus a session built with TLS off (the fake speaks plain ws)."""
    monkeypatch.setattr(gateway_mod, "GATEWAY_HOST", server.host)
    monkeypatch.setattr(gateway_mod, "GATEWAY_PORT", server.port)
    monkeypatch.setattr(gateway_mod, "GATEWAY_PATH", "/")
    real_session = gateway_mod.GatewaySession
    monkeypatch.setattr(
        gateway_mod,
        "GatewaySession",
        lambda *, token, on_dispatch: real_session(
            token=token, on_dispatch=on_dispatch, use_tls=False
        ),
    )


def _armed_gateway(store, bus, monkeypatch, server, token=FAKE_TOKEN):
    """A gateway in the awaiting-bind state (token on disk, nonce armed) pointed at `server`, with
    the idle and backoff waits shortened so a test doesn't sit through the real ones."""
    _point_gateway_at_fake(monkeypatch, server)
    monkeypatch.setattr(gateway_mod, "OFF_IDLE_SEC", 0.02)
    monkeypatch.setattr(gateway_mod, "BACKOFF_BASE_SEC", 0.02)
    secrets.write_discord_bot_token(token)
    store.arm_bind("selly_test_bot", "nonce-abc123", adapter="discord")
    stop = threading.Event()
    gw = DiscordGateway(store=store, config=Config(), bus=bus, stop_event=stop)
    thread = threading.Thread(target=gw.run, daemon=True)
    thread.start()
    return gw, stop, thread


def _serve_hello_and_read_identify(server) -> dict:
    server.wait_for_connection()
    server.send_hello(50)
    return server.recv_op()


def test_a_non_resumable_close_stops_the_gateway_reconnecting(
    store, bus, xdg_tmp, monkeypatch
) -> None:
    """4004 (authentication failed) is terminal for this token: the gateway must go quiet rather
    than re-IDENTIFY on the backoff schedule forever, and must say so exactly once."""
    server = FakeGatewayServer()
    gw, stop, thread = _armed_gateway(store, bus, monkeypatch, server)
    try:
        assert _serve_hello_and_read_identify(server)["op"] == 2
        server.close_connection(4004, "Authentication failed.")

        with pytest.raises(TimeoutError):
            server.wait_for_connection(timeout=0.5)  # no redial: several backoffs have elapsed
        notices = store.list_queued_notices()
        assert len(notices) == 1
        assert "reconnect with `selly-agent connect discord`" in notices[0]["text"]
        assert FAKE_TOKEN not in notices[0]["text"]
    finally:
        stop.set()
        thread.join(timeout=2.0)
        server.close()


def test_a_new_token_clears_the_latch_without_a_restart(store, bus, xdg_tmp, monkeypatch) -> None:
    """The latch is keyed to the refused token's value, so a seller re-running `connect discord`
    with a fresh token is picked up by the running daemon — it must not need a restart to recover
    from a revoked one."""
    server = FakeGatewayServer()
    gw, stop, thread = _armed_gateway(store, bus, monkeypatch, server)
    try:
        _serve_hello_and_read_identify(server)
        server.close_connection(4004, "Authentication failed.")
        with pytest.raises(TimeoutError):
            server.wait_for_connection(timeout=0.3)

        replacement = FAKE_TOKEN[:-1] + ("A" if FAKE_TOKEN[-1] != "A" else "B")
        secrets.write_discord_bot_token(replacement)
        identify = _serve_hello_and_read_identify(server)  # redials on the new value
        assert identify["d"]["token"] == replacement
    finally:
        stop.set()
        thread.join(timeout=2.0)
        server.close()


def test_a_fresh_bind_attempt_clears_the_latch_even_with_the_same_token(
    store, bus, xdg_tmp, monkeypatch
) -> None:
    """Not every non-resumable code is really about the token's identity — a stale intents grant
    (4013/4014) or a transient false-positive 4004 doesn't mean *this token* is bad. A seller who
    fixes the real cause and re-runs `connect discord` with the SAME token must still recover
    without a daemon restart: `bind.connect_discord` calls `store.arm_bind` on every attempt,
    minting a fresh nonce regardless of whether the token string changed, so the latch has to key
    off that too — keying on the token alone would leave the gateway parked forever on a `connect`
    call that the seller was told succeeded."""
    server = FakeGatewayServer()
    gw, stop, thread = _armed_gateway(store, bus, monkeypatch, server)
    try:
        _serve_hello_and_read_identify(server)
        server.close_connection(4004, "Authentication failed.")
        with pytest.raises(TimeoutError):
            server.wait_for_connection(timeout=0.3)

        store.arm_bind("selly_test_bot", "nonce-xyz789", adapter="discord")  # same token, new nonce
        identify = _serve_hello_and_read_identify(server)  # redials despite the identical token
        assert identify["d"]["token"] == FAKE_TOKEN
    finally:
        stop.set()
        thread.join(timeout=2.0)
        server.close()


def test_a_resumable_close_reconnects_and_reidentifies(store, bus, xdg_tmp, monkeypatch) -> None:
    """Every close code outside the non-resumable set — 4000 here, but a bare network drop is the
    common one — is transient, so the gateway backs off and identifies again."""
    server = FakeGatewayServer()
    gw, stop, thread = _armed_gateway(store, bus, monkeypatch, server)
    try:
        assert _serve_hello_and_read_identify(server)["op"] == 2
        server.close_connection(4000, "Unknown error.")

        assert _serve_hello_and_read_identify(server)["op"] == 2  # dialed again
        assert store.list_queued_notices() == []  # transient: nothing to tell the seller
    finally:
        stop.set()
        thread.join(timeout=2.0)
        server.close()
