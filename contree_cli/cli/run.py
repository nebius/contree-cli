"""\
Spawn a sandbox instance from the current session image and execute a command.

Uses the image from the active session (set via `contree use IMAGE`),
or an image specified inline via ``--use IMAGE``.
Commands are passed after -- separator; without --, the first
positional arg is the command.

By default the CLI polls until the operation reaches a terminal
status (SUCCESS, FAILED, CANCELLED) and prints stdout/stderr.
Use -d/--detach to exit immediately after spawning.

File attachments:
  Use --file to inject host files or directories into the sandbox
  before execution. Files are uploaded to the API (with SHA256 dedup)
  and mounted at the specified instance path. Ownership and
  permissions default to host file stat unless overridden.

  Note: non-disposable runs persist filesystem changes into a
  new image. Files attached once are already part of that image
  and do not need re-attachment. Use --disposable to discard
  changes after execution.

  Format: host_path[:instance_path][:uUID][:gGID][:mMODE]

    host_path                             all defaults from stat
    host_path:/inst/path                  point a destination path
    host_path:m0755                       override only mode
    host_path:/inst/path:u0:g0:m0755      all explicit
    host_path:uroot:groot                 uid/gid by name (local)

  Tagged options (u/g/m) can appear in any order after host_path.
  instance_path is detected by its leading /.
  For directory attachments, files are walked recursively and default
  excludes are applied: .*, .git, *.pyc, __pycache__, .venv,
  .mypy_cache, .pytest_cache, node_modules, dist, build.
  Add extra patterns with --file-excludes.

  The CLI also keeps a local upload cache keyed by
  path+inode+mtime+size and reuses known file UUIDs to avoid
  unnecessary re-upload checks/uploads.

  Note: named uid/gid (e.g. uroot) are resolved locally via
  pwd/grp — use numeric IDs if unsure about host/sandbox mismatch.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fnmatch
import functools
import io
import json
import logging
import os
import re
import select
import shlex
import sys
import time
import uuid
from dataclasses import dataclass, field
from multiprocessing.pool import ThreadPool
from typing import Any

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import (
    RETRY_DELAYS,
    RETRYABLE_NETWORK_ERRORS,
    ApiError,
    ContreeClient,
    decode_event_chunk,
    decode_stream,
    iter_sse_events,
    resolve_image,
)
from contree_cli.mapped_file import MAPPING_RULES, MappedFile
from contree_cli.output import (
    DefaultFormatter,
    OutputFormatter,
)
from contree_cli.session import CONTREE_CONCURRENCY, SessionStore
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  contree use ubuntu && contree run -- uname -a
  contree run --use tag:ubuntu:latest -- uname -a
  contree run --shell -- 'echo hello && ls /'
  contree run -e FOO=bar DEBUG=1 -- ./app
  contree run --file ./app.py:/app.py --disposable -- python /app.py
  contree run --file ./src:/app/src --file-excludes '*.log' -- make -C /app/src
  contree run -d -- sleep 3600

for coding agents:
  `run` executes remotely inside the instance image (not on local host)
  local files/dirs must be mapped with --file to be available remotely
  mutates session image unless --disposable is set
  supports directory attachments via --file host_dir:/instance_dir
  local file cache avoids re-upload when path+inode+mtime+size unchanged
  returns command exit code when available
  default formatter prints raw stdout/stderr only
  use -f json for structured operation metadata
"""

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
DEFAULT_FILE_EXCLUDES = (
    ".*",
    ".git",
    "*.pyc",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "node_modules",
    "dist",
    "build",
)


def _read_piped_stdin() -> bytes:
    """Non-blocking stdin read for piped input.

    Returns empty when stdin is a tty, not ready, or unsupported, to avoid hangs
    in agent/CI contexts where stdin is non-tty but has no data.
    """

    if sys.stdin.isatty():
        return b""

    try:
        fd = sys.stdin.fileno()
    except (OSError, io.UnsupportedOperation):
        fd = None

    if isinstance(fd, int) and fd >= 0:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except (OSError, ValueError, TypeError):
            return b""
        if not ready:
            return b""

    try:
        return sys.stdin.buffer.read()
    except (OSError, AttributeError):
        return b""


# Escape sequences that corrupt the terminal when replayed from captured output.
# Matches CSI sequences that are NOT SGR (colors end with 'm') — cursor movement,
# screen clearing, mode set/reset, scroll regions, etc. Also matches RIS (\033c),
# DEC cursor save/restore (\0337/\0338), and OSC sequences (title setting, etc.).
_BREAKING_ESC_RE = re.compile(
    r"\033\[\??[0-9;]*[ABCDEFGHJKSTfhlrsu]"  # CSI non-SGR
    r"|\033c"  # RIS (full reset)
    r"|\0337|\0338"  # DEC cursor save/restore
    r"|\033\][^\033\x07]*(?:\033\\|\x07)"  # OSC sequences
)


@dataclass(frozen=True)
class RunArgs(ArgumentsProtocol):
    command_args: list[str] = field(default_factory=list)
    timeout: int | None = None
    env: list[str] = field(default_factory=list)
    hostname: str = "linuxkit"
    disposable: bool = False
    interpreter: bool = False
    shell: bool = False
    file: list[MappedFile] = field(default_factory=list)
    file_excludes: list[str] = field(default_factory=list)
    truncate: int = 65536
    detach: bool = False
    preserve_env: bool = False
    cwd: str = ""
    use: str = ""

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> RunArgs:
        raw = ns.command_args
        if raw and raw[0] == "--":
            raw = raw[1:]
        excludes = [pattern for group in ns.file_excludes for pattern in group]
        return cls(
            command_args=raw,
            timeout=ns.timeout,
            env=ns.env,
            hostname=ns.hostname,
            disposable=ns.disposable,
            interpreter=ns.interpreter,
            shell=ns.shell,
            file=[MappedFile.parse(f) for f in ns.file],
            file_excludes=excludes,
            truncate=ns.truncate,
            detach=ns.detach,
            preserve_env=ns.preserve_env,
            cwd=ns.cwd,
            use=ns.use or "",
        )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "command_args",
        nargs=argparse.REMAINDER,
        help="Command and arguments (after --)",
    )
    p.add_argument(*FLAGS["timeout"], type=int, help="Timeout in seconds", default=120)
    p.add_argument(
        *FLAGS["cwd"],
        default="",
        help="Working directory inside sandbox, absolute path "
        "or empty string for use sandbox WORKDIR",
    )
    p.add_argument(
        *FLAGS["env"],
        action="append",
        default=[],
        help="Environment variable KEY=VALUE (repeatable)",
    )
    p.add_argument(
        *FLAGS["hostname"],
        default="linuxkit",
        help="Container hostname",
    )
    p.add_argument(
        *FLAGS["disposable"],
        action="store_true",
        help="Drop filesystem changes after run",
    )
    p.add_argument(
        *FLAGS["interpreter"],
        action="store_true",
        help="Interpreter (shebang) mode. Read the script file given "
        "as the first argument, strip the #! line, and send the "
        "body as stdin to /bin/sh -s. "
        "Usage: #!/usr/bin/env -S contree run -I",
    )
    p.add_argument(
        *FLAGS["shell"],
        action="store_true",
        help="Join command args into a single shell expression",
    )
    p.add_argument(
        *FLAGS["file"],
        action="append",
        default=[],
        metavar="FILE",
        help=MAPPING_RULES,
    )
    p.add_argument(
        *FLAGS["file_excludes"],
        nargs="+",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Additional glob exclude patterns for directory attachments (repeatable).",
    )
    p.add_argument(
        *FLAGS["truncate"],
        type=int,
        default=65536,
        help="Truncate output to N bytes",
    )
    p.add_argument(
        *FLAGS["preserve_env"],
        action="store_true",
        help="Preserve env vars from previous run (server-side)",
    )
    p.add_argument(
        *FLAGS["detach"],
        action="store_true",
        help="Exit immediately after spawning (do not wait for result)",
    )
    p.add_argument(
        *FLAGS["use"],
        default="",
        metavar="IMAGE",
        help=(
            "Switch session to IMAGE before running "
            "(UUID or tag:NAME). Equivalent to "
            "'contree use IMAGE' followed by 'run'. "
            "Recorded in session history and can be "
            "rolled back with 'session rollback'."
        ),
    )
    return cmd_run, RunArgs


def _is_excluded(rel_path: str, patterns: tuple[str, ...]) -> bool:
    parts = rel_path.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _expand_mapped_files(
    mapped: list[MappedFile],
    extra_excludes: list[str],
) -> list[MappedFile]:
    patterns = (*DEFAULT_FILE_EXCLUDES, *extra_excludes)
    result: list[MappedFile] = []

    for mf in mapped:
        if os.path.isfile(mf.host_path):
            result.append(mf)
            continue

        if not os.path.isdir(mf.host_path):
            raise ValueError(
                f"host path {mf.host_path!r} is neither file nor directory",
            )

        base_instance = mf.instance_path.rstrip("/") or "/"
        for root, dirnames, filenames in os.walk(mf.host_path, topdown=True):
            rel_root = os.path.relpath(root, mf.host_path)
            rel_root_posix = "" if rel_root == "." else rel_root.replace(os.sep, "/")

            kept_dirs: list[str] = []
            for d in dirnames:
                rel_dir = d if not rel_root_posix else f"{rel_root_posix}/{d}"
                if not _is_excluded(rel_dir, patterns):
                    kept_dirs.append(d)
            dirnames[:] = kept_dirs

            for name in filenames:
                rel_file = name if not rel_root_posix else f"{rel_root_posix}/{name}"
                if _is_excluded(rel_file, patterns):
                    continue

                host_file = os.path.join(root, name)
                if not os.path.isfile(host_file):
                    continue
                st = os.stat(host_file)
                result.append(
                    MappedFile(
                        host_path=host_file,
                        instance_path=f"{base_instance.rstrip('/')}/{rel_file}",
                        uid=mf.uid if mf.uid_explicit else st.st_uid,
                        gid=mf.gid if mf.gid_explicit else st.st_gid,
                        mode=mf.mode if mf.mode_explicit else (st.st_mode & 0o7777),
                        uid_explicit=mf.uid_explicit,
                        gid_explicit=mf.gid_explicit,
                        mode_explicit=mf.mode_explicit,
                    )
                )

    return result


def _local_file_cache_kind(host_path: str) -> str:
    abs_path = os.path.abspath(host_path)
    st = os.stat(abs_path)
    fingerprint = f"{abs_path}:{st.st_ino}:{st.st_mtime_ns}:{st.st_size}"
    digest = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)
    return f"local_file:{digest}"


MAX_CACHE_AGE = 90 * 24 * 3600  # 90 days - server retention is 6 months


def cached_local_uuid(mf: MappedFile, store: SessionStore) -> str | None:
    """Return a cached UUID for *mf* if one was uploaded within MAX_CACHE_AGE."""
    cache_kind = _local_file_cache_kind(mf.host_path)
    cached = store.cache.get(("", cache_kind))
    if isinstance(cached, dict) and cached.get("uuid"):
        age = time.time() - cached.get("uploaded_at", 0)
        if age < MAX_CACHE_AGE:
            logger.debug(
                "File %s reused from local cache (%s)",
                mf.host_path,
                cached["uuid"],
            )
            return str(cached["uuid"])
    return None


def record_local_uuid(mf: MappedFile, file_uuid: str, store: SessionStore) -> None:
    """Persist a host_path → file_uuid mapping in the local cache."""
    cache_kind = _local_file_cache_kind(mf.host_path)
    store.cache[("", cache_kind)] = {
        "uuid": file_uuid,
        "uploaded_at": time.time(),
        "local_path": os.path.abspath(mf.host_path),
    }


def upload_one_remote(client: ContreeClient, mf: MappedFile) -> tuple[MappedFile, str]:
    """HTTP-only upload (sha256 dedup + POST /v1/files). Thread-safe."""
    sha = mf.sha256()
    try:
        resp = client.get(f"/v1/files/{sha}")
        file_uuid = str(json.loads(resp.read())["uuid"])
        logger.info("File reused: %s -> %s", mf.host_path, file_uuid)
        return mf, file_uuid
    except ApiError as exc:
        if exc.status != 404:
            raise

    with open(mf.host_path, "rb") as fh:
        resp = client.request(
            "POST",
            "/v1/files",
            body=fh,
            headers={"Content-Type": "application/octet-stream"},
        )
        file_uuid = str(json.loads(resp.read())["uuid"])
    logger.debug("Uploaded %s (%s)", mf.host_path, file_uuid)
    return mf, file_uuid


def upload_files(
    client: ContreeClient,
    files: list[MappedFile],
    store: SessionStore,
) -> dict[str, str]:
    """Upload host files in parallel, returning host_path → file_uuid."""
    uploaded: dict[str, str] = {}
    pending: list[MappedFile] = []
    for mf in files:
        cached = cached_local_uuid(mf, store)
        if cached:
            uploaded[mf.host_path] = cached
            # Rewrite the entry so older payloads without local_path are
            # backfilled with the current host path.
            record_local_uuid(mf, cached, store)
        else:
            pending.append(mf)

    if not pending:
        return uploaded

    workers = min(CONTREE_CONCURRENCY, len(pending))
    upload = functools.partial(upload_one_remote, client)
    with ThreadPool(workers) as pool:
        for mf, file_uuid in pool.imap_unordered(upload, pending):
            uploaded[mf.host_path] = file_uuid
            record_local_uuid(mf, file_uuid, store)
    return uploaded


def _build_payload(
    args: RunArgs,
    image_uuid: str,
    uploaded: dict[str, str],
    files: list[MappedFile],
    store: SessionStore,
) -> dict[str, object]:
    """Build the JSON payload for POST /v1/instances."""
    if args.shell:
        # API runs `sh -c <command>`. A single arg is already a shell
        # expression (the user pre-quoted it: `run -s -- 'a ; b'`), so
        # passing it through verbatim preserves operators like `;`, `&&`,
        # `|`. Multiple args are individual tokens that need joining with
        # quoting to preserve argument boundaries.
        parts = args.command_args
        command = parts[0] if len(parts) == 1 else shlex.join(parts)
    else:
        # In non-shell mode the API exec's command + args directly,
        # JSON list elements preserve boundaries, no quoting needed.
        parts = args.command_args
        command = parts[0] if parts else ""

    payload: dict[str, object] = {
        "image": image_uuid,
        "command": command,
        "shell": args.shell,
        "disposable": args.disposable,
        "hostname": args.hostname,
        "truncate_output_at": args.truncate,
    }

    if not args.shell and len(args.command_args) > 1:
        payload["args"] = args.command_args[1:]

    if args.timeout is not None:
        payload["timeout"] = args.timeout

    cwd_value = args.cwd or store.get_cwd()
    if cwd_value:
        payload["cwd"] = cwd_value

    # Session env as base, per-run -e overrides
    env_dict: dict[str, str] = store.get_env()
    for item in args.env:
        key, _, value = item.partition("=")
        env_dict[key] = value

    # Skip sending env already baked into the current image
    preserved = store.cache.get((image_uuid, "preserved_env"))
    if env_dict and env_dict != preserved:
        payload["env"] = env_dict
    if args.preserve_env:
        payload["preserve_env"] = True

    if uploaded:
        payload_files: dict[str, object] = {}
        for mf in files:
            file_uuid = uploaded[mf.host_path]
            payload_files[mf.instance_path] = {
                "uuid": file_uuid,
                "uid": mf.uid,
                "gid": mf.gid,
                "mode": f"{mf.mode:04o}",
            }
        payload["files"] = payload_files

    return payload


TERMINAL_OP_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


@dataclass
class TerminalSummary:
    """Authoritative end-of-stream snapshot built from the SSE events
    themselves — `cmd_run` consumes this directly instead of doing a
    second ``GET /operations/{uuid}``.

    If SSE ends without a `completion` frame but a between-attempt GET
    detects the op is already terminal, the full op dict lands in
    ``fallback_op`` so `cmd_run` can use it directly."""

    completion: dict[str, Any] | None = None
    exit_event: dict[str, Any] | None = None
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    fallback_op: dict[str, Any] | None = None


def _check_terminal_via_get(
    client: ContreeClient, op_uuid: str, summary: TerminalSummary
) -> bool:
    """Poll `GET /v1/operations/{uuid}`; if the op is in a terminal
    status, park the full response on ``summary.fallback_op`` and
    return True.  Any GET failure or non-terminal status returns False
    so the caller keeps retrying SSE."""
    try:
        resp = client.request("GET", f"/v1/operations/{op_uuid}")
        op = json.loads(resp.read())
    except (ApiError, ValueError) as exc:
        logger.debug("terminal check GET failed: %s", exc)
        return False
    except RETRYABLE_NETWORK_ERRORS as exc:
        logger.debug("terminal check GET failed: %s", exc)
        return False
    if isinstance(op, dict) and op.get("status") in TERMINAL_OP_STATUSES:
        summary.fallback_op = op
        return True
    return False


def _stream_backoff_sleep(attempt: int) -> None:
    """Sleep for `RETRY_DELAYS[attempt]`, capped at the last entry —
    the loop retries indefinitely, so anything past the sequence just
    reuses the tail delay."""
    if attempt <= 0:
        return
    idx = min(attempt - 1, len(RETRY_DELAYS) - 1)
    time.sleep(RETRY_DELAYS[idx])


def _stream_events_until_close(
    client: ContreeClient,
    op_uuid: str,
    formatter: OutputFormatter,
) -> TerminalSummary:
    """Open `follow=1` SSE for *op_uuid* and write events to stdio,
    transparently resuming on network drops / mid-stream errors using
    ``Last-Event-Id`` for replay-free continuation.

    For ``DefaultFormatter``: ``stdout`` / ``stderr`` events go to
    ``sys.std*.buffer`` directly so the user sees output as it arrives.
    For JSON formatters: data goes to ``log.debug`` so the JSON output
    isn't polluted, but the chunks are accumulated into the returned
    ``TerminalSummary`` so the caller can render full stdout/stderr
    without a follow-up GET.

    Retries the SSE stream indefinitely — only a `completion` event,
    a GET-detected terminal status, ``BrokenPipeError`` from local
    stdout/stderr, or ``KeyboardInterrupt`` breaks the loop.  Between
    every failed SSE cycle (connect error, mid-stream drop, or clean
    close without ``completion``) the streamer polls the plain
    operation endpoint and, if the op is already terminal, parks the
    full op on ``summary.fallback_op`` and returns.

    ``BrokenPipeError`` from a local stdio write propagates unchanged —
    it means the shell pipe closed and retrying cannot help; the
    caller cancels the op and exits.
    """
    is_default = isinstance(formatter, DefaultFormatter)
    attempt = 0
    last_id: int = -1
    summary = TerminalSummary()

    while True:
        headers: dict[str, str] | None = None
        if last_id >= 0:
            headers = {"Last-Event-Id": str(last_id)}
        try:
            resp = client.request(
                "GET",
                f"/v1/operations/{op_uuid}/events?follow=1",
                headers=headers,
            )
        except ApiError as exc:
            logger.debug("event stream open failed (attempt %d): %s", attempt + 1, exc)
            if _check_terminal_via_get(client, op_uuid, summary):
                return summary
            attempt += 1
            _stream_backoff_sleep(attempt)
            continue
        except RETRYABLE_NETWORK_ERRORS as exc:
            logger.debug(
                "event stream connect error (attempt %d): %s",
                attempt + 1,
                exc,
            )
            if _check_terminal_via_get(client, op_uuid, summary):
                return summary
            attempt += 1
            _stream_backoff_sleep(attempt)
            continue

        events_before = last_id
        try:
            for ev in iter_sse_events(resp):
                ev_id = ev.get("id")
                if isinstance(ev_id, int):
                    last_id = ev_id
                ev_type = ev.get("type")
                data = ev.get("data")
                match ev_type:
                    case "stdout":
                        chunk = decode_event_chunk(data)
                        summary.stdout.extend(chunk)
                        if is_default:
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                    case "stderr":
                        chunk = decode_event_chunk(data)
                        summary.stderr.extend(chunk)
                        if is_default:
                            sys.stderr.buffer.write(chunk)
                            sys.stderr.buffer.flush()
                    case "exit":
                        # spid=1 is the main process — its exit code/timed_out
                        # drive the CLI's own exit code.
                        if ev.get("spid") == 1:
                            summary.exit_event = ev
                        logger.debug("event: %s", ev)
                    case "sse_error":
                        logger.warning(
                            "server-side stream error (last_id=%s): %s",
                            last_id,
                            ev.get("message"),
                        )
                    case "completion":
                        # Authoritative terminal frame — don't wait for the server
                        # to close, return the summary for the caller.
                        summary.completion = ev
                        logger.debug("event: %s", ev)
                        return summary
                    case _:
                        logger.debug("event: %s", ev)
        except BrokenPipeError:
            # Local stdout/stderr was closed by the shell (e.g. piping
            # into `head`).  Retrying cannot help — the caller cancels
            # the op and exits.
            raise
        except RETRYABLE_NETWORK_ERRORS as exc:
            logger.debug(
                "event stream broken (attempt %d, last_id=%s): %s",
                attempt + 1,
                last_id,
                exc,
            )
        finally:
            with contextlib.suppress(Exception):
                resp.close()

        if _check_terminal_via_get(client, op_uuid, summary):
            return summary

        # Reset retry budget on forward progress: at least one new
        # event before the stream broke / sse_error fired means the
        # server is alive and Last-Event-Id resumption is working.
        if last_id > events_before:
            attempt = 0
        else:
            attempt += 1
        _stream_backoff_sleep(attempt)


def _build_op_from_summary(op_uuid: str, summary: TerminalSummary) -> dict[str, Any]:
    """Synthesize a `GET /operations/{uuid}` shape from the SSE terminal
    summary — completion event drives status / error / duration / image
    metadata; exit event drives exit_code + timed_out; stdout/stderr
    are reassembled from the streamed chunks.

    Lets `cmd_run` avoid a second HTTP round-trip on the happy path
    while keeping the downstream `_display_operation` consumers
    (default and JSON formatters) untouched."""
    assert summary.completion is not None
    completion_data = summary.completion.get("data") or {}
    status = completion_data.get("status")
    state: dict[str, Any] = {}
    if summary.exit_event:
        exit_data = summary.exit_event.get("data") or {}
        if "code" in exit_data:
            state["exit_code"] = int(exit_data["code"])
        if "timed_out" in exit_data:
            state["timed_out"] = bool(exit_data["timed_out"])
    result_image_uuid = completion_data.get("result_image_uuid")
    return {
        "uuid": op_uuid,
        "kind": "instance",
        "status": status,
        "error": completion_data.get("error"),
        "duration": (completion_data.get("duration_ms") or 0) / 1000.0,
        "image_size": completion_data.get("image_size_bytes"),
        "result_image_uuid": result_image_uuid,
        "metadata": {
            "result": {
                "stdout": {
                    "value": bytes(summary.stdout).decode("utf-8", errors="replace"),
                    "encoding": "ascii",
                    "truncated": False,
                },
                "stderr": {
                    "value": bytes(summary.stderr).decode("utf-8", errors="replace"),
                    "encoding": "ascii",
                    "truncated": False,
                },
                "state": state or None,
            }
        },
        "result": {"image": result_image_uuid, "tag": None},
    }


def _display_operation(
    op: dict[str, object],
    formatter: OutputFormatter,
    live_streamed: bool = False,
) -> None:
    """Display an operation result using the given formatter.

    ``live_streamed=True`` means the SSE path already wrote stdout/
    stderr to stdio incrementally — DefaultFormatter then has nothing
    left to print.  ``live_streamed=False`` is the GET-fallback path
    (legacy server or aborted stream) — DefaultFormatter prints
    sanitized stdout/stderr from the operation's metadata blob.
    """
    result = op.get("result") or {}
    assert isinstance(result, dict)
    metadata = op.get("metadata") or {}
    assert isinstance(metadata, dict)
    instance_result = metadata.get("result") or {}
    assert isinstance(instance_result, dict)

    exit_code = None
    state = instance_result.get("state") or {}
    assert isinstance(state, dict)
    if state:
        exit_code = state.get("exit_code")

    if formatter.STREAM:
        formatter.configure(tail=("error",))
        formatter(
            **{
                **op,
                "exit_code": exit_code,
                "image": result.get("image") or "",
                "tag": result.get("tag") or "",
                "stdout": decode_stream(instance_result.get("stdout")),
                "stderr": decode_stream(instance_result.get("stderr")),
            }
        )
        formatter.flush()
        return

    if not isinstance(formatter, DefaultFormatter):
        raise RuntimeError(
            f"Unsupported formatter type: {type(formatter).__name__}, "
            "only json/json-pretty/default are supported"
        )

    if live_streamed:
        # stdout/stderr already streamed live via SSE — nothing more to print.
        return

    # Legacy / fallback path (no completion event received): print
    # sanitized stdout/stderr from op metadata.
    stdout = _BREAKING_ESC_RE.sub("", decode_stream(instance_result.get("stdout")))
    stderr = _BREAKING_ESC_RE.sub("", decode_stream(instance_result.get("stderr")))
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")


def cmd_run(args: RunArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    store = SESSION_STORE.get()

    # 1. Resolve image: --use switches session first, otherwise use active
    if args.use:
        image_uuid = resolve_image(client, args.use)
        store.set_image(image_uuid, kind="use", title=args.use)
    else:
        image_uuid = resolve_image(client, store.current_image)

    # 2. Expand and upload attached files (supports directories)
    try:
        expanded_files = _expand_mapped_files(args.file, args.file_excludes)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    uploaded = upload_files(client, expanded_files, store)

    # 2b. Include pending files from session store
    pending = store.pending_files()

    # 3. Build and send spawn request
    payload = _build_payload(args, image_uuid, uploaded, expanded_files, store)

    # Merge pending files (explicit --file takes priority)
    if pending:
        files = payload.get("files", {})
        assert isinstance(files, dict)
        for pf in pending:
            if pf.instance_path not in files:
                files[pf.instance_path] = {
                    "uuid": pf.file_uuid,
                    "uid": pf.uid,
                    "gid": pf.gid,
                    "mode": pf.mode,
                }
                logger.debug(
                    "Including pending file %s (%s)",
                    pf.instance_path,
                    pf.file_uuid,
                )
        if files:
            payload["files"] = files

    # Interpreter mode (-I): read script file, strip shebang, send as stdin
    if args.interpreter and args.command_args:
        script_path = args.command_args[0]
        logger.debug("Interpreter mode: reading %s", script_path)
        with open(script_path, "rb") as f:
            script_data = f.read()
        # Normalise CRLF → LF (Windows editors write \r\n)
        script_data = script_data.replace(b"\r\n", b"\n")
        # Strip shebang line
        shebang_line, _, script_body = script_data.partition(b"\n")
        logger.debug("Shebang line: %s", shebang_line.decode(errors="replace"))
        if script_body:
            payload["stdin"] = {
                "value": base64.b64encode(script_body).decode(),
                "encoding": "base64",
            }
            logger.debug("Script body: %d bytes", len(script_body))
        payload["command"] = "/bin/sh"
        payload["shell"] = True
        extra_args = args.command_args[1:]
        if extra_args:
            payload["args"] = ["-s", "--", *extra_args]
            logger.debug("Extra args: %s", extra_args)
        else:
            payload["args"] = ["-s"]

    # Read piped stdin (skip if shebang already set it)
    if "stdin" not in payload:
        stdin_data = _read_piped_stdin()
        if stdin_data:
            payload["stdin"] = {
                "value": base64.b64encode(stdin_data).decode(),
                "encoding": "base64",
            }
            logger.debug("Piped stdin: %d bytes", len(stdin_data))
        elif not sys.stdin.isatty():
            logger.debug("No piped stdin available; skipping read")

    resp = client.post_json("/v1/instances", payload)
    op = json.loads(resp.read())
    op_uuid: str = op["uuid"]

    logger.debug("Spawned operation %s", op_uuid)

    if args.detach:
        pending_key = ("", f"ops:{store.session_key}")
        existing = store.cache.get(pending_key) or []

        def _norm(item: object) -> dict[str, object]:
            if isinstance(item, dict) and "op" in item:
                return {
                    "op": str(item.get("op", "")),
                    "title": str(item.get("title", "")),
                    "disposable": bool(item.get("disposable", False)),
                }
            return {"op": str(item), "title": "", "disposable": False}

        normalized = [_norm(x) for x in existing] if isinstance(existing, list) else []
        if args.disposable:
            branch_name = store.create_disposable_branch(
                op_uuid, " ".join(args.command_args)
            )
        else:
            branch_name = store.create_detached_branch(
                op_uuid, " ".join(args.command_args)
            )
        normalized.append(
            {
                "op": op_uuid,
                "title": " ".join(args.command_args),
                "disposable": bool(args.disposable),
                "branch": branch_name,
            }
        )
        store.cache[pending_key] = normalized

    # 4. Detach mode - exit immediately
    if args.detach:
        formatter.configure(tail=("error",))
        formatter(**{"uuid": op_uuid, "status": "PENDING", **op})
        formatter.flush()
        return None

    # 5. Stream events (follow=1) — write stdout/stderr to stdtio as they
    # come, log other events at debug, accumulate the terminal frame.
    store = SESSION_STORE.get()
    try:
        summary = _stream_events_until_close(client, op_uuid, formatter)
        if summary.completion is not None:
            # Authoritative terminal frame from the server — build the
            # full op dict from the SSE events themselves, no GET.
            op = _build_op_from_summary(op_uuid, summary)
        elif summary.fallback_op is not None:
            # SSE couldn't deliver `completion`, but a GET between
            # retries confirmed the op is terminal — use that dict.
            op = summary.fallback_op
        else:
            # Safety net: streamer normally loops until either
            # completion or a terminal GET, so this path is only hit
            # in tests where the stub queue drains early.
            resp = client.get(f"/v1/operations/{op_uuid}")
            op = json.loads(resp.read())
        cache_key = (op_uuid, "operation")
        store.cache[cache_key] = op
    except KeyboardInterrupt:
        try:
            client.delete(f"/v1/operations/{op_uuid}")
            logger.info("Cancelled operation %s", op_uuid)
        except (ApiError, KeyboardInterrupt, OSError):
            pass
        raise
    except BrokenPipeError:
        # Local stdout/stderr was closed (e.g. `contree run | head`).
        # Cancel the op, silence further stdio writes, then exit 141
        # so callers see the SIGPIPE convention (128 + 13).
        with contextlib.suppress(ApiError, OSError):
            client.delete(f"/v1/operations/{op_uuid}")
        with contextlib.suppress(OSError):
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        raise SystemExit(141) from None

    # 6. Cache terminal operation result
    store.cache[(op_uuid, "operation")] = op

    metadata = op.get("metadata") or {}
    assert isinstance(metadata, dict)
    instance_result = metadata.get("result") or {}
    assert isinstance(instance_result, dict)
    state = instance_result.get("state") or {}
    assert isinstance(state, dict)
    timed_out = bool(state.get("timed_out"))

    if timed_out:
        logger.warning(
            "Operation %s timed out after %ss",
            op_uuid,
            args.timeout if args.timeout is not None else "?",
        )
    elif op["status"] != "SUCCESS":
        logger.fatal(
            "Operation %s ended with status %s%s",
            op_uuid,
            op["status"],
            f": {op['error']}" if op.get("error") else "",
        )

    # 7. Display result
    _display_operation(op, formatter, live_streamed=summary.completion is not None)

    result = op.get("result") or {}
    assert isinstance(result, dict)
    new_image = result.get("image")
    if new_image and op["status"] == "SUCCESS":
        logger.debug("New image: %s", new_image)
        if not args.disposable:
            title = " ".join(args.command_args) if args.command_args else ""
            store.set_image(
                str(new_image),
                kind="run",
                title=title,
                operation_uuid=op_uuid,
            )
            if args.preserve_env:
                env_dict = store.get_env()
                for item in args.env:
                    key, _, value = item.partition("=")
                    env_dict[key] = value
                store.cache[(str(new_image), "preserved_env")] = env_dict
        else:
            title = " ".join(args.command_args) if args.command_args else ""
            store.create_disposable_branch(op_uuid, title)

    exit_code = state.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code
    if op["status"] != "SUCCESS":
        return 1
    return None
