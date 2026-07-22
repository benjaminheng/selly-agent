"""Dispatch for `selly-agent daemon <subcommand>`."""

from __future__ import annotations

import argparse


def dispatch(args: argparse.Namespace) -> int:
    if args.daemon_command == "run":
        from . import daemon

        return daemon.run_daemon(once=args.once)
    raise NotImplementedError(f"daemon {args.daemon_command} is not available in this build")
