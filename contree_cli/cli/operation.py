"""Manage operations (list, inspect, cancel).

Aggregates ps/show/kill under a single namespace, and adds multi-UUID
support to ``show`` and ``cancel`` so several operations can be acted
on in one invocation.

Subcommands:
  list (ls)             List operations. ``contree ps`` is an alias.
  show UUID [UUID...]   Show one or more operation results.
  cancel UUID [UUID...] Cancel one or more operations (or --all).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.cli.show import ShowArgs, cmd_show
from contree_cli.client import ApiError, ContreeClient, PaginatedFetcher
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
STATUS_CHOICES = {
    "P": "PENDING",
    "A": "ASSIGNED",
    "E": "EXECUTING",
    "S": "SUCCESS",
    "F": "FAILED",
    "C": "CANCELLED",
}

EPILOG = """\
for coding agents:
  list/show are read-only; cancel mutates remote state
  show and cancel accept multiple UUIDs in one invocation
  show supports @N session-history references inherited from `contree show`
"""


@dataclass(frozen=True)
class ListArgs(ArgumentsProtocol):
    quiet: bool = False
    all: bool = False
    show_max: int | None = None
    status: str | None = None
    kind: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ListArgs:
        return cls(
            quiet=ns.quiet,
            all=getattr(ns, "all", False),
            show_max=ns.show_max,
            status=ns.status,
            kind=ns.kind,
            since=ns.since,
            until=ns.until,
        )


@dataclass(frozen=True)
class ShowMultiArgs(ArgumentsProtocol):
    uuids: list[str] = field(default_factory=list)

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShowMultiArgs:
        return cls(uuids=list(ns.uuids))


@dataclass(frozen=True)
class CancelArgs(ArgumentsProtocol):
    uuids: list[str] = field(default_factory=list)
    all: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CancelArgs:
        return cls(uuids=list(ns.uuids or []), all=ns.all)


def setup_cancel_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Configure the cancel parser used by both `operation cancel` and `kill`."""
    p.add_argument(
        "uuids",
        nargs="*",
        metavar="UUID",
        help="Operation UUIDs to cancel",
    )
    p.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Cancel every active operation",
    )
    return cmd_cancel, CancelArgs


def setup_list_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Configure the listing parser used by both `operation ls` and `ps`."""
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
    return cmd_list, ListArgs


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    sub = p.add_subparsers(dest="operation_action", required=True)

    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List operations",
        description=("List operations. ``contree ps`` is an alias of this command."),
        epilog="for coding agents: read-only command",
    )
    list_handler, list_loader = setup_list_parser(list_p)
    list_p.set_defaults(handler=list_handler, load_args=list_loader)

    show_p = sub.add_parser(
        "show",
        aliases=["sh"],
        help="Show one or more operation results",
        description=(
            "Fetch and display the result of each given operation. "
            "Same per-UUID behaviour as `contree show`: terminal results "
            "are cached; @N references resolve against session history."
        ),
        epilog=(
            "for coding agents:\n"
            "  read-only command\n"
            "  accepts multiple UUIDs; each rendered as its own row"
        ),
    )
    show_p.add_argument(
        "uuids",
        nargs="+",
        metavar="UUID",
        help="One or more operation UUIDs (or @N history references)",
    )
    show_p.set_defaults(handler=cmd_show_multi, load_args=ShowMultiArgs)

    cancel_p = sub.add_parser(
        "cancel",
        aliases=["kill", "k"],
        help="Cancel one or more operations",
        description=(
            "Cancel each given operation. With --all, cancels every active "
            "operation (PENDING, ASSIGNED, EXECUTING)."
        ),
        epilog=(
            "for coding agents:\n"
            "  mutating command\n"
            "  pass UUIDs to cancel specific operations or --all for everything"
        ),
    )
    cancel_handler, cancel_loader = setup_cancel_parser(cancel_p)
    cancel_p.set_defaults(handler=cancel_handler, load_args=cancel_loader)

    return cmd_show_multi, ShowMultiArgs


CANCEL_ACTIVE_PAGE_SIZE = 100


def list_active(client: ContreeClient) -> list[str]:
    """Collect UUIDs of all active (PENDING/ASSIGNED/EXECUTING) operations."""
    uuids: list[str] = []
    for status in ACTIVE_STATUSES:
        offset = 0
        while True:
            params = {
                "status": status,
                "limit": str(CANCEL_ACTIVE_PAGE_SIZE),
                "offset": str(offset),
            }
            resp = client.get("/v1/operations", params=params)
            operations = json.loads(resp.read())
            if not operations:
                break
            uuids.extend(op["uuid"] for op in operations)
            if len(operations) < CANCEL_ACTIVE_PAGE_SIZE:
                break
            offset += len(operations)
    return uuids


def cmd_list(args: ListArgs) -> None:
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


def cmd_show_multi(args: ShowMultiArgs) -> int | None:
    exit_code = 0
    for uuid in args.uuids:
        try:
            result = cmd_show(ShowArgs(uuid=uuid))
        except ApiError as exc:
            logger.error("Failed to fetch %s: %s", uuid, exc)
            exit_code = max(exit_code, 1)
            continue
        if isinstance(result, int) and result:
            exit_code = max(exit_code, result)
    return exit_code or None


def cmd_cancel(args: CancelArgs) -> int | None:
    client = CLIENT.get()

    if args.all:
        if args.uuids:
            logger.warning("--all overrides explicit UUIDs; cancelling all active")
        uuids = list_active(client)
        if not uuids:
            logger.info("No active operations to cancel")
            return None
    else:
        if not args.uuids:
            logger.error("Provide at least one UUID, or use --all")
            return 1
        uuids = args.uuids

    failed = 0
    for uuid in uuids:
        try:
            client.delete(f"/v1/operations/{uuid}")
            logger.info("Cancelled operation %s", uuid)
        except ApiError as exc:
            logger.error("Failed to cancel %s: %s", uuid, exc)
            failed += 1
    return 1 if failed else None
