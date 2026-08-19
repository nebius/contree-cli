from __future__ import annotations

import csv
import functools
import io
import json
import logging
import shutil
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from contree_cli.types import STDOUT_IS_A_TTY, Colors, parse_datetime

log = logging.getLogger(__name__)


DATETIME_FIELDS = frozenset({"created_at", "updated_at", "ts"})


def transform_field(key: str, value: Any) -> Any:
    """Apply field-name based type conversion for known API shapes."""
    if key in DATETIME_FIELDS and isinstance(value, str):
        return parse_datetime(value)
    if key == "duration" and isinstance(value, (int, float)):
        return timedelta(seconds=value)
    if (key == "error" and value is None) or (key == "tag" and value is None):
        return ""
    if key == "mode" and isinstance(value, int):
        return format(value, "o")
    if key == "mtime" and isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return value


class ListSorter:
    """Reorder an API record dict for table output.

    Nested values (dict/list) are flattened to a compact JSON string
    unless *allow_nested* (JSON-like formatters serialise them as-is).
    Yields columns as ``head``, then discovered keys, then ``tail``.
    ``optional`` (explicit, or auto = discovered keys in order) lists
    columns a table can drop, back first, when there isn't room.
    """

    def __init__(
        self,
        *,
        head: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
        optional: tuple[str, ...] | None = None,
        allow_nested: bool = False,
    ) -> None:
        self.tail = tail
        self.columns: list[str] = list(head)
        self.seen: set[str] = set(head) | set(tail)
        self.allow_nested = allow_nested
        self._explicit_optional = optional
        self.discovered: list[str] = []

    @functools.cached_property
    def optional(self) -> tuple[str, ...]:
        if self._explicit_optional is not None:
            return self._explicit_optional
        return tuple(self.discovered)

    def order(self, fields: dict[str, Any]) -> OrderedDict[str, Any]:
        for key in fields:
            if key in self.seen:
                continue
            self.columns.append(key)
            self.seen.add(key)
            self.discovered.append(key)

        out: OrderedDict[str, Any] = OrderedDict()
        for key in (*self.columns, *self.tail):
            if key not in fields:
                continue
            value = fields[key]
            if not self.allow_nested and isinstance(value, (dict, list)):
                value = json.dumps(value, default=_json_default)
            out[key] = transform_field(key, value)
        return out


@functools.singledispatch
def _format_value(value: object) -> str:
    """Human-friendly string for a value."""
    return str(value)


@_format_value.register
def _(value: datetime) -> str:
    # API returns UTC; render in the user's local timezone for readability.
    if value.tzinfo is not None:
        value = value.astimezone()
    if value.date() == datetime.now(value.tzinfo).date():
        return value.strftime("%H:%M:%S")
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


def row_width(columns: list[str], widths: dict[str, int]) -> int:
    return sum(widths[c] for c in columns) + max(len(columns) - 1, 0) * 2


def layout_columns(
    columns: list[str],
    widths: dict[str, int],
    optional: tuple[str, ...],
    tail: tuple[str, ...],
    term_width: int,
    min_col_width: int,
) -> tuple[list[str], dict[str, int], list[str], bool]:
    """Fit columns into term_width.

    1. Drop optional columns, back of *optional* first (later-discovered
       columns are more peripheral than earlier ones), while it still
       doesn't fit at natural width (never below one column).
    2. If it still doesn't fit, shrink non-tail columns first -- tail
       columns (e.g. "result") keep their natural width as long as
       shrinking the rest is enough.
    3. If that alone isn't enough, shrink everything, tail included.

    Returns (kept_columns, widths, dropped_columns, was_truncated).
    """
    kept = list(columns)
    dropped: list[str] = []
    for col in reversed(optional):
        if len(kept) <= 1 or col not in kept or row_width(kept, widths) <= term_width:
            continue
        kept.remove(col)
        dropped.append(col)

    truncated = False
    if row_width(kept, widths) > term_width:
        separator_space = max(len(kept) - 1, 0) * 2
        shrinkable = [c for c in kept if c not in tail] or kept
        pinned_width = sum(widths[c] for c in kept if c not in shrinkable)
        available = term_width - separator_space - pinned_width
        if available > 0:
            fit_widths, truncated = _fit_columns(
                {c: widths[c] for c in shrinkable}, shrinkable, available, min_col_width
            )
            widths = {**widths, **fit_widths}
        if row_width(kept, widths) > term_width:
            available = term_width - separator_space
            widths, truncated = _fit_columns(
                {c: widths[c] for c in kept}, kept, available, min_col_width
            )
    return kept, widths, dropped, truncated


class OutputFormatter:
    """Base formatter - subclasses decide the serialisation style.

    Maintains an internal :class:`ListSorter` that drops nested values,
    applies light typing to known fields (timestamps, duration, mode,
    mtime, nullable error/tag), and reorders columns according to
    optional ``head``/``tail`` configured via :meth:`configure`.
    """

    # Not suitable for streaming stdout/stderr output (e.g. from `run`).
    # Also controls whether nested dict/list fields survive ListSorter:
    # JSON-like formatters can serialise them as-is, tabular ones can't.
    STREAM = False

    def __init__(self) -> None:
        self.sorter = ListSorter(allow_nested=self.STREAM)

    def configure(
        self,
        *,
        head: tuple[str, ...] = (),
        tail: tuple[str, ...] = (),
        optional: tuple[str, ...] | None = None,
    ) -> None:
        """Configure column ordering for this formatter."""
        self.sorter = ListSorter(
            head=head, tail=tail, optional=optional, allow_nested=self.STREAM
        )

    def __call__(self, **kwargs: object) -> None:
        self.write(self.sorter.order(kwargs))

    def write(self, row: OrderedDict[str, Any]) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        """Flush any buffered output. No-op for streaming formatters."""

    def close(self) -> None:
        """Finalise the output stream. Defaults to a final flush."""
        self.flush()


class CSVFormatter(OutputFormatter):
    def __init__(self) -> None:
        super().__init__()
        self._header_written = False

    def write(self, row: OrderedDict[str, Any]) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf)
        if not self._header_written:
            writer.writerow(row.keys())
            self._header_written = True
        writer.writerow(_format_value(v) for v in row.values())
        sys.stdout.write(buf.getvalue())


class TSVFormatter(OutputFormatter):
    def __init__(self) -> None:
        super().__init__()
        self._header_written = False

    def write(self, row: OrderedDict[str, Any]) -> None:
        buf = io.StringIO()
        writer = csv.writer(buf, dialect="excel-tab")
        if not self._header_written:
            writer.writerow(row.keys())
            self._header_written = True
        writer.writerow(_format_value(v) for v in row.values())
        sys.stdout.write(buf.getvalue())


class JSONFormatter(OutputFormatter):
    STREAM = True

    def write(self, row: OrderedDict[str, Any]) -> None:
        sys.stdout.write(json.dumps(row, default=_json_default) + "\n")


class JSONPrettyFormatter(OutputFormatter):
    STREAM = True

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[OrderedDict[str, Any]] = []
        self._opened = False
        self._first_row = True
        self.flush_lock = threading.RLock()

    def write(self, row: OrderedDict[str, Any]) -> None:
        self._rows.append(row)

    def flush(self) -> None:
        with self.flush_lock:
            if not self._rows:
                return
            if not self._opened:
                sys.stdout.write("[\n")
                self._opened = True
            for row in self._rows:
                prefix = "" if self._first_row else ",\n"
                self._first_row = False
                sys.stdout.write(
                    prefix + json.dumps(row, indent=2, default=_json_default)
                )
            self._rows.clear()

    def close(self) -> None:
        self.flush()
        with self.flush_lock:
            if self._opened:
                sys.stdout.write("\n]\n")
                self._opened = False
                self._first_row = True


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
        super().__init__()
        self._rows: list[OrderedDict[str, Any]] = []
        # Layout decided on the first non-empty flush, reused on subsequent
        # flushes so paginated output keeps the same column alignment.
        self._columns: list[str] | None = None
        self._widths: dict[str, int] | None = None
        self._col_colors: dict[str, Colors] = {}
        self.flush_lock = threading.RLock()

    def write(self, row: OrderedDict[str, Any]) -> None:
        self._rows.append(row)

    def flush(self) -> None:
        with self.flush_lock:
            if not self._rows:
                return
            first_flush = self._columns is None
            if first_flush:
                columns = list(self._rows[0].keys())
                widths = {col: len(col) for col in columns}
                for row in self._rows:
                    for col in columns:
                        for line in _format_value(row.get(col, "")).split("\n"):
                            widths[col] = max(widths[col], len(line))
                truncated = False
                dropped: list[str] = []
                if STDOUT_IS_A_TTY:
                    term_width = shutil.get_terminal_size().columns
                    columns, widths, dropped, truncated = layout_columns(
                        columns,
                        widths,
                        self.sorter.optional,
                        tuple(self.sorter.tail),
                        term_width,
                        self.MIN_COL_WIDTH,
                    )
                self._columns = columns
                self._widths = widths
                if STDOUT_IS_A_TTY:
                    for idx, col in enumerate(columns):
                        self._col_colors[col] = self.COLUMN_PALETTE[
                            idx % len(self.COLUMN_PALETTE)
                        ]
                header_parts: list[str] = []
                for col in columns:
                    # Truncating/padding is a TTY-only affordance.
                    if STDOUT_IS_A_TTY:
                        padded = _truncate(
                            col.upper(), widths[col], self.ELLIPSIS
                        ).ljust(widths[col])
                        padded = Colors.BOLD(padded)
                    else:
                        padded = col.upper()
                    header_parts.append(padded)
                sys.stdout.write("  ".join(header_parts) + "\n")
                if dropped:
                    log.warning(
                        "Hid %s to fit terminal; use --format json to see everything",
                        ", ".join(dropped),
                    )
                if truncated:
                    log.warning(
                        "Output truncated to fit terminal;"
                        " use --format json to see full values",
                    )
            assert self._columns is not None
            assert self._widths is not None
            columns = self._columns
            widths = self._widths
            for row in self._rows:
                split_row = {
                    col: _format_value(row.get(col, "")).split("\n") for col in columns
                }
                height = max(len(split_row[col]) for col in columns)
                for i in range(height):
                    parts: list[str] = []
                    for col in columns:
                        lines = split_row[col]
                        cell = lines[i] if i < len(lines) else ""
                        if STDOUT_IS_A_TTY:
                            padded = _truncate(cell, widths[col], self.ELLIPSIS).ljust(
                                widths[col]
                            )
                        else:
                            padded = cell
                        if col in self._col_colors:
                            color = self.VALUE_COLORS.get(
                                cell.strip(),
                                self._col_colors[col],
                            )
                            padded = color(padded)
                        parts.append(padded)
                    sys.stdout.write("  ".join(parts) + "\n")
            self._rows.clear()


class DefaultFormatter(TableFormatter):
    """Default formatter - commands may detect and replace with custom output."""


class PlainFormatter(OutputFormatter):
    STREAM = True

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def write(self, row: OrderedDict[str, Any]) -> None:
        if self._count:
            sys.stdout.write("---\n")
        self._count += 1
        key_width = max(len(k) for k in row) if row else 0
        for key, val in row.items():
            text = _format_value(val)
            label = f"{key}:".ljust(key_width + 2)
            lines = text.split("\n")
            sys.stdout.write(f"{label}{lines[0]}\n")
            indent = " " * len(label)
            for line in lines[1:]:
                sys.stdout.write(f"{indent}{line}\n")

    def flush(self) -> None:
        sys.stdout.flush()


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
            super().__init__()
            self._rows: list[OrderedDict[str, Any]] = []
            self.flush_lock = threading.RLock()

        def write(self, row: OrderedDict[str, Any]) -> None:
            self._rows.append(row)

        def flush(self) -> None:
            with self.flush_lock:
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
