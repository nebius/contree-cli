"""Search file contents in the session image.

Uses the /inspect/ API (server-side ripgrep) to search file contents
without spawning an instance. Defaults to the session working directory
(set via `cd`) when PATH is omitted -- pass `/` explicitly to search the
whole image. PATH may be repeated to search multiple roots in one call.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Literal

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.output import DefaultFormatter
from contree_cli.types import (
    FLAGS,
    STDOUT_IS_A_TTY,
    Colors,
    context_lines,
    positive_int,
)

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  read-only command (inspect API, no instance spawn)
  defaults to session cwd when PATH is omitted; pass / to search whole image
  exit code 1 means zero matches (like POSIX grep), not an error
  default format prints classic PATH:LINE:TEXT; use -o json/csv/... for rows
  use --raw to preserve submatches/patterns/truncated as JSON
  -A/-B/--context add ripgrep-side context lines (server-computed, not local)
"""

CaseLiteral = Literal["sensitive", "insensitive", "smart"]


@dataclass(frozen=True)
class GrepArgs(ArgumentsProtocol):
    pattern: str
    path: tuple[str, ...]
    glob: str | None = None
    max_count: int | None = None
    max_total: int | None = None
    case: CaseLiteral | None = None
    raw: bool = False
    before_context: int | None = None
    after_context: int | None = None
    context: int | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> GrepArgs:
        return cls(
            pattern=ns.pattern,
            path=tuple(ns.path),
            glob=ns.glob,
            max_count=ns.max_count,
            max_total=ns.max_total,
            case=ns.case,
            raw=ns.raw,
            before_context=ns.before_context,
            after_context=ns.after_context,
            context=ns.context,
        )

    def before(self) -> int | None:
        return self.before_context if self.before_context is not None else self.context

    def after(self) -> int | None:
        return self.after_context if self.after_context is not None else self.context


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("pattern", help="Regex pattern to search for (Rust regex syntax)")
    p.add_argument(
        "path",
        nargs="*",
        help=(
            "File(s)/directory(ies) inside image (defaults to session cwd);"
            " may be repeated to search multiple roots"
        ),
    )
    p.add_argument(
        *FLAGS["glob"],
        default=None,
        help="Restrict search to files matching this glob (e.g. '*.py')",
    )
    p.add_argument(
        *FLAGS["max_count"],
        type=positive_int,
        default=None,
        help="Stop after this many matches per file",
    )
    p.add_argument(
        *FLAGS["max_total"],
        type=positive_int,
        default=None,
        help="Stop after this many matches total (server default: 1000)",
    )
    p.add_argument(
        *FLAGS["case"],
        choices=("sensitive", "insensitive", "smart"),
        default=None,
        help="Case sensitivity (default: sensitive)",
    )
    p.add_argument(
        *FLAGS["before_context"],
        type=context_lines,
        default=None,
        metavar="NUM",
        help="Show NUM lines of context before each match (max 50)",
    )
    p.add_argument(
        *FLAGS["after_context"],
        type=context_lines,
        default=None,
        metavar="NUM",
        help="Show NUM lines of context after each match (max 50)",
    )
    p.add_argument(
        # -C is grep's own long-standing flag for this; registering it
        # in the shared FLAGS map would collide with run's --cwd, so
        # it's added here directly as a one-off exception for grep/
        # POSIX compatibility instead of going through FLAGS.
        "-C",
        *FLAGS["context"],
        type=context_lines,
        default=None,
        metavar="NUM",
        help="Show NUM lines of context before and after (overridden by -A/-B)",
    )
    p.add_argument(
        *FLAGS["raw"],
        action="store_true",
        help="Print raw JSON (preserves submatches/patterns/truncated)",
    )
    return cmd_grep, GrepArgs


def highlight_match(line_text: str, submatches: list[dict[str, Any]]) -> str:
    """Wrap each submatch span in bold red, like `grep --color`.

    Submatch offsets are byte offsets (ripgrep operates on raw bytes),
    so slicing happens on the UTF-8 encoded line to stay correctly
    aligned with multi-byte characters.
    """
    data = line_text.encode("utf-8", errors="surrogateescape")
    parts: list[str] = []
    pos = 0
    for sub in sorted(submatches, key=lambda s: int(s["start"])):
        start, end = int(sub["start"]), int(sub["end"])
        if start < pos or start >= len(data):
            continue  # out-of-order/out-of-range offsets -- skip defensively
        end = min(end, len(data))
        parts.append(data[pos:start].decode("utf-8", errors="replace"))
        parts.append(Colors.BOLD_RED(data[start:end].decode("utf-8", errors="replace")))
        pos = end
    parts.append(data[pos:].decode("utf-8", errors="replace"))
    return "".join(parts)


def write_grep_lines(matches: list[dict[str, Any]], *, with_context: bool) -> None:
    """Print matches in classic grep format: `path:line:text` for an
    actual match, `path-line-text` for a `-A`/`-B`/`--context` line.

    A `--` separator is inserted between output groups whenever
    `with_context` is set and the next row isn't the immediate
    successor (same path, consecutive line number) of the previous
    one -- exactly like GNU grep/ripgrep only ever show `--` when
    context lines are in play.
    """
    prev_path: str | None = None
    prev_line: int | None = None
    for match in matches:
        row_path, row_line = match["path"], match["line_number"]
        contiguous = (
            prev_path == row_path
            and prev_line is not None
            and (row_line == prev_line + 1)
        )
        if with_context and prev_path is not None and not contiguous:
            sys.stdout.write("--\n")

        sep = ":" if match["type"] == "match" else "-"
        line_text = match["line_text"].rstrip("\n")
        path_field, line_field = row_path, str(row_line)
        if STDOUT_IS_A_TTY:
            line_text = highlight_match(line_text, match["submatches"])
            path_field = Colors.MAGENTA(row_path)
            line_field = Colors.GREEN(line_field)
        sys.stdout.write(f"{path_field}{sep}{line_field}{sep}{line_text}\n")

        prev_path, prev_line = row_path, row_line


def cmd_grep(args: GrepArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("line_text",))
    store = SESSION_STORE.get()
    uuid = client.resolve_image(store.current_image)

    paths = (
        [store.resolve_path(p) for p in args.path]
        if args.path
        else [store.get_cwd() or "/"]
    )

    before, after = args.before(), args.after()
    result = client.inspect_image_grep(
        uuid,
        args.pattern,
        path=paths,
        glob=args.glob,
        max_count=args.max_count,
        max_total=args.max_total,
        case=args.case,
        before=before,
        after=after,
    )
    data = result.to_dict()

    if args.raw:
        # Bypasses formatter routing to preserve submatches/patterns/
        # truncated verbatim -- see show.py's --raw for the same idea.
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")
    elif isinstance(formatter, DefaultFormatter):
        # Classic grep output, matching ripgrep's own default rendering
        # (path/line colored, match highlighted, when stdout is a
        # terminal). Structured formats (json/csv/table/...) still get
        # one row per match/context line via the normal pipeline below.
        write_grep_lines(data["matches"], with_context=bool(before or after))
    else:
        for match in data["matches"]:
            formatter(**{**match, "line_text": match["line_text"].rstrip("\n")})
        formatter.flush()

    if data["truncated"]:
        logger.warning(
            "Results truncated (max_total or search deadline reached);"
            " narrow the search with --glob or a more specific PATH.",
        )

    return 1 if not data["matches"] else None
