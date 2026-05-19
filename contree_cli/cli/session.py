"""Manage session branches and history.

Without a subcommand, shows the current session info (key, branch,
image, last operation).

Subcommands:
  list (ls)       List all sessions
  use KEY         Import another session's current image
  branch (br)     List or create branches (--from to fork)
  checkout (co)   Switch active branch
  rollback (rb)   Navigate history: N=absolute, -N=back, +N=forward
  show            Display the session history DAG
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.output import DefaultFormatter
from contree_cli.types import FLAGS, parse_datetime, parse_interval

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  session (no subcommand) is read-only
  branch/checkout/rollback/session use mutates local session pointers
  `session show` defaults to last 20 history entries; pass -a/--all for full DAG
  use `session show` to inspect history DAG before destructive navigation
  `session wait [OPS...]` waits for active or specified operations
"""

WAIT_TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
ACTIVE_STATUSES = frozenset({"PENDING", "ASSIGNED", "EXECUTING"})


@dataclass(frozen=True)
class SessionInfoArgs(ArgumentsProtocol):
    session_name: str | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SessionInfoArgs:
        return cls(session_name=getattr(ns, "session_name", None))


@dataclass(frozen=True)
class ListArgs(ArgumentsProtocol):
    filter_text: str | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ListArgs:
        return cls(filter_text=getattr(ns, "filter_text", None))


@dataclass(frozen=True)
class UseSessionArgs(ArgumentsProtocol):
    name: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> UseSessionArgs:
        return cls(name=ns.session_name)


@dataclass(frozen=True)
class BranchArgs(ArgumentsProtocol):
    name: str | None
    from_branch: str | None
    delete: bool = False
    prune: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> BranchArgs:
        return cls(
            name=ns.branch_name,
            from_branch=ns.from_branch,
            delete=ns.delete,
            prune=ns.prune,
        )


@dataclass(frozen=True)
class RollbackArgs(ArgumentsProtocol):
    target: int
    forward: int

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> RollbackArgs:
        raw: str = ns.target
        if raw.startswith("+"):
            return cls(target=0, forward=int(raw[1:]))
        return cls(target=int(raw), forward=0)


@dataclass(frozen=True)
class CheckoutArgs(ArgumentsProtocol):
    name: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> CheckoutArgs:
        return cls(name=ns.checkout_branch)


@dataclass(frozen=True)
class ShowArgs(ArgumentsProtocol):
    all_entries: bool
    session_name: str | None = None
    kind: str | None = None
    last: int | None = None
    since: datetime | None = None
    until: datetime | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShowArgs:
        return cls(
            all_entries=ns.all_entries,
            session_name=getattr(ns, "session_name", None),
            kind=getattr(ns, "kind", None),
            last=getattr(ns, "last", None),
            since=getattr(ns, "since", None),
            until=getattr(ns, "until", None),
        )


@dataclass(frozen=True)
class WaitArgs(ArgumentsProtocol):
    op_ids: list[str]

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> WaitArgs:
        return cls(op_ids=ns.op_ids)


@dataclass(frozen=True)
class DeleteArgs(ArgumentsProtocol):
    keys: list[str]
    force: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> DeleteArgs:
        return cls(keys=ns.keys, force=ns.force)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    sub = p.add_subparsers(dest="session_action")

    # session list
    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List all sessions",
        description="List locally known sessions and their current branch/image.",
        epilog="for coding agents: read-only command",
    )
    list_p.add_argument(
        *FLAGS["filter"],
        dest="filter_text",
        default=None,
        help="Filter session keys containing this text",
    )
    list_p.set_defaults(handler=cmd_list, load_args=ListArgs)

    # session use SESSION
    use_p = sub.add_parser(
        "use",
        help="Import another session's image",
        description=(
            "Set current session image to another session's tip image. "
            "Accepts exact key or key suffix."
        ),
        epilog="for coding agents: mutates current session history",
    )
    use_p.add_argument("session_name", help="Session key or suffix to match")
    use_p.set_defaults(handler=cmd_use_session, load_args=UseSessionArgs)

    # session branch [NAME] [--from BRANCH]
    branch_p = sub.add_parser(
        "branch",
        aliases=["br"],
        help="List, create, delete, or prune branches",
        description=(
            "List branches (no args). Create with NAME (optionally --from). "
            "Delete with --delete NAME. Prune disposable-/detached- branches "
            "with --prune."
        ),
        epilog=(
            "for coding agents:\n"
            "  read-only when NAME/--delete/--prune omitted\n"
            "  mutating when creating/deleting/pruning"
        ),
    )
    branch_p.add_argument(
        "branch_name",
        nargs="?",
        default=None,
        help="Branch name (create/delete target)",
    )
    branch_p.add_argument(
        *FLAGS["from_branch"],
        dest="from_branch",
        default=None,
        help="Source branch (default: active branch)",
    )
    branch_p.add_argument(
        *FLAGS["delete"],
        action="store_true",
        dest="delete",
        default=False,
        help="Delete the specified branch (NAME required, must not be active)",
    )
    branch_p.add_argument(
        *FLAGS["prune"],
        action="store_true",
        dest="prune",
        help="Prune disposable-/detached- branches (non-active only)",
    )
    branch_p.set_defaults(handler=cmd_branch, load_args=BranchArgs)

    # session checkout BRANCH
    co_p = sub.add_parser(
        "checkout",
        aliases=["co"],
        help="Switch active branch",
        description="Move current session to another existing branch tip.",
        epilog="for coding agents: mutates active branch pointer",
    )
    co_p.add_argument("checkout_branch", help="Branch to switch to")
    co_p.set_defaults(
        handler=cmd_checkout,
        load_args=CheckoutArgs,
    )

    # session rollback [TARGET]
    rollback_p = sub.add_parser(
        "rollback",
        aliases=["rb"],
        help="Navigate history: N=absolute, -N=back, +N=forward",
        description=(
            "Move branch pointer in session history. Supports absolute ID, "
            "relative backward (-N), and forward (+N)."
        ),
        epilog="for coding agents: mutates active branch history pointer",
    )
    rollback_p.add_argument(
        "target",
        nargs="?",
        type=str,
        default="-1",
        help="History target: ID (absolute), -N (back), +N (forward)"
        " — use `-- -N` for negative values",
    )
    rollback_p.set_defaults(handler=cmd_rollback, load_args=RollbackArgs)

    # session show
    show_p = sub.add_parser(
        "show",
        help="Show session history",
        description=(
            "Print session history DAG entries and branch labels. "
            "By default shows last 20 entries; use -a/--all for full history."
        ),
        epilog="for coding agents: read-only command",
    )
    show_p.add_argument(
        "session_name",
        nargs="?",
        default=None,
        help="Session key or suffix (default: current session)",
    )
    show_p.add_argument(
        *FLAGS["all"],
        action="store_true",
        dest="all_entries",
        help="Show full history (default: last 20 entries)",
    )
    show_p.add_argument(
        *FLAGS["kind"],
        dest="kind",
        default=None,
        help="Filter history entries by kind (e.g., run, use, cd)",
    )
    show_p.add_argument(
        *FLAGS["last"],
        dest="last",
        type=int,
        default=None,
        help="Show last N entries after filtering",
    )
    show_p.add_argument(
        *FLAGS["since"],
        dest="since",
        default=None,
        type=parse_interval,
        help="Show entries since. " + str(parse_interval.__doc__),
    )
    show_p.add_argument(
        *FLAGS["until"],
        dest="until",
        default=None,
        type=parse_interval,
        help="Show entries before. " + str(parse_interval.__doc__),
    )
    show_p.set_defaults(handler=cmd_show, load_args=ShowArgs)

    wait_p = sub.add_parser(
        "wait",
        help="Wait for operations to reach terminal state",
        description=(
            "Wait for specific operations (by UUID). Without arguments, waits for "
            "active operations of the current session only."
        ),
        epilog="for coding agents: read-only command",
    )
    wait_p.add_argument(
        "op_ids",
        nargs="*",
        help="Operation UUIDs to wait for (default: all active operations)",
    )
    wait_p.set_defaults(handler=cmd_wait, load_args=WaitArgs)

    delete_p = sub.add_parser(
        "delete",
        aliases=["rm", "del"],
        help="Delete sessions by key",
    )
    delete_p.add_argument(
        "keys", nargs="+", metavar="KEY", help="Session keys to delete"
    )
    delete_p.add_argument(
        *FLAGS["force"], action="store_true", help="Do not ask for confirmation"
    )
    delete_p.set_defaults(handler=cmd_delete, load_args=DeleteArgs)

    return cmd_session_info, SessionInfoArgs


def cmd_session_info(args: SessionInfoArgs) -> int | None:
    store = SESSION_STORE.get()
    formatter = FORMATTER.get()

    if args.session_name:
        try:
            target = store.find_session(args.session_name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        formatter(
            session_key=target.session_key,
            active_branch=target.active_branch,
            current_image=target.current_image,
            last_kind=target.last_kind,
            last_title=target.last_title,
            updated_at=target.updated_at,
        )
        return None

    session = store.session
    if session is None:
        print(
            "No active session. Run `contree use IMAGE` to start one.\n"
            "Agents: read `contree agent` for workflow and set a session first.",
            file=sys.stderr,
        )
        return 1

    formatter(
        session_key=session.session_key,
        active_branch=session.active_branch,
        current_image=session.current_image,
        last_kind=session.last_kind,
        last_title=session.last_title,
        updated_at=session.updated_at,
    )
    return None


def cmd_list(args: ListArgs) -> int | None:
    store = SESSION_STORE.get()
    sessions = store.list_sessions()
    if args.filter_text:
        sessions = [s for s in sessions if args.filter_text in s.session_key]
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return None
    formatter = FORMATTER.get()
    current_key = store.session_key
    for s in sessions:
        active = s.session_key == current_key
        formatter(
            active=active,
            session_key=s.session_key,
            active_branch=s.active_branch,
            current_image=s.current_image,
            updated_at=s.updated_at,
        )
    return None


def cmd_use_session(args: UseSessionArgs) -> int | None:
    store = SESSION_STORE.get()
    try:
        target = store.find_session(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    store.set_image(
        target.current_image,
        kind="session use",
        title=target.session_key,
    )
    formatter = FORMATTER.get()
    formatter(
        image=target.current_image,
        from_session=target.session_key,
    )
    return None


def cmd_branch(args: BranchArgs) -> int | None:
    store = SESSION_STORE.get()
    if args.name is None and not args.delete and not args.prune:
        # List branches
        branches = store.list_branches()
        if not branches:
            print("No branches (no active session).", file=sys.stderr)
            return None
        formatter = FORMATTER.get()
        for name, is_active in branches:
            if isinstance(formatter, DefaultFormatter):
                marker = "* " if is_active else "  "
                formatter(marker=marker, branch=name)
            else:
                formatter(branch=name, active=is_active)
        return None

    if args.delete:
        if not args.name:
            print("--delete requires a branch NAME", file=sys.stderr)
            return 1
        try:
            store.delete_branch(args.name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Deleted branch {args.name!r}")
        return None

    if args.prune:
        removed = store.prune_branches()
        if removed:
            for name in removed:
                print(f"Pruned branch {name!r}")
        else:
            print("No disposable/detached branches to prune.")
        return None

    if not args.name:
        print("Branch NAME is required (or use --delete/--prune)", file=sys.stderr)
        return 1

    # Create branch
    try:
        store.create_branch(args.name, args.from_branch)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Created branch {args.name!r}")
    return None


def cmd_checkout(args: CheckoutArgs) -> int | None:
    store = SESSION_STORE.get()
    try:
        entry = store.switch_branch(args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    formatter = FORMATTER.get()
    formatter(
        branch=args.name,
        image=entry.image_uuid,
    )
    return None


def cmd_rollback(args: RollbackArgs) -> int | None:
    store = SESSION_STORE.get()
    try:
        if args.forward > 0:
            entry = store.navigate_forward(args.forward)
        else:
            entry = store.navigate(args.target)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    formatter = FORMATTER.get()
    formatter(
        image=entry.image_uuid,
        kind=entry.kind,
        title=entry.title,
        created_at=entry.created_at,
    )
    return None


def cmd_show(args: ShowArgs) -> int | None:
    store = SESSION_STORE.get()
    if args.session_name:
        try:
            _ = store.find_session(args.session_name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        entries, branch_map = store.history_dag_for(args.session_name)
    else:
        entries, branch_map = store.history_dag()
    if args.kind is not None:
        entries = [
            e
            for e in entries
            if e.kind == args.kind or e.kind.startswith(f"{args.kind}-")
        ]

    def _parse_ts(value: str) -> datetime:
        try:
            return parse_datetime(value)
        except Exception:
            try:
                return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

    def _parse_filter(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value
        try:
            return parse_datetime(value)
        except Exception:
            unit = value[-1]
            num_part = value[:-1]
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            seconds = multipliers.get(unit)
            if seconds and num_part.isdigit():
                return datetime.now(timezone.utc) - timedelta(
                    seconds=int(num_part) * seconds
                )
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    if args.since:
        since_dt = _parse_filter(args.since)
        entries = [e for e in entries if _parse_ts(e.created_at) >= since_dt]
    if args.until:
        until_dt = _parse_filter(args.until)
        entries = [e for e in entries if _parse_ts(e.created_at) <= until_dt]

    if not entries:
        print("No history.", file=sys.stderr)
        return None

    if args.last is not None and args.last > 0:
        entries = entries[-args.last :]

    visible_entries = entries if args.all_entries else entries[-20:]
    if not args.all_entries and len(entries) > len(visible_entries):
        logger.info(
            "Showing last %s of %s history entries. "
            "Use `contree session show --all` for full history.",
            len(visible_entries),
            len(entries),
        )

    formatter = FORMATTER.get()
    for entry in visible_entries:
        branches = branch_map.get(entry.id, [])
        formatter(
            id=entry.id,
            image=entry.image_uuid,
            parent_id=entry.parent_id,
            kind=entry.kind,
            title=entry.title,
            operation=entry.operation_uuid,
            created_at=entry.created_at,
            branches=", ".join(branches) if branches else "",
        )
    return None


def cmd_wait(args: WaitArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("error",))

    store = SESSION_STORE.get()
    op_ids = list(args.op_ids)
    pending_key: tuple[str, str] | None = None
    pending_ops: set[str] = set()

    if not op_ids:
        session = store.session
        if session is None:
            print(
                "No active session. Run `contree use IMAGE` to start one.\n"
                "Agents: read `contree agent` for workflow and set a session first.",
                file=sys.stderr,
            )
            return 1

        pending_key = ("", f"ops:{session.session_key}")
        cached = store.cache.get(pending_key)
        pending_meta: dict[str, dict[str, object]] = {}
        if isinstance(cached, list):
            for item in cached:
                if isinstance(item, dict) and "op" in item:
                    op_id = str(item.get("op", ""))
                    pending_meta[op_id] = {
                        "title": str(item.get("title", "")),
                        "disposable": bool(item.get("disposable", False)),
                        "branch": str(item.get("branch", "")),
                    }
                elif isinstance(item, str):
                    pending_meta[item] = {
                        "title": "",
                        "disposable": False,
                        "branch": "",
                    }
        pending_ops = set(pending_meta)

        if pending_ops:
            op_ids = list(pending_ops)
        else:
            resp = client.get("/v1/operations")
            operations = json.loads(resp.read())
            api_op_ids: list[str] = []
            for op in operations:
                if op.get("status") not in ACTIVE_STATUSES:
                    continue
                op_session = op.get("session_key")
                if op_session == session.session_key:
                    api_op_ids.append(op["uuid"])
            op_ids = api_op_ids
            if not op_ids:
                print("No active operations for this session.", file=sys.stderr)
                return None
    else:
        pending_key = None
        pending_meta = {}
        pending_ops = set()

    logger.info("Waiting for %d operations...", len(op_ids))

    exit_status = 0
    for op_id in op_ids:
        sleep_time = 0.5
        while True:
            resp = client.get(f"/v1/operations/{op_id}")
            op = json.loads(resp.read())
            status = op.get("status", "")
            if status in WAIT_TERMINAL_STATUSES:
                metadata = op.get("metadata") or {}
                instance_result = metadata.get("result") or {}
                state = instance_result.get("state") or {}
                exit_code_raw = state.get("exit_code")
                if exit_code_raw is None:
                    exit_code_raw = (op.get("result") or {}).get("exit_code")
                exit_code: int | None
                try:
                    if exit_code_raw is not None:
                        exit_code = int(exit_code_raw)
                    else:
                        exit_code = None
                except (TypeError, ValueError):
                    exit_code = None

                meta = pending_meta.get(op_id)
                title = (
                    str(meta.get("title") or op.get("title") or "")
                    if meta
                    else str(op.get("title") or "")
                )

                effective_status = status
                if status == "SUCCESS" and exit_code not in (None, 0):
                    effective_status = "FAILED"

                if effective_status == "SUCCESS" and op.get("kind") == "instance":
                    result = op.get("result") or {}
                    new_image = result.get("image")
                    if meta and meta.get("disposable", False):
                        store.create_disposable_branch(op_id, title)
                    elif new_image and meta and not meta.get("disposable", False):
                        store.set_image(
                            str(new_image),
                            kind="run",
                            title=title,
                            operation_uuid=op_id,
                        )
                formatter(
                    **{
                        **op,
                        "uuid": op_id,
                        "status": effective_status,
                        "exit_code": exit_code,
                        "title": title,
                    }
                )
                failure_exit = 0
                if effective_status != "SUCCESS":
                    failure_exit = 1
                if exit_code is not None and exit_code != 0:
                    failure_exit = max(failure_exit, exit_code)
                exit_status = max(exit_status, failure_exit)
                if pending_key and op_id in pending_ops:
                    pending_ops.discard(op_id)
                    remaining_meta = [
                        {
                            "op": oid,
                            "title": str(pending_meta.get(oid, {}).get("title", "")),
                            "disposable": bool(
                                pending_meta.get(oid, {}).get("disposable", False)
                            ),
                            "branch": str(pending_meta.get(oid, {}).get("branch", "")),
                        }
                        for oid in pending_ops
                    ]
                    if remaining_meta:
                        store.cache[pending_key] = remaining_meta
                    else:
                        with contextlib.suppress(KeyError):
                            del store.cache[pending_key]
                break
            time.sleep(sleep_time)
            if sleep_time < 5:
                sleep_time = min(5.0, sleep_time * 2)
    if exit_status:
        return exit_status
    return None


def cmd_delete(args: DeleteArgs) -> int | None:
    store = SESSION_STORE.get()
    failed = False
    for key in args.keys:
        if not args.force:
            answer = input(f"Delete session {key!r}? [y/N] ")
            if answer.lower() != "y":
                print("Aborted.")
                failed = True
                continue
        if store.delete_session(key):
            logger.info("Deleted session %r", key)
        else:
            logger.error("Session %r not found", key)
            failed = True
    return 1 if failed else None
