"""launchd integration: golden plist render, and mode logic with launchctl stubbed."""

from __future__ import annotations

from pathlib import Path

from selly_agent import config, paths, supervisor
from selly_agent.platform.macos import MacOSPlatform

GOLDEN = Path(__file__).resolve().parent / "golden" / "com.selly.agent.plist"


class FakePlatform(MacOSPlatform):
    """A macOS platform whose launchctl calls are recorded in-memory instead of executed."""

    def __init__(self):
        self.registered_labels: set[str] = set()
        self.register_calls: list[Path] = []
        self.unregister_calls: list[str] = []

    def register(self, config_path: Path) -> None:
        self.register_calls.append(Path(config_path))
        self.registered_labels.add(Path(config_path).stem)

    def unregister(self, label: str) -> None:
        self.unregister_calls.append(label)
        self.registered_labels.discard(label)

    def is_registered(self, label: str) -> bool:
        return label in self.registered_labels


# --- pure render --------------------------------------------------------------------------


def test_plist_render_matches_golden() -> None:
    text = MacOSPlatform().render_supervisor(
        label="com.selly.agent",
        program_args=["/usr/bin/python3", "/opt/current/bin/selly-agent", "daemon", "run"],
        stdout_path=Path("/state/logs/agent.out.log"),
        stderr_path=Path("/state/logs/agent.err.log"),
        marker=supervisor.MARKER,
    )
    assert text == GOLDEN.read_text()


# --- mode logic ---------------------------------------------------------------------------


def test_install_manual_places_in_config_dir_and_does_not_register(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="manual", platform=fake)
    assert rc == 0

    plist = paths.config_dir() / "com.selly.agent.plist"
    assert plist.exists() and supervisor.MARKER in plist.read_text()
    assert fake.register_calls == []  # manual mode does not auto-start
    assert config.load().daemon_mode == "manual"
    assert paths.current().is_symlink()


def test_install_login_start_places_in_launch_agents_and_registers(xdg_tmp) -> None:
    fake = FakePlatform()
    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 0

    plist = paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist"
    assert plist.exists()
    assert fake.is_registered("com.selly.agent")
    assert config.load().daemon_mode == "login-start"


def test_flip_moves_the_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    manual_plist = paths.config_dir() / "com.selly.agent.plist"
    assert manual_plist.exists()

    supervisor.install(mode="login-start", platform=fake)
    login_plist = paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist"
    assert login_plist.exists()
    assert not manual_plist.exists()  # moved, not duplicated
    assert config.load().daemon_mode == "login-start"


def test_install_refuses_foreign_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    la_dir = paths.launch_agents_dir(platform=fake)
    la_dir.mkdir(parents=True)
    foreign = la_dir / "com.selly.agent.plist"
    foreign.write_text("<plist>legacy daemon, not ours</plist>")

    rc = supervisor.install(mode="login-start", platform=fake)
    assert rc == 2
    assert foreign.read_text() == "<plist>legacy daemon, not ours</plist>"  # untouched
    assert fake.register_calls == []


def test_start_then_stop(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)

    assert supervisor.start(platform=fake) == 0
    assert fake.is_registered("com.selly.agent")
    assert supervisor.start(platform=fake) == 0  # idempotent friendly no-op

    assert supervisor.stop(platform=fake) == 0
    assert not fake.is_registered("com.selly.agent")


def test_start_without_install_reports_not_installed(xdg_tmp) -> None:
    fake = FakePlatform()
    assert supervisor.start(platform=fake) == 2


def test_uninstall_removes_our_plist(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", platform=fake)
    assert supervisor.uninstall(platform=fake) == 0
    assert not (paths.launch_agents_dir(platform=fake) / "com.selly.agent.plist").exists()
    assert not fake.is_registered("com.selly.agent")


def test_status_manual_stopped(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="manual", platform=fake)
    st = supervisor.gather_status(platform=fake)
    assert st.mode == "manual"
    assert st.registered is False
    assert st.label == "com.selly.agent"


def test_label_override_is_recorded_and_used(xdg_tmp) -> None:
    fake = FakePlatform()
    supervisor.install(mode="login-start", label="com.selly.agent.dev", platform=fake)
    assert (paths.launch_agents_dir(platform=fake) / "com.selly.agent.dev.plist").exists()
    assert config.load().daemon_label == "com.selly.agent.dev"
    assert supervisor.gather_status(platform=fake).label == "com.selly.agent.dev"
