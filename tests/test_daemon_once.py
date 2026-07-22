"""daemon run --once end to end, and the duplicate-instance exit-0 path (INV-27)."""

from __future__ import annotations

import os

from selly_agent import daemon, heartbeat, lock, paths
from selly_agent.db import connect_reader
from selly_agent.events import query_events


def _event_kinds(events_db_path) -> list[str]:
    conn = connect_reader(events_db_path)
    try:
        return [e.kind for e in query_events(conn)]
    finally:
        conn.close()


def test_run_once_migrates_heartbeats_and_ledgers(xdg_tmp) -> None:
    rc = daemon.run_daemon(once=True)
    assert rc == 0

    # heartbeat written
    hb = heartbeat.read(paths.heartbeat_path())
    assert hb["pid"] == os.getpid()

    # both DBs migrated
    conn = connect_reader(paths.selly_db())
    try:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] >= 1
    finally:
        conn.close()

    kinds = _event_kinds(paths.events_db())
    assert "daemon.start" in kinds
    assert "daemon.stop" in kinds
    assert "migration.applied" in kinds
    assert "task.start" in kinds and "task.ok" in kinds  # retention lane exercised


def test_duplicate_instance_exits_zero_without_starting(xdg_tmp) -> None:
    paths.ensure_state_dirs()
    held = lock.acquire(paths.lock_path())  # simulate a live holder
    try:
        rc = daemon.run_daemon(once=True)
        assert rc == 0
        # it exited before opening the event store, so no events DB was created
        assert not paths.events_db().exists()
    finally:
        os.close(held.fd)
