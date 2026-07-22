"""Daemon configuration — read-only from the daemon's side.

Reads config.json from the config dir. A missing file means all defaults: the daemon never
writes config (config writers are the installer and tools). Invalid values are rejected at
startup with a clear error rather than sanitized — a fat-fingered config should fail loud,
not silently become something the user didn't ask for. Unknown keys warn and are ignored so
a newer config stays readable by an older build across an update.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_VALID_DAEMON_MODES = {"login-start", "manual"}


class ConfigError(Exception):
    """A config value is present but invalid. Raised at startup; never sanitized away."""


@dataclass(frozen=True)
class Config:
    log_level: str = "INFO"
    tick_interval_sec: float = 5.0
    retention_days: int = 14
    backups_keep: int = 5
    # Recorded by the installer, read by daemon status. Not consumed by the daemon loop.
    daemon_mode: str = "manual"
    daemon_label: str | None = None


def _is_real_number(value: object) -> bool:
    # bool is an int subclass; a JSON true/false is never a valid numeric knob.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(raw: dict) -> Config:
    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            log.warning("unknown config key %r ignored", key)

    values: dict = {}

    if "log_level" in raw:
        level = raw["log_level"]
        if not isinstance(level, str) or level.upper() not in _VALID_LOG_LEVELS:
            raise ConfigError(
                f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {level!r}"
            )
        values["log_level"] = level.upper()

    if "tick_interval_sec" in raw:
        tick = raw["tick_interval_sec"]
        if not _is_real_number(tick) or tick <= 0:
            raise ConfigError(f"tick_interval_sec must be a positive number, got {tick!r}")
        values["tick_interval_sec"] = float(tick)

    if "retention_days" in raw:
        days = raw["retention_days"]
        if not _is_real_int(days) or days < 1:
            raise ConfigError(f"retention_days must be an integer >= 1, got {days!r}")
        values["retention_days"] = days

    if "backups_keep" in raw:
        keep = raw["backups_keep"]
        if not _is_real_int(keep) or keep < 0:
            raise ConfigError(f"backups_keep must be an integer >= 0, got {keep!r}")
        values["backups_keep"] = keep

    if "daemon_mode" in raw:
        mode = raw["daemon_mode"]
        if mode not in _VALID_DAEMON_MODES:
            raise ConfigError(
                f"daemon_mode must be one of {sorted(_VALID_DAEMON_MODES)}, got {mode!r}"
            )
        values["daemon_mode"] = mode

    if "daemon_label" in raw:
        label = raw["daemon_label"]
        if label is not None and not isinstance(label, str):
            raise ConfigError(f"daemon_label must be a string or null, got {label!r}")
        values["daemon_label"] = label

    return Config(**values)


def load(path: Path | None = None) -> Config:
    """Load config from `path` (default: the canonical config path). Missing file → defaults."""
    target = path if path is not None else paths.config_path()
    try:
        text = target.read_text()
    except FileNotFoundError:
        return Config()
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{target} must contain a JSON object, got {type(raw).__name__}")
    return _validate(raw)


def merge_into_file(updates: dict, path: Path | None = None) -> None:
    """Merge keys into config.json, preserving the rest. For the installer and tools — NOT the
    daemon, which only ever reads config. Values are validated on the next load()."""
    target = path if path is not None else paths.config_path()
    paths.ensure_config_dir()
    try:
        raw = json.loads(target.read_text())
        if not isinstance(raw, dict):
            raw = {}
    except FileNotFoundError:
        raw = {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{target} is not valid JSON: {exc}") from exc
    raw.update(updates)
    target.write_text(json.dumps(raw, indent=2) + "\n")
