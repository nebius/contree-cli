"""Tab completion for the interactive shell."""

from __future__ import annotations

import argparse
import json
import logging
import shlex
from typing import TYPE_CHECKING

from contree_cli.output import FORMATTERS
from contree_cli.shell.parser import CommandInfo, ShellArgumentParser, get_command_names
from contree_cli.shell.trie import Handler, PrefixRouter

if TYPE_CHECKING:
    from contree_cli.client import ContreeClient
    from contree_cli.session import ImageCache, SessionStore

log = logging.getLogger(__name__)


class ShellCompleter:
    """Readline completer with PrefixRouter-based dispatch."""

    def __init__(
        self,
        commands: dict[str, CommandInfo],
        client: ContreeClient | None = None,
        store: SessionStore | None = None,
        root_parser: ShellArgumentParser | None = None,
    ) -> None:
        self._commands = commands
        self._command_names = get_command_names()
        self._client = client
        self._store = store
        self._root_parser = root_parser
        self._matches: list[str] = []

        self.router: PrefixRouter = PrefixRouter()
        self._build_router()

    def _build_router(self) -> None:
        r = self.router

        # Shell builtins with no argument completion
        for name in ("exit", "quit", "pwd", "history", "clear"):
            r[(name,)] = self._complete_noop

        r[("help",)] = self._complete_help_names
        r[("cd",)] = self._complete_dir_only

        # Editors → sandbox path
        for name in ("vim", "vi", "nano"):
            r[(name,)] = self._complete_sandbox_path

        # Bare aliases → sandbox path (same as contree ls/cat)
        r[("ls",)] = self._complete_sandbox_path
        r[("cat",)] = self._complete_sandbox_path

        # contree subcommand argument completers
        arg_map: dict[str, Handler] = {
            "ls": self._complete_sandbox_path,
            "cat": self._complete_sandbox_path,
            "cp": self._complete_sandbox_path,
            "cd": self._complete_dir_only,
            "use": self._complete_image,
            "tag": self._complete_image,
            "show": self._complete_operation,
            "kill": self._complete_operation,
        }

        # Register all contree commands (names + aliases)
        for name in self._command_names:
            handler = arg_map.get(name, self._complete_noop)
            r[("contree", name)] = handler

        # Register subparser children so subcommand name completion works.
        # E.g. "contree file <TAB>" should show "edit", "cp", etc.
        for name, info in self._commands.items():
            for sub_name in self._get_subcommand_names(info.parser):
                key = ("contree", name, sub_name)
                if key not in r:
                    r[key] = self._complete_noop

        # --format / -f — complete format names
        r[("--format",)] = self._complete_format_name
        r[("-f",)] = self._complete_format_name

        # Nested subcommands with specific completers
        r[("contree", "session", "use")] = self._complete_session_name
        r[("contree", "session", "checkout")] = self._complete_branch
        r[("contree", "session", "co")] = self._complete_branch
        r[("contree", "session", "branch")] = self._complete_branch
        r[("contree", "session", "br")] = self._complete_branch
        r[("contree", "file", "edit")] = self._complete_sandbox_path
        r[("contree", "file", "e")] = self._complete_sandbox_path

    def complete(self, text: str, state: int) -> str | None:
        """Readline completer function (called repeatedly with state=0,1,...)."""
        if state == 0:
            try:
                import readline

                line = readline.get_line_buffer()
                begidx = readline.get_begidx()
            except (ImportError, AttributeError):
                line = text
                begidx = 0
            self._matches = self.compute_completions(text, line, begidx)
        if state < len(self._matches):
            return self._matches[state]
        return None

    def compute_completions(
        self,
        text: str,
        line: str,
        begidx: int,
    ) -> list[str]:
        """Determine context and return matching completions."""
        before_cursor = line[:begidx]
        try:
            tokens = shlex.split(before_cursor)
        except ValueError:
            return []

        # No tokens yet → root child names
        if not tokens:
            return [n + " " for n in self.router.children if n.startswith(text)]

        node, depth = self.router.resolve(tuple(tokens))
        remaining = tuple(tokens[depth:])

        # Flag-value completion: -f/--format <TAB> → format names
        if tokens and tokens[-1] in ("-f", "--format"):
            return self._complete_format_name((), text)

        # Flags: look up the parser from commands dict
        if text.startswith("-"):
            parser = self._find_parser(tokens)
            if parser is not None:
                return self._complete_flags(parser, text)

        # Children → subcommand name completion
        if node.children:
            matches = [n + " " for n in node.children if n.startswith(text)]
            if matches:
                return matches

        # Handler with remaining tokens
        if node.value is not None:
            return node.value(remaining, text)

        # Fallback: implicit run mode — path-like text gets path completion,
        # anything else gets root command names as suggestions.
        if self._looks_like_path(text):
            return self._complete_sandbox_path((), text)

        return [n + " " for n in self.router.children if n.startswith(text)]

    # ------------------------------------------------------------------
    # Parser lookup for flag completion
    # ------------------------------------------------------------------

    def _find_parser(
        self,
        tokens: list[str],
    ) -> argparse.ArgumentParser | None:
        """Find the argparse parser for the current command context."""
        # Strip "contree" prefix if present
        cmd_tokens = tokens[1:] if tokens and tokens[0] == "contree" else tokens
        if not cmd_tokens:
            return None
        cmd_name = cmd_tokens[0]
        if cmd_name not in self._commands:
            return None
        return self._commands[cmd_name].parser

    # ------------------------------------------------------------------
    # Completion handlers  (remaining, text) -> list[str]
    # ------------------------------------------------------------------

    def _complete_noop(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        return []

    def _complete_help_names(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """``help <name>`` — all known command / alias / shell names."""
        all_names = sorted({*self._command_names, *self.router.children})
        return [n + " " for n in all_names if n.startswith(text)]

    def _complete_format_name(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete output format names (``--format json``, ``-f table``)."""
        return [n + " " for n in sorted(FORMATTERS) if n.startswith(text)]

    def _complete_dir_only(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete sandbox directory paths (no files)."""
        return self._complete_sandbox_path_inner(text, dirs_only=True)

    def _complete_sandbox_path(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete a sandbox file/directory path."""
        return self._complete_sandbox_path_inner(text)

    @staticmethod
    def _looks_like_path(text: str) -> bool:
        """Return True when *text* looks like a filesystem path."""
        return "/" in text or text.startswith(".") or text.startswith("~")

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_subcommand_names(
        parser: argparse.ArgumentParser,
    ) -> list[str]:
        """Extract subcommand names from a parser (if it has subparsers)."""
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return list(action.choices.keys())
        return []

    def _complete_flags(
        self,
        parser: argparse.ArgumentParser,
        text: str,
    ) -> list[str]:
        """Complete flag names from parser actions."""
        flags: list[str] = []
        for action in parser._actions:
            for opt in action.option_strings:
                if opt.startswith(text):
                    flags.append(opt + " ")
        return flags

    def _complete_sandbox_path_inner(
        self,
        text: str,
        *,
        dirs_only: bool = False,
    ) -> list[str]:
        """Complete a sandbox file path via the inspect API."""
        if self._client is None or self._store is None:
            return []

        try:
            session = self._store.session
            if session is None:
                return []
            image_uuid = session.current_image
        except (SystemExit, Exception):
            return []

        # Split into directory + prefix for the API query.
        # resolve_path handles cwd joining and .. normalisation.
        if "/" in text:
            last_slash = text.rindex("/")
            user_dir = text[: last_slash + 1] or "/"
            prefix = text[last_slash + 1 :]
            resolved = self._store.resolve_path(user_dir)
            api_dir = resolved if resolved == "/" else resolved + "/"
        else:
            user_dir = ""
            prefix = text
            resolved = self._store.resolve_path("")
            api_dir = resolved if resolved == "/" else resolved + "/"

        entries = self._list_dir(image_uuid, api_dir)
        if entries is None:
            return []

        results: list[str] = []
        for entry in entries:
            path = entry.get("path", "")
            if not isinstance(path, str) or not path:
                continue
            is_dir = bool(entry.get("is_dir"))
            if dirs_only and not is_dir:
                continue
            # API returns full paths like "/etc/hosts" — extract basename
            name = path.rsplit("/", 1)[-1]
            if not name.startswith(prefix):
                continue
            # Return the path as the user typed it (relative or absolute)
            full = user_dir + name
            if is_dir:
                full += "/"
            else:
                full += " "
            results.append(full)
        return results

    def _complete_image(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete image references (``tag:NAME`` or UUID).

        When the user types a ``tag:`` prefix, matches are filtered
        against the full ``tag:NAME`` candidate.  Otherwise bare text
        is matched against tag names directly and the completion
        inserts the ``tag:`` prefix for the user.
        """
        images = self._list_images()
        if images is None:
            return []

        results: list[str] = []
        for img in images:
            tag = img.get("tag")
            if isinstance(tag, str) and tag:
                prefixed = f"tag:{tag}"
                if text.startswith("tag:"):
                    if prefixed.startswith(text):
                        results.append(prefixed + " ")
                elif tag.startswith(text):
                    results.append(prefixed + " ")
            uuid_str = img.get("uuid")
            if isinstance(uuid_str, str) and uuid_str.startswith(text):
                results.append(uuid_str + " ")
        return results

    def _complete_operation(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete operation UUIDs for ``show`` and ``kill``."""
        ops = self._list_operations()
        if ops is None:
            return []
        results: list[str] = []
        for op in ops:
            uuid_str = op.get("uuid")
            if isinstance(uuid_str, str) and uuid_str.startswith(text):
                results.append(uuid_str + " ")
        return results

    def _list_operations(self) -> list[dict[str, object]] | None:
        """Fetch recent operations from the API (no caching)."""
        if self._client is None:
            return None
        try:
            resp = self.client.get(
                "/v1/operations",
                params={"limit": "100"},
            )
            data = json.loads(resp.read())
            return data.get("operations", [])  # type: ignore[no-any-return]
        except Exception:
            log.debug("Operation completion failed")
            return None

    def _complete_session_name(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete session names for ``session use``."""
        if self._store is None:
            return []
        try:
            sessions = self._store.list_sessions()
        except Exception:
            return []
        results: list[str] = []
        for s in sessions:
            key = s.session_key
            # Match full key
            if key.startswith(text):
                results.append(key + " ")
            # Match suffix (last component after _)
            suffix = key.rsplit("_", 1)[-1] if "_" in key else ""
            if suffix and suffix != key and suffix.startswith(text):
                results.append(suffix + " ")
        return results

    def _complete_branch(
        self,
        remaining: tuple[str, ...],
        text: str,
    ) -> list[str]:
        """Complete branch names for ``session checkout/branch``."""
        if self._store is None:
            return []
        try:
            branches = self._store.list_branches()
        except Exception:
            return []
        return [name + " " for name, _active in branches if name.startswith(text)]

    @property
    def client(self) -> ContreeClient:
        if self._client is None:
            raise RuntimeError("ContreeClient is not set")
        return self._client

    @property
    def cache(self) -> ImageCache:
        if self._store is None:
            raise RuntimeError("SessionStore is not set")
        return self._store.cache

    def cached(
        self,
        key: tuple[str, str],
    ) -> list[dict[str, object]] | None:
        """Return a cached value or ``None``."""
        result = self.cache.get(key)
        return result  # type: ignore[return-value]

    def _list_images(self) -> list[dict[str, object]] | None:
        """Fetch image list from the API, with persistent caching."""
        if self._client is None or self._store is None:
            return None

        cache_key = ("", "images")
        cached = self.cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self.client.get(
                "/v1/images",
                params={"limit": "100"},
            )
            data = json.loads(resp.read())
            images: list[dict[str, object]] = data.get("images", [])
            self.cache[cache_key] = images
            return images
        except Exception:
            log.debug("Image completion failed")
            return None

    def _list_dir(
        self,
        image_uuid: str,
        dir_path: str,
    ) -> list[dict[str, object]] | None:
        """List a sandbox directory, with persistent caching."""
        cache_key = (image_uuid, f"files:{dir_path}")

        cached = self.cached(cache_key)
        if cached is not None:
            return cached

        try:
            from contree_cli.client import resolve_image

            uuid = resolve_image(self.client, image_uuid)
            resp = self.client.get(
                f"/v1/inspect/{uuid}/list",
                params={"path": dir_path},
            )
            data = json.loads(resp.read())
            file_list: list[dict[str, object]] = data.get("files", [])
            self.cache[cache_key] = file_list
            return file_list
        except Exception:
            log.debug(
                "Path completion failed for %s:%s",
                image_uuid,
                dir_path,
            )
            return None
