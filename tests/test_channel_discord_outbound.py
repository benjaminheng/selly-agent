"""The Discord deliver/typing mechanism the core outbound policy calls — driven against the fake
REST API, mirroring tests/test_channel_transport.py's Telegram counterpart at the outbound layer.
"""

from __future__ import annotations

from fake_discord_api import CHANNEL_ID, FAKE_TOKEN, FakeDiscordAPI
from sellee import secrets
from sellee.channel.discord import outbound
from sellee.config import Config


def test_deliver_sends_text_and_controls(xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    with FakeDiscordAPI() as api:
        deliver = outbound.make_deliver(Config(discord_api_base=api.base_url + "/api/v10"))
        deliver(CHANNEL_ID, "hello", [("Pause", "pause")])
        assert api.outbox[-1]["content"] == "hello"
        assert api.outbox[-1]["components"][0]["components"][0]["custom_id"] == "pause"


def test_deliver_with_no_controls(xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    with FakeDiscordAPI() as api:
        deliver = outbound.make_deliver(Config(discord_api_base=api.base_url + "/api/v10"))
        deliver(CHANNEL_ID, "hello")
        assert "components" not in api.outbox[-1]


def test_typing(xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    with FakeDiscordAPI() as api:
        typing = outbound.make_typing(Config(discord_api_base=api.base_url + "/api/v10"))
        typing(CHANNEL_ID)
        assert api.typing_pulses == [True]
