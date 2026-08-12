"""Discord's connect flow: validate + prove the token, persist it, arm a fresh bind nonce, and
return the OAuth invite URL — driven against the real DiscordClient over the fake REST API.
"""

from __future__ import annotations

import pytest

from fake_discord_api import APPLICATION_ID, BOT, FAKE_TOKEN, FakeDiscordAPI
from selly_agent import secrets
from selly_agent.channel.discord.bind import BindError, channel_status, connect_discord
from selly_agent.channel.discord.transport import DiscordClient
from selly_agent.config import Config


def _make_client_for(api):
    def _make(token, config):
        return DiscordClient(token, api_base=api.base_url + "/api/v10")

    return _make


def test_connect_discord_writes_token_arms_nonce_and_returns_invite_url(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        result = connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
        assert result["bot_username"] == BOT["username"]
        assert result["application_id"] == APPLICATION_ID
        assert result["invite_url"] == (
            f"https://discord.com/oauth2/authorize?client_id={APPLICATION_ID}"
            "&scope=bot&permissions=0"
        )
        assert secrets.read_discord_bot_token() == FAKE_TOKEN
        ch = store.get_channel()
        assert ch["adapter"] == "discord"
        assert ch["bind_nonce"] == result["nonce"]
        assert ch["chat_id"] is None


def test_connect_discord_rejects_a_bad_token_shape(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        with pytest.raises(BindError) as exc:
            connect_discord(store, Config(), "not-a-real-token", make_client=_make_client_for(api))
        assert exc.value.kind == "bad_token_format"
        assert secrets.read_discord_bot_token() is None


def test_channel_status_reports_awaiting_bind_after_connect(store, xdg_tmp) -> None:
    with FakeDiscordAPI() as api:
        connect_discord(store, Config(), FAKE_TOKEN, make_client=_make_client_for(api))
    status = channel_status(store)
    assert status["awaiting_bind"] is True
    assert status["bound"] is False
    assert status["bot_username"] == BOT["username"]
