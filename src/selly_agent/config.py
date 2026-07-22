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
    # A fixed port keeps generated harness configs stable across daemon restarts.
    http_port: int = 7355
    pass_deadline_sec: float = 900.0
    pass_model: str = "sonnet"
    # Explicit path to the harness CLI; null means resolve from PATH (plus the
    # conventional user install locations) at spawn time.
    claude_bin: str | None = None
    carousell_ai_api_base: str = "https://api.carousell.ai"
    carousell_ai_web_base_url: str = "https://www.carousell.ai"


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

    if "http_port" in raw:
        port = raw["http_port"]
        # 0 is an escape hatch meaning "let the OS choose an ephemeral port" — useful for
        # side-by-side dev daemons and tests. It is not for normal use: generated harness
        # configs pin the port, so an ephemeral one goes stale on restart.
        if not _is_real_int(port) or (port != 0 and not (1024 <= port <= 65535)):
            raise ConfigError(f"http_port must be 0 or an integer in 1024..65535, got {port!r}")
        values["http_port"] = port

    if "pass_deadline_sec" in raw:
        deadline = raw["pass_deadline_sec"]
        if not _is_real_number(deadline) or deadline <= 0:
            raise ConfigError(f"pass_deadline_sec must be a positive number, got {deadline!r}")
        values["pass_deadline_sec"] = float(deadline)

    if "pass_model" in raw:
        model = raw["pass_model"]
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"pass_model must be a non-empty string, got {model!r}")
        values["pass_model"] = model.strip()

    if "claude_bin" in raw:
        claude_bin = raw["claude_bin"]
        if claude_bin is not None and (not isinstance(claude_bin, str) or not claude_bin.strip()):
            raise ConfigError(f"claude_bin must be a non-empty string or null, got {claude_bin!r}")
        values["claude_bin"] = claude_bin

    for key in ("carousell_ai_api_base", "carousell_ai_web_base_url"):
        if key in raw:
            base = raw[key]
            if (
                not isinstance(base, str)
                or not base.startswith(("http://", "https://"))
                or base != base.strip()
            ):
                raise ConfigError(f"{key} must be an http(s) URL, got {base!r}")
            values[key] = base.rstrip("/")

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
