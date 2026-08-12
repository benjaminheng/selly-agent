"""Discord-specific command surface: rendering the core's provider-neutral controls spec into
Discord message components. There is no slash-command menu here (out of scope for this provider —
plain text works fine over DM, same as Telegram's own commands)."""

from __future__ import annotations

from selly_agent.channel.discord.transport import build_components


def render_controls(spec) -> list | None:
    """Render the core's (label, token) control spec into a single action row of buttons, or None
    when there are no controls (the fast paths that reply with plain text)."""
    if not spec:
        return None
    return build_components(spec)
