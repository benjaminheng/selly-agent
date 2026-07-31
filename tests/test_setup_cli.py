"""`selly-agent setup`, machine half: the gates, the layout it writes, and the daemon gate.

The world is faked at the probe boundary — no brew, no claude, no launchctl, no live daemon —
but everything between is the real code: real staging, the real symlink swap, the real plist
render, the real config writes.
"""

from __future__ import annotations

import pytest
from tests.test_supervisor import FakePlatform

from selly_agent import cli, config, heartbeat, passes, paths, setup_cli
from selly_agent.installer import checks, materialize, preflight


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "checkout"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "selly-agent").write_text("#!/usr/bin/env python3\n")
    (root / "src" / "selly_agent").mkdir(parents=True)
    (root / "src" / "selly_agent" / "__init__.py").write_text("__version__ = '9.9.9'\n")
    return root


@pytest.fixture
def world(monkeypatch, xdg_tmp, tree):
    """Every gate passing, a launchctl that only records, and a daemon that comes up."""
    platform = FakePlatform()
    monkeypatch.setattr(setup_cli, "get_platform", lambda: platform)
    monkeypatch.setattr(materialize, "source_tree", lambda: tree)
    monkeypatch.setattr(preflight, "check_platform", lambda: checks.ok("platform", "macOS"))
    monkeypatch.setattr(preflight, "check_node", lambda: checks.ok("node", "v22"))
    monkeypatch.setattr(preflight, "check_chrome", lambda chrome_bin=None: checks.ok("chrome", "-"))
    monkeypatch.setattr(preflight, "check_claude", lambda cfg: checks.ok("claude CLI", "signed in"))
    monkeypatch.setattr(preflight, "prewarm_playwright", lambda cfg: checks.ok("playwright", "-"))
    monkeypatch.setattr(preflight, "agent_context", lambda env=None: "")
    monkeypatch.setattr(passes, "resolve_claude_bin", lambda cfg: "/opt/claude/bin/claude")
    monkeypatch.setattr(heartbeat, "wait_fresh", lambda path, **kwargs: True)
    # A PATH that already has ~/.local/bin, so the rc-file offer stays out of the way unless a
    # test is about it.
    monkeypatch.setenv("PATH", f"/usr/bin:{paths.user_bin_dir()}")
    return platform


def setup_main(*argv) -> int:
    return cli.main(["selly-agent", "setup", *argv])


# --- the happy path ----------------------------------------------------------------------------


def test_a_default_install_stages_a_version_and_brings_the_daemon_up(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0

    from selly_agent import __version__

    version_dir = paths.versions_dir() / __version__
    assert (version_dir / "bin" / "selly-agent").is_file()
    assert materialize.current_version() == __version__
    assert paths.shim_path().is_symlink()

    cfg = config.load()
    assert cfg.daemon_mode == "manual"
    assert cfg.claude_bin == "/opt/claude/bin/claude"

    out = capsys.readouterr().out
    assert "SELLY:" in out
    assert "Worker is up." in out


def test_setup_announces_every_location_before_writing(world, capsys) -> None:
    setup_main("--yes", "--manual")
    out = capsys.readouterr().out
    for location in (paths.data_root(), paths.config_dir(), paths.cache_dir()):
        assert str(location) in out


def test_manual_mode_starts_the_daemon_and_says_what_manual_costs(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert world.is_registered("com.selly.agent")  # started now...
    out = capsys.readouterr().out
    assert "won't come back on its own" in out  # ...but not after a logout


def test_login_start_mode_registers_the_plist_in_launch_agents(world) -> None:
    assert setup_main("--yes", "--login-start") == 0
    plist = paths.launch_agents_dir(platform=world) / "com.selly.agent.plist"
    assert plist.exists()
    assert config.load().daemon_mode == "login-start"


def test_dev_mode_points_current_at_the_tree_instead_of_copying(world, tree) -> None:
    assert setup_main("--yes", "--manual", "--dev") == 0
    assert materialize.current_target() == tree.resolve()
    assert materialize.is_dev_install() is True
    assert not (paths.versions_dir() / "0.1.0.dev0").exists()


def test_a_re_run_is_idempotent(world) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert setup_main("--yes", "--manual") == 0
    assert materialize.current_version() is not None
    assert paths.shim_path().is_symlink()


# --- gates -------------------------------------------------------------------------------------


def test_an_unsupported_platform_is_one_honest_line_not_a_banner(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        preflight, "check_platform", lambda: checks.fail("platform", "linux", "macOS only")
    )
    assert setup_main("--yes") == 1
    captured = capsys.readouterr()
    assert "linux" in captured.err
    assert "SELLY" not in captured.out  # no banner, no path preview


def test_a_tree_under_a_protected_folder_stops_setup_before_it_writes(world, capsys) -> None:
    from selly_agent.installer import preflight as pf

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            pf,
            "check_tree_location",
            lambda tree: checks.fail("install location", "under ~/Documents", "Move it"),
        )
        assert setup_main("--yes", "--manual") == 1
    assert not paths.current().exists()
    assert "Move it" in capsys.readouterr().err


def test_a_signed_out_claude_offers_the_login_flow_and_re_probes(
    world, monkeypatch, capsys
) -> None:
    answers = iter(
        [
            checks.fail("claude CLI", "installed but signed out", "claude auth login"),
            checks.ok("claude CLI", "signed in"),
        ]
    )
    monkeypatch.setattr(preflight, "check_claude", lambda cfg: next(answers))
    logins = []
    monkeypatch.setattr(preflight, "claude_login", lambda cfg: logins.append(cfg) or 0)
    monkeypatch.setattr(setup_cli.Ui, "_detect_interactive", lambda self: True)

    assert setup_main("--yes", "--manual") == 0
    assert len(logins) == 1
    assert "Handing over to `claude auth login`" in capsys.readouterr().out


def test_a_signed_out_claude_with_no_terminal_stops_rather_than_pretending(
    world, monkeypatch, capsys
) -> None:
    # `--yes` is not a person: the login flow prints a URL and reads back a pasted code.
    monkeypatch.setattr(
        preflight,
        "check_claude",
        lambda cfg: checks.fail("claude CLI", "signed out", "claude auth login"),
    )
    logins = []
    monkeypatch.setattr(preflight, "claude_login", lambda cfg: logins.append(cfg) or 0)

    assert setup_main("--yes", "--manual") == 1
    assert logins == []
    assert "claude auth login" in capsys.readouterr().err


def test_a_missing_claude_cli_is_fatal_and_never_installed_for_you(world, monkeypatch) -> None:
    monkeypatch.setattr(
        preflight, "check_claude", lambda cfg: checks.fail("claude CLI", "not installed", "curl …")
    )
    monkeypatch.setattr(passes, "resolve_claude_bin", lambda cfg: None)
    assert setup_main("--yes", "--manual") == 1


def test_a_missing_dependency_offers_brew_then_re_probes(world, monkeypatch, capsys) -> None:
    probes = iter(
        [
            checks.fail("node", "not installed", "brew install node"),
            checks.ok("node", "v22 at /opt/homebrew/bin/node"),
        ]
    )
    monkeypatch.setattr(preflight, "check_node", lambda: next(probes))
    monkeypatch.setattr(preflight, "homebrew_path", lambda: "/opt/homebrew/bin/brew")
    installed = []
    monkeypatch.setattr(
        preflight,
        "brew_install",
        lambda package, cask=False: (installed.append((package, cask)), (True, ""))[1],
    )

    assert setup_main("--yes", "--manual") == 0
    assert installed == [("node", False)]
    assert "brew install node" in capsys.readouterr().out


def test_without_homebrew_a_missing_dependency_is_fatal_and_brew_is_never_bootstrapped(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        preflight, "check_node", lambda: checks.fail("node", "not installed", "brew install node")
    )
    monkeypatch.setattr(preflight, "homebrew_path", lambda: "")

    assert setup_main("--yes", "--manual") == 1
    err = capsys.readouterr().err
    assert "https://brew.sh" in err
    assert not paths.current().exists()


def test_a_failing_prewarm_is_a_warning_not_a_stop(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        preflight, "prewarm_playwright", lambda cfg: checks.warn("playwright", "offline")
    )
    assert setup_main("--yes", "--manual") == 0
    assert "⚠️ playwright" in capsys.readouterr().out


def test_an_agent_session_stops_setup_asking_questions(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(preflight, "agent_context", lambda env=None: "CLAUDECODE")
    monkeypatch.setattr(setup_cli.Ui, "_detect_interactive", lambda self: True)

    assert setup_main("--manual") == 0
    assert "inside an agent session" in capsys.readouterr().out


# --- the daemon gate -----------------------------------------------------------------------------


def test_a_daemon_that_never_heartbeats_fails_with_its_own_log(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(heartbeat, "wait_fresh", lambda path, **kwargs: False)
    paths.ensure_state_dirs()
    (paths.logs_dir() / "agent.err.log").write_text("Traceback…\nOSError: port 7355 in use\n")

    assert setup_main("--yes", "--manual") == 1
    err = capsys.readouterr().err
    assert "didn't start" in err
    assert "port 7355 in use" in err


# --- PATH -----------------------------------------------------------------------------------


def test_a_missing_path_entry_is_offered_and_written_to_the_rc_file(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--yes", "--manual") == 0

    rc = materialize.shell_rc_target("/bin/zsh")
    assert materialize.rc_block_present(rc.read_text())
    assert "isn't on your PATH" in capsys.readouterr().out


def test_no_modify_path_prints_the_line_and_leaves_the_rc_file_alone(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--yes", "--manual", "--no-modify-path") == 0

    assert not materialize.shell_rc_target("/bin/zsh").exists()
    assert materialize.RC_BLOCK_BODY in capsys.readouterr().out


def test_a_piped_run_that_never_said_yes_gets_the_line_not_an_edit(
    world, monkeypatch, capsys
) -> None:
    # No terminal to ask at and no --yes: nobody consented to a dotfile edit.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--manual") == 0

    assert not materialize.shell_rc_target("/bin/zsh").exists()
    assert materialize.RC_BLOCK_BODY in capsys.readouterr().out
