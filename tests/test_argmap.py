"""Smoke tests for the shell completion (command_path, dest) registry.

Guards against three regressions:

1. Every source name referenced in :mod:`contree_cli.shell.argmap` must
   exist in :mod:`contree_cli.shell.sources`.``SOURCES``.
2. Every registered ``(command_path, dest)`` must resolve to a real
   argparse Action on the live parser tree -- if a command renames a
   dest or moves a subcommand, this test catches it.
3. Alias dispatch (e.g. ``op show`` for ``operation show``) reaches the
   same registry entry, so completion works regardless of which alias
   the user typed.
"""

from __future__ import annotations

import argparse

import pytest

from contree_cli.arguments import parser as root_parser
from contree_cli.shell.argmap import ARG_SOURCES, lookup
from contree_cli.shell.argspec import find_subparsers, walk
from contree_cli.shell.sources import SOURCES


def find_action(
    parser: argparse.ArgumentParser,
    command_path: tuple[str, ...],
    dest: str,
) -> argparse.Action | None:
    """Walk subparsers by canonical name and locate an action by dest."""
    current = parser
    for name in command_path:
        sub = find_subparsers(current)
        if sub is None:
            return None
        if name not in sub.choices:
            return None
        chosen = sub.choices[name]
        assert isinstance(chosen, argparse.ArgumentParser)
        current = chosen
    for action in current._actions:
        if action.dest == dest:
            return action
    return None


class TestRegistryIntegrity:
    def test_every_source_name_is_registered(self):
        unknown = {
            (command_path, dest, source)
            for (command_path, dest), source in ARG_SOURCES.items()
            if source not in SOURCES
        }
        assert not unknown, f"argmap references unregistered sources: {unknown}"

    @pytest.mark.parametrize("key", list(ARG_SOURCES.keys()))
    def test_every_key_resolves_to_a_live_action(self, key):
        command_path, dest = key
        action = find_action(root_parser, command_path, dest)
        assert action is not None, (
            f"argmap entry {key!r} points at a non-existent argparse action; "
            "did a command rename its dest or move its subcommand?"
        )

    def test_lookup_returns_none_for_unknown_key(self):
        assert lookup(("nonexistent",), "foo") is None
        assert lookup((), "no-such-flag") is None


class TestAliasDispatch:
    """``op show`` and ``operation show`` resolve to the same registry entry."""

    def test_op_alias_walks_to_canonical_operation(self):
        result = walk(root_parser, ["op", "show", "uuid-1"])
        assert result.command_path == ("operation", "show")

    def test_list_alias_walks_to_canonical(self):
        result = walk(root_parser, ["operation", "ls"])
        assert result.command_path == ("operation", "list")

    def test_session_aliases_normalised(self):
        # `co` is the alias for `checkout`
        result = walk(root_parser, ["session", "co", "main"])
        assert result.command_path == ("session", "checkout")

    def test_op_show_resolves_operation_source(self):
        # Walking via the alias plus argmap lookup must find the "operation" source.
        result = walk(root_parser, ["op", "show"])
        assert lookup(result.command_path, "uuids") == "operation"

    def test_kill_uuids_resolves_operation_source(self):
        result = walk(root_parser, ["kill"])
        assert lookup(result.command_path, "uuids") == "operation"

    def test_run_use_flag_resolves_image_source(self):
        # `--use` is a flag, walk just records it; positional/flag-value
        # resolution still hits the same `(("run",), "use")` entry.
        walk(root_parser, ["run", "--use"])
        assert lookup(("run",), "use") == "image"


class TestRepresentativeMappings:
    """Spot-check a handful of registry entries end-to-end."""

    @pytest.mark.parametrize(
        "command_path,dest,expected",
        [
            (("show",), "uuid", "operation"),
            (("use",), "image", "image"),
            (("tag",), "args", "image"),
            (("ls",), "path", "sandbox-path"),
            (("cd",), "path", "sandbox-dir"),
            (("cp",), "dest", "host-path"),
            (("env",), "vars", "env-key"),
            (("run",), "command_args", "sandbox-path"),
            (("run",), "cwd", "sandbox-dir"),
            (("run",), "file", "mapped-file"),
            (("file", "edit"), "path", "sandbox-path"),
            (("file", "edit"), "editor", "editor"),
            (("file", "cp"), "src", "host-path"),
            (("session", "checkout"), "checkout_branch", "branch"),
            (("session", "branch"), "from_branch", "branch"),
            (("session", "wait"), "op_ids", "operation"),
            (("operation", "show"), "uuids", "operation"),
            (("operation", "cancel"), "uuids", "operation"),
            (("auth", "switch"), "profile_name", "profile"),
            (("skill", "install"), "specs", "skill-spec"),
            ((), "profile", "profile"),
        ],
    )
    def test_mapping(self, command_path, dest, expected):
        assert lookup(command_path, dest) == expected


class TestCliFilesHaveNoShellImports:
    """Layering: cli/ must not depend on shell/."""

    def test_no_argspec_imports_in_cli(self):
        import pathlib

        repo = pathlib.Path(__file__).resolve().parent.parent
        cli_dir = repo / "contree_cli" / "cli"
        offenders: list[str] = []
        for path in sorted(cli_dir.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "contree_cli.shell.argspec" in text:
                offenders.append(str(path.relative_to(repo)))
        assert not offenders, f"cli/ must not import from shell.argspec: {offenders}"
