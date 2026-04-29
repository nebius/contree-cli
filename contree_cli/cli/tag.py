"""Assign or remove a tag from an image.

Tags provide human-readable names for image UUIDs, making them easier
to reference in commands like `contree use tag:NAME`.

With one argument, tags the current session image.
With two arguments, the first is the image reference and the second is the tag.

Use -d/--delete to remove a tag instead of assigning one.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from contree_cli import CLIENT, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import resolve_image
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  contree tag python-dev:latest            # tag current session image
  contree tag UUID python-dev:latest       # tag specific image by UUID
  contree tag tag:alpine:latest my-alpine  # re-tag by reference
  contree tag -d UUID my-tag               # remove a tag

for coding agents:
  mutating command
  default action assigns tag; use --delete to remove mapping
"""


@dataclass(frozen=True)
class TagArgs(ArgumentsProtocol):
    tag: str
    image_ref: str | None = None
    delete: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> TagArgs:
        raw = ns.args
        if len(raw) == 1:
            return cls(tag=raw[0], image_ref=None, delete=ns.delete)
        return cls(tag=raw[-1], image_ref=raw[0], delete=ns.delete)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "args",
        nargs="+",
        metavar="ARG",
        help="TAG (current image) or IMAGE_REF TAG",
    )
    p.add_argument(
        *FLAGS["delete"],
        action="store_true",
        help="Remove tag from image",
    )
    return cmd_tag, TagArgs


def cmd_tag(args: TagArgs) -> int | None:
    client = CLIENT.get()

    if args.image_ref is not None:
        image_uuid = resolve_image(client, args.image_ref)
    else:
        store = SESSION_STORE.get()
        session = store.session
        if session is None:
            logger.error("No active session. Run `contree use IMAGE` first.")
            return 1
        image_uuid = session.current_image

    if args.delete:
        client.delete(f"/v1/images/{image_uuid}/tag?tag={args.tag}")
        logger.info("Removed tag %r from image %s", args.tag, image_uuid)
        return None

    client.patch_json(f"/v1/images/{image_uuid}/tag", {"tag": args.tag})
    logger.info("Tagged image %s as %s", image_uuid, args.tag)
    return None
