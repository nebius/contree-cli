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

from contree_client.models import TERMINAL_STATUSES, decode_stream

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol
from contree_cli.output import DefaultFormatter
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


def cmd_show(args: ShowArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(
        head=("uuid", "status", "exit_code", "duration", "result_image_uuid"),
        tail=("error", "result"),
    )
    store = SESSION_STORE.get()

    try:
        op_uuid = resolve_operation_uuid(args.uuid, store)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    cache_key = (op_uuid, "operation")
    cached = store.cache.get(cache_key)
    if isinstance(cached, dict) and cached.get("status") in TERMINAL_STATUSES:
        op = cast("dict[str, Any]", cached)
    else:
        # Note: to_dict() round-trips through the typed model, so
        # fields unknown to the model are dropped -- `--raw` is only
        # as raw as the model allows.
        op = client.get_operation_status(op_uuid).to_dict()
        if op.get("status") in TERMINAL_STATUSES:
            store.cache[cache_key] = op

    if args.raw:
        # One operation per line (JSONL), so multi-UUID `op show --raw`
        # streams cleanly into `jq -c`, `awk`, etc. Skips formatter
        # routing, derived columns, and stdout/stderr decoding -- the
        # typed-model round-trip above is still in effect, though (see
        # the note on `op` a few lines up).
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

    # "metadata" is the raw spawn-request echo, not operation state;
    # "result" is superseded by the derived version below.
    op_fields = {k: v for k, v in op.items() if k not in ("metadata", "result")}

    formatter(
        **{
            **op_fields,
            "exit_code": exit_code,
            "image": result.get("image") or "",
            "tag": result.get("tag") or "",
            "result": {
                **instance_result,
                "stdout": decode_stream(instance_result.get("stdout")),
                "stderr": decode_stream(instance_result.get("stderr")),
            },
        }
    )
    formatter.flush()

    if not isinstance(formatter, DefaultFormatter):
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
