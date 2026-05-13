"""Tab completion for the interactive shell.

Argparse-driven: walks the live ``ShellArgumentParser`` tree returned
by :func:`build_shell_parser` to decide what the user is typing
(subcommand, flag name, flag value, positional). Per-action completion
sources live in :mod:`contree_cli.shell.argmap`, keyed by
``(command_path, dest)``.

The trie handles only bare-token shell builtins (``cd``, ``pwd``,
``vim``, ``ls``, ``cat``, ``--format``, ``-f``, ...) since
``repl.execute`` intercepts them before argparse sees them.

Public entry points: :class:`ShellCompleter` with
``.complete(text, state)`` (readline hook) and
``.compute_completions(text, line, begidx)``.
"""

from __future__ import annotations

import argparse
import logging
import shlex
from typing import TYPE_CHECKING

from contree_cli.shell import argspec
from contree_cli.shell.argmap import lookup as argmap_lookup
from contree_cli.shell.cache import SourceCache
from contree_cli.shell.parser import (
    CommandInfo,
    ShellArgumentParser,
    build_shell_parser,
    get_command_names,
)
from contree_cli.shell.sources import (
    SOURCES,
    CompletionContext,
    complete_choices,
    complete_command_name,
    complete_sandbox_dir,
    complete_sandbox_path,
)
from contree_cli.shell.trie import Handler, PrefixRouter

if TYPE_CHECKING:
    from contree_cli.client import ContreeClient
    from contree_cli.session import ImageCache, SessionStore

log = logging.getLogger(__name__)


BUILTIN_BARE_COMMANDS: tuple[str, ...] = (
    "cd",
    "pwd",
    "history",
    "help",
    "clear",
    "exit",
    "quit",
    "vim",
    "vi",
    "nvim",
    "nano",
    "ls",
    "cat",
    "--format",
    "-f",
)


class ShellCompleter:
    """Readline completer with argparse-driven dispatch."""

    def __init__(
        self,
        commands: dict[str, CommandInfo],
        client: ContreeClient | None = None,
        store: SessionStore | None = None,
        root_parser: ShellArgumentParser | None = None,
    ) -> None:
        self.commands = commands
        self.command_names = get_command_names()
        self.client = client
        self.store = store
        self.matches: list[str] = []

        if root_parser is None:
            root_parser, _ = build_shell_parser()
        self.root_parser = root_parser

        self.router: PrefixRouter = PrefixRouter()
        self.build_router()

    # -- public API used by readline and tests ----------------------------

    def complete(self, text: str, state: int) -> str | None:
        """Readline completer hook (called repeatedly with state=0,1,...)."""
        if state == 0:
            try:
                import readline

                line = readline.get_line_buffer()
                begidx = readline.get_begidx()
            except (ImportError, AttributeError):
                line = text
                begidx = 0
            self.matches = self.compute_completions(text, line, begidx)
        if state < len(self.matches):
            return self.matches[state]
        return None

    def compute_completions(
        self,
        text: str,
        line: str,
        begidx: int,
    ) -> list[str]:
        """Return matches for *text* given the full *line* and cursor index."""
        before_cursor = line[:begidx]
        try:
            tokens = shlex.split(before_cursor)
        except ValueError:
            return []

        ctx = self.context()

        # First token completion: nothing typed yet, or a single partial.
        if not tokens:
            return [n + " " for n in self.router.children if n.startswith(text)]

        # Bare-token shell builtins go through the trie.
        first = tokens[0]
        if first in BUILTIN_BARE_COMMANDS:
            return self.dispatch_trie(tokens, text, ctx)

        # Everything else is argparse-driven: with or without the literal
        # "contree" prefix.
        argparse_tokens = tokens[1:] if first == "contree" else tokens
        return self.complete_argparse(argparse_tokens, text, ctx)

    # -- trie path --------------------------------------------------------

    def build_router(self) -> None:
        r = self.router

        for name in ("exit", "quit", "pwd", "clear"):
            r[(name,)] = handler_noop

        r[("history",)] = handler_noop

        r[("help",)] = self.handler_help
        r[("cd",)] = self.handler_sandbox_dir

        for name in ("vim", "vi", "nvim", "nano"):
            r[(name,)] = self.handler_sandbox_path

        r[("ls",)] = self.handler_sandbox_path
        r[("cat",)] = self.handler_sandbox_path

        # Format flag belongs to the trie so "--format <TAB>" works as a
        # standalone shell builtin (intercepted in repl.execute).
        r[("--format",)] = self.handler_format
        r[("-f",)] = self.handler_format

        # Register every contree command name (and aliases) as router roots
        # so first-token completion still lists them. Their values are
        # resolved by the argparse path, so the handler is a noop here.
        for name in self.command_names:
            r[("contree", name)] = handler_noop

    def dispatch_trie(
        self,
        tokens: list[str],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        """Resolve a builtin via the trie."""
        node, depth = self.router.resolve(tuple(tokens))
        # Format flag value: "--format <TAB>" or "-f <TAB>".
        if tokens and tokens[-1] in ("--format", "-f"):
            return self.handler_format((), text, ctx)
        if node.value is not None:
            remaining = tuple(tokens[depth:])
            return node.value(remaining, text, ctx)
        return []

    # -- argparse path ----------------------------------------------------

    def complete_argparse(
        self,
        tokens: list[str],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        walk_result = argspec.walk(self.root_parser, tokens)
        target = argspec.next_target(walk_result, tokens, text)

        if isinstance(target, argspec.End):
            # Implicit-run fallback: bare command not recognised by argparse,
            # path-like text gets sandbox-path completion.
            if self.looks_like_path(text):
                return complete_sandbox_path(text, ctx)
            if not tokens:
                return [n + " " for n in self.router.children if n.startswith(text)]
            return []

        if isinstance(target, argspec.Subcommand):
            choices = list(target.action.choices.keys())
            return [n + " " for n in choices if n.startswith(text)]

        if isinstance(target, argspec.FlagName):
            return self.list_flag_names(target.parser, text)

        if isinstance(target, argspec.FlagValue):
            return self.complete_action_value(
                target.action,
                target.value_text,
                ctx,
                walk_result.command_path,
            )

        if isinstance(target, argspec.Positional):
            return self.complete_action_value(
                target.action,
                text,
                ctx,
                walk_result.command_path,
            )

        return []

    def list_flag_names(
        self,
        parser: argparse.ArgumentParser,
        text: str,
    ) -> list[str]:
        flags: list[str] = []
        for action in parser._actions:
            for opt in action.option_strings:
                if opt.startswith(text):
                    flags.append(opt + " ")
        return flags

    def complete_action_value(
        self,
        action: argparse.Action,
        text: str,
        ctx: CompletionContext,
        command_path: tuple[str, ...],
    ) -> list[str]:
        source_name = argmap_lookup(command_path, action.dest)
        if source_name is not None:
            fn = SOURCES.get(source_name)
            if fn is not None:
                return fn(text, ctx)
        if action.choices is not None:
            return complete_choices(action.choices, text)
        return []

    # -- handlers used by trie -------------------------------------------

    def handler_help(
        self,
        remaining: tuple[str, ...],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        return complete_command_name(text, ctx)

    def handler_sandbox_dir(
        self,
        remaining: tuple[str, ...],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        return complete_sandbox_dir(text, ctx)

    def handler_sandbox_path(
        self,
        remaining: tuple[str, ...],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        return complete_sandbox_path(text, ctx)

    def handler_format(
        self,
        remaining: tuple[str, ...],
        text: str,
        ctx: CompletionContext,
    ) -> list[str]:
        from contree_cli.output import FORMATTERS

        return [n + " " for n in sorted(FORMATTERS) if n.startswith(text)]

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def looks_like_path(text: str) -> bool:
        return "/" in text or text.startswith(".") or text.startswith("~")

    def list_dir(
        self,
        image_uuid: str,
        dir_path: str,
    ) -> list[dict[str, object]] | None:
        """Method-form wrapper around :func:`sources.list_sandbox_dir`.

        Bound on the instance so callers (including tests) can override
        the sandbox listing strategy by patching one attribute.
        """
        from contree_cli.shell.sources import list_sandbox_dir

        return list_sandbox_dir(self.context(), image_uuid, dir_path)

    def context(self) -> CompletionContext:
        cache = (
            SourceCache(self.store.cache, self.profile_name())
            if self.store is not None
            else None
        )
        cwd = ""
        if self.store is not None:
            try:
                cwd_value = self.store.get_cwd()
                cwd = cwd_value if isinstance(cwd_value, str) else ""
            except Exception:
                cwd = ""
        return CompletionContext(
            client=self.client,
            store=self.store,
            cache=cache,
            profile=self.profile_name(),
            cwd=cwd,
            tokens=tuple(),
            list_dir=self.list_dir,
        )

    @staticmethod
    def profile_name() -> str:
        from contree_cli import PROFILE

        try:
            return PROFILE.get().name
        except LookupError:
            return "default"

    @property
    def cache(self) -> ImageCache:
        if self.store is None:
            raise RuntimeError("SessionStore is not set")
        return self.store.cache


# ---------------------------------------------------------------------------
# Trie handlers shared across multiple commands
# ---------------------------------------------------------------------------


def handler_noop(
    remaining: tuple[str, ...],
    text: str,
    ctx: CompletionContext,
) -> list[str]:
    return []


__all__ = [
    "Handler",
    "PrefixRouter",
    "ShellCompleter",
]
