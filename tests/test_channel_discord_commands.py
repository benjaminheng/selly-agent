from __future__ import annotations

from selly_agent.channel.discord.commands import render_controls


def test_render_controls_none_for_no_spec() -> None:
    assert render_controls(None) is None
    assert render_controls([]) is None


def test_render_controls_builds_one_action_row() -> None:
    components = render_controls([("Pause", "pause"), ("What needs me", "needsme")])
    assert components[0]["type"] == 1
    labels = [c["label"] for c in components[0]["components"]]
    assert labels == ["Pause", "What needs me"]
