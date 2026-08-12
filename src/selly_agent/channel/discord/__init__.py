"""The Discord provider: everything Gateway/REST-specific behind the channel core.

`ws_client` is a minimal hand-rolled RFC 6455 WebSocket client (no third-party dependency is
allowlisted for one); `gateway` is the Gateway session loop and its off/awaiting-bind/bound states
— the Discord analog of `telegram/poller.py`, since Discord has no long-poll receive API and
instead requires a persistent, outbound-initiated WebSocket connection; `transport` is the REST
client (the one HTTP network module here) plus the pure `_normalize`; `bind` is the invite+DM-nonce
connect flow; `commands` renders the core's control spec into Discord message components; `outbound`
supplies the `deliver`/`typing` callables the core outbound policy calls.
"""

from __future__ import annotations
