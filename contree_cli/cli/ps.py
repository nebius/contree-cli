"""List operations (running and completed instances, image imports).

By default shows only active operations (PENDING, ASSIGNED, EXECUTING).
Use -a/--all to include completed ones, or -S/--status to filter by a
specific status. Use -K/--kind to filter by operation type.

Use -q/--quiet to print only UUIDs, useful for scripting.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.output import OutputFormatter
from contree_cli.types import FLAGS, isoformat_datetime, parse_datetime, parse_interval

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
ACTIVE_STATUSES = frozenset({"PENDING", "ASSIGNED", "EXECUTING"})

EPILOG = """\
for coding agents:
  read-only command
  default view is active operations only; use --all for full history
  use -q for UUID-only output in scripts
"""


@dataclass(frozen=True)
class PsArgs(ArgumentsProtocol):
    quiet: bool = False
    all: bool = False
    show_max: int | None = None
    status: str | None = None
    kind: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> PsArgs:
        return cls(
            quiet=ns.quiet,
            all=getattr(ns, "all", False),
            show_max=ns.show_max,
            status=ns.status,
            kind=ns.kind,
            since=ns.since,
            until=ns.until,
        )


STATUS_CHOICES = {
    "P": "PENDING",
    "A": "ASSIGNED",
    "E": "EXECUTING",
    "S": "SUCCESS",
    "F": "FAILED",
    "C": "CANCELLED",
}


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        *FLAGS["quiet"],
        action="store_true",
        help="Only show UUIDs, useful for scripting",
    )
    p.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Show all operations (default: active only)",
    )
    p.add_argument(
        *FLAGS["status"],
        choices=tuple(itertools.chain.from_iterable(STATUS_CHOICES.items())),
        default=None,
        help="Filter by status (default: EXECUTING only, unless -a is used)",
    )
    p.add_argument(
        *FLAGS["kind"],
        choices=("image_import", "instance"),
        help="Filter by operation kind",
    )
    p.add_argument(
        *FLAGS["since"],
        type=parse_interval,
        help=str(parse_interval.__doc__),
    )
    p.add_argument(
        *FLAGS["until"],
        type=parse_interval,
        help="Show operations before. " + str(parse_interval.__doc__),
    )

    p.add_argument(
        *FLAGS["show_max"],
        type=int,
        default=1000,
        help=(
            "Show at most this many operations, useful"
            " for --all with large history (default: 1000)"
        ),
    )

    return cmd_ps, PsArgs


def _emit_op(formatter: OutputFormatter, op: dict[str, Any], *, quiet: bool) -> None:
    row = dict(
        uuid=op["uuid"],
        status=op["status"],
        kind=op["kind"],
        created_at=parse_datetime(op["created_at"]),
        duration=timedelta(seconds=op["duration"])
        if op.get("duration") is not None
        else None,
        error=op.get("error") or "",
    )
    if quiet:
        print(row["uuid"])
    else:
        formatter(**row)


def cmd_ps(args: PsArgs) -> None:
    formatter: OutputFormatter = FORMATTER.get()
    client = CLIENT.get()

    status: str | None = None
    if args.status is not None:
        if len(args.status) == 1:
            status = STATUS_CHOICES.get(args.status, args.status)
        else:
            status = args.status
    elif not args.all:
        status = "EXECUTING"

    base_params: dict[str, str] = {}
    if status:
        base_params["status"] = status
    if args.kind:
        base_params["kind"] = args.kind
    if args.since is not None:
        base_params["since"] = isoformat_datetime(args.since)
    if args.until is not None:
        base_params["until"] = isoformat_datetime(args.until)

    limit = args.show_max
    offset = 0
    emitted = 0
    hit_limit = False

    while limit is None or emitted < limit:
        page_size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - emitted)
        params = {
            **base_params,
            "offset": str(offset),
            "limit": str(page_size),
        }
        resp = client.get("/v1/operations", params=params)
        operations = json.loads(resp.read())
        if not operations:
            return
        for op in operations:
            if limit is not None and emitted >= limit:
                hit_limit = True
                break
            _emit_op(formatter, op, quiet=args.quiet)
            emitted += 1
        if hit_limit:
            break
        if len(operations) < page_size:
            return
        offset += len(operations)
        if limit is None or emitted < limit:
            logger.info(
                "Fetched %d operations so far... (press Ctrl+C to break)",
                emitted,
            )

    if limit is None:
        return

    # Hit the limit. Probe one extra record (offset=emitted, limit=1) to
    # detect truncation without re-fetching a full page.
    probe_params = {**base_params, "offset": str(emitted), "limit": "1"}
    resp = client.get("/v1/operations", params=probe_params)
    operations = json.loads(resp.read())
    if operations:
        formatter.flush()
        logger.warning(
            "Output truncated at --show-max=%d operations; more results"
            " are available. Raise --show-max or filter with"
            " --status/--kind/--since/--until.",
            limit,
        )
