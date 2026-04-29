"""Set or show the current session image.

With an IMAGE argument, resolves it (UUID or tag:NAME) and sets it as
the active session image. Prints a shell export line so that the
session key can be captured with eval:

  eval $(contree use tag:ubuntu:latest)

Without arguments, displays the current session info (image, branch,
last operation).

Use -N/--new to start a fresh session instead of resuming the current
one. The new session key is printed as an export line.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from contree_cli import (
    CLIENT,
    FORMATTER,
    IN_SHELL,
    PROFILE,
    SESSION_STORE,
    ArgumentsProtocol,
    SetupResult,
)
from contree_cli.client import resolve_image
from contree_cli.session import SessionStore
from contree_cli.types import FLAGS

log = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  use IMAGE starts/switches a session and prints CONTREE_SESSION export
  use (without IMAGE) is read-only and prints current session state
  use --new IMAGE creates a fresh session key
  without CONTREE_SESSION env var, key is auto-generated as <cwd>+<8hex>
  (derived from profile+ppid+tty); export your own for stability
"""


def _shell_name() -> str:
    return PurePosixPath(os.getenv("SHELL", "/bin/sh")).name


def _print_shell_export(key: str, value: str) -> None:
    if _shell_name() == "fish":
        sys.stdout.write(f"set -gx {key} {value}\n")
    else:
        sys.stdout.write(f"export {key}={value}\n")


@dataclass(frozen=True)
class UseArgs(ArgumentsProtocol):
    image: str | None
    new: bool

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> UseArgs:
        return cls(image=ns.image, new=ns.new)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("image", nargs="?", default=None, help="Image UUID or tag")
    p.add_argument(
        *FLAGS["new"],
        action="store_true",
        default=False,
        help="Start a new session instead of resuming the current one",
    )
    return cmd_use, UseArgs


def cmd_use(args: UseArgs) -> int | None:
    store = SESSION_STORE.get()

    if args.new:
        if args.image is None:
            print(
                "--new requires an IMAGE argument.",
                file=sys.stderr,
            )
            return 1
        new_key = str(uuid.uuid4())
        store = SessionStore(PROFILE.get().session_db_path, new_key)
        SESSION_STORE.set(store)

    if args.image is not None:
        client = CLIENT.get()
        image_uuid = resolve_image(client, args.image)
        store.set_image(image_uuid, kind="use", title=args.image)
        if not IN_SHELL.get(False):
            _print_shell_export("CONTREE_SESSION", store.session_key)
            if args.new and sys.stdout.isatty():
                if _shell_name() == "fish":
                    eval_hint = "eval (contree use -N IMAGE)"
                else:
                    eval_hint = "eval $(contree use -N IMAGE)"
                log.warning(
                    "Session is not active until exported. "
                    "Either paste the line above, or use: %s",
                    eval_hint,
                )
        return None

    session = store.session
    if session is None:
        print(
            "No active session. Run `contree use IMAGE` to start one.",
            file=sys.stderr,
        )
        return 1

    formatter = FORMATTER.get()

    formatter(
        session_key=session.session_key,
        active_branch=session.active_branch,
        current_image=session.current_image,
        last_kind=session.last_kind,
        last_title=session.last_title,
        updated_at=session.updated_at,
    )
    return None
