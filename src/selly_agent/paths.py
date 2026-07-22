"""The one path authority.

Every filesystem location the daemon uses is resolved here, and only here — this is the
structural defense against the stale-clone class of bug, where a generator writes to a
location the running daemon never reads. XDG overrides are honored (they are also the test
seam: point $XDG_*_HOME at a tmpdir). Nothing else in the package may reach for the home
directory or an XDG variable; a guard test enforces that.

Paths are resolved at call time, not import time, so an XDG override set by a test (or the
environment) always takes effect.
"""

from __future__ import annotations

import os
from pathlib import Path

from .platform import get_platform

APP = "selly-agent"


def _home() -> Path:
    return Path.home()


def _xdg_base(var: str, default_rel: str) -> Path:
    override = os.environ.get(var)
    base = Path(override) if override else _home() / default_rel
    return base / APP


# --- XDG roots -----------------------------------------------------------------------------


def data_root() -> Path:
    """Immutable installs + business data live here (never pruned)."""
    return _xdg_base("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    """Transcripts, DB backups, logs — prunable by definition; safe to delete wholesale."""
    return _xdg_base("XDG_STATE_HOME", ".local/state")


def config_dir() -> Path:
    """config.json + secrets (0700)."""
    return _xdg_base("XDG_CONFIG_HOME", ".config")


def cache_dir() -> Path:
    """Downloaded release tarballs (regenerable)."""
    return _xdg_base("XDG_CACHE_HOME", ".cache")


# --- data_root children --------------------------------------------------------------------


def versions_dir() -> Path:
    return data_root() / "versions"


def current() -> Path:
    """The atomic swap point: a symlink into versions/<v> (dev mode: into the checkout)."""
    return data_root() / "current"


def data_dir() -> Path:
    return data_root() / "data"


def selly_db() -> Path:
    """Business data — migrated and snapshotted before migrations."""
    return data_dir() / "selly.db"


# --- state_dir children --------------------------------------------------------------------


def events_db() -> Path:
    """Event/transcript store — prunable; deletable without data loss; recreated on startup."""
    return state_dir() / "events.db"


def backups_dir() -> Path:
    return state_dir() / "backups"


def logs_dir() -> Path:
    return state_dir() / "logs"


def heartbeat_path() -> Path:
    return state_dir() / "daemon.heartbeat.json"


def lock_path() -> Path:
    return state_dir() / "daemon.lock"


# --- config_dir children -------------------------------------------------------------------


def config_path() -> Path:
    return config_dir() / "config.json"


# --- platform-owned -----------------------------------------------------------------------


def launch_agents_dir() -> Path:
    """The per-user auto-start directory, composed here from the platform's OS-specific rule
    (step-8 install must never compose this itself)."""
    return get_platform().launch_agents_dir(_home())


# --- ensure helpers ------------------------------------------------------------------------


def _ensure(path: Path, mode: int) -> Path:
    """Create a directory with an exact mode, from creation (umask neutralized so a sensitive
    mode like 0700 is never widened, and never applied via a post-creation chmod window)."""
    old_umask = os.umask(0)
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
    finally:
        os.umask(old_umask)
    return path


def ensure_data_dirs() -> None:
    _ensure(data_root(), 0o755)
    _ensure(versions_dir(), 0o755)
    _ensure(data_dir(), 0o755)


def ensure_state_dirs() -> None:
    _ensure(state_dir(), 0o755)
    _ensure(backups_dir(), 0o755)
    _ensure(logs_dir(), 0o755)


def ensure_config_dir() -> None:
    # 0700 from creation: the config dir holds secrets in later workstreams.
    _ensure(config_dir(), 0o700)


def ensure_runtime_dirs() -> None:
    """Everything the daemon needs present before it starts."""
    ensure_data_dirs()
    ensure_state_dirs()
    ensure_config_dir()
