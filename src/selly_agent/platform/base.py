"""The OS-abstraction interface. Concrete platforms implement it; callers depend only here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class UnsupportedPlatform(Exception):
    """Raised by get_platform() when the host OS has no implementation yet."""


class Platform(ABC):
    """OS-specific operations the daemon needs. Keep this surface small.

    `home` is always passed in by paths.py rather than resolved here: home/XDG resolution is
    the sole responsibility of paths.py, so this module never reaches for it directly.
    """

    name: str = "base"

    @abstractmethod
    def launch_agents_dir(self, home: Path) -> Path:
        """The per-user auto-start directory the supervisor reads at login."""
