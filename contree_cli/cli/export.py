"""Export the session image rootfs (or a subtree) as a tar archive.

Docker-export-like, with one extra power: PATH selects any subtree of
the image instead of the whole rootfs. The archive is streamed from
the /inspect/ API; by default the gzip the server applies on the wire
is passed through untouched, so the output is a ``.tar.gz`` produced
with zero local compression work. ``--decompress`` writes a plain tar
instead (the client inflates the stream).

Output goes to ``-F FILE`` or to stdout for piping
(``contree export | tar -tzf -``); a terminal stdout is refused, the
stream is binary.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from contree_client.exceptions import NotFoundError

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.cli.cp import LOG_INTERVAL, fmt_size
from contree_cli.output import DefaultFormatter
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  contree export -F rootfs.tar.gz          # whole rootfs
  contree export /etc -F etc.tar.gz        # a subtree only
  contree export /opt/app -F app.tar       # .tar implies --decompress
  contree export /etc | tar -tzf - | head  # stream to a pipe

for coding agents:
  read-only command (inspect API, no instance spawn)
  default output is gzip (tar.gz) streamed as served by the API
  --decompress (or -F *.tar) writes a plain tar instead
  stdout must be redirected or piped; a terminal is refused
  --format is ignored; output is the raw archive stream
"""


@dataclass(frozen=True)
class ExportArgs(ArgumentsProtocol):
    path: str
    output: str = ""
    decompress: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ExportArgs:
        return cls(
            path=ns.path if ns.path is not None else "/",
            output=ns.output or "",
            decompress=ns.decompress,
        )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path inside image to export (default: / — the whole rootfs)",
    )
    p.add_argument(
        *FLAGS["file"],
        dest="output",
        default="",
        metavar="FILE",
        help="Write the archive to FILE (default: stdout)",
    )
    p.add_argument(
        *FLAGS["decompress"],
        action="store_true",
        help="Write a plain tar instead of the default tar.gz",
    )
    return cmd_export, ExportArgs


def wants_plain_tar(args: ExportArgs) -> bool:
    """Plain tar on --decompress or when the target is named *.tar."""
    if args.decompress:
        return True
    return args.output.endswith(".tar")


def write_stream(chunks: Iterator[bytes], sink: IO[bytes]) -> int:
    """Pump chunks into *sink* with periodic progress on stderr."""
    written = 0
    start = time.monotonic()
    last_log = start
    for chunk in chunks:
        sink.write(chunk)
        written += len(chunk)
        now = time.monotonic()
        if now - last_log >= LOG_INTERVAL:
            last_log = now
            elapsed = now - start
            speed = written / elapsed if elapsed > 0 else 0
            logger.info(
                "%s exported | %s/s",
                fmt_size(written),
                fmt_size(speed),
            )
    return written


def cmd_export(args: ExportArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    if not isinstance(formatter, DefaultFormatter):
        logger.warning("export always outputs the raw archive; --format is ignored")

    if not args.output and sys.stdout.isatty():
        logger.error(
            "The archive is a binary stream and stdout is a terminal."
            " Write it to a file:  contree export %(path)s -F rootfs.tar.gz\n"
            "or pipe it:           contree export %(path)s | tar -tzf -",
            {"path": args.path},
        )
        return 1

    store = SESSION_STORE.get()
    path = store.resolve_path(args.path)
    uuid = client.resolve_image(store.current_image)

    chunks: Iterator[bytes] = client.inspect_image_archive(
        uuid,
        path,
        compressed=not wants_plain_tar(args),
    )

    start = time.monotonic()
    tmp_path: str | None = None
    try:
        if args.output:
            dest = Path(args.output)
            fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
            with os.fdopen(fd, "wb") as sink:
                written = write_stream(chunks, sink)
            os.replace(tmp_path, dest)
            tmp_path = None
        else:
            written = write_stream(chunks, sys.stdout.buffer)
            sys.stdout.buffer.flush()
    except NotFoundError:
        logger.error("export: %s: not found in image", path)
        return 1
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)

    elapsed = time.monotonic() - start
    speed = written / elapsed if elapsed > 0 else 0
    logger.info(
        "Exported %s from %s to %s (%s/s)",
        fmt_size(written),
        path,
        args.output or "stdout",
        fmt_size(speed),
    )
    return None
