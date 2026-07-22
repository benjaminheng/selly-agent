"""Retention prune: age deletion, log truncation, and a prune.done event with counts."""

from __future__ import annotations

import time

from selly_agent import migrations, retention
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


def _bus(tmp_path) -> EventBus:
    events_db = Database(tmp_path / "events.db")
    data_db = Database(tmp_path / "selly.db")
    migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
    )
    return EventBus(EventStore(events_db))


def test_run_retention_prunes_events_and_reports(tmp_path) -> None:
    bus = _bus(tmp_path)
    bus.publish("task.start", {})
    bus.publish("task.ok", {})
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir()

    counts = retention.run_retention(
        bus=bus,
        retention_days=1,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
        logs_dir=tmp_path / "logs",
        now=time.time() + 10 * retention.SECONDS_PER_DAY,
    )
    assert counts["events_deleted"] == 2
    # the prune.done event itself survives (published after the delete)
    kinds = [e.kind for e in bus.store.read()]
    assert kinds == ["prune.done"]


def test_run_retention_keeps_recent_events(tmp_path) -> None:
    bus = _bus(tmp_path)
    bus.publish("task.start", {})
    (tmp_path / "logs").mkdir()
    counts = retention.run_retention(
        bus=bus,
        retention_days=14,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
        logs_dir=tmp_path / "logs",
    )
    assert counts["events_deleted"] == 0


def test_truncate_log_trims_to_cap(tmp_path) -> None:
    log_file = tmp_path / "agent.err.log"
    log_file.write_bytes(b"A" * 500 + b"B" * 500)
    reclaimed = retention._truncate_log(log_file, cap=200)
    assert reclaimed == 800
    assert log_file.read_bytes() == b"B" * 200


def test_truncate_log_leaves_small_files(tmp_path) -> None:
    log_file = tmp_path / "small.log"
    log_file.write_bytes(b"tiny")
    assert retention._truncate_log(log_file, cap=1024) == 0
    assert log_file.read_bytes() == b"tiny"
