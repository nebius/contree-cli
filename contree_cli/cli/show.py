"""Per-UUID inspect handler used by `contree operation show` (and its
top-level shortcut ``contree show``).

The top-level ``show`` command is registered against
:func:`contree_cli.cli.operation.setup_show_parser`; that handler loops
over each UUID and calls :func:`cmd_show` here. This module owns the
single-UUID logic: ``@N`` history-reference resolution, terminal
operation caching, and stdout/stderr decoding.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, cast

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol
from contree_cli.client import decode_stream
from contree_cli.output import DefaultFormatter, JSONFormatter, JSONPrettyFormatter
from contree_cli.refs import history_spec_from_ref, resolve_operation_uuid

# Re-exported for backwards compatibility with anything that historically
# imported these helpers from `contree_cli.cli.show`.
__all__ = [
    "ShowArgs",
    "cmd_show",
    "history_spec_from_ref",
    "resolve_operation_uuid",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShowArgs(ArgumentsProtocol):
    uuid: str
    raw: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ShowArgs:
        return cls(uuid=ns.uuid, raw=getattr(ns, "raw", False))


TERMINAL = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


def cmd_show(args: ShowArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("error",))
    store = SESSION_STORE.get()

    try:
        op_uuid = resolve_operation_uuid(args.uuid, store)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cache_key = (op_uuid, "operation")
    cached = store.cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("status") in TERMINAL:
        op = cast(dict[str, Any], cached)
    else:
        resp = client.get(f"/v1/operations/{op_uuid}")
        op = json.loads(resp.read())
        if op.get("status") in TERMINAL:
            store.cache[cache_key] = op

    if args.raw:
        # Pass through the server payload verbatim, one operation per
        # line (JSONL), so multi-UUID `op show --raw` streams cleanly
        # into `jq -c`, `awk`, etc. Skips formatter routing, derived
        # columns, and stdout/stderr decoding -- the user asked for raw.
        json.dump(op, sys.stdout)
        sys.stdout.write("\n")
        return None

    result = op.get("result") or {}
    metadata = op.get("metadata") or {}
    instance_result = metadata.get("result") or {}

    exit_code = None
    state = instance_result.get("state") or {}
    if state:
        exit_code = state.get("exit_code")

    formatter(
        **{
            **op,
            "exit_code": exit_code,
            "image": result.get("image") or "",
            "tag": result.get("tag") or "",
        }
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
