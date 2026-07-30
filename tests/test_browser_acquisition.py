"""Acquiring the browser means ensuring it runs — the daemon's one acquisition path.

Every actor that needs the browser (the read lane, the reply send, the selector probe, the
fan-out) goes through the factory `make_browser_factory` builds. These tests pin what acquiring
does: Node first, Chrome ensured on every call, the window announced when a launch happened, and
`BrowserUnavailable` carrying the by-hand command when Chrome will not come up.
"""

from __future__ import annotations

import pytest

from selly_agent import daemon
from selly_agent.browser import chrome
from selly_agent.browser.client import BrowserUnavailable
from selly_agent.config import Config


def _notices(store):
    return [n["text"] for n in store.claim_queued_notices(10)]


def _launch_events(bus):
    return [ev for ev in bus.store.read() if ev.kind == "browser.chrome_launched"]


# --- ensure_chrome ------------------------------------------------------------------------------


def test_a_ready_chrome_is_acquired_silently(store, bus, monkeypatch) -> None:
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: chrome.READY)
    daemon.ensure_chrome(Config(), store, bus)
    assert _notices(store) == []
    assert _launch_events(bus) == []


def test_a_launched_chrome_is_announced_and_evented(store, bus, monkeypatch) -> None:
    """A window appearing on its own is alarming; the seller hears why before anything drives it.
    The copy names no flow, because any actor may be the one that opens it."""
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: chrome.LAUNCHED)
    daemon.ensure_chrome(Config(), store, bus)
    assert _notices(store) == [daemon.CHROME_STARTED_NOTICE]
    assert len(_launch_events(bus)) == 1


def test_a_chrome_that_cannot_start_raises_the_by_hand_command(store, bus, monkeypatch) -> None:
    monkeypatch.setattr(daemon.chrome, "ensure_running", lambda port, **kw: chrome.UNAVAILABLE)
    with pytest.raises(BrowserUnavailable) as exc:
        daemon.ensure_chrome(Config(), store, bus)
    assert "--remote-debugging-port" in str(exc.value)
    assert _notices(store) == []


# --- the factory --------------------------------------------------------------------------------


def test_node_is_checked_before_chrome_is_started(store, bus, monkeypatch) -> None:
    """A machine that cannot drive a browser must never have one opened for it."""
    ensures = []

    def _no_node(command):
        raise BrowserUnavailable("'npx' is not installed")

    monkeypatch.setattr(daemon.browser_client, "ensure_available", _no_node)
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: ensures.append(port) or chrome.READY
    )

    factory = daemon.make_browser_factory(Config(), store, bus, {})
    with pytest.raises(BrowserUnavailable):
        factory()
    assert ensures == []


def test_every_acquisition_re_ensures_chrome_and_reuses_the_client(store, bus, monkeypatch) -> None:
    """The client is cached for the daemon's life, but Chrome can be closed at any point after —
    so the ensure runs per call, outside the construction guard."""
    ensures = []
    monkeypatch.setattr(daemon.browser_client, "ensure_available", lambda command: None)
    monkeypatch.setattr(
        daemon.chrome, "ensure_running", lambda port, **kw: ensures.append(port) or chrome.READY
    )

    holder: dict = {}
    factory = daemon.make_browser_factory(Config(), store, bus, holder)
    first = factory()
    second = factory()
    assert first is second is holder["client"]
    assert len(ensures) == 2
