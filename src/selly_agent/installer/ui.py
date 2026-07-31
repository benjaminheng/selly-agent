"""Setup's voice: the one place that prints as the installer and the one place that prompts.

The `SELLY:` prefix marks the orchestrator speaking, and nothing else in the codebase may write
it — a CLI verb setup invokes (connect, healthcheck) owns its own polished output, and wrapping
that in a second voice reads as two programs arguing. Setup frames a child invocation with its
own lines before and after instead.

Everything decorative is conditional: colour only on a TTY with NO_COLOR unset, the banner only
when the terminal is wide enough for it. A run whose stdin is not a human — CI, a pipe, an agent
session — never blocks on a prompt; each one answers with its default and says that it did, so a
scripted install is a readable transcript rather than a hang.
"""

from __future__ import annotations

import os
import shutil
import sys

PREFIX = "SELLY:"

# The banner is skipped below this width rather than wrapped: a wrapped banner is unreadable
# noise, and the one-line fallback carries the same information.
BANNER_MIN_COLUMNS = 64

BANNER = (
    "███████╗███████╗██╗     ██╗     ██╗   ██╗    ▟██▙     ▟██▙",
    "██╔════╝██╔════╝██║     ██║     ╚██╗ ██╔╝   ▟█████████████▙",
    "███████╗█████╗  ██║     ██║      ╚████╔╝   ▐████  ███  ████▌",
    "╚════██║██╔══╝  ██║     ██║       ╚██╔╝    ▐███████▄███████▌",
    "███████║███████╗███████╗███████╗   ██║      ▜█████████████▛",
    "╚══════╝╚══════╝╚══════╝╚══════╝   ╚═╝        ▀▀▀▀▀▀▀▀▀▀▀",
)

_TEAL = "\033[36m"
_BOLD_TEAL = "\033[1;36m"
_DIM = "\033[2m"
_RESET = "\033[0m"


class Abort(Exception):
    """Setup cannot continue. Carries the reason and, where there is one, the fix.

    Raised rather than printed at the failure site so there is exactly one place that renders a
    fatal error, and so every gate is a pure `raise` a test can assert on.
    """

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message)
        self.message = message
        self.fix = fix


class Ui:
    """Setup's terminal. Construct once and pass it down; tests build one over a StringIO."""

    def __init__(
        self,
        *,
        stream=None,
        err=None,
        interactive: bool | None = None,
        color: bool | None = None,
        width: int | None = None,
        assume_yes: bool = False,
        input_fn=None,
    ):
        self.stream = stream if stream is not None else sys.stdout
        self.err = err if err is not None else sys.stderr
        self.interactive = self._detect_interactive() if interactive is None else interactive
        self.color = self._detect_color() if color is None else color
        self._width = width
        self.assume_yes = assume_yes
        self._input = input_fn if input_fn is not None else input

    # --- environment ---------------------------------------------------------------------

    def _detect_interactive(self) -> bool:
        return bool(getattr(sys.stdin, "isatty", lambda: False)()) and bool(
            getattr(self.stream, "isatty", lambda: False)()
        )

    def _detect_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    @property
    def width(self) -> int:
        if self._width is not None:
            return self._width
        return shutil.get_terminal_size(fallback=(80, 24)).columns

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    # --- output --------------------------------------------------------------------------

    def say(self, text: str = "") -> None:
        """One message in setup's voice. Every line carries the prefix, so a paragraph reads as
        one speaker rather than as output that lost its attribution halfway down."""
        marker = self._paint(PREFIX, _BOLD_TEAL)
        for line in (text or "").split("\n"):
            print(f"{marker} {line}".rstrip(), file=self.stream)

    def plain(self, text: str = "") -> None:
        """Unprefixed output — a URL, a path listing, a command to copy. The prefix is the
        orchestrator talking *about* something; this is the something."""
        print(text, file=self.stream)

    def warn(self, text: str) -> None:
        self.say(f"⚠️  {text}")

    def note(self, text: str) -> None:
        """A dimmed aside: what a step assumed, what was skipped, where to look."""
        self.plain(self._paint(f"   {text}", _DIM))

    def banner(self, version: str) -> None:
        if self.width < BANNER_MIN_COLUMNS:
            self.plain(self._paint(f"SELLY v{version}", _BOLD_TEAL))
            return
        for line in BANNER:
            self.plain(self._paint(line, _TEAL))

    def fatal(self, exc: Abort) -> None:
        """Render an Abort. The only place a fatal error is printed."""
        print(f"{PREFIX} {exc.message}", file=self.err)
        if exc.fix:
            for line in exc.fix.split("\n"):
                print(f"{PREFIX}   {line}", file=self.err)

    def die(self, message: str, fix: str = "") -> None:
        raise Abort(message, fix)

    # --- prompts -------------------------------------------------------------------------

    def _ask_raw(self, prompt: str) -> str:
        print(f"{self._paint(PREFIX, _BOLD_TEAL)} {prompt}", end="", file=self.stream, flush=True)
        try:
            return self._input().strip()
        except (EOFError, KeyboardInterrupt):
            self.plain("")
            raise Abort("interrupted — nothing further was changed") from None

    def confirm(self, question: str, *, default: bool = True) -> bool:
        """A yes/no question. `--yes` and a non-interactive run both take the default, out loud."""
        suffix = "[Y/n]" if default else "[y/N]"
        if self.assume_yes or not self.interactive:
            self.say(f"{question} {suffix} {'y' if default else 'n'}")
            return default
        while True:
            answer = self._ask_raw(f"{question} {suffix} ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False

    def ask(self, question: str, *, default: str = "") -> str:
        """Free text, with a default shown when there is one."""
        if not self.interactive:
            self.say(f"{question} {default}")
            return default
        shown = f" [{default}]" if default else ""
        return self._ask_raw(f"{question}{shown} ") or default

    def choose(self, question: str, options, *, default_index: int = 0) -> int:
        """Pick one of a numbered list; returns its index."""
        if not self.interactive:
            self.say(f"{question} {options[default_index]}")
            return default_index
        self.say(question)
        for number, option in enumerate(options, start=1):
            self.plain(f"  {number}) {option}")
        while True:
            raw = self._ask_raw(f"Enter 1-{len(options)} [default {default_index + 1}]: ")
            if not raw:
                return default_index
            try:
                picked = int(raw)
            except ValueError:
                continue
            if 1 <= picked <= len(options):
                return picked - 1

    def multiselect(self, question: str, options) -> list:
        """Pick any number of a numbered list; returns their indices, possibly none.

        Skipping is a first-class answer (plain Enter), because every caller of this is an
        optional step — offering a list must never become a step you cannot get past.
        """
        if not options:
            return []
        if not self.interactive:
            self.say(f"{question} (skipped — no terminal to ask at)")
            return []
        self.say(question)
        for number, option in enumerate(options, start=1):
            self.plain(f"  {number}) {option}")
        self.say("Enter the numbers (comma-separated), 'a' for all, or Enter to skip.")
        while True:
            raw = self._ask_raw("Which? ").lower()
            if not raw:
                return []
            if raw in ("a", "all"):
                return list(range(len(options)))
            picked = []
            for part in raw.replace(" ", ",").split(","):
                if not part:
                    continue
                try:
                    index = int(part) - 1
                except ValueError:
                    picked = []
                    break
                if not 0 <= index < len(options) or index in picked:
                    picked = []
                    break
                picked.append(index)
            if picked:
                return sorted(picked)
