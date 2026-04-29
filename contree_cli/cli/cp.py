"""Copy a file from the session image to a local path.

Downloads the file at PATH inside the current session image and writes
it to DEST on the local filesystem. Progress is logged for large files.
Unlike `cat`, this command handles binary content and does not require
a terminal.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import resolve_image, stream_response
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


def fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


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
    uuid = resolve_image(client, image)
    resp = client.get(f"/v1/inspect/{uuid}/download", params={"path": path})

    total: int | None = None
    cl = resp.getheader("Content-Length")
    if cl is not None:
        total = int(cl)

    downloaded = 0
    start = time.monotonic()
    last_log = start

    with Path(args.dest).open("wb") as f:
        for chunk in stream_response(resp):
            f.write(chunk)
            downloaded += len(chunk)

            now = time.monotonic()
            if now - last_log >= LOG_INTERVAL:
                last_log = now
                elapsed = now - start
                speed = downloaded / elapsed if elapsed > 0 else 0
                parts = [f"{fmt_size(downloaded)} downloaded"]
                if total:
                    pct = downloaded / total * 100
                    remaining = (total - downloaded) / speed if speed > 0 else 0
                    parts.append(f"{pct:.0f}%")
                    parts.append(f"ETA {fmt_duration(remaining)}")
                parts.append(f"{fmt_size(speed)}/s")
                logger.info("%s", " | ".join(parts))

    elapsed = time.monotonic() - start
    speed = downloaded / elapsed if elapsed > 0 else 0
    logger.info(
        "Written %s to %s (%s/s)",
        fmt_size(downloaded),
        args.dest,
        fmt_size(speed),
    )
    return None
