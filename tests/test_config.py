"""Config loads defaults, honors overrides, rejects invalid values, warns on unknown keys."""

from __future__ import annotations

import json
import logging

import pytest

from selly_agent import paths
from selly_agent.config import Config, ConfigError, load


def _write_config(obj) -> None:
    paths.ensure_config_dir()
    paths.config_path().write_text(json.dumps(obj))


def test_missing_file_yields_all_defaults(xdg_tmp) -> None:
    cfg = load()
    assert cfg == Config()
    assert cfg.log_level == "INFO"
    assert cfg.tick_interval_sec == 5.0


def test_values_are_read_from_the_config_path(xdg_tmp) -> None:
    _write_config(
        {
            "log_level": "debug",
            "tick_interval_sec": 2,
            "retention_days": 30,
            "backups_keep": 3,
        }
    )
    cfg = load()
    assert cfg.log_level == "DEBUG"  # normalized to canonical case
    assert cfg.tick_interval_sec == 2.0
    assert cfg.retention_days == 30
    assert cfg.backups_keep == 3


def test_pass_and_http_knobs_are_read(xdg_tmp) -> None:
    _write_config(
        {
            "http_port": 8123,
            "pass_deadline_sec": 300,
            "pass_model": "opus",
            "claude_bin": "/opt/claude/bin/claude",
            "carousell_ai_api_base": "http://127.0.0.1:9999/",
            "carousell_ai_web_base_url": "http://127.0.0.1:9998",
        }
    )
    cfg = load()
    assert cfg.http_port == 8123
    assert cfg.pass_deadline_sec == 300.0
    assert cfg.pass_model == "opus"
    assert cfg.claude_bin == "/opt/claude/bin/claude"
    assert cfg.carousell_ai_api_base == "http://127.0.0.1:9999"  # trailing slash trimmed
    assert cfg.carousell_ai_web_base_url == "http://127.0.0.1:9998"


def test_claude_bin_defaults_to_null(xdg_tmp) -> None:
    _write_config({"claude_bin": None})
    assert load().claude_bin is None


@pytest.mark.parametrize(
    "obj",
    [
        {"log_level": "LOUD"},
        {"tick_interval_sec": 0},
        {"tick_interval_sec": -1},
        {"tick_interval_sec": "fast"},
        {"tick_interval_sec": True},
        {"retention_days": 0},
        {"retention_days": 1.5},
        {"backups_keep": -1},
        {"http_port": 80},
        {"http_port": 70000},
        {"http_port": "7355"},
        {"http_port": True},
        {"pass_deadline_sec": 0},
        {"pass_deadline_sec": "long"},
        {"pass_model": ""},
        {"pass_model": 5},
        {"claude_bin": ""},
        {"claude_bin": 5},
        {"carousell_ai_api_base": "api.carousell.ai"},
        {"carousell_ai_api_base": " https://api.carousell.ai"},
        {"carousell_ai_web_base_url": "ftp://x"},
    ],
)
def test_invalid_values_are_rejected_not_sanitized(xdg_tmp, obj) -> None:
    _write_config(obj)
    with pytest.raises(ConfigError):
        load()


def test_unknown_keys_warn_and_are_ignored(xdg_tmp, caplog) -> None:
    _write_config({"tick_interval_sec": 7, "future_knob": "whatever"})
    with caplog.at_level(logging.WARNING):
        cfg = load()
    assert cfg.tick_interval_sec == 7.0
    assert any("future_knob" in rec.message for rec in caplog.records)


def test_non_object_json_is_rejected(xdg_tmp) -> None:
    _write_config([1, 2, 3])
    with pytest.raises(ConfigError):
        load()
