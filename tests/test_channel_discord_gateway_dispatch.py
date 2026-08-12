"""DiscordGateway: the off/awaiting-bind/bound state derivation, nonce-in-a-DM bind matching, and
fast-path dispatch — the Discord analog of tests/test_channel_bind.py + the poller half of
test_channel_transport.py, driven against the fake REST API for sends and a direct on_dispatch
call for the Gateway side (the full-stack WS round trip is covered by Task 7's session tests, so
this task's tests inject dispatch payloads directly rather than re-proving the transport).
"""

from __future__ import annotations

from fake_discord_api import CHANNEL_ID, FAKE_TOKEN, FakeDiscordAPI
from selly_agent import secrets
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
