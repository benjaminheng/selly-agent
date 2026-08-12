"""DiscordGateway: the off/awaiting-bind/bound state derivation, nonce-in-a-DM bind matching, and
fast-path dispatch — the Discord analog of tests/test_channel_bind.py + the poller half of
test_channel_transport.py, driven against the fake REST API for sends and a direct on_dispatch
call for the Gateway side (the full-stack WS round trip is covered by Task 7's session tests, so
this task's tests inject dispatch payloads directly rather than re-proving the transport).
"""

from __future__ import annotations

import time

import pytest

from fake_discord_api import CHANNEL_ID, FAKE_TOKEN, FakeDiscordAPI
from selly_agent import secrets
from selly_agent.channel.discord import gateway
from selly_agent.channel.discord.gateway import DiscordGateway
from selly_agent.config import Config

_NONCE = "nonce-abc123"


def _gateway(store, bus, api):
    config = Config(discord_api_base=api.base_url + "/api/v10")
    return DiscordGateway(store=store, config=config, bus=bus)


def test_state_off_with_no_token(store) -> None:
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(None, store.get_channel()) == "off"


def test_state_awaiting_bind_with_token_and_nonce(store, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(FAKE_TOKEN, store.get_channel()) == "awaiting_bind"


def test_state_bound_once_chat_id_is_set(store, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0)
    gw = DiscordGateway(store=store, config=Config(), bus=None)
    assert gw._state(FAKE_TOKEN, store.get_channel()) == "bound"


def test_a_dm_matching_the_nonce_binds(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_awaiting_bind_message(
            {"id": "1", "channel_id": str(CHANNEL_ID), "author": {"bot": False}, "content": _NONCE}
        )
        ch = store.get_channel()
        assert ch["chat_id"] == CHANNEL_ID
        assert ch["bind_nonce"] is None


def test_a_dm_not_matching_the_nonce_never_binds(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_awaiting_bind_message(
            {"id": "1", "channel_id": str(CHANNEL_ID), "author": {"bot": False}, "content": "wrong"}
        )
        assert store.get_channel()["chat_id"] is None


def test_fast_path_command_replies_and_marks_handled(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0)
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_bound_message(
            {
                "id": "1",
                "channel_id": str(CHANNEL_ID),
                "author": {"bot": False},
                "content": "/status",
            }
        )
        assert any("Status:" in m["content"] for m in api.outbox)


def test_free_text_stays_pending_for_the_channel_pass(store, bus, xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    store.arm_bind("selly_test_bot", _NONCE, adapter="discord")
    store.complete_bind(CHANNEL_ID, 0)
    with FakeDiscordAPI() as api:
        gw = _gateway(store, bus, api)
        gw._handle_bound_message(
            {
                "id": "1",
                "channel_id": str(CHANNEL_ID),
                "author": {"bot": False},
                "content": "how much for the lamp",
            }
        )
    assert store.has_active_channel_pass()


# --- run()'s throttling with no stop_event (the constructor's own default) --------------------
#
# A bare DiscordGateway() — no daemon, no stop_event supplied — must never busy-spin: the
# off-idle wait and the error-backoff wait inside run() have to keep sleeping for real even
# though there's no real threading.Event to wait on. Covered two ways: a direct, deterministic
# unit test of _NeverStop.wait() itself (no need to touch run()'s loop at all), and a timed
# run() call bounded to a handful of iterations (run() has no natural exit with stop=None, so the
# bound is a monkeypatched _NeverStop.wait that raises after N calls — the same technique a real
# stop_event's is_set() flipping True would trigger, just deterministic instead of racy).


def test_neverstop_wait_actually_sleeps() -> None:
    stopper = gateway._NeverStop()
    started = time.monotonic()
    result = stopper.wait(0.05)
    elapsed = time.monotonic() - started
    assert result is False
    assert elapsed >= 0.05


class _StopTestLoop(Exception):
    """Raised from a monkeypatched _NeverStop.wait to give run()'s otherwise-infinite
    (stop_event=None) loop a deterministic exit after a fixed number of iterations."""


def test_run_throttles_the_off_idle_wait_with_no_stop_event(store, xdg_tmp, monkeypatch) -> None:
    """No token is ever written (xdg_tmp keeps secrets hermetic), so _state is always "off" and
    run() only ever takes the off-idle branch. OFF_IDLE_SEC is monkeypatched down so the test
    stays fast; _NeverStop.wait is wrapped to count real calls and bail out after a few — proving
    run() calls a real, sleeping wait() on every iteration (not a no-op), the exact gap that let
    a stop_event=None DiscordGateway busy-spin at 100% CPU before this fix."""
    monkeypatch.setattr(gateway, "OFF_IDLE_SEC", 0.05)
    real_wait = gateway._NeverStop.wait
    calls: list = []

    def counting_wait(self, timeout):
        calls.append(timeout)
        if len(calls) >= 3:
            raise _StopTestLoop
        return real_wait(self, timeout)

    monkeypatch.setattr(gateway._NeverStop, "wait", counting_wait)
    gw = DiscordGateway(store=store, config=Config(), bus=None)  # stop_event defaults to None
    started = time.monotonic()
    with pytest.raises(_StopTestLoop):
        gw.run()
    elapsed = time.monotonic() - started
    assert calls == [0.05, 0.05, 0.05]
    # Two real sleeps must have elapsed before the third call raised — a busy spin would finish
    # in microseconds regardless of OFF_IDLE_SEC.
    assert elapsed >= 0.05 * 2
