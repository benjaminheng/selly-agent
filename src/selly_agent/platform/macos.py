"""macOS platform — launchd supervisor and its per-user auto-start directory."""

from __future__ import annotations

from pathlib import Path

from .base import Platform


class MacOSPlatform(Platform):
    name = "macos"

    def launch_agents_dir(self, home: Path) -> Path:
        return home / "Library" / "LaunchAgents"
