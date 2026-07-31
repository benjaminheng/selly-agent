"""`selly-agent setup` — the deterministic installer, start to finish, in one terminal.

No model is involved anywhere in this file. Every phase is a machine step with a known answer,
and every phase is idempotent off real state rather than off a sentinel file, so a re-run after
a failure resumes instead of refusing or double-applying.

The ordering is load-bearing. Identity and auth gates come before slow work, so a machine that
cannot possibly finish fails in seconds rather than after a package download. And the daemon
comes up before anything that needs it: the attended token is minted at first start, and the
region, provisioning and marketplace phases all reach the daemon over its control routes rather
than writing state behind its back.
"""

from __future__ import annotations

import time

from selly_agent import __version__, config, heartbeat, passes, paths, supervisor
from selly_agent.installer import checks, materialize, preflight
from selly_agent.installer.ui import Abort, Ui
from selly_agent.platform import get_platform

# How long the daemon gets to write its first heartbeat after being started. Startup is
# migrations plus a bind, so seconds; this is the "something is wrong" boundary, not a target.
DAEMON_READY_TIMEOUT_SEC = 60.0
# How much of the daemon's stderr to show when it does not come up — enough for a traceback.
_LOG_TAIL_LINES = 20


def run(args) -> int:
    ui = Ui(assume_yes=getattr(args, "yes", False))
    try:
        _run(args, ui)
    except Abort as exc:
        ui.fatal(exc)
        return 1
    except materialize.LayoutError as exc:
        ui.fatal(Abort(exc.message, exc.fix))
        return 1
    return 0


def _run(args, ui: Ui) -> None:
    # Before the banner: on an OS we do not support, a friendly wall of paths we will never
    # write is worse than one honest line.
    platform_check = preflight.check_platform()
    if platform_check.failed:
        raise Abort(platform_check.detail, platform_check.fix)
    platform = get_platform()
    tree = materialize.source_tree()

    _intro(ui, platform)
    _gates(ui, tree)
    _install_layout(ui, args, tree)
    _start_daemon(ui, args, platform)


# --- what this is, and what it will touch ---------------------------------------------------


def _intro(ui: Ui, platform) -> None:
    ui.banner(__version__)
    ui.say("I'm SELLY — your marketplace agent. I list what you photograph, answer buyers,")
    ui.say("and haggle for you. This sets me up on this Mac; it takes a few minutes.")
    ui.say("")
    ui.say("In order:")
    ui.say("  • Checks — Node, Chrome, and the claude CLI (installed and signed in)")
    ui.say("  • Install — this version into place, plus the `selly-agent` command")
    ui.say("  • Background worker — started, and whether it comes back at login")
    ui.say("")
    ui.say("Everything I write goes in these places, and nowhere else:")
    for line in materialize.layout_preview(platform=platform):
        ui.plain(line)
    ui.say("")

    agent_var = preflight.agent_context()
    if agent_var and ui.interactive:
        # A TTY exists, but an agent is holding it. Questions asked here would be answered by
        # a model rather than the seller, so the run takes its defaults and says so.
        ui.warn(f"Running inside an agent session (${agent_var}) — I won't ask questions.")
        ui.interactive = False


# --- the gates ------------------------------------------------------------------------------


def _gates(ui: Ui, tree) -> None:
    ui.say("Checking this machine…")
    cfg = config.load()

    _require(ui, checks.fail_open("install location", lambda: preflight.check_tree_location(tree)))
    _gate_claude(ui, cfg)
    _gate_dependency(ui, "node", lambda: preflight.check_node(), package="node")
    _gate_dependency(
        ui,
        "chrome",
        lambda: preflight.check_chrome(cfg.chrome_bin),
        package="google-chrome",
        cask=True,
    )

    ui.say("Warming up the browser server (first run downloads it)…")
    _report(ui, checks.fail_open("playwright", lambda: preflight.prewarm_playwright(cfg)))


def _report(ui: Ui, check: checks.Check) -> checks.Check:
    for line in check.render():
        ui.plain(f"  {line}")
    return check


def _require(ui: Ui, check: checks.Check) -> None:
    _report(ui, check)
    if check.failed:
        raise Abort(f"{check.name}: {check.detail}", check.fix)


def _gate_claude(ui: Ui, cfg) -> None:
    """The harness must be installed and signed in — offering the login flow until it is.

    Signed-out-but-installed is the failure the internal test round produced most, and it is
    invisible until the first pass spawns hours later, so it is settled here.
    """
    while True:
        check = checks.fail_open("claude CLI", lambda: preflight.check_claude(cfg))
        _report(ui, check)
        if not check.failed:
            return
        if passes.resolve_claude_bin(cfg) is None:
            # Installing it is a `curl | bash` of someone else's script: their call, not ours.
            raise Abort("the claude CLI is not installed", check.fix)
        if not ui.interactive:
            # `--yes` cannot stand in for a person here: the login is an interactive OAuth flow
            # that prints a URL and reads back a pasted code. With no terminal there is nobody
            # to hand it to.
            raise Abort("the claude CLI is signed out", check.fix)
        if not ui.confirm("Sign in to Claude now? I'll hand the terminal over.", default=True):
            raise Abort("the claude CLI is signed out", check.fix)
        ui.say("Handing over to `claude auth login` — come back here when it's done.")
        preflight.claude_login(cfg)


def _gate_dependency(ui: Ui, name: str, probe, *, package: str, cask: bool = False) -> None:
    """A dependency we can offer to install, once. Homebrew itself is never bootstrapped —
    piping a remote installer into a shell is a trust decision the machine's owner owns."""
    check = _report(ui, checks.fail_open(name, probe))
    if not check.failed:
        return

    brew = preflight.homebrew_path()
    if not brew:
        raise Abort(
            f"{name}: {check.detail}",
            f"{check.fix}\n(Homebrew isn't installed — get it from https://brew.sh, or install "
            f"{name} however you prefer, then re-run ./setup.)",
        )
    if not ui.confirm(f"Install {name} with Homebrew now?", default=True):
        raise Abort(f"{name}: {check.detail}", check.fix)

    ui.say(f"Running `brew install {package}` — this can take a few minutes…")
    ok, detail = preflight.brew_install(package, cask=cask)
    if not ok:
        raise Abort(f"installing {name} failed: {detail}", check.fix)
    _require(ui, checks.fail_open(name, probe))


# --- the layout -----------------------------------------------------------------------------


def _install_layout(ui: Ui, args, tree) -> None:
    if args.dev:
        ui.say(f"Dev mode: pointing the install at {tree} — edits are live on the next restart.")
        materialize.install_dev(tree)
    else:
        ui.say(f"Installing version {__version__}…")
        dest = materialize.install_version(tree, __version__)
        ui.note(f"{dest}")
        removed = materialize.prune_versions()
        if removed:
            ui.note(f"removed older version(s): {', '.join(removed)}")

    shim = materialize.install_shim()
    ui.say(f"The `selly-agent` command is at {shim}.")
    _offer_path(ui, args)
    _record_claude_bin(ui)


def _offer_path(ui: Ui, args) -> None:
    """Make `selly-agent` findable, or say exactly why it is not.

    The uv/rustup convention: install into the user's own bin dir, then *offer* to touch the
    shell rc with an explicit way to decline — never silently edit dotfiles, and never silently
    leave a command that is not found.
    """
    if materialize.user_bin_on_path():
        return
    bin_dir = paths.user_bin_dir()
    export_line = materialize.RC_BLOCK_BODY
    ui.warn(f"{bin_dir} isn't on your PATH, so `selly-agent` won't be found yet.")

    # Editing a dotfile needs a signal that someone agreed to it: either a person who can answer,
    # or `--yes`, which is that agreement given up front. A plain piped run has neither, so it
    # gets the line to paste rather than a surprise edit.
    consented = ui.interactive or ui.assume_yes
    if args.no_modify_path or not consented:
        ui.say("Add this to your shell's startup file:")
        ui.plain(f"  {export_line}")
        return

    rc_path = materialize.shell_rc_target()
    if not ui.confirm(f"Add it to {rc_path}?", default=True):
        ui.say("Left your shell alone. Add this when you like:")
        ui.plain(f"  {export_line}")
        return

    if materialize.add_rc_block(rc_path):
        ui.say(f"Added to {rc_path} — open a new terminal, or run: source {rc_path}")
    else:
        ui.say(f"{rc_path} already had it.")


def _record_claude_bin(ui: Ui) -> None:
    """Pin the resolved `claude` path into config.

    The daemon runs under launchd with a minimal PATH, so "whatever `claude` resolves to in an
    interactive shell" is not something it can look up later. Resolved once, here, where a real
    shell's PATH is available.
    """
    resolved = passes.resolve_claude_bin(config.load())
    if resolved is None:
        return
    config.merge_into_file({"claude_bin": resolved})
    ui.note(f"claude: {resolved}")


# --- the daemon -----------------------------------------------------------------------------


def _start_daemon(ui: Ui, args, platform) -> None:
    mode = args.mode or _ask_login_mode(ui)
    started_after = time.time()

    if supervisor.install(mode=mode, platform=platform) != 0:
        raise Abort("could not register the background worker (see the message above)")
    if mode == supervisor.MANUAL:
        ui.say("Manual mode: I'll start it now, but it won't come back on its own after you")
        ui.say("log out or restart — run `selly-agent daemon start` when you want it.")
        supervisor.start(platform=platform)

    ui.say("Waiting for the worker to come up…")
    if not _wait_for_daemon(started_after):
        raise Abort(
            "the background worker didn't start",
            _daemon_diagnostics(),
        )
    ui.say("Worker is up.")


def _ask_login_mode(ui: Ui) -> str:
    if ui.confirm("Start the worker automatically when you log in?", default=True):
        return supervisor.LOGIN_START
    return supervisor.MANUAL


def _wait_for_daemon(started_after: float) -> bool:
    return heartbeat.wait_fresh(
        paths.heartbeat_path(),
        newer_than=started_after,
        timeout_sec=DAEMON_READY_TIMEOUT_SEC,
    )


def _daemon_diagnostics() -> str:
    """What to look at when the worker did not come up — with the tail of its own stderr, since
    that is where the reason actually is and nobody finds that path on their own."""
    log_path = paths.logs_dir() / "agent.err.log"
    lines = [f"Its log is at {log_path}", "Run `selly-agent daemon status` for its view."]
    try:
        tail = log_path.read_text().splitlines()[-_LOG_TAIL_LINES:]
    except OSError:
        tail = []
    if tail:
        lines.append("Last lines:")
        lines.extend(f"  {line}" for line in tail)
    return "\n".join(lines)
