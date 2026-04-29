"""Change the working directory in the current session.

Sets the session's cwd used by subsequent commands (run, ls, cat).
Relative paths are resolved against the current cwd. Without an
argument, prints the current working directory.

The path is validated against the image filesystem via the inspect API.
"""

from __future__ import annotations

import argparse
import json
import logging
import posixpath
from dataclasses import dataclass

from contree_cli import CLIENT, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError, resolve_image

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  mutates local session cwd pointer
  validates path exists in image via inspect API
"""


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("path", nargs="?", default=None, help="Target directory")
    return cmd_cd, CdArgs


@dataclass(frozen=True)
class CdArgs(ArgumentsProtocol):
    path: str | None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CdArgs:
        return cls(path=ns.path)


def cmd_cd(args: CdArgs) -> int | None:
    store = SESSION_STORE.get()

    if args.path is None:
        cwd = store.get_cwd()
        print(cwd or "/")
        return None

    target = args.path
    current = store.get_cwd() or "/"

    if target.startswith("/"):
        new_cwd = posixpath.normpath(target)
    else:
        new_cwd = posixpath.normpath(posixpath.join(current, target))

    # Validate path exists in image (skip if no session yet)
    session = store.session
    if session is not None:
        try:
            client = CLIENT.get()
            uuid = resolve_image(client, session.current_image)
            resp = client.get(f"/v1/inspect/{uuid}/list", params={"path": new_cwd})
            data = json.loads(resp.read())
            if not data:
                logger.error("cd: %s: not a directory", new_cwd)
                return 1
        except ApiError as exc:
            if exc.status == 404:
                logger.error("cd: %s: no such directory", new_cwd)
                return 1
            raise

    store.set_cwd(new_cwd)
    return None
