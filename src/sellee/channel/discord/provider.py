"""The Discord provider's lifecycle: start its Gateway session thread + delivery lanes, and shut
them down. Mirrors telegram/provider.py exactly at this seam — `start` spins the Gateway on its own
stop event and registers the provider-specific delivery tasks (notice drain, typing pulse), which
use Discord's `deliver`/`typing` mechanisms over the core outbound policy; the returned handle stops
the Gateway and removes those lanes. `is_configured` is "a bot token has been written."
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sellee import secrets
from sellee.channel import outbound
from sellee.channel.discord import outbound as discord_outbound
from sellee.channel.discord.gateway import DiscordGateway
from sellee.scheduler import Task

_DRAIN_TASK = "notice_drain"
_TYPING_TASK = "typing_pulse"


@dataclass
class DiscordHandle:
    stop: threading.Event
    thread: threading.Thread
    scheduler: object
    task_names: list

    def shutdown(self) -> None:
        self.stop.set()
        for name in self.task_names:
            self.scheduler.deregister(name)
        self.thread.join(timeout=10.0)


def is_configured() -> bool:
    return secrets.read_discord_bot_token() is not None


def start(*, bus, store, config, scheduler) -> DiscordHandle:
    stop = threading.Event()
    gateway = DiscordGateway(store=store, config=config, bus=bus, stop_event=stop)
    scheduler.register(
        Task(
            name=_DRAIN_TASK,
            interval_sec=outbound.NOTICE_DRAIN_INTERVAL_SEC,
            func=lambda: outbound.drain_notices(
                store=store, bus=bus, deliver=discord_outbound.make_deliver(config)
            ),
        )
    )
    scheduler.register(
        Task(
            name=_TYPING_TASK,
            interval_sec=outbound.TYPING_PULSE_INTERVAL_SEC,
            func=lambda: outbound.pulse_typing(
                store=store, typing=discord_outbound.make_typing(config)
            ),
        )
    )
    thread = threading.Thread(target=gateway.run, name="channel-discord-gateway", daemon=True)
    thread.start()
    return DiscordHandle(
        stop=stop, thread=thread, scheduler=scheduler, task_names=[_DRAIN_TASK, _TYPING_TASK]
    )
