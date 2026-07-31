"""`selly-agent settings list|set|approve|cancel|undo` — the attended settings door.

Harness-independent: these verbs talk to the running daemon over its control routes (attended
bearer from the config-dir secret), never writing selly.db directly. The door is the same trust as
the channel buttons — an authenticated surface, a deterministic parse, a deterministic apply — so an
unbound, attended-only install can still approve a held change (the id comes from `settings list`).

`set` skips the approval round-trip that `propose_setting_change` goes through, and only that:
the gate is there to keep the *model* from changing things unasked, and someone typing here has
already given the signal it waits for. The value is JSON, so a list stays a list; a bare word is
taken as text for the settings that hold text.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from selly_agent import config, secrets

_LOCALHOST_ORIGIN = "http://127.0.0.1"


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _require_token() -> str | None:
    token = secrets.read_mcp_token()
    if not token:
        print(
            "selly-agent: no MCP token found — start the daemon first (selly-agent daemon run)",
            file=sys.stderr,
        )
    return token


def _post(url: str, token: str, body: dict) -> tuple:
    """POST and return (status, parsed body). A 4xx body is read too — it carries the reason a
    value was refused, which is the whole point of validating at the door."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": _LOCALHOST_ORIGIN,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return exc.code, {}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Origin": _LOCALHOST_ORIGIN})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run(args) -> int:
    token = _require_token()
    if not token:
        return 1
    port = config.load().http_port
    if args.settings_command == "list":
        return _list(port, token)
    if args.settings_command == "set":
        return set_setting(port, token, args.key, args.value)
    return _decide(port, token, args.settings_command, args.change_id)


def set_setting(port: int, token: str, key: str, value: str) -> int:
    """Apply one setting through the daemon. Shared with the installer, which sets the
    marketplaces the seller opted into through this same door rather than writing them itself."""
    try:
        status, result = _post(
            f"{_base_url(port)}/control/settings-set", token, {"key": key, "value": value}
        )
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"selly-agent: {result.get('error', 'could not set that')}", file=sys.stderr)
        return 1
    print(result.get("message", result.get("status", "done")))
    return 0


def _list(port: int, token: str) -> int:
    try:
        data = _get(f"{_base_url(port)}/control/settings-list?token={token}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    pending = data.get("pending", [])
    if pending:
        print("Pending changes (approve/cancel by id):")
        for p in pending:
            print(f"  {p['change_id']}  {p['label']}: {p['current']} → {p['proposed']}")
    else:
        print("No pending changes.")
    print("\nSettings:")
    for s in data.get("settings", []):
        gate = " (needs approval)" if s["requires_approval"] else ""
        print(f"  {s['key']}: {s['rendered']}{gate}")
    return 0


def _decide(port: int, token: str, action: str, change_id: str) -> int:
    try:
        _status, result = _post(
            f"{_base_url(port)}/control/settings-decide",
            token,
            {"action": action, "change_id": change_id},
        )
    except (urllib.error.URLError, OSError) as exc:
        print(f"selly-agent: could not reach the daemon: {exc}", file=sys.stderr)
        return 1
    print(result.get("message", result.get("status", "done")))
    return 0 if result.get("status") in ("applied", "cancelled", "undone") else 1
