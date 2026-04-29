"""Shell-safe argument parser that raises instead of calling sys.exit()."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import NoReturn

from contree_cli import Handler
from contree_cli.output import FORMATTERS
from contree_cli.types import (
    COMMAND_REGISTRY,
    FLAGS,
    ArgumentsFormatter,
    get_command_docs,
)


class ShellParseError(Exception):
    """Raised instead of sys.exit() when argparse encounters bad input."""

    def __init__(self, message: str, usage: str, status: int = 2) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage
        self.status = status


class ShellArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises ShellParseError instead of calling sys.exit()."""

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise ShellParseError(message or "", usage="", status=status)

    def error(self, message: str) -> NoReturn:
        raise ShellParseError(message, usage=self.format_usage())


@dataclass(frozen=True)
class CommandInfo:
    """Metadata about a registered shell command."""

    name: str
    handler: Handler
    parser: ShellArgumentParser
    aliases: list[str]


def build_shell_parser() -> tuple[ShellArgumentParser, dict[str, CommandInfo]]:
    """Build a parser tree from the same setup_parser() functions used by the CLI.

    Returns the root parser and a dict mapping command names (and aliases)
    to their CommandInfo for use by the completer.
    """
    root = ShellArgumentParser(prog="", add_help=False)
    root.add_argument(
        *FLAGS["format"],
        dest="output_format",
        default=None,
        choices=sorted(FORMATTERS),
    )
    subparsers = root.add_subparsers(
        dest="command",
        parser_class=ShellArgumentParser,
    )

    commands: dict[str, CommandInfo] = {}

    for name, help_text, setup_fn, aliases in COMMAND_REGISTRY:
        description, epilog = get_command_docs(setup_fn)
        sub_parser = subparsers.add_parser(
            name,
            help=help_text,
            aliases=aliases,
            description=description,
            epilog=epilog,
            formatter_class=ArgumentsFormatter,
        )
        handler, loader = setup_fn(sub_parser)
        sub_parser.set_defaults(handler=handler, load_args=loader)

        info = CommandInfo(
            name=name,
            handler=handler,
            parser=sub_parser,
            aliases=aliases,
        )
        commands[name] = info
        for alias in aliases:
            commands[alias] = info

    return root, commands


def get_command_names() -> list[str]:
    """Return all command names and aliases (for completion)."""
    names: list[str] = []
    for name, _, _, aliases in COMMAND_REGISTRY:
        names.append(name)
        names.extend(aliases)
    return names
