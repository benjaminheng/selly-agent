"""Liveness heartbeat — a JSON file, not a DB row or an event.

Written once per scheduler tick so get_status / a healthcheck can read {ts, pid} without
opening a DB, and so it never spams the transcript store. Write failures are logged at debug
and never crash the loop: a heartbeat miss is a symptom to surface, never a reason to die.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def write(path: Path, pid: int | None = None) -> None:
    pid = os.getpid() if pid is None else pid
    try:
        Path(path).write_text(json.dumps({"ts": time.time(), "pid": pid}))
    except OSError as exc:
        log.debug("heartbeat write failed: %s", exc)


def read(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
