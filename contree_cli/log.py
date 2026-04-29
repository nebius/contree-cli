import logging
import sys
from types import MappingProxyType

from contree_cli.types import STDERR_IS_A_TTY, Colors


class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = MappingProxyType(
        {
            logging.DEBUG: Colors.CYAN,
            logging.INFO: Colors.GREEN,
            logging.WARNING: Colors.YELLOW,
            logging.ERROR: Colors.RED,
            logging.CRITICAL: Colors.BOLD_RED,
        }
    )

    TEXT_COLORS = MappingProxyType(
        {
            logging.DEBUG: Colors.GRAY,
            logging.INFO: Colors.DEFAULT,
            logging.WARNING: Colors.DEFAULT,
            logging.ERROR: Colors.DEFAULT,
            logging.CRITICAL: Colors.RED,
        }
    )

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def format(self, record: logging.LogRecord) -> str:
        level_color = self.LEVEL_COLORS.get(record.levelno, Colors.DEFAULT)
        message_color = self.TEXT_COLORS.get(record.levelno, Colors.DEFAULT)
        lvl = level_color(f"[{record.levelname}]")
        msg = message_color(record.getMessage())
        return f"{lvl} {msg}"


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(ColorFormatter(tty=STDERR_IS_A_TTY))
    logging.basicConfig(level=level, handlers=[handler], force=True)
