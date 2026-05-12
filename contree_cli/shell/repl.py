"""Interactive REPL for the contree shell."""

from __future__ import annotations

import contextlib
import logging
import re
import shlex
import sys
from functools import cached_property

from contree_cli import FORMATTER, IN_SHELL, PROFILE, SESSION_STORE, ArgumentsProtocol
from contree_cli.client import ApiError
from contree_cli.output import FORMATTERS, OutputFormatter
from contree_cli.session import SessionStore
from contree_cli.shell.cache import SourceCache
from contree_cli.shell.completer import ShellCompleter
from contree_cli.shell.parser import ShellArgumentParser, ShellParseError
from contree_cli.types import Colors

log = logging.getLogger(__name__)

_PROMPT_BASE = "contree"

# Regex matching ANSI escape sequences (CSI and OSC).
ANSI_RE = re.compile(r"(\033\[[0-9;]*m)")

try:
    import readline

    LIBEDIT: bool = "libedit" in (getattr(readline, "__doc__", "") or "")
    if LIBEDIT:
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")
    READLINE_AVAILABLE = True
except ImportError:
    READLINE_AVAILABLE = False
    LIBEDIT = False


def _readline_safe_prompt(prompt: str) -> str:
    """Make *prompt* safe for readline cursor-position tracking.

    GNU readline recognises ``\\x01``/``\\x02`` markers around invisible
    sequences.  macOS libedit does not handle them reliably (even with
    ``EL_PROMPT_ESC``), so we strip ANSI codes entirely — the coloured
    status line printed to stderr still provides visual context.
    """
    if LIBEDIT:
        return ANSI_RE.sub("", prompt)
    return ANSI_RE.sub(lambda m: "\x01" + m.group() + "\x02", prompt)


DURATION_RE = re.compile(r"\A(\d+(?:\.\d+)?)([smhd]?)\Z")
DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int | None:
    """Parse a ``timeout`` duration spec like ``60``, ``30s``, ``5m``, ``1h``.

    Returns the duration in whole seconds, or ``None`` if *text* is not a
    valid spec (in which case the caller should fall through and treat the
    user input as a regular command line).
    """
    match = DURATION_RE.match(text)
    if match is None:
        return None
    value = float(match.group(1))
    multiplier = DURATION_UNITS[match.group(2)]
    seconds = int(value * multiplier)
    if seconds <= 0:
        return None
    return seconds


def intercept_timeout(line: str) -> tuple[int, str] | None:
    """Detect ``timeout DURATION COMMAND...`` and split off the duration.

    Returns ``(seconds, remainder_line)`` when *line* starts with the
    ``timeout`` builtin followed by a parseable duration and at least one
    further token; ``None`` otherwise. Whitespace and quoting in the
    remainder are preserved verbatim so ``sh -c`` sees the same expression
    the user typed.
    """
    parts = line.split(None, 2)
    if len(parts) < 3 or parts[0] != "timeout":
        return None
    seconds = parse_duration(parts[1])
    if seconds is None:
        return None
    return seconds, parts[2]


# Bare names that are forwarded as contree management commands.
CONTREE_ALIASES = frozenset({"ls", "cat"})

# Bare editor names that map to ``contree file edit`` with EDITOR override.
EDITOR_ALIASES = frozenset({"vim", "vi", "nvim", "nano"})

# Aliases for ``help <topic>`` lookup.
_HELP_ALIASES: dict[str, str] = {
    "quit": "exit",
    "-f": "--format",
    "vi": "vim",
}

# Per-builtin help text shown by ``help <topic>``.
_BUILTIN_HELP: dict[str, str] = {
    "cd": (
        "Usage: cd [PATH]\n"
        "\n"
        "Change the working directory for subsequent commands.\n"
        "  cd /app     change to /app\n"
        "  cd src      change relative to current directory\n"
        "  cd -        switch to previous directory\n"
        "  cd          reset to sandbox default"
    ),
    "pwd": "Usage: pwd\n\nPrint the current working directory.",
    "history": (
        "Usage: history [N]\n"
        "\n"
        "Show command history for the current session.\n"
        "Optional argument N limits output to the last N entries."
    ),
    "help": (
        "Usage: help [TOPIC]\n"
        "\n"
        "Show general shell help, or help for a specific command.\n"
        "  help         general shell help\n"
        "  help cd      help for the cd builtin\n"
        "  help run     help for the run command"
    ),
    "clear": "Usage: clear\n\nClear the terminal screen.",
    "timeout": (
        "Usage: timeout DURATION COMMAND...\n"
        "\n"
        "Run COMMAND in the sandbox with the API operation timeout set\n"
        "to DURATION. Mirrors the convention of the GNU 'timeout'\n"
        "binary but enforces the limit server-side instead of spawning\n"
        "a local one.\n"
        "\n"
        "DURATION is an integer or float, optionally followed by a\n"
        "unit suffix: s (seconds, default), m (minutes), h (hours),\n"
        "d (days). When DURATION cannot be parsed, the line is sent to\n"
        "the sandbox unmodified so the in-image 'timeout' binary still\n"
        "works for advanced cases (signals, --kill-after, etc)."
    ),
    "exit": "Usage: exit | quit\n\nExit the interactive shell (Ctrl-D also works).",
    "--format": (
        "Usage: --format [NAME] | -f [NAME]\n"
        "\n"
        "Change the output format for the session, or show the current\n"
        "format name when called without arguments.\n"
        "\n"
        "Available formats: " + ", ".join(sorted(FORMATTERS))
    ),
    "vim": (
        "Usage: vim <PATH>\n"
        "\n"
        "Open a sandbox file in vim via 'contree file edit'.\n"
        "Downloads the file, opens it locally, and stages changes\n"
        "as a pending upload for the next run."
    ),
    "nvim": (
        "Usage: nvim <PATH>\n"
        "\n"
        "Open a sandbox file in nvim via 'contree file edit'.\n"
        "Downloads the file, opens it locally, and stages changes\n"
        "as a pending upload for the next run."
    ),
    "nano": (
        "Usage: nano <PATH>\n"
        "\n"
        "Open a sandbox file in nano via 'contree file edit'.\n"
        "Downloads the file, opens it locally, and stages changes\n"
        "as a pending upload for the next run."
    ),
}


class ContreeShell:
    """Interactive REPL that dispatches to existing command handlers.

    Commands prefixed with ``contree`` are dispatched as management
    commands via argparse (e.g. ``contree ls /etc``).  Everything else
    is treated as an implicit ``run`` — the tokens are joined into a
    shell expression and executed inside the current session sandbox.

    Several bare names are intercepted for convenience:

    * ``ls``, ``cat`` — forwarded as the corresponding contree commands.
    * ``vim``, ``vi``, ``nano`` — open ``contree file edit`` with the
      host editor.
    * ``cd`` — change the working directory for subsequent ``run``
      commands (tracked in memory).
    """

    def __init__(
        self,
        parser: ShellArgumentParser,
        completer: ShellCompleter,
    ) -> None:
        self._parser = parser
        self._completer = completer
        self.__prev_cwd = "/"

    @property
    def cwd(self) -> str:
        return self.session_store.get_cwd() or "/"

    @cwd.setter
    def cwd(self, value: str) -> None:
        self.__prev_cwd = self.cwd
        self.session_store.set_cwd(value)

    @property
    def prev_cwd(self) -> str:
        return self.__prev_cwd

    @cached_property
    def session_store(self) -> SessionStore:
        store = SESSION_STORE.get()
        if store is None:
            raise RuntimeError("Session store was not set")
        return store

    def print_status_line(self) -> None:
        """Print the info line (session/image) above the input prompt.

        Written to *stderr* so that readline (which manages stdout) is
        never confused by the extra output between prompts.
        """
        session_key = ""
        image_uuid = ""
        depth = 0
        try:
            session = self.session_store.session
            if session is not None:
                session_key = session.session_key
                image_uuid = session.current_image
                depth = self.session_store.history_depth()
        except (LookupError, Exception):
            pass
        line = (
            f"{Colors.GRAY('session: ')}{Colors.YELLOW(session_key)} "
            f"{Colors.CYAN(f'[{depth}]')} "
            f"{Colors.GRAY('image: ')}"
            f"{Colors.GREEN(image_uuid)}"
        )
        print(line, file=sys.stderr)

    @property
    def _prompt(self) -> str:
        """Short input prompt — only cwd and branch, no ANSI length issues."""
        branch = ""
        try:
            session = self.session_store.session
            if session is not None:
                branch = session.active_branch
        except (LookupError, Exception):
            pass
        branch_part = Colors.MAGENTA(f"({branch})") + " " if branch else ""
        return branch_part + Colors.BOLD_BLUE(self.cwd) + " $ "

    def run(self) -> None:
        """Main REPL loop: readline setup -> input(prompt) -> dispatch."""
        token = IN_SHELL.set(True)

        if READLINE_AVAILABLE:
            readline.set_completer(self._completer.complete)
            readline.set_completer_delims(" \t\n")

        print("contree interactive shell (type 'help' for commands, Ctrl-D to exit)")
        try:
            while True:
                try:
                    self.print_status_line()
                    line = input(_readline_safe_prompt(self._prompt))
                except KeyboardInterrupt:
                    # Ctrl-C on empty prompt — print newline, continue
                    print()
                    continue
                line = line.strip()
                if not line:
                    continue
                # Handle line continuation (trailing \ or unclosed quotes)
                line = self._read_continuation(line)
                if not line:
                    continue
                self.execute(line)
        except EOFError:
            # Ctrl-D — clean exit
            print()
        finally:
            IN_SHELL.reset(token)

    @staticmethod
    def _read_continuation(line: str) -> str:
        """Prompt for continuation lines when input is incomplete.

        Handles trailing backslash (line continuation) and unclosed
        quotes, similar to how interactive shells behave.  Returns an
        empty string when the user cancels via Ctrl-C or Ctrl-D.

        Backslash-newline pairs are resolved as line continuations
        (both characters removed) before returning, because Python's
        ``shlex.split`` does not treat ``\\<newline>`` as a line
        continuation — it keeps the newline as a literal character.
        """
        while True:
            try:
                shlex.split(line)
                return line.replace("\\\n", "")
            except ValueError:
                pass
            try:
                continuation = input(_readline_safe_prompt("> "))
            except KeyboardInterrupt:
                print()
                return ""
            except EOFError:
                print()
                return ""
            line += "\n" + continuation

    def execute(self, line: str) -> None:
        """Parse line and dispatch to the appropriate command handler."""
        # Tokenize
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"Parse error: {exc}", file=sys.stderr)
            return

        if not tokens:
            return

        match cmd := tokens[0]:
            case "exit" | "quit":
                raise EOFError
            case "help":
                self.handle_help(tokens[1:])
            case "clear":
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.flush()
            case "--format" | "-f":
                self.handle_format_command(tokens[1:])
            case "cd":
                self.handle_cd(tokens[1:])
            case "pwd":
                print(self.cwd)
            case "history":
                self.handle_history(tokens[1:])
            case "ls" | "cat":
                args = tokens[1:]
                if not self.has_pending_files() and self.is_simple_alias(cmd, args):
                    resolved = [cmd] + [self.resolve_path(a) for a in args]
                    self.dispatch_contree(resolved)
                else:
                    self.dispatch_run(line)
            case "vim" | "vi" | "nvim" | "nano":
                self.dispatch_edit(cmd, tokens[1:])
            case "contree":
                self.dispatch_contree(tokens[1:])
            case "timeout":
                intercepted = intercept_timeout(line)
                if intercepted is None:
                    self.dispatch_run(line)
                else:
                    seconds, remainder = intercepted
                    self.dispatch_run(remainder, timeout=seconds)
            case _:
                self.dispatch_run(line)

    def session_snapshot(self) -> tuple[str, str, str, str]:
        """Capture the state we watch for completion-cache invalidation."""
        try:
            profile = PROFILE.get().name
        except LookupError:
            profile = "default"
        try:
            session = self.session_store.session
        except (LookupError, Exception):
            session = None
        if session is None:
            return profile, self.session_store.session_key, "", ""
        return (
            profile,
            session.session_key,
            session.current_image,
            session.active_branch,
        )

    def invalidate_completion_cache(
        self,
        before: tuple[str, str, str, str],
        after: tuple[str, str, str, str],
    ) -> None:
        """Bust completion caches when watched session state changed."""
        profile_before, key_before, image_before, branch_before = before
        profile_after, key_after, image_after, branch_after = after
        try:
            cache = SourceCache(self.session_store.cache, profile_after)
        except (LookupError, Exception):
            return

        if profile_before != profile_after:
            cache.invalidate_all()
            with contextlib.suppress(Exception):
                SourceCache(
                    self.session_store.cache,
                    profile_before,
                ).invalidate_all()
            return

        if key_before != key_after:
            cache.invalidate_kind_prefix("")
            return

        if image_before != image_after:
            cache.invalidate("", "images")
            cache.invalidate_scope(image_before)
            cache.invalidate_scope(image_after)
            cache.invalidate("", "operations")

        if branch_before != branch_after:
            cache.invalidate_kind_prefix("branches")

    def dispatch_contree(self, tokens: list[str]) -> None:
        """Dispatch a contree management command via argparse."""
        try:
            ns = self._parser.parse_args(tokens)
        except ShellParseError as exc:
            # status=0 means --help was triggered (already printed)
            if exc.status == 0:
                return
            if exc.usage:
                print(exc.usage, file=sys.stderr, end="")
            if exc.message:
                print(f"error: {exc.message}", file=sys.stderr)
            return

        if ns.command is None or "-h" in tokens or "--help" in tokens:
            self.print_shell_help()
            return

        if ns.command == "shell":
            # Prevent recursive shell invocation (confusing and unsafe).
            print(
                "Error: 'contree shell' cannot be run from within the shell.",
                file=sys.stderr,
            )
            return

        handler = ns.handler
        loader: type[ArgumentsProtocol] = ns.load_args

        # Per-command format override via -f/--format
        fmt_token = None
        fmt_name: str | None = getattr(ns, "output_format", None)
        if fmt_name is not None:
            fmt_token = FORMATTER.set(FORMATTERS[fmt_name]())

        formatter = FORMATTER.get()

        before = self.session_snapshot()
        try:
            handler(loader.from_args(ns))
        except ApiError as exc:
            print(f"API error: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print()
        except SystemExit:
            # --help raises SystemExit; keep shell alive and continue.
            pass
        except Exception as exc:
            log.error("Command failed: %s", exc, exc_info=True)
        finally:
            formatter.flush()
            if fmt_token is not None:
                FORMATTER.reset(fmt_token)
            self.invalidate_completion_cache(before, self.session_snapshot())

    def dispatch_run(self, line: str, *, timeout: int | None = None) -> None:
        """Dispatch a raw input line as an implicit ``run`` in the sandbox.

        The line is forwarded verbatim as a single shell expression so the
        remote ``sh -c`` sees operators like ``|``, ``;``, ``&&`` as the
        user typed them, rather than as quoted literal tokens.

        ``timeout`` overrides the API operation timeout. When ``None`` the
        server falls back to its default. Set explicitly by the ``timeout``
        shell builtin (``timeout 60 long-build``).
        """
        from contree_cli.cli.run import RunArgs, cmd_run

        args = RunArgs(
            command_args=[line],
            shell=True,
            cwd=self.cwd,
            timeout=timeout,
        )
        formatter = FORMATTER.get()

        before = self.session_snapshot()
        try:
            cmd_run(args)
        except ApiError as exc:
            print(f"API error: {exc}", file=sys.stderr)
        except KeyboardInterrupt:
            print()
        except SystemExit:
            pass
        except Exception as exc:
            log.error("Command failed: %s", exc, exc_info=True)
        finally:
            formatter.flush()
            self.invalidate_completion_cache(before, self.session_snapshot())

    def dispatch_edit(self, editor: str, args: list[str]) -> None:
        """Open a sandbox file in a host editor via ``file edit``."""
        if not args:
            print(f"Usage: {editor} <path>", file=sys.stderr)
            return
        resolved = self.resolve_path(args[0])
        self.dispatch_contree(["file", "edit", "--editor", editor, resolved])

    GLOB_CHARS = frozenset("*?[")

    def has_pending_files(self) -> bool:
        """Return True when the session has files awaiting upload."""
        try:
            return bool(self.session_store.pending_files())
        except (LookupError, Exception):
            return False

    def resolve_path(self, path: str) -> str:
        """Resolve a sandbox path via session store."""
        if not path:
            return path
        return self.session_store.resolve_path(path)

    def is_simple_alias(self, cmd: str, args: list[str]) -> bool:
        """Check whether alias args are simple enough for contree dispatch.

        Returns ``False`` (fall back to ``run``) when the arguments
        contain flags, glob characters, or an unexpected number of
        positional arguments for the given command.
        """
        for a in args:
            if a.startswith("-"):
                return False
            if self.GLOB_CHARS & set(a):
                return False
        if cmd == "cat":
            return len(args) == 1
        if cmd == "ls":
            return len(args) <= 1
        return True

    def handle_cd(self, args: list[str]) -> None:
        """Change the working directory for subsequent ``run`` commands."""
        if not args:
            self.cwd = ""
            return

        target = args[0]

        if target == "-" and self.prev_cwd:
            self.cwd = self.prev_cwd
            print(self.cwd)
            return
        self.cwd = self.session_store.resolve_path(target)

    def handle_history(self, args: list[str]) -> None:
        """Print shell history from the session database."""
        try:
            lines = self.session_store.load_shell_history()
        except (LookupError, Exception):
            lines = []
        if not lines:
            print("(no history)")
            return
        # Optional: limit output with an argument (e.g. ``history 20``)
        count = len(lines)
        if args:
            with contextlib.suppress(ValueError):
                count = int(args[0])
        for i, line in enumerate(lines[-count:], start=max(1, len(lines) - count + 1)):
            print(f" {i:5d}  {line}")

    @staticmethod
    def format_name(formatter: OutputFormatter) -> str:
        """Return the FORMATTERS key for *formatter*'s type."""
        for name, cls in FORMATTERS.items():
            if type(formatter) is cls:
                return name
        return type(formatter).__name__

    def handle_format_command(self, args: list[str]) -> None:
        """Handle ``--format [NAME]`` as a shell builtin."""
        if not args:
            # Print current format name
            print(self.format_name(FORMATTER.get()))
            return
        name = args[0]
        if name not in FORMATTERS:
            names = ", ".join(sorted(FORMATTERS))
            print(f"Unknown format {name!r}. Available: {names}", file=sys.stderr)
            return
        FORMATTER.set(FORMATTERS[name]())

    def handle_help(self, args: list[str]) -> None:
        """Show general shell help or help for a specific topic."""
        if not args:
            self.print_shell_help()
            return
        topic = _HELP_ALIASES.get(args[0], args[0])
        if topic in _BUILTIN_HELP:
            print(_BUILTIN_HELP[topic])
            return
        # Delegate to the contree command's --help.
        self.dispatch_contree([topic, "--help"])

    def print_shell_help(self) -> None:
        """Print custom shell help text."""
        print(
            "Contree interactive shell\n"
            "\n"
            "  Bare commands are executed in the sandbox as implicit\n"
            "  'contree run --shell' commands. Use the 'contree' prefix\n"
            "  for management commands (e.g. 'contree session branch').\n"
            "\n"
            "Builtins:\n"
            "  cd [PATH]          Change working directory (cd - for previous)\n"
            "  pwd                Print working directory\n"
            "  history [N]        Show command history (optional limit)\n"
            "  help [TOPIC]       Show help for a command or builtin\n"
            "  clear              Clear the terminal screen\n"
            "  --format [NAME]    Change or show the output format\n"
            "  exit / quit        Exit the shell (also Ctrl-D)\n"
            "\n"
            "Aliases:\n"
            "  ls [PATH]          List sandbox files (contree ls)\n"
            "  cat PATH           Show file content (contree cat)\n"
            "  vim/vi/nvim PATH   Edit via contree file edit\n"
            "  nano PATH          Edit via contree file edit\n"
            "\n"
            "  Aliases fall back to sandbox execution when args\n"
            "  contain flags or globs, or when files are pending.\n"
            "  Bypass with: contree run -- ls -la\n"
            "\n"
            "Line continuation:\n"
            "  Trailing \\ continues input on the next line.\n"
            "  Unclosed quotes also trigger continuation.\n"
            "\n"
            "Tab completion:\n"
            "  Commands, flags, sandbox paths, images, operations,\n"
            "  branches, and sessions all support Tab completion.\n"
            "\n"
            "Type 'help <command>' for detailed help on any command."
        )
