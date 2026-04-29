from __future__ import annotations

import pytest

from contree_cli.shell.parser import (
    ShellArgumentParser,
    ShellParseError,
    build_shell_parser,
    get_command_names,
)


class TestShellArgumentParser:
    def test_error_raises_shell_parse_error(self):
        parser = ShellArgumentParser()
        with pytest.raises(ShellParseError) as exc_info:
            parser.error("bad input")
        assert exc_info.value.message == "bad input"
        assert exc_info.value.status == 2

    def test_exit_raises_shell_parse_error(self):
        parser = ShellArgumentParser()
        with pytest.raises(ShellParseError) as exc_info:
            parser.exit(0, "help shown")
        assert exc_info.value.status == 0
        assert exc_info.value.message == "help shown"

    def test_exit_no_message(self):
        parser = ShellArgumentParser()
        with pytest.raises(ShellParseError) as exc_info:
            parser.exit(1)
        assert exc_info.value.message == ""

    def test_does_not_call_sys_exit(self):
        """Verify argparse bad input does not SystemExit."""
        parser = ShellArgumentParser()
        parser.add_argument("required_arg")
        with pytest.raises(ShellParseError):
            parser.parse_args([])

    def test_help_does_not_sys_exit(self):
        parser = ShellArgumentParser()
        with pytest.raises(ShellParseError) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.status == 0


class TestBuildShellParser:
    def test_returns_parser_and_commands(self):
        parser, commands = build_shell_parser()
        assert isinstance(parser, ShellArgumentParser)
        assert isinstance(commands, dict)
        assert len(commands) > 0

    def test_all_commands_registered(self):
        _, commands = build_shell_parser()
        expected = {
            "use",
            "run",
            "images",
            "tag",
            "ps",
            "kill",
            "show",
            "ls",
            "cat",
            "cp",
            "file",
            "session",
            "auth",
            "cd",
        }
        assert expected.issubset(set(commands.keys()))

    def test_aliases_registered(self):
        _, commands = build_shell_parser()
        assert "ci" in commands  # alias for use
        assert "img" in commands  # alias for images
        assert "s" in commands  # alias for session

    def test_alias_points_to_same_info(self):
        _, commands = build_shell_parser()
        assert commands["ci"] is commands["use"]
        assert commands["img"] is commands["images"]

    def test_parse_simple_command(self):
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["ls", "/etc"])
        assert ns.command == "ls"
        assert ns.path == "/etc"

    def test_parse_unknown_command_raises(self):
        parser, _ = build_shell_parser()
        with pytest.raises(ShellParseError):
            parser.parse_args(["nonexistent"])

    def test_subparser_inherits_shell_behavior(self):
        """Subcommand parsers should also raise ShellParseError, not SystemExit."""
        parser, _ = build_shell_parser()
        # 'tag' requires image_uuid and tag_name arguments
        with pytest.raises(ShellParseError):
            parser.parse_args(["tag"])

    def test_parse_ls_without_path(self):
        """ls without path should succeed (path defaults to None)."""
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["ls"])
        assert ns.command == "ls"
        assert ns.path is None

    def test_parse_run_with_remainder(self):
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["run", "--", "echo", "hello"])
        assert ns.command == "run"
        assert ns.command_args == ["--", "echo", "hello"]


class TestFormatFlag:
    """``-f``/``--format`` on the root shell parser."""

    def test_format_flag_with_command(self):
        """-f json ls parses correctly."""
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["-f", "json", "ls", "/etc"])
        assert ns.output_format == "json"
        assert ns.command == "ls"
        assert ns.path == "/etc"

    def test_format_long_flag_with_command(self):
        """--format table ls parses correctly."""
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["--format", "table", "ls"])
        assert ns.output_format == "table"
        assert ns.command == "ls"

    def test_no_format_flag_defaults_to_none(self):
        """Without -f, output_format is None."""
        parser, _ = build_shell_parser()
        ns = parser.parse_args(["ls"])
        assert ns.output_format is None

    def test_invalid_format_raises(self):
        """Invalid format name raises ShellParseError."""
        parser, _ = build_shell_parser()
        with pytest.raises(ShellParseError):
            parser.parse_args(["-f", "nonexistent", "ls"])


class TestGetCommandNames:
    def test_returns_names_and_aliases(self):
        names = get_command_names()
        assert "run" in names
        assert "ls" in names
        assert "ci" in names  # alias
        assert "img" in names  # alias

    def test_no_duplicates_in_base_names(self):
        names = get_command_names()
        base_names = [n for n in names if n not in ("ci", "img", "s")]
        assert len(base_names) == len(set(base_names))
