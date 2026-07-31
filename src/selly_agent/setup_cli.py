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

import json
import time

from selly_agent import (
    __version__,
    config,
    connect_cli,
    control,
    healthcheck,
    heartbeat,
    marketplaces,
    pass_cli,
    passes,
    paths,
    secrets,
    settings_cli,
    supervisor,
)
from selly_agent.browser import markets as market_adapters
from selly_agent.installer import checks, materialize, preflight
from selly_agent.installer import region as region_guess
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
    except control.DaemonUnreachable as exc:
        ui.fatal(
            Abort(
                f"the background worker stopped answering ({exc})",
                _daemon_diagnostics(),
            )
        )
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
    _install_layout(ui, args, platform, tree)
    _start_daemon(ui, args, platform)

    # Everything past here talks to the running daemon, so it needs the token minted at its
    # first start. Read now rather than at import: before this line there was none.
    port = config.load().http_port
    token = secrets.read_mcp_token()
    if not token:
        raise Abort("the daemon is running but minted no attended token", _daemon_diagnostics())

    region = _seller_region(ui, args, port, token)
    _provision_rail(ui, region)
    _connect_markets(ui, args, port, token, region)
    _offer_telegram(ui, args, port, token)
    _attended_workspace(ui)
    _finish(ui, platform)


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
    ui.say("  • Where you sell — the region, currency and timezone I price in")
    ui.say("  • Marketplaces — I open my browser once so you can sign in to the ones you want")
    ui.say("  • Telegram — optional; where buyer chats reach you on your phone")
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


def _install_layout(ui: Ui, args, platform, tree) -> None:
    # A re-run replaces the very directory a running daemon is executing out of, so it is stopped
    # first. Skipping this is not merely untidy: `launchctl bootstrap` is a no-op on a label that
    # is already loaded, so the old process would keep running — still ticking heartbeats, so the
    # wait below would pass — while setup reported the new version as up.
    if supervisor.gather_status(platform=platform).registered:
        ui.say("Stopping the running worker so it can pick up this version…")
        supervisor.stop(platform=platform)

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


# --- where the seller sells ------------------------------------------------------------------


def _seller_region(ui: Ui, args, port: int, token: str):
    """Record region, currency and timezone, and answer with the region the daemon now holds.

    The machine's timezone already implies all three, so this confirms a proposal rather than
    conducting an interview. A machine that implies nothing (or a seller who says no) is asked.
    Provisioning and the marketplace list both key off the answer, so it is read back from the
    daemon rather than assumed — on a re-run the region may already be there.
    """
    known = _stored_basics(port, token)
    if known.get("region") and not args.region:
        ui.say(f"You sell in {region_guess.render(known)} — unchanged.")
        return known["region"]

    basics = _basics_from_flag(args) if args.region else region_guess.guess()
    if basics and not args.region:
        if not ui.confirm(f"You sell in {region_guess.render(basics)}, right?", default=True):
            basics = None
    if basics is None:
        basics = _ask_basics(ui)
    if not basics:
        ui.warn("No region recorded, so I can't set up carousell.ai or name your marketplaces.")
        ui.note("Tell me later in a session and I'll finish those two steps.")
        return None

    status, body = control.post(port, token, "/control/seller-basics", basics)
    if status != 200:
        raise Abort(f"could not record your region: {body.get('error', status)}")
    ui.say(f"Set — {region_guess.render(body['basics'])}.")
    return body["basics"].get("region")


def _stored_basics(port: int, token: str) -> dict:
    try:
        return control.get(port, token, "/control/seller-basics").get("basics") or {}
    except control.DaemonUnreachable:
        return {}


def _basics_from_flag(args) -> dict:
    code = str(args.region).strip().upper()
    basics = {"region": code, "timezone": region_guess.system_timezone()}
    currency = region_guess.CURRENCIES.get(code)
    if currency:
        basics["currency"] = currency
    return {key: value for key, value in basics.items() if value}


def _ask_basics(ui: Ui):
    """Ask for the three values outright. Answers nothing when there is nobody to ask."""
    if not ui.interactive:
        return None
    code = ui.ask("Which country do you sell in? (two-letter code, e.g. SG)").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    currency = (
        region_guess.CURRENCIES.get(code)
        or ui.ask("And the currency you price in? (three-letter code, e.g. SGD)").strip().upper()
    )
    timezone = ui.ask("Your timezone?", default=region_guess.system_timezone()).strip()
    basics = {"region": code, "currency": currency, "timezone": timezone}
    return {key: value for key, value in basics.items() if value}


# --- the rail ----------------------------------------------------------------------------------


def _provision_rail(ui: Ui, region) -> None:
    """Get the carousell.ai guest key. Quiet on success, and never fatal.

    A provisioning hiccup is a network problem, not an install problem: everything except the
    rail works without it, and the key can be obtained later. Saying so beats stopping.
    """
    from selly_agent.rail import provision

    if not region:
        return
    status = provision.ensure(region, api_base=config.load().carousell_ai_api_base)
    if status.get("status") == "ok":
        ui.say("carousell.ai is ready — it's on by default, nothing to sign in to.")
        return
    ui.warn(f"Couldn't set up carousell.ai just now ({status.get('error')}).")
    ui.note("Re-run `selly-agent provision carousell-ai` once you're back online.")


# --- marketplaces ---------------------------------------------------------------------------


def _connect_markets(ui: Ui, args, port: int, token: str, region) -> None:
    """Offer the marketplaces this seller could list on, and sign in to the ones they pick.

    This step *is* the opt-in to cross-listing: what they choose here becomes the setting the
    fan-out reads. carousell.ai is never in the list — it is the rail every listing goes on, with
    nothing to sign in to.
    """
    if args.skip_markets or not region:
        return
    available = market_adapters.publishable_markets(region)
    if not available:
        ui.say("No other marketplaces are available for your region yet — carousell.ai only.")
        return

    ui.say("I can also list on these. Signing in happens in my own Chrome window, and I never")
    ui.say("sign in for you — you can skip this and do it later with `selly-agent connect <name>`.")
    names = [marketplaces.display_name(market) for market in available]
    picked = [available[index] for index in ui.multiselect("Which should I list on?", names)]
    if not picked:
        ui.say("Sticking to carousell.ai. Change that any time from the /selly menu.")
        return

    # The setting first: it is what the seller opted into, and it holds even if a sign-in is
    # interrupted — the fan-out re-checks the login every time it publishes anyway.
    if settings_cli.set_setting(port, token, "crosslist_markets", json.dumps(picked)) != 0:
        ui.warn("Couldn't record those marketplaces — carousell.ai only for now.")
        return

    for market in picked:
        ui.say(f"Opening {marketplaces.display_name(market)}…")
        connect_cli.market_flow(port, token, market, interactive=ui.interactive)


# --- Telegram ---------------------------------------------------------------------------------


def _offer_telegram(ui: Ui, args, port: int, token: str) -> None:
    """Offer the phone channel. Declining is a first-class answer — the agent runs without it,
    and everything it would push is queued and shown at the start of an attended session."""
    if args.skip_telegram:
        return
    if _channel_bound(port, token):
        ui.say("Telegram is already connected.")
        return

    ui.say("Want buyer chats on your phone? I can connect a Telegram bot (about two minutes).")
    ui.say("Skip it and I'll still run — I'll just keep everything for your next session here.")
    if not ui.interactive or not ui.confirm("Connect Telegram now?", default=True):
        ui.say("Skipped. Connect it later with: selly-agent connect telegram")
        return

    ui.say("Handing over to the Telegram setup —")
    code = connect_cli.bind_flow(port, token, interactive=ui.interactive)
    if code != 0:
        ui.say("Not connected yet. Pick it up later with: selly-agent connect telegram")


def _channel_bound(port: int, token: str) -> bool:
    try:
        return bool(control.get(port, token, "/control/channel-status").get("bound"))
    except control.DaemonUnreachable:
        return False


# --- the attended session ----------------------------------------------------------------------


def _attended_workspace(ui: Ui) -> None:
    """Generate the Claude Code workspace at a fixed, documented place.

    A fixed location rather than wherever the terminal happened to be: this is the directory the
    seller will be told to `cd` into for months afterwards, so it has to have a name.
    """
    dest = paths.data_root() / "attended"
    if pass_cli.harness_config(dest) != 0:
        ui.warn("Couldn't write the attended workspace — `selly-agent harness config` will.")
        return
    ui.say("To talk to me in a terminal:")
    ui.plain(f"  cd {dest} && claude")


# --- the last word ------------------------------------------------------------------------------


def _finish(ui: Ui, platform) -> None:
    ui.say("")
    ui.say("Checking everything over:")
    for line in checks.render(healthcheck.run_checks(platform=platform)):
        ui.plain(f"  {line}")
    ui.say("")
    ui.say("Done — I'm running.")
    ui.say("  • Change settings any time: `/selly` in the attended session")
    ui.say("  • Check on me:              selly-agent daemon status")
    ui.say("  • Update:                   selly-agent update")


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
