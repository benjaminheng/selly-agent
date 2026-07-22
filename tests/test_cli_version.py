"""The version subcommand prints the single __version__ constant."""

from __future__ import annotations

from selly_agent import __version__
from selly_agent.cli import main


def test_version_prints_constant(capsys) -> None:
    rc = main(["selly-agent", "version"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == __version__
