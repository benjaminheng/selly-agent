# selly-agent

A local, single-tenant agent that sells and buys on peer-to-peer marketplaces
from the user's own machine. It runs as one always-on Python process under
launchd, exposes a typed tool surface to an LLM harness, drives a real
logged-in browser, and talks to the user over an optional chat channel.

This repository is the rewrite of the original implementation. It is a
greenfield core that ports the battle-tested engines from the legacy repo.

## Status

Early. The current code is the **core skeleton**: a single stdlib-only process
that runs idle under launchd and is observable end to end — XDG paths, config,
a SQLite state layer with a startup migration runner, an event bus + transcript
store, a scheduler loop, launchd integration, and the `inspect` CLI. The tool
surface, pass runner, engines, channel, and browser layer land in later
workstreams.

## Where the plans live

Design, architecture decisions, invariants, and the plan this code implements
are tracked in a separate **projects repo** (not here) — this repo holds code
only. If you are implementing against a plan, start there.

## Runtime constraints

- **Python stdlib only at runtime.** The user's own `python3` is the only
  runtime dependency — there is no pip install step on a user machine. A guard
  test fails the suite if any module under `src/` imports a non-stdlib package.
- **Python 3.9 is the floor.** macOS Command Line Tools ship 3.9; the suite
  must pass on it. In practice: `from __future__ import annotations` in every
  module, no `match`, no runtime `X | Y` unions (annotations are fine), no
  `tomllib`.

## Dev quickstart

Dev/test tooling (pytest, ruff) lives in the `[dev]` extra — never under
`src/`.

```sh
make test            # pytest on the current interpreter
make test-3.9        # the suite on a 3.9 interpreter (skips with a note if absent)
make lint            # ruff check + ruff format --check
make fmt             # ruff format

# run the daemon once in the foreground (lock -> migrate -> one tick -> stop)
bin/selly-agent daemon run --once

# tail the event store (works whether or not the daemon is running)
bin/selly-agent inspect --follow
```

Tests point `$XDG_*_HOME` at a tmpdir, so they never touch a real install.

## Layout

```
bin/selly-agent            single CLI launcher (resolves src/, dispatches argv)
src/selly_agent/
  cli.py                   argparse dispatch (daemon, inspect, version)
  paths.py                 the one path authority (XDG; only module touching home/XDG)
  platform/                OS seam (macOS launchd; Windows is a later port)
  config.py                read-only config.json loader (+ installer-side writer)
  db.py                    SQLite: WAL, one write connection per DB, readers
  migrations/              forward-only numbered SQL migrations + the runner
  events.py                event bus + transcript store (the observability record)
  retention.py             daily prune task
  lock.py                  PID-aware single-instance lock
  heartbeat.py             liveness heartbeat file
  scheduler.py             the loop: due tasks -> executor, backoff, task events
  daemon.py                wires it together; the daemon process
  supervisor.py            launchd install/start/stop/status/uninstall
  inspect_cli.py           the event tail
tests/                     plain pytest
```

## Filesystem locations (XDG)

```
~/.local/share/selly-agent/   versions/, current -> …, data/selly.db
~/.local/state/selly-agent/   events.db, backups/, logs/, heartbeat, lock
~/.config/selly-agent/        config.json (0700)
~/.cache/selly-agent/         downloaded release tarballs
```
