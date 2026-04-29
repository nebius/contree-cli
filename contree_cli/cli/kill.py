"""Cancel a running operation.

Sends a DELETE request to stop the specified operation. Use --all to
cancel every active operation (PENDING, ASSIGNED, EXECUTING) in one go.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass

from contree_cli import CLIENT, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
ACTIVE_STATUSES = ("PENDING", "ASSIGNED", "EXECUTING")

EPILOG = """\
for coding agents:
  mutating command
  use UUID to cancel one operation, or --all to cancel all active ones
"""


@dataclass(frozen=True)
class KillArgs(ArgumentsProtocol):
    uuid: str | None = None
    all: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> KillArgs:
        return cls(uuid=ns.uuid, all=ns.all)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("uuid", nargs="?", help="Operation UUID")
    target.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Cancel all active operations",
    )
    return cmd_kill, KillArgs


def _list_active(client: object) -> list[str]:
    """Collect UUIDs of all active operations."""
    from contree_cli.client import ContreeClient

    assert isinstance(client, ContreeClient)
    uuids: list[str] = []
    for status in ACTIVE_STATUSES:
        offset = 0
        while True:
            params = {
                "status": status,
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
            }
            resp = client.get("/v1/operations", params=params)
            operations = json.loads(resp.read())
            if not operations:
                break
            uuids.extend(op["uuid"] for op in operations)
            if len(operations) < PAGE_SIZE:
                break
            offset += len(operations)
    return uuids


def cmd_kill(args: KillArgs) -> int | None:
    client = CLIENT.get()

    if args.all:
        uuids = _list_active(client)
        if not uuids:
            logger.info("No active operations to cancel")
            return None
        failed = 0
        for uuid in uuids:
            try:
                client.delete(f"/v1/operations/{uuid}")
                logger.info("Cancelled operation %s", uuid)
            except ApiError as exc:
                logger.error("Failed to cancel %s: %s", uuid, exc)
                failed += 1
        return 1 if failed else None

    client.delete(f"/v1/operations/{args.uuid}")
    logger.info("Cancelled operation %s", args.uuid)
    return None
