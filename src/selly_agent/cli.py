"""Command-line dispatch — one front door for the daemon, inspect, and version.

argparse subcommands over sys.argv; subcommand implementations are imported lazily so the
CLI module itself stays cheap to load and free of import cycles.
"""

from __future__ import annotations

import argparse

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="selly-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version and exit")

    daemon = sub.add_parser("daemon", help="daemon lifecycle")
    dsub = daemon.add_subparsers(dest="daemon_command", required=True)

    run = dsub.add_parser("run", help="run the daemon in the foreground")
    run.add_argument(
        "--once",
        action="store_true",
        help="lock, migrate, run a single tick, then stop cleanly",
    )

    install = dsub.add_parser("install", help="provision the layout and register the daemon")
    mode = install.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--login-start",
        dest="mode",
        action="store_const",
        const="login-start",
        help="start automatically at login (plist in ~/Library/LaunchAgents)",
    )
    mode.add_argument(
        "--manual",
        dest="mode",
        action="store_const",
        const="manual",
        help="start only on demand (plist kept in the config dir)",
    )
    install.add_argument(
        "--label",
        default=None,
        help="override the launchd label (side-by-side dev testing)",
    )

    for name, helptext in (
        ("uninstall", "unregister the daemon and remove its plist"),
        ("start", "register the daemon with launchd"),
        ("stop", "unregister the daemon from launchd"),
        ("status", "report daemon state, mode, heartbeat, and recent events"),
    ):
        p = dsub.add_parser(name, help=helptext)
        p.add_argument("--label", default=None, help="override the launchd label")

    inspect = sub.add_parser("inspect", help="tail the event store")
    inspect.add_argument("--follow", action="store_true", help="poll for new events (~1s)")
    inspect.add_argument("--pass", dest="pass_id", default=None, help="filter by pass id")
    inspect.add_argument(
        "--since", default=None, help="only events newer than a duration (e.g. 30m)"
    )
    inspect.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        default=None,
        help="filter by event kind (repeatable)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    args = _build_parser().parse_args((argv or sys.argv)[1:])

    if args.command == "version":
        print(__version__)
        return 0

    if args.command == "daemon":
        from . import daemon_cli

        return daemon_cli.dispatch(args)

    if args.command == "inspect":
        from . import inspect_cli

        return inspect_cli.run(args)

    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover
