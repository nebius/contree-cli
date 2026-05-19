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
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.cli.show import ShowArgs, cmd_show
from contree_cli.client import ApiError, ContreeClient, PaginatedFetcher
from contree_cli.output import OutputFormatter
from contree_cli.refs import (
    history_spec_from_ref,
    looks_like_history_ref,
    resolve_operation_uuids,
)
from contree_cli.session import CONTREE_CONCURRENCY
from contree_cli.types import (
    FLAGS,
    isoformat_datetime,
    parse_interval,
    positive_int,
)

# Re-exported for backwards compatibility with code/tests that historically
# pulled these helpers from `contree_cli.cli.operation`.
__all__ = [
    "history_spec_from_ref",
    "looks_like_history_ref",
    "resolve_operation_uuids",
]

logger = logging.getLogger(__name__)

PAGE_SIZE = PaginatedFetcher.DEFAULT_PAGE_SIZE

ACTIVE_STATUSES = frozenset({"PENDING", "ASSIGNED", "EXECUTING"})
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
WAIT_TIMEOUT_DEFAULT = 60
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
    raw: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShowMultiArgs:
        return cls(
            uuids=resolve_operation_uuids(list(ns.uuids)),
            raw=getattr(ns, "raw", False),
        )


@dataclass(frozen=True)
class CancelArgs(ArgumentsProtocol):
    uuids: list[str] = field(default_factory=list)
    all: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CancelArgs:
        return cls(uuids=resolve_operation_uuids(list(ns.uuids or [])), all=ns.all)


@dataclass(frozen=True)
class WaitArgs(ArgumentsProtocol):
    uuids: list[str] = field(default_factory=list)
    all: bool = False
    timeout: int = WAIT_TIMEOUT_DEFAULT

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> WaitArgs:
        return cls(
            uuids=resolve_operation_uuids(list(ns.uuids or [])),
            all=ns.all,
            timeout=ns.timeout,
        )


def setup_cancel_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Configure the cancel parser used by both `operation cancel` and `kill`."""
    p.add_argument(
        "uuids",
        nargs="*",
        metavar="UUID_OR_REF",
        help=(
            "Operations to cancel. Accepts UUIDs and session-history "
            "references (HEAD, HEAD~N, @, @N, @-N, @+N, :N, bare N)."
        ),
    )
    p.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Cancel every active operation",
    )
    return cmd_cancel, CancelArgs


def setup_show_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Configure the show parser used by both `operation show` and `show`."""
    p.add_argument(
        "uuids",
        nargs="+",
        metavar="UUID_OR_REF",
        help=(
            "Operations to inspect. Accepts UUIDs and session-history "
            "references: @ or HEAD for the active branch tip, @N for "
            "an absolute history id, @-N or HEAD~N for N steps back, "
            "@+N for N steps forward."
        ),
    )
    p.add_argument(
        *FLAGS["raw"],
        action="store_true",
        help=(
            "Print each operation's full server payload as JSONL "
            "(one JSON object per line) to stdout, verbatim. Skips "
            "formatter routing and derived columns; streams cleanly "
            "into `jq -c`. Useful for debugging or for fields the "
            "table view omits."
        ),
    )
    return cmd_show_multi, ShowMultiArgs


def setup_wait_parser(p: argparse.ArgumentParser) -> SetupResult:
    """Configure the wait parser for `operation wait`."""
    p.add_argument(
        "uuids",
        nargs="*",
        metavar="UUID_OR_REF",
        help=(
            "Operations to wait for. Accepts UUIDs and session-history "
            "references (HEAD, HEAD~N, @, @N, @-N, @+N, :N, bare N)."
        ),
    )
    p.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Wait for every active operation",
    )
    p.add_argument(
        *FLAGS["timeout"],
        type=positive_int,
        default=WAIT_TIMEOUT_DEFAULT,
        help=(
            "Fail with exit code 1 if not all operations reach a terminal"
            f" status within this many seconds (default: {WAIT_TIMEOUT_DEFAULT})"
        ),
    )
    return cmd_wait, WaitArgs


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
    show_handler, show_loader = setup_show_parser(show_p)
    show_p.set_defaults(handler=show_handler, load_args=show_loader)

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

    wait_p = sub.add_parser(
        "wait",
        aliases=["w"],
        help="Wait for operations to reach a terminal status",
        description=(
            "Poll the given operations until each reaches a terminal "
            "status (SUCCESS, FAILED, CANCELLED) and print one row per "
            "completion. With --all, waits for every currently active "
            "operation (PENDING, ASSIGNED, EXECUTING)."
        ),
        epilog=(
            "for coding agents:\n"
            "  read-only command (polls the API; no state mutation)\n"
            "  fails with exit code 1 if --timeout is hit before all complete\n"
            "  exit code 1 also when any operation finished non-SUCCESS"
        ),
    )
    wait_handler, wait_loader = setup_wait_parser(wait_p)
    wait_p.set_defaults(handler=wait_handler, load_args=wait_loader)

    return cmd_show_multi, ShowMultiArgs


CANCEL_ACTIVE_PAGE_SIZE = 100


def extract_exit_code(op: dict[str, Any]) -> int | None:
    """Pull the sandbox-process exit code out of an operation payload.

    Operation ``status`` reflects orchestration -- whether the API ran the
    job to completion -- and is left as-is. The sandbox process's own
    exit code lives in ``metadata.result.state.exit_code`` (newer API
    shape) or ``result.exit_code`` (older shape); this helper returns
    whichever is present, or ``None`` if neither.
    """
    metadata = op.get("metadata") or {}
    instance_result = metadata.get("result") if isinstance(metadata, dict) else None
    state = instance_result.get("state") if isinstance(instance_result, dict) else None
    raw = state.get("exit_code") if isinstance(state, dict) else None
    if raw is None:
        result = op.get("result")
        raw = result.get("exit_code") if isinstance(result, dict) else None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


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
    emitted = 0
    hit_limit = False
    with PaginatedFetcher(
        client,
        "/v1/operations",
        base_params,
        json.loads,
        limit=limit,
        concurrency=CONTREE_CONCURRENCY,
    ) as fetcher:
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
            result = cmd_show(ShowArgs(uuid=uuid, raw=args.raw))
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


def cmd_wait(args: WaitArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    # Pin uuid/status/exit_code/timed_out/duration up front for the typical
    # eyeball scan ("did it finish? how long? what was the exit?"); `error`
    # stays in the trailing slot.
    formatter.configure(
        head=("uuid", "status", "exit_code", "timed_out", "duration"),
        tail=("error",),
    )

    if args.all:
        if args.uuids:
            logger.warning("--all overrides explicit UUIDs; waiting for all active")
        uuids = list_active(client)
        if not uuids:
            logger.info("No active operations to wait for")
            return None
    else:
        if not args.uuids:
            logger.error("Provide at least one UUID, or use --all")
            return 1
        uuids = list(args.uuids)

    deadline = time.monotonic() + args.timeout
    pending = set(uuids)
    exit_status = 0
    sleep_time = 0.5

    while pending and time.monotonic() < deadline:
        for uuid in list(pending):
            resp = client.get(f"/v1/operations/{uuid}")
            op = json.loads(resp.read())
            if op.get("status") in TERMINAL_STATUSES:
                pending.discard(uuid)
                exit_code = extract_exit_code(op)
                formatter(
                    **{
                        **op,
                        "exit_code": exit_code,
                        "timed_out": False,
                    }
                )
                # The operation status reflects orchestration (did the job
                # run?), not what the sandbox process did with its exit
                # code. Both feed the CLI's own exit status independently:
                # non-SUCCESS ops fail the wait; SUCCESS ops with non-zero
                # exit codes propagate that code so `op wait && next` does
                # the right thing.
                if op.get("status") != "SUCCESS":
                    exit_status = max(exit_status, 1)
                if exit_code is not None and exit_code != 0:
                    exit_status = max(exit_status, exit_code)
        formatter.flush()
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(sleep_time, max(remaining, 0.0)))
        sleep_time = min(5.0, sleep_time * 2)

    # Anything still pending after the deadline timed out; emit one last
    # row per UUID with its observed non-terminal status so the user sees
    # what state each operation was stuck in.
    for uuid in sorted(pending):
        try:
            resp = client.get(f"/v1/operations/{uuid}")
            op = json.loads(resp.read())
        except ApiError as exc:
            logger.error("Failed to fetch %s: %s", uuid, exc)
            continue
        formatter(**{**op, "timed_out": True})
    if pending:
        formatter.flush()
        logger.warning(
            "Timeout: %d operation(s) did not finish in %ds",
            len(pending),
            args.timeout,
        )
        exit_status = max(exit_status, 1)

    return exit_status or None
