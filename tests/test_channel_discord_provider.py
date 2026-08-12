"""The Discord provider's lifecycle: is_configured, start spins the Gateway thread and registers
the drain/typing lanes, shutdown tears both down. Mirrors tests/test_channel_manager.py's Telegram
provider coverage.
"""

from __future__ import annotations

from fake_discord_api import FAKE_TOKEN
from selly_agent import secrets
from selly_agent.channel.discord import provider
from selly_agent.config import Config
from selly_agent.scheduler import Scheduler


def test_not_configured_with_no_token(xdg_tmp) -> None:
    assert provider.is_configured() is False


def test_configured_once_a_token_is_written(xdg_tmp) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    assert provider.is_configured() is True


def test_start_registers_drain_and_typing_lanes_then_shutdown_removes_them(
    store, bus, xdg_tmp
) -> None:
    secrets.write_discord_bot_token(FAKE_TOKEN)
    scheduler = Scheduler(bus, tick_interval_sec=100.0)
    handle = provider.start(bus=bus, store=store, config=Config(), scheduler=scheduler)
    assert "notice_drain" in scheduler._reg.tasks
    assert "typing_pulse" in scheduler._reg.tasks
    handle.shutdown()
    assert "notice_drain" not in scheduler._reg.tasks
    assert "typing_pulse" not in scheduler._reg.tasks
