from __future__ import annotations

import posixpath
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

import pytest
from conftest import ContreeTestClient

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.client import ApiError
from contree_cli.output import (
    DefaultFormatter,
    JSONFormatter,
    TableFormatter,
)
from contree_cli.shell.completer import ShellCompleter
from contree_cli.shell.parser import build_shell_parser
from contree_cli.shell.repl import ContreeShell


def _make_shell() -> ContreeShell:
    parser, commands = build_shell_parser()
    completer = ShellCompleter(commands)
    return ContreeShell(parser, completer)


@contextmanager
def _mock_session(
    cwd: str = "",
    *,
    pending_files: list[object] | None = None,
) -> Generator[MagicMock]:
    """Patch SESSION_STORE with a mock that resolves paths against *cwd*.

    *pending_files* defaults to ``[]`` (no pending uploads).
    """
    _state = {"cwd": cwd}
    store = MagicMock()
    store.get_cwd.side_effect = lambda: _state["cwd"]
    store.set_cwd.side_effect = lambda c: _state.__setitem__("cwd", c)
    store.pending_files.return_value = pending_files if pending_files else []

    def _resolve(path: str) -> str:
        if not path:
            return _state["cwd"] or "/"
        if not PurePosixPath(path).is_absolute():
            base = _state["cwd"] or "/"
            path = base.rstrip("/") + "/" + path
        return posixpath.normpath(path)

    store.resolve_path.side_effect = _resolve
    mock_cv = MagicMock()
    mock_cv.get.return_value = store
    with patch("contree_cli.shell.repl.SESSION_STORE", mock_cv):
        yield store


class TestExecute:
    def test_empty_line_is_noop(self):
        shell = _make_shell()
        # Should not raise
        shell.execute("")

    def test_exit_raises_eof(self):
        shell = _make_shell()
        with pytest.raises(EOFError):
            shell.execute("exit")

    def test_quit_raises_eof(self):
        shell = _make_shell()
        with pytest.raises(EOFError):
            shell.execute("quit")

    def test_contree_unknown_command_prints_error(self, capsys):
        shell = _make_shell()
        shell.execute("contree nonexistent_cmd")
        err = capsys.readouterr().err
        assert "error" in err.lower()

    def test_bad_quotes_prints_error(self, capsys):
        shell = _make_shell()
        shell.execute("ls 'unclosed")
        err = capsys.readouterr().err
        assert "parse error" in err.lower() or "no closing" in err.lower()

    def test_contree_dispatches_to_handler(self, session_store):
        """Verify that 'contree <cmd>' dispatches to the correct handler."""
        shell = _make_shell()
        client = ContreeTestClient()
        formatter = DefaultFormatter()

        CLIENT.set(client)
        FORMATTER.set(formatter)
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="test")
        SESSION_STORE.set(session_store)
        ctx = copy_context()

        called = {}

        def fake_handler(args):
            called["args"] = args

        # Patch the parser to use our fake handler
        with patch.object(
            shell._parser,
            "parse_args",
        ) as mock_parse:
            ns = MagicMock()
            ns.handler = fake_handler
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "parsed_args"
            ns.output_format = None
            mock_parse.return_value = ns

            ctx.run(shell.execute, "contree ls /etc")

        assert called["args"] == "parsed_args"

    def test_contree_api_error_caught(self, capsys, session_store):
        """API errors in contree commands should be printed but not crash."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock(side_effect=ApiError(404, "Not Found", "gone"))
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = None
            mock_parse.return_value = ns
            shell.execute("contree something")

        err = capsys.readouterr().err
        assert "API error" in err

    def test_contree_keyboard_interrupt_caught(self, capsys):
        """Ctrl-C during contree command should not crash the REPL."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock(side_effect=KeyboardInterrupt)
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = None
            mock_parse.return_value = ns
            shell.execute("contree something")
        # Should not raise

    def test_contree_system_exit_caught(self, capsys):
        """SystemExit from contree handlers should not crash the REPL."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock(side_effect=SystemExit(1))
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = None
            mock_parse.return_value = ns
            shell.execute("contree something")
        # Should not raise


class TestContreeAliases:
    """``ls`` and ``cat`` are forwarded as contree management commands."""

    def test_ls_dispatches_as_contree(self):
        """Bare 'ls /etc' should dispatch via _dispatch_contree."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.execute("ls /etc")
        mock.assert_called_once_with(["ls", "/etc"])

    def test_cat_dispatches_as_contree(self):
        """Bare 'cat /etc/hosts' should dispatch via _dispatch_contree."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.execute("cat /etc/hosts")
        mock.assert_called_once_with(["cat", "/etc/hosts"])

    def test_ls_does_not_go_to_run(self):
        """ls should NOT be dispatched as implicit run."""
        shell = _make_shell()
        with (
            patch.object(shell, "dispatch_contree"),
            patch.object(shell, "dispatch_run") as mock_run,
        ):
            shell.execute("ls /tmp")
        mock_run.assert_not_called()

    # -- Path resolution --------------------------------------------------

    def test_cat_relative_path_resolved(self):
        """'cat .bashrc' after 'cd /root' should resolve to /root/.bashrc."""
        shell = _make_shell()
        with _mock_session("/root"), patch.object(shell, "dispatch_contree") as mock:
            shell.execute("cat .bashrc")
        mock.assert_called_once_with(["cat", "/root/.bashrc"])

    def test_ls_relative_path_resolved(self):
        """'ls src' after 'cd /app' should resolve to /app/src."""
        shell = _make_shell()
        with _mock_session("/app"), patch.object(shell, "dispatch_contree") as mock:
            shell.execute("ls src")
        mock.assert_called_once_with(["ls", "/app/src"])

    def test_ls_no_args_no_cwd_stays_bare(self):
        """Bare 'ls' with no cwd should dispatch as ['ls']."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.execute("ls")
        mock.assert_called_once_with(["ls"])

    # -- Fallback to run ---------------------------------------------------

    def test_cat_no_args_falls_back_to_run(self):
        """Bare 'cat' with no args should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("cat")
        mock_run.assert_called_once_with(["cat"])

    def test_cat_with_flags_falls_back_to_run(self):
        """'cat -n /etc/hosts' should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("cat -n /etc/hosts")
        mock_run.assert_called_once_with(["cat", "-n", "/etc/hosts"])

    def test_cat_with_glob_falls_back_to_run(self):
        """'cat *.py' should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("cat *.py")
        mock_run.assert_called_once_with(["cat", "*.py"])

    def test_cat_multiple_args_falls_back_to_run(self):
        """'cat a b' should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("cat a b")
        mock_run.assert_called_once_with(["cat", "a", "b"])

    def test_ls_with_flags_falls_back_to_run(self):
        """'ls -la' should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("ls -la")
        mock_run.assert_called_once_with(["ls", "-la"])

    def test_ls_multiple_args_falls_back_to_run(self):
        """'ls /etc /tmp' should fall back to implicit run."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("ls /etc /tmp")
        mock_run.assert_called_once_with(["ls", "/etc", "/tmp"])

    # -- Pending files force run -----------------------------------------------

    def test_cat_with_pending_files_falls_back_to_run(self):
        """'cat /etc/hosts' with pending files should use run."""
        shell = _make_shell()
        pending = [MagicMock()]  # one pending file
        with (
            _mock_session(pending_files=pending),
            patch.object(shell, "dispatch_run") as mock_run,
        ):
            shell.execute("cat /etc/hosts")
        mock_run.assert_called_once_with(["cat", "/etc/hosts"])

    def test_ls_with_pending_files_falls_back_to_run(self):
        """'ls /etc' with pending files should use run."""
        shell = _make_shell()
        pending = [MagicMock()]
        with (
            _mock_session(pending_files=pending),
            patch.object(shell, "dispatch_run") as mock_run,
        ):
            shell.execute("ls /etc")
        mock_run.assert_called_once_with(["ls", "/etc"])

    def test_cat_without_pending_files_dispatches_contree(self):
        """'cat /etc/hosts' without pending files uses contree dispatch."""
        shell = _make_shell()
        with (
            _mock_session(),
            patch.object(shell, "dispatch_contree") as mock,
        ):
            shell.execute("cat /etc/hosts")
        mock.assert_called_once_with(["cat", "/etc/hosts"])


class TestEditorAliases:
    """``vim``, ``vi``, ``nano`` open ``contree file edit`` with EDITOR."""

    def test_vim_dispatches_edit(self):
        shell = _make_shell()
        with patch.object(shell, "dispatch_edit") as mock:
            shell.execute("vim /etc/hosts")
        mock.assert_called_once_with("vim", ["/etc/hosts"])

    def test_nano_dispatches_edit(self):
        shell = _make_shell()
        with patch.object(shell, "dispatch_edit") as mock:
            shell.execute("nano /app/config.ini")
        mock.assert_called_once_with("nano", ["/app/config.ini"])

    def test_vi_dispatches_edit(self):
        shell = _make_shell()
        with patch.object(shell, "dispatch_edit") as mock:
            shell.execute("vi /tmp/script.sh")
        mock.assert_called_once_with("vi", ["/tmp/script.sh"])

    def test_dispatch_edit_passes_editor_flag(self):
        """dispatch_edit should pass --editor instead of mutating env."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.dispatch_edit("nano", ["/etc/hosts"])
        mock.assert_called_once_with(["file", "edit", "--editor", "nano", "/etc/hosts"])

    def test_vim_relative_path_resolved(self):
        """'vim .bashrc' after 'cd /root' should resolve the path."""
        shell = _make_shell()
        with _mock_session("/root"), patch.object(shell, "dispatch_contree") as mock:
            shell.dispatch_edit("vim", [".bashrc"])
        mock.assert_called_once_with(
            ["file", "edit", "--editor", "vim", "/root/.bashrc"]
        )

    def test_dispatch_edit_no_path_prints_usage(self, capsys):
        """Editor without a path should print usage."""
        shell = _make_shell()
        shell.dispatch_edit("vim", [])
        err = capsys.readouterr().err
        assert "Usage:" in err


class TestCd:
    """``cd`` changes the working directory for implicit ``run``."""

    def test_cd_absolute(self):
        shell = _make_shell()
        with _mock_session():
            shell.handle_cd(["/app"])
        assert shell.cwd == "/app"

    def test_cd_relative(self):
        shell = _make_shell()
        with _mock_session("/app"):
            shell.handle_cd(["src"])
        assert shell.cwd == "/app/src"

    def test_cd_relative_from_empty(self):
        """Relative cd when no cwd is set uses / as base."""
        shell = _make_shell()
        with _mock_session():
            shell.handle_cd(["etc"])
        assert shell.cwd == "/etc"

    def test_cd_dotdot(self):
        shell = _make_shell()
        with _mock_session("/app/src"):
            shell.handle_cd([".."])
        assert shell.cwd == "/app"

    def test_cd_normalizes(self):
        shell = _make_shell()
        with _mock_session():
            shell.handle_cd(["/app/./src/../lib"])
        assert shell.cwd == "/app/lib"

    def test_cd_resolves_via_execute(self):
        """'cd src' executed via shell dispatch resolves relative to cwd."""
        shell = _make_shell()
        with _mock_session("/app"):
            shell.execute("cd src")
            assert shell.cwd == "/app/src"

    def test_cd_persists_to_session(self):
        """_handle_cd should call store.set_cwd."""
        shell = _make_shell()
        with _mock_session() as store:
            shell.handle_cd(["/app"])
        store.set_cwd.assert_called_once_with("/app")

    def test_cd_bare_persists_empty(self):
        """Bare cd should persist empty cwd."""
        shell = _make_shell()
        with _mock_session("/app") as store:
            shell.handle_cd([])
        store.set_cwd.assert_called_once_with("")

    def test_cd_dash_persists(self):
        """cd - should persist the swapped cwd."""
        shell = _make_shell()
        with _mock_session("/tmp") as store:
            shell.handle_cd(["/app"])  # cwd=/app, prev=/tmp
            store.set_cwd.reset_mock()
            shell.handle_cd(["-"])  # swap back to /tmp
        store.set_cwd.assert_called_once_with("/tmp")

    def test_cwd_reads_from_session(self):
        """cwd property should read from session store."""
        shell = _make_shell()
        with _mock_session("/opt"):
            assert shell.cwd == "/opt"


class TestClear:
    """``clear`` writes ANSI escape sequence instead of spawning an instance."""

    def test_clear_writes_escape_sequence(self, capsys):
        shell = _make_shell()
        shell.execute("clear")
        out = capsys.readouterr().out
        assert "\033[2J" in out
        assert "\033[H" in out

    def test_clear_does_not_dispatch_run(self):
        shell = _make_shell()
        with patch.object(shell, "dispatch_run") as mock_run:
            shell.execute("clear")
        mock_run.assert_not_called()


class TestImplicitRun:
    """Tests for bare commands dispatched as implicit ``run``."""

    def test_bare_command_dispatches_run(self):
        """'echo hello' should call dispatch_run."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_run") as mock:
            shell.execute("echo hello")
        mock.assert_called_once_with(["echo", "hello"])

    def test_dispatch_run_constructs_run_args(self):
        """dispatch_run should construct RunArgs with shell=True."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with _mock_session(), patch("contree_cli.cli.run.cmd_run") as mock_cmd:
            shell.dispatch_run(["grep", "-r", "root", "/etc"])

        args = mock_cmd.call_args[0][0]
        assert args.command_args == ["grep", "-r", "root", "/etc"]
        assert args.shell is True

    def test_bare_command_api_error_caught(self, capsys):
        """API errors in implicit run should be printed but not crash."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with (
            _mock_session(),
            patch("contree_cli.cli.run.cmd_run", side_effect=ApiError(500, "err", "")),
        ):
            shell.dispatch_run(["echo", "hello"])

        err = capsys.readouterr().err
        assert "API error" in err

    def test_bare_command_keyboard_interrupt_caught(self):
        """Ctrl-C during implicit run should not crash the REPL."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with (
            _mock_session(),
            patch("contree_cli.cli.run.cmd_run", side_effect=KeyboardInterrupt),
        ):
            shell.dispatch_run(["sleep", "100"])
        # Should not raise

    def test_bare_command_system_exit_caught(self):
        """SystemExit from implicit run should not crash the REPL."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with (
            _mock_session(),
            patch("contree_cli.cli.run.cmd_run", side_effect=SystemExit(1)),
        ):
            shell.dispatch_run(["false"])
        # Should not raise

    def test_pipe_expression(self):
        """A pipe expression should be passed as-is with shell=True."""
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with _mock_session(), patch("contree_cli.cli.run.cmd_run") as mock_cmd:
            shell.dispatch_run(["ls", "/etc", "|", "head"])

        args = mock_cmd.call_args[0][0]
        assert args.command_args == ["ls", "/etc", "|", "head"]
        assert args.shell is True


class TestRun:
    def test_eof_exits_cleanly(self):
        shell = _make_shell()
        with _mock_session(), patch("builtins.input", side_effect=EOFError):
            shell.run()  # Should return without error

    def test_keyboard_interrupt_continues(self, capsys):
        """Ctrl-C on the prompt should continue the loop."""
        call_count = 0

        def mock_input(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise KeyboardInterrupt
            raise EOFError

        shell = _make_shell()
        with _mock_session(), patch("builtins.input", side_effect=mock_input):
            shell.run()
        assert call_count == 2


class TestFormatOverride:
    """Per-command ``-f``/``--format`` override and persistent switching."""

    def test_per_command_override_uses_json(self):
        """'contree -f json ls' should use JSONFormatter for that command."""
        shell = _make_shell()
        original = DefaultFormatter()
        FORMATTER.set(original)

        captured_formatter = {}

        def fake_handler(args):
            captured_formatter["type"] = type(FORMATTER.get())

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = fake_handler
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = "json"
            mock_parse.return_value = ns
            shell.dispatch_contree(["-f", "json", "ls"])

        assert captured_formatter["type"] is JSONFormatter

    def test_per_command_override_restores_original(self):
        """After -f json, the original formatter is restored."""
        shell = _make_shell()
        original = DefaultFormatter()
        FORMATTER.set(original)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock()
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = "json"
            mock_parse.return_value = ns
            shell.dispatch_contree(["-f", "json", "ls"])

        assert FORMATTER.get() is original

    def test_per_command_override_restores_on_exception(self):
        """Formatter is restored even if handler raises."""
        shell = _make_shell()
        original = DefaultFormatter()
        FORMATTER.set(original)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock(side_effect=ApiError(500, "err", ""))
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = "json"
            mock_parse.return_value = ns
            shell.dispatch_contree(["-f", "json", "ls"])

        assert FORMATTER.get() is original

    def test_no_format_flag_keeps_formatter(self):
        """Without -f, session formatter is unchanged."""
        shell = _make_shell()
        original = DefaultFormatter()
        FORMATTER.set(original)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.handler = MagicMock()
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = None
            mock_parse.return_value = ns
            shell.dispatch_contree(["ls"])

        assert FORMATTER.get() is original


class TestHelpBuiltin:
    """``help`` displays custom shell help and per-topic help."""

    def test_help_no_args_prints_shell_help(self, capsys):
        shell = _make_shell()
        shell.execute("help")
        out = capsys.readouterr().out
        assert "Builtins:" in out
        assert "Aliases:" in out
        assert "Tab completion:" in out

    def test_help_cd_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help cd")
        out = capsys.readouterr().out
        assert "Usage: cd" in out
        assert "previous" in out

    def test_help_pwd_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help pwd")
        out = capsys.readouterr().out
        assert "Usage: pwd" in out

    def test_help_history_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help history")
        out = capsys.readouterr().out
        assert "Usage: history" in out

    def test_help_clear_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help clear")
        out = capsys.readouterr().out
        assert "Usage: clear" in out

    def test_help_exit_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help exit")
        out = capsys.readouterr().out
        assert "exit" in out.lower()
        assert "quit" in out.lower()

    def test_help_quit_shows_exit_help(self, capsys):
        """'help quit' resolves to 'help exit' via alias."""
        shell = _make_shell()
        shell.execute("help quit")
        out = capsys.readouterr().out
        assert "exit" in out.lower()

    def test_help_vim_prints_alias(self, capsys):
        shell = _make_shell()
        shell.execute("help vim")
        out = capsys.readouterr().out
        assert "Usage: vim" in out
        assert "file edit" in out

    def test_help_vi_shows_vim_help(self, capsys):
        """'help vi' resolves to 'help vim' via alias."""
        shell = _make_shell()
        shell.execute("help vi")
        out = capsys.readouterr().out
        assert "Usage: vim" in out

    def test_help_nano_prints_alias(self, capsys):
        shell = _make_shell()
        shell.execute("help nano")
        out = capsys.readouterr().out
        assert "Usage: nano" in out
        assert "file edit" in out

    def test_help_nvim_prints_alias(self, capsys):
        shell = _make_shell()
        shell.execute("help nvim")
        out = capsys.readouterr().out
        assert "Usage: nvim" in out

    def test_help_command_delegates_to_contree(self):
        """'help run' should delegate to dispatch_contree."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.execute("help run")
        mock.assert_called_once_with(["run", "--help"])

    def test_help_unknown_delegates_to_contree(self):
        """'help nonexistent' should try dispatch_contree."""
        shell = _make_shell()
        with patch.object(shell, "dispatch_contree") as mock:
            shell.execute("help nonexistent")
        mock.assert_called_once_with(["nonexistent", "--help"])

    def test_help_f_shows_format_help(self, capsys):
        """'help -f' resolves to 'help --format' via alias."""
        shell = _make_shell()
        shell.execute("help -f")
        out = capsys.readouterr().out
        assert "--format" in out
        assert "format" in out.lower()

    def test_help_help_prints_builtin(self, capsys):
        shell = _make_shell()
        shell.execute("help help")
        out = capsys.readouterr().out
        assert "Usage: help" in out


class TestFormatCommand:
    """``--format`` / ``-f`` as a persistent shell builtin."""

    def test_format_prints_current(self, capsys):
        """'--format' with no args prints current format name."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("--format")
        out = capsys.readouterr().out.strip()
        assert out == "default"

    def test_format_prints_json(self, capsys):
        """'--format' after switching to json prints 'json'."""
        shell = _make_shell()
        FORMATTER.set(JSONFormatter())
        shell.execute("--format")
        out = capsys.readouterr().out.strip()
        assert out == "json"

    def test_format_switches_to_json(self):
        """'--format json' switches the session formatter."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("--format json")
        assert type(FORMATTER.get()) is JSONFormatter

    def test_format_switches_to_table(self):
        """'--format table' switches the session formatter."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("--format table")
        assert type(FORMATTER.get()) is TableFormatter

    def test_format_short_flag(self):
        """'-f json' works the same as '--format json'."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("-f json")
        assert type(FORMATTER.get()) is JSONFormatter

    def test_format_short_flag_prints_current(self, capsys):
        """'-f' with no args prints current format name."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("-f")
        out = capsys.readouterr().out.strip()
        assert out == "default"

    def test_format_unknown_prints_error(self, capsys):
        """'--format bad' prints error with available names."""
        shell = _make_shell()
        FORMATTER.set(DefaultFormatter())
        shell.execute("--format bad")
        err = capsys.readouterr().err
        assert "Unknown format" in err
        assert "json" in err

    def test_help_format_prints_usage(self, capsys):
        """'help --format' prints format help text."""
        shell = _make_shell()
        shell.execute("help --format")
        out = capsys.readouterr().out
        assert "usage:" in out.lower()
        assert "--format" in out


class TestPwdBuiltin:
    def test_pwd_prints_cwd(self, capsys):
        shell = _make_shell()
        with _mock_session("/app"):
            shell.execute("pwd")
        out = capsys.readouterr().out.strip()
        assert out == "/app"

    def test_pwd_prints_root_when_no_cwd(self, capsys):
        shell = _make_shell()
        with _mock_session():
            shell.execute("pwd")
        out = capsys.readouterr().out.strip()
        assert out == "/"


class TestHistoryBuiltin:
    def test_history_no_lines(self, capsys):
        shell = _make_shell()
        with _mock_session() as store:
            store.load_shell_history.return_value = []
            shell.execute("history")
        out = capsys.readouterr().out
        assert "(no history)" in out

    def test_history_with_lines(self, capsys):
        shell = _make_shell()
        with _mock_session() as store:
            store.load_shell_history.return_value = ["ls /etc", "cat /etc/hosts"]
            shell.execute("history")
        out = capsys.readouterr().out
        assert "ls /etc" in out
        assert "cat /etc/hosts" in out

    def test_history_with_count_limit(self, capsys):
        shell = _make_shell()
        with _mock_session() as store:
            store.load_shell_history.return_value = [
                "cmd-1",
                "cmd-2",
                "cmd-3",
                "cmd-4",
                "cmd-5",
            ]
            shell.execute("history 2")
        out = capsys.readouterr().out
        assert "cmd-4" in out
        assert "cmd-5" in out
        assert "cmd-1" not in out


class TestShellPrevention:
    def test_shell_inside_shell_prints_error(self, capsys):
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.command = "shell"
            ns.handler = MagicMock()
            ns.load_args = MagicMock()
            ns.output_format = None
            mock_parse.return_value = ns
            shell.dispatch_contree(["shell"])

        err = capsys.readouterr().err
        assert "cannot be run from within the shell" in err


class TestFormatName:
    def test_known_formatter(self):
        from contree_cli.output import JSONFormatter

        shell = _make_shell()
        assert shell.format_name(JSONFormatter()) == "json"

    def test_default_formatter(self):
        shell = _make_shell()
        assert shell.format_name(DefaultFormatter()) == "default"

    def test_unknown_formatter(self):
        shell = _make_shell()

        class CustomFormatter:
            pass

        result = shell.format_name(CustomFormatter())  # type: ignore[arg-type]
        assert result == "CustomFormatter"


class TestDispatchRunExceptionLogging:
    def test_general_exception_logged(self, capsys):
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with (
            _mock_session(),
            patch(
                "contree_cli.cli.run.cmd_run", side_effect=RuntimeError("unexpected")
            ),
        ):
            shell.dispatch_run(["echo", "hello"])
        # Should not raise -- error is logged

    def test_dispatch_contree_general_exception_logged(self, capsys):
        shell = _make_shell()
        formatter = DefaultFormatter()
        FORMATTER.set(formatter)

        with patch.object(shell._parser, "parse_args") as mock_parse:
            ns = MagicMock()
            ns.command = "ls"
            ns.handler = MagicMock(side_effect=RuntimeError("oops"))
            ns.load_args = MagicMock()
            ns.load_args.from_args.return_value = "args"
            ns.output_format = None
            mock_parse.return_value = ns
            shell.dispatch_contree(["ls"])
        # Should not raise


class TestDispatchEditDoesNotMutateEnv:
    def test_env_untouched(self):
        """dispatch_edit forwards --editor as a flag, never mutates os.environ."""
        import os

        shell = _make_shell()
        before = dict(os.environ)
        with patch.object(shell, "dispatch_contree"):
            shell.dispatch_edit("nano", ["/etc/hosts"])
        assert dict(os.environ) == before


class TestNvimAlias:
    def test_nvim_dispatches_edit(self):
        shell = _make_shell()
        with patch.object(shell, "dispatch_edit") as mock:
            shell.execute("nvim /etc/hosts")
        mock.assert_called_once_with("nvim", ["/etc/hosts"])


class TestReadContinuation:
    def test_complete_line_returns_unchanged(self):
        result = ContreeShell._read_continuation("ls /etc")
        assert result == "ls /etc"

    def test_trailing_backslash_joins_lines(self):
        with patch("builtins.input", return_value="/etc"):
            result = ContreeShell._read_continuation("ls \\")
        # backslash-newline removed, space before \ provides separation
        assert result == "ls /etc"

    def test_unclosed_quote_preserves_newline(self):
        with patch("builtins.input", return_value='world"'):
            result = ContreeShell._read_continuation('echo "hello')
        # bare newline inside quotes is preserved (no preceding \)
        assert result == 'echo "hello\nworld"'

    def test_ctrl_c_cancels_continuation(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = ContreeShell._read_continuation("ls \\")
        assert result == ""

    def test_ctrl_d_cancels_continuation(self):
        with patch("builtins.input", side_effect=EOFError):
            result = ContreeShell._read_continuation("ls \\")
        assert result == ""

    def test_multi_line_continuation(self):
        inputs = iter(["/a \\", "/b"])
        with patch("builtins.input", side_effect=inputs):
            result = ContreeShell._read_continuation("ls \\")
        assert result == "ls /a /b"

    def test_continuation_produces_correct_tokens(self):
        with patch("builtins.input", side_effect=["-alh \\", "/sys"]):
            result = ContreeShell._read_continuation("ls \\")
        import shlex

        assert shlex.split(result) == ["ls", "-alh", "/sys"]
