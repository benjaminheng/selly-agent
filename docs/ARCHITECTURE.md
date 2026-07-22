# Architecture

A high-level map of the repository. It describes the shape of the program and
where responsibilities live; read the modules themselves for detail. As more
subsystems land, this page becomes the index that links out to their docs.

## The one-process model

selly-agent is a single long-running Python process, kept alive by the OS
(launchd on macOS). It is stdlib-only at runtime: the user's own `python3` is
the only runtime dependency, enforced by a guard test over `src/` imports.
Concurrency is a few threads sharing SQLite state.

Everything is reachable from one front door: `bin/selly-agent` resolves the
package and dispatches argv via `cli.py` (`daemon run/install/start/stop/status/
uninstall`, `inspect`, `version`). launchd's job points at this launcher.

## Layout

```
bin/selly-agent          CLI launcher
src/selly_agent/          the package
tests/                    plain pytest (guards under tests/guard/)
docs/                     this document and friends
Makefile                  local entry points (test, lint, fmt)
```

## The package, by responsibility

Foundations:

- **`paths.py`** — the single path authority. Every location is resolved here,
  honoring the XDG base directories; a guard test enforces that nothing else
  touches home/XDG.
- **`platform/`** — the OS seam (`get_platform()`, `base.Platform`, `macos.py`).
  The "port once" boundary; no launchd string leaks past it.
- **`config.py`** — reads `config.json` (missing → defaults; invalid → rejected;
  unknown keys ignored). The daemon only reads config; the installer writes it.

State — two SQLite databases, kept apart:

- **`db.py`** — WAL, one write connection per database behind a lock, read-only
  connections for readers, explicit transactions.
- **`migrations/`** — one forward-only runner for both databases; numbered SQL
  applied at startup, each in one transaction. The business database is
  snapshotted before pending migrations run.
- **`data/selly.db`** is business data (migrated, snapshotted).
  **`state/events.db`** is the event/transcript store (prunable; recreated from
  migrations if deleted). The two are never joined.

Observability:

- **`events.py`** — an in-process bus over a durable store. `publish` stamps the
  journal clock at write; that timestamp is the sole ordering key. Subscribers
  may register (the seam a web tail plugs into later).
- **`retention.py`** — the daily prune (events past a window, snapshots to a
  keep count, logs to a size cap).
- **`inspect_cli.py`** — `selly-agent inspect`, a read-only tail of the event
  store; works whether or not the daemon is running (`--follow` polls).

Lifecycle:

- **`lock.py`** — a PID-aware single-instance lock (a live duplicate exits
  clean; a dead holder's lock is reclaimed).
- **`heartbeat.py`** — a `{ts, pid}` file written each tick.
- **`scheduler.py`** — one loop thread submits due tasks to a small pool; a task
  never overlaps itself, every attempt is ledgered, repeated failures back off.
- **`daemon.py`** — the process: lock, ensure dirs, run startup migrations, open
  the bus, run the scheduler; a signal drains cleanly and exits 0.
- **`supervisor.py`** — the OS-agnostic orchestration behind
  `daemon install/start/stop/status/uninstall`.

## Startup, in order

1. Acquire the instance lock (a live duplicate exits 0).
2. Ensure directories; open both databases.
3. Apply pending migrations (snapshot the business database first if any are
   pending); a failure aborts startup.
4. Open the event bus; emit `daemon.start` and one `migration.applied` each.
5. Register tasks and run the scheduler, writing the heartbeat each tick.
6. On SIGTERM/SIGINT: drain, emit `daemon.stop`, clear the lock, exit 0.

`daemon run --once` runs a single tick and stops — the deterministic test seam.

## Filesystem locations

Resolved by `paths.py` from the XDG base directories:

```
~/.local/share/selly-agent/   versions/, current -> …, data/selly.db (business data)
~/.local/state/selly-agent/   events.db, backups/, logs/, heartbeat, lock (prunable)
~/.config/selly-agent/        config.json (0700)
~/.cache/selly-agent/         downloaded release artifacts
```

Tests point the XDG variables at a temporary directory.

## Conventions

- Stdlib only at runtime; dev tools (pytest, ruff) live in the `[dev]` extra.
- Python 3.9 is the floor; ruff is pinned to `py39`.
- State changes go through typed code, one writer per store.
- Guard tests under `tests/guard/` enforce the load-bearing rules.

See `AGENTS.md` for the contributor-facing version of these rules.
