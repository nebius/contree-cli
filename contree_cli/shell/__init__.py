"""Start an interactive shell session.

Launches a REPL where bare commands (e.g. `apt install curl`) are
executed in the session sandbox via `run --shell`, and prefixed
commands (e.g. `contree ls /etc`) are dispatched as management
commands.

Built-in commands: cd, pwd, history, help, exit/quit.
Tab completion for commands, flags, image paths, tags, and branches.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

from contree_cli import CLIENT, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.shell.completer import ShellCompleter
from contree_cli.shell.history import load_history, save_history
from contree_cli.shell.parser import build_shell_parser
from contree_cli.shell.repl import ContreeShell

EPILOG = """\
for coding agents:
  bare commands are implicit `contree run --shell`
  management commands must be prefixed with `contree`
  exit with `exit`/`quit` or Ctrl-D
"""


@dataclass(frozen=True)
class ShellArgs(ArgumentsProtocol):
    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShellArgs:
        return cls()


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Register 'contree shell' subcommand."""
    return cmd_shell, ShellArgs


def cmd_shell(_args: ShellArgs) -> int | None:
    """Build parser + completer, start REPL."""
    parser, commands = build_shell_parser()

    try:
        client = CLIENT.get()
    except LookupError:
        client = None
    try:
        store = SESSION_STORE.get()
    except LookupError:
        store = None

    completer = ShellCompleter(commands, client=client, store=store, root_parser=parser)
    shell = ContreeShell(parser, completer)

    if store is not None:
        load_history(store)
    try:
        shell.run()
    finally:
        if store is not None:
            save_history(store)

    return None
