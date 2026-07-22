"""Harness emitters: golden argv/settings/mcp/toml, round-trip validators, and posture pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from selly_agent.harness import claude, codex
from selly_agent.harness.model import PassSpec

GOLDEN = Path(__file__).parent / "golden"


def _spec(**overrides) -> PassSpec:
    base = dict(
        prompt="publish item item_123 using only your tools",
        model="sonnet",
        mcp_endpoint="http://127.0.0.1:7355/mcp",
        mcp_token="TESTTOKEN",
        allowed_tools=(
            "mcp__selly__get_item",
            "mcp__selly__carousell_ai_publish_listing",
            "mcp__selly__send_message",
        ),
        max_turns=20,
    )
    base.update(overrides)
    return PassSpec(**base)


# --- spec validation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"prompt": ""},
        {"model": ""},
        {"mcp_endpoint": "ftp://x"},
        {"mcp_token": ""},
        {"server_name": "bad name!"},
    ],
)
def test_passspec_rejects_malformed(overrides) -> None:
    with pytest.raises(ValueError):
        _spec(**overrides)


# --- claude goldens ---------------------------------------------------------------------------


def test_claude_pass_argv_matches_golden() -> None:
    argv = claude.pass_argv(_spec(), claude_bin="claude")
    assert argv == json.loads((GOLDEN / "claude_pass_argv.json").read_text())


def test_claude_workspace_matches_golden() -> None:
    files = claude.render_workspace(_spec())
    assert files[".mcp.json"] == (GOLDEN / "claude_mcp.json").read_text()
    assert files[".claude/settings.json"] == (GOLDEN / "claude_settings.json").read_text()


def test_allowed_tools_is_last_and_no_bash() -> None:
    argv = claude.pass_argv(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1 :] == list(_spec().allowed_tools)  # nothing after the tool list
    assert "Bash" not in argv
    # settings deny the escape vectors explicitly
    deny = claude.settings_json(_spec())["permissions"]["deny"]
    assert "Bash" in deny


def test_stream_json_forces_verbose() -> None:
    argv = claude.pass_argv(_spec())
    assert "--verbose" in argv
    text_argv = claude.pass_argv(_spec(output_format="text"))
    assert "--verbose" not in text_argv


def test_token_is_in_the_header_not_bare() -> None:
    cfg = claude.mcp_config(_spec())
    assert cfg["mcpServers"]["selly"]["headers"]["Authorization"] == "Bearer TESTTOKEN"


# --- codex golden + round trip ----------------------------------------------------------------


def test_codex_config_matches_golden() -> None:
    assert codex.render_config(_spec()) == (GOLDEN / "codex_config.toml").read_text()


def test_codex_round_trips_and_points_at_proxy() -> None:
    parsed = codex.parse_toml_min(codex.render_config(_spec(model="opus")))
    assert parsed["model"] == "opus"
    assert parsed["mcp_servers"]["selly"] == {"command": "selly-agent", "args": ["mcp-proxy"]}


def test_toml_min_parser_handles_the_subset() -> None:
    text = 'a = "x"\nn = 3\n[s.t]\narr = ["p", "q"]\n'
    parsed = codex.parse_toml_min(text)
    assert parsed == {"a": "x", "n": 3, "s": {"t": {"arr": ["p", "q"]}}}
