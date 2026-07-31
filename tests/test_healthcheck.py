"""The five checks: each decision against fake inputs, plus the render and the exit code.

The decisions are pure, so they are tested as such. What the probes do is fetch their arguments,
and the one thing worth asserting about that is the fail-open contract: a probe that explodes is
a failed line, never the end of the report.
"""

from __future__ import annotations

from selly_agent import healthcheck, secrets, supervisor
from selly_agent.installer import checks

# --- the daemon ---------------------------------------------------------------------------------


def test_a_running_daemon_with_a_fresh_heartbeat_is_fine() -> None:
    result = healthcheck.daemon_check(mode="login-start", registered=True, heartbeat_age=3.0)
    assert result.status == checks.OK
    assert "3s ago" in result.detail


def test_a_registered_daemon_gone_quiet_is_a_failure() -> None:
    result = healthcheck.daemon_check(mode="login-start", registered=True, heartbeat_age=600.0)
    assert result.status == checks.FAIL
    assert "600s ago" in result.detail


def test_a_registered_daemon_that_never_beat_is_a_failure() -> None:
    result = healthcheck.daemon_check(mode="login-start", registered=True, heartbeat_age=None)
    assert result.status == checks.FAIL
    assert "never" in result.detail


def test_manual_mode_not_running_is_reported_not_blamed() -> None:
    # The seller asked for a daemon they start themselves. Calling that a failure would train
    # them to ignore the report.
    result = healthcheck.daemon_check(mode=supervisor.MANUAL, registered=False, heartbeat_age=None)
    assert result.status == checks.WARN
    assert result.fix == "selly-agent daemon start"


def test_login_start_not_running_is_a_failure() -> None:
    result = healthcheck.daemon_check(mode="login-start", registered=False, heartbeat_age=None)
    assert result.status == checks.FAIL


# --- the channel ---------------------------------------------------------------------------------


def test_a_bound_channel_passes_and_an_unbound_one_only_warns() -> None:
    assert healthcheck.channel_check(bound=True).status == checks.OK
    unbound = healthcheck.channel_check(bound=False)
    assert unbound.status == checks.WARN  # optional by design — never a failure
    assert unbound.fix == "selly-agent connect telegram"


# --- the browser ---------------------------------------------------------------------------------


def test_no_external_marketplaces_is_a_pass_with_the_reason() -> None:
    result = healthcheck.browser_check(enabled=[], cdp_ready=False, states=[])
    assert result.status == checks.OK
    assert "carousell.ai only" in result.detail


def test_a_closed_chrome_with_markets_enabled_is_a_note_not_a_fault() -> None:
    result = healthcheck.browser_check(enabled=["carousell"], cdp_ready=False, states=[])
    assert result.status == checks.WARN
    assert "I start it when I need it" in result.fix


def test_being_signed_out_of_an_enabled_market_fails_with_the_command_to_fix_it() -> None:
    result = healthcheck.browser_check(
        enabled=["carousell"],
        cdp_ready=True,
        states=[{"market": "carousell", "state": "logged_out"}],
    )
    assert result.status == checks.FAIL
    assert result.fix == "selly-agent connect carousell"


def test_an_unconfirmable_login_warns_rather_than_claiming_signed_out() -> None:
    result = healthcheck.browser_check(
        enabled=["carousell"],
        cdp_ready=True,
        states=[{"market": "carousell", "state": "unknown"}],
    )
    assert result.status == checks.WARN


def test_every_enabled_market_signed_in_passes() -> None:
    result = healthcheck.browser_check(
        enabled=["carousell"],
        cdp_ready=True,
        states=[{"market": "carousell", "state": "logged_in"}],
    )
    assert result.status == checks.OK


# --- the rail key -----------------------------------------------------------------------------


def test_the_rail_key_is_reported_by_presence_never_by_value() -> None:
    present = healthcheck.rail_key_check(present=True)
    assert present.status == checks.OK
    assert present.detail == "present"
    assert healthcheck.rail_key_check(present=False).status == checks.FAIL


# --- the whole report --------------------------------------------------------------------------


def test_a_probe_that_explodes_becomes_a_line_not_the_end_of_the_report(monkeypatch) -> None:
    def explode(_platform=None):
        raise RuntimeError("launchctl vanished")

    monkeypatch.setattr(healthcheck, "_daemon_probe", explode)
    results = healthcheck.run_checks()
    assert len(results) == 5
    assert results[0].status == checks.FAIL
    assert "launchctl vanished" in results[0].detail


def test_the_report_renders_a_glyph_per_check_and_a_verdict() -> None:
    text = healthcheck.report(
        [
            checks.ok("daemon", "running"),
            checks.warn("channel", "not connected", "selly-agent connect telegram"),
        ]
    )
    assert "✅ daemon: running" in text
    assert "⚠️ channel: not connected" in text
    assert "→ selly-agent connect telegram" in text
    assert text.endswith("All good.")


def test_a_warning_alone_still_exits_zero_but_a_failure_does_not() -> None:
    assert checks.exit_code([checks.warn("channel", "not connected")]) == 0
    assert checks.exit_code([checks.fail("daemon", "not running")]) == 1
    assert "needs attention" in healthcheck.report([checks.fail("daemon", "not running")])


def test_the_browser_check_says_unknown_rather_than_none_when_the_daemon_is_down(
    xdg_tmp, monkeypatch
) -> None:
    from selly_agent import control
    from selly_agent.config import Config

    monkeypatch.setattr(secrets, "read_mcp_token", lambda: "tok")

    def refuse(*a, **k):
        raise control.DaemonUnreachable("connection refused")

    monkeypatch.setattr(control, "get", refuse)
    result = healthcheck._browser_probe(Config())
    assert result.status == checks.WARN
    assert "isn't answering" in result.detail
