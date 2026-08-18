"""Discord outbound mechanism: the `deliver` / `typing` callables the core outbound policy calls.

Each builds a transport client from the current token and performs one send. The core policy (when
to send, FIFO, bump-and-retry, the typing-pulse gate) lives in `channel.outbound`; these are only
the Discord-specific act of putting bytes on the wire.
"""

from __future__ import annotations

from sellee import secrets
from sellee.channel.discord.transport import ChannelError, DiscordClient


def _client(config) -> DiscordClient:
    token = secrets.read_discord_bot_token()
    if not token:
        # Only reachable in a crash window (chat bound, token gone); the core gates on chat_id.
        raise ChannelError("discord token missing")
    return DiscordClient(token, api_base=config.discord_api_base)


def make_deliver(config):
    def deliver(chat_id, text, controls=None) -> None:
        _client(config).send_message(chat_id, text, components=controls)

    return deliver


def make_typing(config):
    def typing(chat_id) -> None:
        _client(config).trigger_typing(chat_id)

    return typing
