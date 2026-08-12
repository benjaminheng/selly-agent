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

This module holds session mechanics (connect/Identify/heartbeat/Resume — proved against a fake
Gateway speaking real opcodes over a real local WebSocket connection in
tests/test_channel_discord_gateway_session.py) and, in `DiscordGateway` below, the wiring of
Dispatch payloads into the store: bind, DM ingest, and fast-path dispatch — the Discord analog of
`telegram/poller.py`.
"""

from __future__ import annotations

import json
import logging
import random
import time

from selly_agent import paths
from selly_agent.channel import fastpaths, outbound, routing
from selly_agent.channel.discord import transport as discord_transport
from selly_agent.channel.discord.transport import ChannelError, DiscordClient
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
    lifecycle) — `is_set()` is always False, so `_pump_until_stopped` only returns via an exception.
    `wait(timeout)` actually sleeps, so `DiscordGateway.run()`'s off-idle and error-backoff waits
    still throttle even with no real stop_event supplied — without this, a `DiscordGateway()`
    built with the constructor's own `stop_event=None` default would busy-spin at 100% CPU while
    off, and reconnect/IDENTIFY with zero delay after every failed session (an unthrottled storm
    against gateway.discord.gg — exactly what Discord's session_start_limit exists to punish)."""

    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float) -> bool:
        time.sleep(timeout)
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
        the caller decides whether to Resume or re-Identify from there.

        The heartbeat deadline is an absolute wall-clock time (`next_heartbeat_at`), computed once
        when due and left alone until a heartbeat is actually sent — not recomputed on every loop
        iteration. A fresh `wait_for` recomputed from the full interval on every inbound message
        would never reach zero on a connection that's receiving messages more often than the
        heartbeat interval (plausible during an active DM conversation, since Discord's real
        interval is ~41.25s), so heartbeats would never fire and Discord would eventually close the
        connection as a zombie — the opposite of what this loop exists to prevent."""
        first_heartbeat = True
        next_heartbeat_at: float | None = None
        while not stop_event.is_set():
            if next_heartbeat_at is None:
                jitter = random.random() if first_heartbeat else 1.0
                next_heartbeat_at = time.monotonic() + self._heartbeat_interval_sec * jitter
                first_heartbeat = False
            wait_for = max(0.0, next_heartbeat_at - time.monotonic())
            if not self._ws.wait_readable(wait_for):
                if self._ack_pending:
                    raise ConnectionClosed("zombied: no Heartbeat ACK before the next was due")
                self._send_heartbeat()
                next_heartbeat_at = None  # re-armed (full interval) on the next loop iteration
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


class DiscordGateway:
    """Owns the Discord Gateway connection end to end: which of off/awaiting-bind/bound applies
    (derived from durable rows each reconnect, exactly like `telegram/poller.py`'s three states),
    the bind-nonce match, and dispatching a bound DM's MESSAGE_CREATE/INTERACTION_CREATE into the
    same ingest -> fast-path -> routing pipeline the Telegram poller uses. Run `run()` on its own
    thread; `stop_event` is shared with the daemon's shutdown."""

    def __init__(self, *, store, config, bus, stop_event=None, client_factory=None):
        self.store = store
        self.config = config
        self.bus = bus
        self.stop = stop_event
        self._client_factory = client_factory or self._default_client_factory
        self._backoff = 0.0

    def _default_client_factory(self, token: str) -> DiscordClient:
        return DiscordClient(token, api_base=self.config.discord_api_base)

    def run(self) -> None:
        # Resolved once, not per-branch: `_NeverStop.wait` actually sleeps, so the off-idle and
        # error-backoff waits below throttle unconditionally even with the constructor's own
        # `stop_event=None` default — a bare `DiscordGateway()` must never busy-spin re-reading the
        # token/channel row, nor reconnect/IDENTIFY with zero delay after a failed session.
        stopper = self.stop or _NeverStop()
        while not stopper.is_set():
            token = self._read_token()
            ch = self.store.get_channel()
            state = self._state(token, ch)
            if state == "off":
                stopper.wait(OFF_IDLE_SEC)
                continue
            try:
                self._run_session(token)
            except Exception:
                self._arm_backoff()
                log.exception("Discord gateway session failed (backing off %.0fs)", self._backoff)
                stopper.wait(self._backoff)
            else:
                self._backoff = 0.0

    @staticmethod
    def _read_token() -> str | None:
        from selly_agent import secrets

        return secrets.read_discord_bot_token()

    def _arm_backoff(self) -> None:
        self._backoff = min(
            self._backoff * 2 if self._backoff else BACKOFF_BASE_SEC, BACKOFF_CAP_SEC
        )

    @staticmethod
    def _state(token, ch) -> str:
        if not token:
            return "off"
        if ch["chat_id"] is not None:
            return "bound"
        if ch["bind_nonce"]:
            return "awaiting_bind"
        return "off"

    def _run_session(self, token: str) -> None:
        client = self._client_factory(token)
        session = GatewaySession(
            token=token, on_dispatch=lambda t, d: self._on_dispatch(client, t, d)
        )
        try:
            session.connect_and_identify()
            session._pump_until_stopped(self.stop if self.stop is not None else _NeverStop())
        finally:
            session.close()

    def _on_dispatch(self, client: DiscordClient, event_type, data) -> None:
        if event_type not in ("MESSAGE_CREATE", "INTERACTION_CREATE"):
            return
        # Re-checked per message (not trusted from when the session started) — a bind can
        # complete mid-session, and this is cheap (one indexed row read).
        ch = self.store.get_channel()
        if ch["chat_id"] is None:
            if event_type == "MESSAGE_CREATE":
                self._handle_awaiting_bind_message(data)
            return
        if event_type == "MESSAGE_CREATE":
            self._handle_bound_message(data, client=client)
        else:
            self._handle_interaction(data, client=client)

    def _handle_awaiting_bind_message(self, data: dict) -> None:
        if (data.get("author") or {}).get("bot"):
            return
        ch = self.store.get_channel()
        nonce = ch["bind_nonce"]
        if nonce is None or data.get("content") != nonce:
            return  # unattributable pre-bind traffic — never adopts a stranger
        channel_id = int(data["channel_id"])
        self.store.complete_bind(channel_id, 0)
        ch = self.store.get_channel()
        if not ch["welcomed_at"]:
            try:
                outbound.queue_welcome(self.store)
            except Exception:
                log.exception("welcome queue failed (bind still complete)")
        self.bus.publish("channel.bound", {"bot_username": ch["bot_username"]})

    def _handle_bound_message(self, data: dict, *, client: DiscordClient | None = None) -> None:
        # `client` defaults to None so a test (or any other caller without a live session) can
        # inject a dispatch payload directly, the same way `_handle_awaiting_bind_message` needs
        # no client at all — `_on_dispatch` always supplies the session's client explicitly.
        if client is None:
            client = self._client_factory(self._read_token())
        ch = self.store.get_channel()
        if int(data["channel_id"]) != ch["chat_id"]:
            return  # not the authorized DM — dropped, never ingested
        event = discord_transport._normalize({"t": "MESSAGE_CREATE", "d": data})
        if event is None:
            return
        if event["kind"] == "photo":
            event["media_paths"] = self._download_photo(client, event)
        inserted = self.store.ingest_updates([event], 0)
        for row in inserted:
            routing.publish_channel_in(self.bus, row)
        self._dispatch_fast_paths(client, ch["chat_id"], inserted)
        routing.route_channel_pass(self.store, self.bus)

    def _handle_interaction(self, data: dict, *, client: DiscordClient) -> None:
        if data.get("type") != 3:  # only MESSAGE_COMPONENT (button) interactions
            return
        ch = self.store.get_channel()
        if int(data["channel_id"]) != ch["chat_id"]:
            return
        event = discord_transport._normalize({"t": "INTERACTION_CREATE", "d": data})
        if event is None:
            return
        inserted = self.store.ingest_updates([event], 0)
        for row in inserted:
            routing.publish_channel_in(self.bus, row)
        self._dispatch_fast_paths(client, ch["chat_id"], inserted)
        routing.route_channel_pass(self.store, self.bus)

    def _dispatch_fast_paths(self, client: DiscordClient, channel_id, inserted) -> None:
        """Mirrors `telegram/poller.py`'s `_dispatch_fast_paths`: answer deterministic fast paths
        instantly, before any pass routing, so /pause is heard even mid-pass. An interaction is
        acknowledged first (Discord requires the REST callback even though it arrived over the
        Gateway); everything else stays pending for the channel pass. `controls` (the core's raw
        (label, token) spec, or None) passes straight through to `send_message`, which builds
        the components itself — no separate render step here, same reasoning as outbound.py's
        deliver."""
        handled: list = []
        for row in inserted:
            event = {"kind": row["kind"], "text": row["text"], "payload": row["payload"]}
            if not fastpaths.is_fast_path(event):
                continue
            if fastpaths.is_settings_door(event):
                text, controls = fastpaths.handle_settings_door(self.store, self.bus, event)
            else:
                text, controls = fastpaths.handle_fast_path(self.store, event)
            interaction_id = row["payload"].get("interaction_id")
            if interaction_id:
                try:
                    client.acknowledge_interaction(
                        interaction_id, row["payload"]["interaction_token"]
                    )
                except ChannelError as exc:
                    log.warning("interaction acknowledge failed: %s", exc)
            try:
                client.send_message(channel_id, text, components=controls)
            except ChannelError as exc:
                log.warning("fast-path reply send failed: %s", exc)
            handled.append(row["id"])
            self.bus.publish("channel.handled", {"kind": row["kind"], "by": "fast_path"})
        if handled:
            self.store.mark_inbox_handled(handled, "fast_path")

    def _download_photo(self, client: DiscordClient, event: dict) -> list:
        url = event["payload"].get("url")
        if not url:
            return []
        dest = paths.media_dir() / str(event["event_id"]) / "photo.jpg"
        try:
            client.download_attachment(url, dest)
        except ChannelError as exc:
            log.warning("photo download failed for event %s: %s", event["event_id"], exc)
            return []
        return [str(dest)]
