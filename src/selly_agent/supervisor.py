"""launchd integration — install/start/stop/status/uninstall (OS-agnostic orchestration).

The OS-specific bits (plist render, launchctl) live behind the platform seam; everything here
(mode logic, config recording, ours-vs-foreign refusal) is portable. Start-on-login is
expressed by plist *placement*, not a RunAtLoad toggle: login-start mode places the plist in
the launch-agents dir (launchd auto-loads it at login); manual mode keeps it in the config dir
and registers it only on demand. Crash keep-alive is identical in both modes once registered.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, heartbeat, paths
from .db import connect_reader
from .events import query_events
from .platform import Platform, get_platform

# Embedded in every plist we render, so a re-install/flip only ever touches our own file and
# never silently replaces a foreign (e.g. legacy) daemon's plist with the same label.
MARKER = "selly-agent-generated (managed by selly-agent; do not edit by hand)"

LOGIN_START = "login-start"
MANUAL = "manual"


def _checkout_root() -> Path:
    # src/selly_agent/supervisor.py -> repo root
    return Path(__file__).resolve().parents[2]


def _resolve_platform(platform: Platform | None) -> Platform:
    return platform if platform is not None else get_platform()


def _resolve_label(platform: Platform, label: str | None) -> str:
    if label:
        return label
    return config.load().daemon_label or platform.default_label()


def _is_ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text()
    except OSError:
        return False


def _plist_locations(platform: Platform, label: str) -> dict:
    filename = platform.supervisor_filename(label)
    return {
        LOGIN_START: paths.launch_agents_dir(platform=platform) / filename,
        MANUAL: paths.config_dir() / filename,
    }


def _find_installed(platform: Platform, label: str) -> Path | None:
    for location in _plist_locations(platform, label).values():
        if location.exists() and _is_ours(location):
            return location
    return None


def _provision_layout() -> None:
    """Provision the versioned layout and point current -> the checkout (dev-mode F4)."""
    paths.ensure_runtime_dirs()
    current = paths.current()
    checkout = _checkout_root()
    if current.is_symlink() or current.exists():
        if current.is_symlink():
            current.unlink()
        else:
            return  # a real dir/file here is not ours to replace
    current.symlink_to(checkout)


def install(*, mode: str, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = label or platform.default_label()
    locations = _plist_locations(platform, label)

    # Refuse if a foreign plist with our label already occupies either target location.
    for location in locations.values():
        if location.exists() and not _is_ours(location):
            print(
                f"refusing to install: a plist labelled {label!r} at {location} was not written "
                f"by selly-agent — remove it first (never replacing a foreign daemon's plist).",
                file=sys.stderr,
            )
            return 2

    _provision_layout()

    interpreter = os.path.realpath(sys.executable)
    program_args = [interpreter, str(paths.current() / "bin" / "selly-agent"), "daemon", "run"]
    plist_text = platform.render_supervisor(
        label=label,
        program_args=program_args,
        stdout_path=paths.logs_dir() / "agent.out.log",
        stderr_path=paths.logs_dir() / "agent.err.log",
        marker=MARKER,
    )

    # Remove any of our own plists from the other location (a mode flip moves the plist).
    for other_mode, location in locations.items():
        if other_mode != mode and location.exists() and _is_ours(location):
            if platform.is_registered(label):
                platform.unregister(label)
            location.unlink()

    dest = locations[mode]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(plist_text)
    config.merge_into_file({"daemon_mode": mode, "daemon_label": label})

    if mode == LOGIN_START:
        platform.register(dest)
        print(f"installed (login-start) and started; plist at {dest}")
    else:
        print(f"installed (manual mode); start with: selly-agent daemon start\nplist at {dest}")
    return 0


def start(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    plist = _find_installed(platform, label)
    if plist is None:
        print(
            "not installed — run: selly-agent daemon install --login-start|--manual",
            file=sys.stderr,
        )
        return 2
    if platform.is_registered(label):
        print("already running")
        return 0
    platform.register(plist)
    print("started")
    return 0


def stop(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    if not platform.is_registered(label):
        print("not running")
        return 0
    platform.unregister(label)
    print("stopped")
    return 0


def uninstall(*, label: str | None = None, platform: Platform | None = None) -> int:
    platform = _resolve_platform(platform)
    label = _resolve_label(platform, label)
    if platform.is_registered(label):
        platform.unregister(label)
    for location in _plist_locations(platform, label).values():
        if location.exists() and _is_ours(location):
            location.unlink()
    print("uninstalled")
    return 0


@dataclass
class Status:
    label: str
    mode: str
    registered: bool
    heartbeat_age_sec: float | None
    recent_events: list


def gather_status(*, label: str | None = None, platform: Platform | None = None) -> Status:
    platform = _resolve_platform(platform)
    cfg = config.load()
    label = label or cfg.daemon_label or platform.default_label()

    hb = heartbeat.read(paths.heartbeat_path())
    hb_age = None
    if hb and isinstance(hb.get("ts"), (int, float)):
        hb_age = max(0.0, time.time() - hb["ts"])

    recent: list = []
    if paths.events_db().exists():
        conn = connect_reader(paths.events_db())
        try:
            all_events = query_events(conn)
            recent = all_events[-5:]
        finally:
            conn.close()

    return Status(
        label=label,
        mode=cfg.daemon_mode,
        registered=platform.is_registered(label),
        heartbeat_age_sec=hb_age,
        recent_events=recent,
    )


def status(*, label: str | None = None, platform: Platform | None = None) -> int:
    st = gather_status(label=label, platform=platform)
    if st.registered:
        state = "running"
    elif st.mode == MANUAL:
        state = "stopped (manual mode — selly-agent daemon start)"
    else:
        state = "stopped"
    print(f"label:     {st.label}")
    print(f"mode:      {st.mode}")
    print(f"state:     {state}")
    if st.heartbeat_age_sec is None:
        print("heartbeat: none")
    else:
        print(f"heartbeat: {st.heartbeat_age_sec:.0f}s ago")
    if st.recent_events:
        print("recent events:")
        for ev in st.recent_events:
            print(f"  {ev.kind}")
    return 0
