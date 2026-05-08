from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType

from contree_cli import SetupResult

FORCE_COLOR = bool(os.getenv("FORCE_COLOR"))
STDERR_IS_A_TTY = sys.stderr.isatty() or FORCE_COLOR
STDOUT_IS_A_TTY = sys.stdout.isatty() or FORCE_COLOR
IS_A_TTY = STDERR_IS_A_TTY

# Standard CLI flag conventions.
# The main goal is guarantee unique short and long flags for all subcommands
# Use as: parser.add_argument(*FLAGS["force"], ...)
FLAGS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        # global
        "version": ("-v", "--version"),
        "config": ("-c", "--config"),
        "format": ("-f", "--format"),
        "log_level": ("-L", "--log-level"),
        "session": ("-S", "--session"),
        "project": ("-P", "--project"),
        "token": ("--token",),
        "url": ("-u", "--url"),
        # shared across commands
        "all": ("-a", "--all"),
        "delete": ("-U", "--delete", "--rm"),
        "force": ("-y", "--force"),
        "kind": ("-k", "--kind"),
        "quiet": ("-q", "--quiet"),
        "since": ("--since",),
        "until": ("--until",),
        "timeout": ("-t", "--timeout"),
        "profile": ("-p", "--profile"),
        "offline": ("-O", "--offline"),
        "status": ("--status",),
        # run
        "cwd": ("-C", "--cwd"),
        "detach": ("-d", "--detach", "--no-wait"),
        "disposable": ("-D", "--disposable"),
        "editor": ("-E", "--editor"),
        "env": ("-e", "--env"),
        "file": ("-F", "--file"),
        "file_excludes": ("--file-excludes",),
        "filter": ("--filter",),
        "hostname": ("-H", "--hostname"),
        "interpreter": ("-I", "--interpreter"),
        "preserve_env": ("--preserve-env",),
        "shell": ("-s", "--shell"),
        "truncate": ("-T", "--truncate"),
        "use": ("--use",),
        # images
        "prefix": ("--prefix",),
        "uuid": ("-i", "--uuid"),
        "username": ("--username",),
        "password": ("--password",),
        "limit": ("--limit",),
        # use
        "new": ("-N", "--new"),
        # session
        "from_branch": ("--from",),
        "last": ("-l", "--last"),
        "prune": ("--prune",),
        "show_max": ("-M", "--show-max"),
    }
)


class ArgumentsFormatter(argparse.RawDescriptionHelpFormatter):
    """Formatter that preserves description/epilog whitespace.

    Set CONTREE_WRAP_TEXT=1 to switch to wrapping mode (used by docs generator).
    """

    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if (
            action.default not in (None, False, [], argparse.SUPPRESS)
            and "%(default)" not in help_text
        ):
            help_text += " (default: %(default)s)"
        return help_text


def get_command_docs(setup_fn: SetupFn) -> tuple[str | None, str | None]:
    """Extract description and epilog from the module that defines *setup_fn*.

    The module's ``__doc__`` becomes the description; an optional
    module-level ``EPILOG`` variable becomes the epilog.
    """
    mod = sys.modules.get(setup_fn.__module__)
    if mod is None:
        return None, None
    return mod.__doc__, getattr(mod, "EPILOG", None)


class Colors(Enum):
    """
    Each enum value is callable and can be used to wrap text in the corresponding color.

        Example usage:
            print(Colors.BOLD_RED("This is bold red text"))
    """

    DEFAULT = "\033[0m"
    BOLD = "\033[1m"
    BOLD_BLACK = "\033[1;30m"
    BOLD_BLUE = "\033[1;34m"
    BOLD_CYAN = "\033[1;36m"
    BOLD_GRAY = "\033[1;90m"
    BOLD_GREEN = "\033[1;32m"
    BOLD_MAGENTA = "\033[1;35m"
    BOLD_RED = "\033[1;31m"
    BOLD_WHITE = "\033[1;37m"
    BOLD_YELLOW = "\033[1;33m"
    BLACK = "\033[30m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    GREEN = "\033[32m"
    MAGENTA = "\033[35m"
    RED = "\033[31m"
    WHITE = "\033[37m"
    YELLOW = "\033[33m"

    def __call__(self, text: str) -> str:
        if not IS_A_TTY:
            return text
        return f"{self.value}{text}{Colors.DEFAULT.value}"


if sys.version_info >= (3, 11):

    def parse_datetime(value: str) -> datetime:
        """Parse an ISO 8601 datetime string from the API."""
        return datetime.fromisoformat(value).astimezone(tz=timezone.utc)
else:

    def parse_datetime(value: str) -> datetime:
        """Parse an ISO 8601 datetime string from the API.

        Python 3.10 ``fromisoformat`` does not accept the ``Z`` suffix.
        """
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(tz=timezone.utc)


def isoformat_datetime(dt: datetime) -> str:
    return dt.astimezone(tz=timezone.utc).isoformat().replace("+00:00", "Z")


_INTERVAL_RE = re.compile(r"([+-]?\d+)([smhdMy]?)")
_DATE_RE = re.compile(
    r"^(?P<year>\d{4})(?:[./\-\s]?(?P<month>\d{1,2}))?(?:[./\-\s]?(?P<day>\d{1,2}))?"
    r"(?:[T\s](?P<hour>\d{1,2})(?:[:./\-\s](?P<minutes>\d{1,2}))?(?:[:./\-\s](?P<seconds>\d{1,2}))?)?"
    r"Z?"
)


def parse_interval(
    value: str | None,
    now: datetime | None = None,
) -> datetime | None:
    """Parse +/- intervals (bare seconds or smhdMy) or ISO/date to UTC datetime."""

    if not value:
        return None

    # Absolute datetime/date first
    m = _DATE_RE.match(value)
    if m and m.group("year"):
        try:
            return datetime(
                int(m.group("year")),
                int(m.group("month") or 1),
                int(m.group("day") or 1),
                int(m.group("hour") or 0),
                int(m.group("minutes") or 0),
                int(m.group("seconds") or 0),
                tzinfo=timezone.utc,
            )
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    if now is None:
        now = datetime.now(timezone.utc)

    # Time-only (HH:MM[:SS]) defaults to today UTC
    if ":" in value and not any(sep in value for sep in "-/."):
        parts = value.split(":")
        if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
            hour = int(parts[0])
            minute = int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
            try:
                return datetime(
                    now.year,
                    now.month,
                    now.day,
                    hour,
                    minute,
                    second,
                    tzinfo=timezone.utc,
                )
            except ValueError as exc:
                raise argparse.ArgumentTypeError(str(exc)) from exc

    total_seconds = 0
    for count_str, unit in _INTERVAL_RE.findall(value):
        count = int(count_str.lstrip("+-"))
        if unit == "":
            total_seconds += count
            continue
        match unit:
            case "s":
                total_seconds += count
            case "m":
                total_seconds += count * 60
            case "h":
                total_seconds += count * 3600
            case "d":
                total_seconds += count * 86400
            case "M":
                total_seconds += count * 30 * 86400
            case "y":
                total_seconds += count * 365 * 86400
            case _:
                raise argparse.ArgumentTypeError(f"Invalid interval unit in {value!r}")

    # allow 0 seconds (no-op)
    return now - timedelta(seconds=total_seconds)


SetupFn = Callable[[argparse.ArgumentParser], SetupResult]
COMMAND_REGISTRY: list[tuple[str, str, SetupFn, list[str]]] = []
