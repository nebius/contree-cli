"""Manage operations (list, inspect, cancel).

Aggregates ps/show/kill under a single namespace, and adds multi-UUID
support to ``show`` and ``cancel`` so several operations can be acted
on in one invocation.

Subcommands:
  list (ls)             List operations. Same flags as `contree ps`.
  show UUID [UUID...]   Show one or more operation results.
  cancel UUID [UUID...] Cancel one or more operations (or --all).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field

from contree_cli import CLIENT, ArgumentsProtocol, SetupResult
from contree_cli.cli import kill as kill_module
from contree_cli.cli import ps as ps_module
from contree_cli.cli.show import ShowArgs, cmd_show
from contree_cli.client import ApiError
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  list/show are read-only; cancel mutates remote state
  show and cancel accept multiple UUIDs in one invocation
  show supports @N session-history references inherited from `contree show`
"""


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


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    sub = p.add_subparsers(dest="operation_action", required=True)

    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List operations",
        description=("List operations. Accepts the same flags as `contree ps`."),
        epilog="for coding agents: read-only command",
    )
    list_handler, list_loader = ps_module.setup_parser(list_p)
    list_p.set_defaults(handler=list_handler, load_args=list_loader)

    show_p = sub.add_parser(
        "show",
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
    cancel_p.add_argument(
        "uuids",
        nargs="*",
        metavar="UUID",
        help="Operation UUIDs to cancel",
    )
    cancel_p.add_argument(
        *FLAGS["all"],
        action="store_true",
        help="Cancel every active operation",
    )
    cancel_p.set_defaults(handler=cmd_cancel, load_args=CancelArgs)

    return cmd_show_multi, ShowMultiArgs


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
        uuids = kill_module._list_active(client)
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
