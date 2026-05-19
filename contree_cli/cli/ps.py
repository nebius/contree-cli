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
from datetime import datetime

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.client import PaginatedFetcher
from contree_cli.output import OutputFormatter
from contree_cli.session import CONTREE_CONCURRENCY
from contree_cli.types import (
    FLAGS,
    isoformat_datetime,
    parse_interval,
    positive_int,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
# Hard cap on pages fetched when --show-max is omitted (1M operations).
UNLIMITED_PAGE_CAP = 1000
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
        type=positive_int,
        default=1000,
        help=(
            "Show at most this many operations, useful"
            " for --all with large history (default: 1000)"
        ),
    )

    return cmd_ps, PsArgs


def cmd_ps(args: PsArgs) -> None:
    formatter: OutputFormatter = FORMATTER.get()
    formatter.configure(tail=("error",))
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
    # +1 page so we can detect "more results exist" beyond the limit.
    max_pages = limit // PAGE_SIZE + 2 if limit is not None else UNLIMITED_PAGE_CAP

    fetcher = PaginatedFetcher(
        client,
        "/v1/operations",
        base_params,
        json.loads,
        page_size=PAGE_SIZE,
        max_pages=max_pages,
        concurrency=CONTREE_CONCURRENCY,
    )

    emitted = 0
    hit_limit = False
    for page in fetcher:
        for op in page:
            if limit is not None and emitted >= limit:
                hit_limit = True
                break
            if args.quiet:
                print(op["uuid"])
            else:
                formatter(**op)
            emitted += 1
        formatter.flush()
        if hit_limit:
            fetcher.stop()
            break

    if hit_limit:
        logger.warning(
            "Output truncated at --show-max=%d operations; more results"
            " are available. Raise --show-max or filter with"
            " --status/--kind/--since/--until.",
            limit,
        )
