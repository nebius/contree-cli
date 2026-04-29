"""Show file content from the session image.

Downloads and displays a file from the current session image via the
/inspect/ API without spawning an instance. Binary files are refused
when stdout is a terminal — use shell redirection or `contree cp` to
save them locally.

Results are cached per (image, path) so repeated reads are instant.
"""

from __future__ import annotations

import argparse
import base64
import logging
import sys
from dataclasses import dataclass
from typing import cast

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import resolve_image
from contree_cli.output import DefaultFormatter

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  read-only command (inspect API, no instance spawn)
  binary output is blocked on interactive TTY; pipe or use cp for binaries
  --format is ignored; output is raw file content
"""


@dataclass(frozen=True)
class CatArgs(ArgumentsProtocol):
    path: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CatArgs:
        return cls(path=ns.path)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("path", help="Path inside image")
    return cmd_cat, CatArgs


def cmd_cat(args: CatArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    if not isinstance(formatter, DefaultFormatter):
        logger.warning("cat always outputs raw content; --format is ignored")

    store = SESSION_STORE.get()
    image = store.current_image
    path = store.resolve_path(args.path)
    uuid = resolve_image(client, image)

    cache_key = (uuid, f"download:{path}")
    cached = store.cache.get(cache_key)
    if cached is not None:
        data = base64.b64decode(cast(str, cached))
    else:
        resp = client.get(f"/v1/inspect/{uuid}/download", params={"path": path})
        data = resp.read()
        store.cache[cache_key] = base64.b64encode(data).decode("ascii")

    if not sys.stdout.isatty():
        sys.stdout.buffer.write(data)
        return None

    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        logger.error(
            "Binary content - refusing to write to terminal. "
            "Use shell redirection or `contree cp`.",
        )
        return 1

    sys.stdout.buffer.write(data)
    return None
