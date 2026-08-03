"""daemon run --once end to end, and the duplicate-instance exit-0 path (INV-27)."""

from __future__ import annotations

import json
import os

from selly_agent import config, daemon, heartbeat, lock, paths
from selly_agent.db import connect_reader
from selly_agent.events import query_events


def _event_kinds(events_db_path) -> list[str]:
    conn = connect_reader(events_db_path)
    try:
        return [e.kind for e in query_events(conn)]
    finally:
        conn.close()


def _tasks_run(events_db_path) -> set:
    conn = connect_reader(events_db_path)
    try:
        return {e.payload.get("task") for e in query_events(conn) if e.kind == "task.start"}
    finally:
        conn.close()


def _write_config(obj) -> None:
    paths.ensure_config_dir()
    paths.config_path().write_text(json.dumps(obj))


def test_run_once_migrates_heartbeats_and_ledgers(xdg_tmp) -> None:
    _write_config({"http_port": 0})  # an ephemeral port so the test never collides on 7355
    assert config.load().http_port == 0
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

    # Every lane is registered and survives a tick against an empty store. A lane that is built but
    # never scheduled is the failure this pins: the browser publish shipped that way.
    ran = _tasks_run(paths.events_db())
    assert {"pass_lane", "inbox_read", "reply_lane", "crosslist_lane"} <= ran
    # The release check is deliberately absent: every task's first tick is immediate, so having it
    # here would reach the release host from a smoke run — and from this test.
    assert "update_probe" not in ran
    assert "task.error" not in kinds


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
