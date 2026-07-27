"""Search file contents in the session image.

Uses the /inspect/ API (server-side ripgrep) to search file contents
without spawning an instance. Defaults to the session working directory
(set via `cd`) when PATH is omitted -- pass `/` explicitly to search the
whole image.
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
from contree_cli.types import FLAGS, STDOUT_IS_A_TTY, Colors, positive_int

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  read-only command (inspect API, no instance spawn)
  defaults to session cwd when PATH is omitted; pass / to search whole image
  exit code 1 means zero matches (like POSIX grep), not an error
  default format prints classic PATH:LINE:TEXT; use -o json/csv/... for rows
  use --raw to preserve submatches/patterns/truncated as JSON
"""

CaseLiteral = Literal["sensitive", "insensitive", "smart"]


@dataclass(frozen=True)
class GrepArgs(ArgumentsProtocol):
    pattern: str
    path: str | None
    glob: str | None = None
    max_count: int | None = None
    max_total: int | None = None
    case: CaseLiteral | None = None
    raw: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> GrepArgs:
        return cls(
            pattern=ns.pattern,
            path=ns.path,
            glob=ns.glob,
            max_count=ns.max_count,
            max_total=ns.max_total,
            case=ns.case,
            raw=ns.raw,
        )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("pattern", help="Regex pattern to search for (Rust regex syntax)")
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="File or directory inside image (defaults to session cwd)",
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


def cmd_grep(args: GrepArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("line_text",))
    store = SESSION_STORE.get()
    uuid = client.resolve_image(store.current_image)

    path = (
        store.resolve_path(args.path)
        if args.path is not None
        else (store.get_cwd() or "/")
    )

    result = client.inspect_image_grep(
        uuid,
        args.pattern,
        path=path,
        glob=args.glob,
        max_count=args.max_count,
        max_total=args.max_total,
        case=args.case,
    )
    data = result.to_dict()

    if args.raw:
        # Bypasses formatter routing to preserve submatches/patterns/
        # truncated verbatim -- see show.py's --raw for the same idea.
        json.dump(data, sys.stdout)
        sys.stdout.write("\n")
    elif isinstance(formatter, DefaultFormatter):
        # Classic `path:line:text` grep output, matching ripgrep's own
        # default rendering (path/line colored, match highlighted, when
        # stdout is a terminal). Structured formats (json/csv/table/...)
        # still get one row per match via the normal pipeline below.
        for match in data["matches"]:
            line_text = match["line_text"].rstrip("\n")
            path, line_number = match["path"], match["line_number"]
            if STDOUT_IS_A_TTY:
                line_text = highlight_match(line_text, match["submatches"])
                path = Colors.MAGENTA(path)
                line_number = Colors.GREEN(str(line_number))
            sys.stdout.write(f"{path}:{line_number}:{line_text}\n")
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
