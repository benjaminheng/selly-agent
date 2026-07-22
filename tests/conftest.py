"""Shared fixtures: an isolated XDG environment pointed at a tmpdir."""

from __future__ import annotations

import pytest

from selly_agent import migrations
from selly_agent.db import Database
from selly_agent.events import EventBus, EventStore


@pytest.fixture
def bus(tmp_path):
    """A ready EventBus backed by freshly-migrated data/events DBs under tmp_path."""
    data_db = Database(tmp_path / "selly.db")
    events_db = Database(tmp_path / "events.db")
    migrations.run_startup_migrations(
        data_db=data_db,
        events_db=events_db,
        backups_dir=tmp_path / "backups",
        backups_keep=5,
    )
    return EventBus(EventStore(events_db))


@pytest.fixture
def xdg_tmp(tmp_path, monkeypatch):
    """Point HOME and every XDG_*_HOME at a fresh tmpdir so path resolution is hermetic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return tmp_path
