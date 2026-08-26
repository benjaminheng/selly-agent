"""The browser layer's request rows: sign-in requests the seller made from chat.

A handoff between two threads, not a history. The provider's receive loop writes a row when the
seller taps **Sign in on desktop** (it must not drive Chrome itself — that loop answers every other
message), and `browser.connect`'s lane reads it, serves it, and deletes it. The durable record of
what happened is the notice queued back to the seller, never a row left behind here.
"""

from __future__ import annotations

from typing import TypedDict

from sellee.db import Database
from sellee.store.helpers import _now

# What the lane should do for a request. `open` is the seller asking to be signed in: navigate,
# pull the tab forward, and raise the window. `probe` is them saying they already have — re-read
# the login state without touching what is in front of them.
CONNECT_MODE_OPEN = "open"
CONNECT_MODE_PROBE = "probe"
CONNECT_MODES = (CONNECT_MODE_OPEN, CONNECT_MODE_PROBE)


class MarketConnectRequest(TypedDict):
    market: str
    mode: str
    requested_ts: float


class BrowserMixin:
    # Bound by Store.__init__; declared so a checker resolves it inside each mixin.
    _db: Database

    def request_market_connect(self, market: str, mode: str = CONNECT_MODE_OPEN) -> None:
        """Ask the connect lane to sign the seller in to `market`.

        Idempotent per market by the row's primary key: a seller who taps the button twice (or
        taps Check again while an open is still pending) replaces the request they already have
        rather than queueing a second navigation of the daemon's one shared tab. The newest tap
        wins, including its mode — it is the one that reflects what they are looking at now.
        """
        if mode not in CONNECT_MODES:
            raise ValueError(f"unknown market connect mode: {mode!r}")
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO market_connect_requests (market, mode, requested_ts) "
                "VALUES (?, ?, ?) ON CONFLICT (market) DO UPDATE SET "
                "mode = excluded.mode, requested_ts = excluded.requested_ts",
                (market, mode, _now()),
            )

    def pending_market_connects(self) -> list[MarketConnectRequest]:
        """Every outstanding request, oldest first — the order the lane serves them in."""
        rows = self._db.query(
            "SELECT market, mode, requested_ts FROM market_connect_requests "
            "ORDER BY requested_ts ASC, market ASC"
        )
        return [
            MarketConnectRequest(market=r["market"], mode=r["mode"], requested_ts=r["requested_ts"])
            for r in rows
        ]

    def clear_market_connect_request(self, market: str) -> None:
        """Drop a request once it has an answer. Safe to call for a row that is already gone."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM market_connect_requests WHERE market = ?", (market,))
