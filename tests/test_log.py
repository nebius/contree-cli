import logging
import sys
from io import StringIO
from unittest.mock import patch

from contree_cli.log import ColorFormatter, setup_logging
from contree_cli.types import Colors


class TestFormatter:
    def _make_record(
        self,
        msg: str,
        level: int = logging.INFO,
    ) -> logging.LogRecord:
        return logging.LogRecord(
            name="test",
            level=level,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_plain_format(self) -> None:
        fmt = ColorFormatter(tty=False)
        with patch("contree_cli.types.IS_A_TTY", False):
            result = fmt.format(self._make_record("hello"))
        assert result == "[INFO] hello"

    def test_plain_format_warning(self) -> None:
        fmt = ColorFormatter(tty=False)
        with patch("contree_cli.types.IS_A_TTY", False):
            result = fmt.format(self._make_record("oops", logging.WARNING))
        assert result == "[WARNING] oops"

    def test_tty_info_uses_colors(self) -> None:
        fmt = ColorFormatter(tty=True)
        with patch("contree_cli.types.IS_A_TTY", True):
            result = fmt.format(self._make_record("hello"))
        level_color = Colors.GREEN.value
        msg_color = Colors.DEFAULT.value
        reset = Colors.DEFAULT.value
        expected = f"{level_color}[INFO]{reset} {msg_color}hello{reset}"
        assert result == expected

    def test_tty_warning_uses_yellow(self) -> None:
        fmt = ColorFormatter(tty=True)
        with patch("contree_cli.types.IS_A_TTY", True):
            result = fmt.format(
                self._make_record("oops", logging.WARNING),
            )
        level_color = Colors.YELLOW.value
        reset = Colors.DEFAULT.value
        expected = f"{level_color}[WARNING]{reset} {reset}oops{reset}"
        assert result == expected

    def test_tty_error_uses_red(self) -> None:
        fmt = ColorFormatter(tty=True)
        with patch("contree_cli.types.IS_A_TTY", True):
            result = fmt.format(
                self._make_record("fail", logging.ERROR),
            )
        level_color = Colors.RED.value
        reset = Colors.DEFAULT.value
        expected = f"{level_color}[ERROR]{reset} {reset}fail{reset}"
        assert result == expected

    def test_tty_debug_uses_cyan(self) -> None:
        fmt = ColorFormatter(tty=True)
        with patch("contree_cli.types.IS_A_TTY", True):
            result = fmt.format(
                self._make_record("trace", logging.DEBUG),
            )
        level_color = Colors.CYAN.value
        msg_color = Colors.GRAY.value
        reset = Colors.DEFAULT.value
        expected = f"{level_color}[DEBUG]{reset} {msg_color}trace{reset}"
        assert result == expected

    def test_tty_critical_uses_bold_red(self) -> None:
        fmt = ColorFormatter(tty=True)
        with patch("contree_cli.types.IS_A_TTY", True):
            result = fmt.format(
                self._make_record("boom", logging.CRITICAL),
            )
        level_color = Colors.BOLD_RED.value
        msg_color = Colors.RED.value
        reset = Colors.DEFAULT.value
        expected = f"{level_color}[CRITICAL]{reset} {msg_color}boom{reset}"
        assert result == expected

    def test_no_color_when_not_tty(self) -> None:
        fmt = ColorFormatter(tty=False)
        with patch("contree_cli.types.IS_A_TTY", False):
            result = fmt.format(self._make_record("hello"))
        assert "\033[" not in result


class TestSetupLogging:
    def test_attaches_handler_to_root(self) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            root.handlers.clear()
            setup_logging()
            assert len(root.handlers) == 1
            assert isinstance(root.handlers[0], logging.StreamHandler)
            assert root.handlers[0].stream is sys.stderr
        finally:
            root.handlers[:] = original_handlers

    def test_no_duplicate_handlers(self) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            root.handlers.clear()
            setup_logging()
            setup_logging()
            assert len(root.handlers) == 1
        finally:
            root.handlers[:] = original_handlers

    def test_output_goes_to_stderr(self) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        try:
            root.handlers.clear()
            buf = StringIO()
            with (
                patch.object(sys, "stderr", buf),
                patch("contree_cli.types.IS_A_TTY", False),
            ):
                setup_logging()
                logging.info("test message")
            assert "[INFO] test message" in buf.getvalue()
        finally:
            root.handlers[:] = original_handlers

    def test_sets_level(self) -> None:
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        original_level = root.level
        try:
            root.handlers.clear()
            setup_logging(level=logging.DEBUG)
            assert root.level == logging.DEBUG
        finally:
            root.handlers[:] = original_handlers
            root.setLevel(original_level)
