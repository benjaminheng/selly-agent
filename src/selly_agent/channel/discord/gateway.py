"""The Discord Gateway session — the receive side, since Discord has no long-poll equivalent for
DMs: a persistent, outbound-initiated WebSocket connection this bot dials, authenticates over
(IDENTIFY), and keeps alive (heartbeat). This is the Discord analog of `telegram/poller.py`'s
long-poll loop; the state derivation (off/awaiting-bind/bound) and the 5s->60s backoff-on-error
shape are the same idea, adapted to a connection that stays open rather than one HTTP call per
tick.

Opcodes and payload shapes below are Discord Gateway v10, confirmed against the official developer
documentation (docs.discord.com/developers/events/gateway) at the time this was written:
Dispatch=0, Heartbeat=1, Identify=2, Resume=6, Reconnect=7, Invalid Session=9, Hello=10,
Heartbeat ACK=11. Close codes 4004/4010/4011/4012/4013/4014 must not reconnect (a bad token, shard,
API version, or intent grant will never resolve itself); every other close — including a bare
network drop — attempts Resume, falling back to a fresh Identify if the server says the session
isn't resumable (Invalid Session with d=false).

The Gateway URL is the well-known static host (gateway.discord.gg) rather than the one GET
/gateway/bot returns dynamically: that endpoint's extra session_start_limit info matters for large,
multi-shard bots managing a start budget, not a single-shard, one-bot-one-seller bot like this one.

Intents request only DIRECT_MESSAGES (4096): message content in a DM is exempt from the privileged
MESSAGE_CONTENT intent by Discord's own documented policy ("Content in DMs with the app" is
delivered without it), so this bot needs zero privileged grants and no GUILDS intent either — it
never looks at guild events.

This module holds session mechanics only (connect/Identify/heartbeat/Resume) — proved against a
fake Gateway speaking real opcodes over a real local WebSocket connection in
tests/test_channel_discord_gateway_session.py. Wiring Dispatch payloads into the store (bind, DM
ingest, fast-paths) is a later task, appended below this point.
"""

from __future__ import annotations

import json
import logging
import random

from selly_agent.channel.discord.ws_client import ConnectionClosed, connect

log = logging.getLogger(__name__)

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_DIRECT_MESSAGES = 1 << 12

NON_RESUMABLE_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})

GATEWAY_HOST = "gateway.discord.gg"
GATEWAY_PORT = 443
GATEWAY_PATH = "/?v=10&encoding=json"

OFF_IDLE_SEC = 1.0
BACKOFF_BASE_SEC = 5.0
BACKOFF_CAP_SEC = 60.0


def _identify_payload(token: str) -> dict:
    return {
        "op": OP_IDENTIFY,
        "d": {
            "token": token,
            "intents": INTENT_DIRECT_MESSAGES,
            "properties": {"os": "linux", "browser": "selly-agent", "device": "selly-agent"},
        },
    }


def _resume_payload(*, token: str, session_id: str, seq: int) -> dict:
    return {"op": OP_RESUME, "d": {"token": token, "session_id": session_id, "seq": seq}}


class _NeverStop:
    """A stop_event stand-in for a session run outside the daemon (tests, or a caller with its own
    lifecycle) — `is_set()` is always False, so `_pump_until_stopped` only returns via an
    exception."""

    def is_set(self) -> bool:
        return False


class GatewaySession:
    """One connect-through-disconnect Gateway session. `on_dispatch(event_type, data)` is called
    for every Dispatch (`op:0`) payload — `DiscordGateway` (below) wires this to ingest
    MESSAGE_CREATE/INTERACTION_CREATE into the store; this class knows nothing about the store."""

    def __init__(self, *, token: str, on_dispatch, use_tls: bool = True):
        self._token = token
        self._on_dispatch = on_dispatch
        self._use_tls = use_tls
        self._ws = None
        self._session_id: str | None = None
        self._seq: int | None = None
        self._heartbeat_interval_sec: float = 0.0
        self._ack_pending = False

    def connect_and_identify(self) -> None:
        self._ws = connect(GATEWAY_HOST, GATEWAY_PORT, GATEWAY_PATH, use_tls=self._use_tls)
        self._read_until_ready()

    def connect_and_resume(self) -> None:
        self._ws = connect(GATEWAY_HOST, GATEWAY_PORT, GATEWAY_PATH, use_tls=self._use_tls)
        hello = json.loads(self._ws.recv_text())
        self._heartbeat_interval_sec = hello["d"]["heartbeat_interval"] / 1000.0
        self._ws.send_text(
            json.dumps(
                _resume_payload(token=self._token, session_id=self._session_id, seq=self._seq)
            )
        )

    def _read_until_ready(self) -> None:
        """Consume HELLO, send IDENTIFY, wait for READY (capturing session_id/seq)."""
        hello = json.loads(self._ws.recv_text())
        assert hello["op"] == OP_HELLO
        self._heartbeat_interval_sec = hello["d"]["heartbeat_interval"] / 1000.0
        self._ws.send_text(json.dumps(_identify_payload(self._token)))
        while True:
            message = json.loads(self._ws.recv_text())
            if message["s"] is not None:
                self._seq = message["s"]
            if message["op"] == OP_DISPATCH and message.get("t") == "READY":
                self._session_id = message["d"]["session_id"]
                return
            if message["op"] == OP_DISPATCH:
                self._on_dispatch(message.get("t"), message.get("d"))

    def _send_heartbeat(self) -> None:
        self._ws.send_text(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))
        self._ack_pending = True

    def _pump_until_stopped(self, stop_event) -> None:
        """The steady-state loop: race "a message arrived" against "the heartbeat is due", for as
        long as `stop_event` isn't set. Raises ConnectionClosed on a close frame, a zombied
        connection (no ACK before the next heartbeat), or Reconnect/non-resumable Invalid Session —
        the caller decides whether to Resume or re-Identify from there."""
        first_heartbeat = True
        while not stop_event.is_set():
            wait_for = self._heartbeat_interval_sec * (random.random() if first_heartbeat else 1.0)
            first_heartbeat = False
            if not self._ws.wait_readable(wait_for):
                if self._ack_pending:
                    raise ConnectionClosed("zombied: no Heartbeat ACK before the next was due")
                self._send_heartbeat()
                continue
            message = json.loads(self._ws.recv_text())
            if message.get("s") is not None:
                self._seq = message["s"]
            op = message["op"]
            if op == OP_HEARTBEAT_ACK:
                self._ack_pending = False
            elif op == OP_HEARTBEAT:
                self._send_heartbeat()
            elif op == OP_RECONNECT:
                raise ConnectionClosed("server requested a reconnect")
            elif op == OP_INVALID_SESSION:
                resumable = bool(message.get("d"))
                if not resumable:
                    self._session_id = None
                raise ConnectionClosed(f"invalid session (resumable={resumable})")
            elif op == OP_DISPATCH:
                self._on_dispatch(message.get("t"), message.get("d"))

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
