"""`store.KNOWN_ADAPTERS` is the adapter allowlist, so it has to describe reality.

`channel.adapter` carries no enumerating CHECK — widening one means recreating the table in SQLite
— so the column accepts any non-empty string and `arm_bind` does the validating instead. That makes
KNOWN_ADAPTERS load-bearing in two directions, both pinned here: an adapter it omits can never be
bound however well its provider works, and an adapter it names but the daemon cannot start would
leave a seller armed and waiting for a provider that never runs.
"""

from __future__ import annotations

import ast
from pathlib import Path

from sellee import store

SRC = Path(__file__).resolve().parents[2] / "src"
DAEMON = SRC / "sellee" / "daemon.py"
CHANNEL = SRC / "sellee" / "channel"


def _daemon_provider_names() -> set:
    """The keys of the `providers={...}` literal the daemon hands ChannelManager, read out of the
    source: the map is built inside `serve()`, so reaching it any other way means booting a
    daemon."""
    tree = ast.parse(DAEMON.read_text(), filename=str(DAEMON))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "ChannelManager":
            continue
        for keyword in node.keywords:
            if keyword.arg == "providers" and isinstance(keyword.value, ast.Dict):
                return {k.value for k in keyword.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("no ChannelManager(providers={...}) literal found in daemon.py")


def test_known_adapters_match_the_daemons_providers() -> None:
    assert _daemon_provider_names() == set(store.KNOWN_ADAPTERS)


def test_every_known_adapter_has_a_provider_package() -> None:
    missing = [a for a in store.KNOWN_ADAPTERS if not (CHANNEL / a / "provider.py").exists()]
    assert not missing, f"KNOWN_ADAPTERS names a provider that does not exist: {missing}"
