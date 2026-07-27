"""Copy a file from the session image to a local path.

Downloads the file at PATH inside the current session image and writes
it to DEST on the local filesystem. Progress is logged for large files.
Unlike `cat`, this command handles binary content and does not require
a terminal.
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.output import DefaultFormatter

logger = logging.getLogger(__name__)

LOG_INTERVAL = 5.0  # seconds between progress logs

EPILOG = """\
for coding agents:
  read-only command against remote image, writes local file DEST
  suitable for binary files
  --format is ignored; command writes bytes directly
"""


def fmt_size(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


@dataclass(frozen=True)
class CpArgs(ArgumentsProtocol):
    path: str
    dest: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CpArgs:
        return cls(path=ns.path, dest=ns.dest)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("path", help="Path inside image")
    p.add_argument("dest", help="Local destination path")
    return cmd_cp, CpArgs


def cmd_cp(args: CpArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    if not isinstance(formatter, DefaultFormatter):
        logger.warning("cp always outputs raw content; --format is ignored")

    store = SESSION_STORE.get()
    image = store.current_image
    path = store.resolve_path(args.path)
    uuid = client.resolve_image(image)

    dest = Path(args.dest)
    if dest.is_dir():
        dest = dest / Path(path).name

    # The streaming download API exposes no response headers, so the
    # total size (Content-Length) is unknown and progress is reported
    # as running volume/speed only.
    downloaded = 0
    start = time.monotonic()
    last_log = start

    tmp_path: str | None = None
    fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in client.inspect_image_download_stream(uuid, path):
                f.write(chunk)
                downloaded += len(chunk)

                now = time.monotonic()
                if now - last_log >= LOG_INTERVAL:
                    last_log = now
                    elapsed = now - start
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    logger.info(
                        "%s downloaded | %s/s",
                        fmt_size(downloaded),
                        fmt_size(speed),
                    )
        os.replace(tmp_path, dest)
        tmp_path = None
    finally:
        if tmp_path is not None:
            Path(tmp_path).unlink(missing_ok=True)

    elapsed = time.monotonic() - start
    speed = downloaded / elapsed if elapsed > 0 else 0
    logger.info(
        "Written %s to %s (%s/s)",
        fmt_size(downloaded),
        dest,
        fmt_size(speed),
    )
    return None
