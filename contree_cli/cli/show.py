"""Show the result of an operation.

Fetches the operation by UUID and displays its status, duration, exit
code, result image, and captured stdout/stderr. Terminal operations
(SUCCESS, FAILED, CANCELLED) are cached locally to avoid redundant API
calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import decode_stream
from contree_cli.output import DefaultFormatter, JSONFormatter, JSONPrettyFormatter
from contree_cli.session import SessionStore

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  read-only command
  terminal operation states are cached locally
  use -f json for structured metadata + decoded stdout/stderr fields
"""


@dataclass(frozen=True)
class ShowArgs(ArgumentsProtocol):
    uuid: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShowArgs:
        return cls(uuid=ns.uuid)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument("uuid", help="Operation UUID or session entry (e.g., @12)")
    return cmd_show, ShowArgs


def _resolve_operation_uuid(raw: str, store: SessionStore) -> str:
    # Support @N, :N, or bare numeric history IDs for current session
    prefix_stripped = raw[1:] if raw.startswith(("@", ":")) else raw
    if prefix_stripped.isdigit():
        session = store.session
        if session is None:
            raise ValueError(
                "No active session; cannot resolve history entry. "
                "Run `contree use` first.",
            )
        entry_id = int(prefix_stripped)
        entry = store._get_history_entry(entry_id)
        op_uuid = entry.operation_uuid
        if not op_uuid:
            raise ValueError(f"History entry {entry_id} has no operation UUID")
        return op_uuid
    return raw


_TERMINAL = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


def cmd_show(args: ShowArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    store = SESSION_STORE.get()

    try:
        op_uuid = _resolve_operation_uuid(args.uuid, store)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cache_key = (op_uuid, "operation")
    cached = store.cache.get(cache_key)
    if cached is not None:
        op = cast(dict[str, Any], cached)
    else:
        resp = client.get(f"/v1/operations/{op_uuid}")
        op = json.loads(resp.read())
        if op.get("status") in _TERMINAL:
            store.cache[cache_key] = op

    duration = (
        timedelta(seconds=op["duration"]) if op.get("duration") is not None else None
    )
    result = op.get("result") or {}
    metadata = op.get("metadata") or {}
    instance_result = metadata.get("result") or {}

    exit_code = None
    state = instance_result.get("state") or {}
    if state:
        exit_code = state.get("exit_code")

    status = op.get("status", "")
    if status == "SUCCESS" and exit_code not in (None, 0):
        status = "FAILED"

    formatter(
        uuid=op["uuid"],
        kind=op["kind"],
        status=status,
        duration=duration,
        exit_code=exit_code,
        error=op.get("error") or "",
        image=result.get("image") or "",
        tag=result.get("tag") or "",
    )
    formatter.flush()

    _STREAM_FMTS = (DefaultFormatter, JSONFormatter, JSONPrettyFormatter)
    if not isinstance(formatter, _STREAM_FMTS):
        return None

    stdout = decode_stream(instance_result.get("stdout"))
    stderr = decode_stream(instance_result.get("stderr"))

    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")

    return None
