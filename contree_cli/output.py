from __future__ import annotations

import csv
import functools
import io
import json
import logging
import shutil
import sys
from datetime import datetime, timedelta
from types import MappingProxyType

from contree_cli.types import STDOUT_IS_A_TTY, Colors

log = logging.getLogger(__name__)


@functools.singledispatch
def _format_value(value: object) -> str:
    """Human-friendly string for a value."""
    return str(value)


@_format_value.register
def _(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


@_format_value.register
def _(value: timedelta) -> str:
    total = int(value.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60}s"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h{minutes}m"


@_format_value.register
def _(value: float) -> str:
    return f"{value:.2f}"


@_format_value.register
def _(value: bool) -> str:
    return str(value).lower()


@_format_value.register(type(None))
def _(value: None) -> str:
    return ""


@functools.singledispatch
def _json_default(value: object) -> object:
    """JSON serialiser fallback for non-standard types."""
    raise TypeError(type(value))


@_json_default.register
def _(value: datetime) -> object:
    return value.isoformat()


@_json_default.register
def _(value: timedelta) -> object:
    return value.total_seconds()


def _truncate(text: str, width: int, ellipsis: str = "\u2026") -> str:
    """Truncate *text* to *width* characters, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    elen = len(ellipsis)
    if width > elen:
        return text[: width - elen] + ellipsis
    return text[:width]


def _fit_columns(
    widths: dict[str, int],
    columns: list[str],
    available: int,
    min_col_width: int = 3,
) -> tuple[dict[str, int], bool]:
    """Shrink column widths to fit within *available* characters.

    Allocates space fairly: narrow columns keep their natural width,
    wide columns share the remaining space equally.
    Returns (adjusted_widths, was_truncated).
    """
    result = dict(widths)
    sorted_cols = sorted(columns, key=lambda c: result[c])
    remaining = available
    truncated = False
    for idx, col in enumerate(sorted_cols):
        cols_left = len(sorted_cols) - idx
        fair_share = remaining // cols_left if cols_left else remaining
        allocated = min(result[col], max(fair_share, min_col_width))
        if allocated < result[col]:
            truncated = True
        result[col] = allocated
        remaining -= allocated
    return result, truncated


class OutputFormatter:
    """Base formatter - subclasses decide the serialisation style."""

    # Not suitable for streaming stdout/stderr output (e.g. from `run`)
    STREAM = False

    def __call__(self, **kwargs: object) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        """Flush any buffered output. No-op for streaming formatters."""


class CSVFormatter(OutputFormatter):
    def __init__(self) -> None:
        self._header_written = False

    def __call__(self, **kwargs: object) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf)
        if not self._header_written:
            writer.writerow(kwargs.keys())
            self._header_written = True
        writer.writerow(_format_value(v) for v in kwargs.values())
        sys.stdout.write(buf.getvalue())


class TSVFormatter(OutputFormatter):
    def __init__(self) -> None:
        self._header_written = False

    def __call__(self, **kwargs: object) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf, dialect="excel-tab")
        if not self._header_written:
            writer.writerow(kwargs.keys())
            self._header_written = True
        writer.writerow(_format_value(v) for v in kwargs.values())
        sys.stdout.write(buf.getvalue())


class JSONFormatter(OutputFormatter):
    STREAM = True

    def __call__(self, **kwargs: object) -> None:
        sys.stdout.write(json.dumps(kwargs, default=_json_default) + "\n")


class JSONPrettyFormatter(OutputFormatter):
    STREAM = True

    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> None:
        self._rows.append(kwargs)

    def flush(self) -> None:
        if not self._rows:
            return
        sys.stdout.write(
            json.dumps(self._rows, indent=2, default=_json_default) + "\n",
        )
        self._rows.clear()


class TableFormatter(OutputFormatter):
    ELLIPSIS = "\u2026" if STDOUT_IS_A_TTY else "..."
    MIN_COL_WIDTH = 3
    COLUMN_PALETTE: tuple[Colors, ...] = (
        Colors.CYAN,
        Colors.GREEN,
        Colors.YELLOW,
        Colors.BLUE,
        Colors.MAGENTA,
        Colors.DEFAULT,
    )
    VALUE_COLORS = MappingProxyType(
        {
            "SUCCESS": Colors.GREEN,
            "FAILED": Colors.RED,
            "CANCELLED": Colors.YELLOW,
            "PENDING": Colors.GRAY,
            "ASSIGNED": Colors.CYAN,
            "EXECUTING": Colors.BLUE,
            "true": Colors.GREEN,
            "false": Colors.RED,
            "": Colors.GRAY,
        }
    )

    def __init__(self) -> None:
        self._rows: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> None:
        self._rows.append(kwargs)

    def flush(self) -> None:
        if not self._rows:
            return
        columns = list(self._rows[0].keys())
        widths = {col: len(col) for col in columns}
        # Split each cell into lines and compute natural column widths.
        split_rows: list[dict[str, list[str]]] = []
        for row in self._rows:
            split_row: dict[str, list[str]] = {}
            for col in columns:
                lines = _format_value(row.get(col, "")).split("\n")
                split_row[col] = lines
                for line in lines:
                    widths[col] = max(widths[col], len(line))
            split_rows.append(split_row)
        # Constrain to terminal width when outputting to a TTY.
        truncated = False
        if STDOUT_IS_A_TTY:
            term_width = shutil.get_terminal_size().columns
            separator_space = (len(columns) - 1) * 2
            available = term_width - separator_space
            if sum(widths.values()) > available > 0:
                widths, truncated = _fit_columns(
                    widths,
                    columns,
                    available,
                    self.MIN_COL_WIDTH,
                )
        # Assign a color per column (cycling through the palette).
        palette = self.COLUMN_PALETTE
        col_colors: dict[str, Colors] = {}
        if STDOUT_IS_A_TTY:
            for idx, col in enumerate(columns):
                col_colors[col] = palette[idx % len(palette)]
        # Render header (bold when TTY).
        header_parts: list[str] = []
        for col in columns:
            padded = _truncate(
                col.upper(),
                widths[col],
                self.ELLIPSIS,
            ).ljust(widths[col])
            if STDOUT_IS_A_TTY:
                padded = Colors.BOLD(padded)
            header_parts.append(padded)
        sys.stdout.write("  ".join(header_parts) + "\n")
        # Render rows.
        for split_row in split_rows:
            height = max(len(split_row[col]) for col in columns)
            for i in range(height):
                parts: list[str] = []
                for col in columns:
                    lines = split_row[col]
                    cell = lines[i] if i < len(lines) else ""
                    padded = _truncate(
                        cell,
                        widths[col],
                        self.ELLIPSIS,
                    ).ljust(widths[col])
                    if col in col_colors:
                        color = self.VALUE_COLORS.get(
                            cell.strip(),
                            col_colors[col],
                        )
                        padded = color(padded)
                    parts.append(padded)
                sys.stdout.write("  ".join(parts) + "\n")
        if truncated:
            log.warning(
                "Output truncated to fit terminal;"
                " use --format json to see full values",
            )
        self._rows.clear()


class DefaultFormatter(TableFormatter):
    """Default formatter - commands may detect and replace with custom output."""


class PlainFormatter(OutputFormatter):
    STREAM = True

    def __init__(self) -> None:
        self._count = 0

    def __call__(self, **kwargs: object) -> None:
        if self._count:
            sys.stdout.write("---\n")
        self._count += 1
        key_width = max(len(k) for k in kwargs) if kwargs else 0
        for key, val in kwargs.items():
            text = _format_value(val)
            label = f"{key}:".ljust(key_width + 2)
            lines = text.split("\n")
            sys.stdout.write(f"{label}{lines[0]}\n")
            indent = " " * len(label)
            for line in lines[1:]:
                sys.stdout.write(f"{indent}{line}\n")


FORMATTERS: dict[str, type[OutputFormatter]] = {
    "csv": CSVFormatter,
    "tsv": TSVFormatter,
    "json": JSONFormatter,
    "json-pretty": JSONPrettyFormatter,
    "plain": PlainFormatter,
    "table": TableFormatter,
    "default": DefaultFormatter,
}


@functools.singledispatch
def _toml_value(value: object) -> str:
    return json.dumps(str(value))


@_toml_value.register
def _(value: str) -> str:
    # TOML basic string with escaping
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


@_toml_value.register
def _(value: bool) -> str:
    return "true" if value else "false"


@_toml_value.register
def _(value: int) -> str:
    return str(value)


@_toml_value.register
def _(value: float) -> str:
    return str(value)


@_toml_value.register
def _(value: datetime) -> str:
    return value.isoformat()


@_toml_value.register
def _(value: timedelta) -> str:
    return str(value.total_seconds())


@_toml_value.register(type(None))
def _(value: None) -> str:
    return '""'


try:
    import tomllib as _tomllib  # type: ignore[import-not-found]  # noqa: F401

    class TOMLFormatter(OutputFormatter):
        STREAM = True

        def __init__(self) -> None:
            self._rows: list[dict[str, object]] = []

        def __call__(self, **kwargs: object) -> None:
            self._rows.append(kwargs)

        def flush(self) -> None:
            if not self._rows:
                return
            parts: list[str] = []
            for row in self._rows:
                parts.append("[[results]]")
                for key, val in row.items():
                    parts.append(f"{key} = {_toml_value(val)}")
                parts.append("")
            sys.stdout.write("\n".join(parts))
            self._rows.clear()

    FORMATTERS["toml"] = TOMLFormatter

except ImportError:
    pass
