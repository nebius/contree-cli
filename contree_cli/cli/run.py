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
import fnmatch
import io
import json
import logging
import os
import re
import select
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError, ContreeClient, decode_stream, resolve_image
from contree_cli.mapped_file import MappedFile
from contree_cli.output import (
    DefaultFormatter,
    OutputFormatter,
)
from contree_cli.session import SessionStore
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
        help="Attach file or directory (repeatable, dirs recurse). "
        "Format: host[:inst_path][:uUID][:gGID][:mMODE]. "
        "Tagged options (u/g/m) in any order; "
        "uid/gid resolved locally from pwd/grp; defaults from host stat.",
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


MAX_CACHE_AGE = 90 * 24 * 3600  # 90 days — server retention is 6 months


def _upload_file(
    client: ContreeClient,
    mf: MappedFile,
    store: SessionStore,
) -> str:
    """Upload a host file and return its UUID, reusing if already present."""
    cache_kind = _local_file_cache_kind(mf.host_path)
    cached = store.cache.get(("", cache_kind))
    if isinstance(cached, dict) and cached.get("uuid"):
        age = time.time() - cached.get("uploaded_at", 0)
        if age < MAX_CACHE_AGE:
            logger.debug(
                "File %s reused from local cache (%s)", mf.host_path, cached["uuid"]
            )
            return str(cached["uuid"])

    try:
        resp = client.get("/v1/files", params={"sha256": mf.sha256()})
        file_uuid = str(json.loads(resp.read())["uuid"])
        logger.info("File %s already uploaded (%s)", mf.host_path, file_uuid)
        store.cache[("", cache_kind)] = {"uuid": file_uuid, "uploaded_at": time.time()}
        return file_uuid
    except ApiError as exc:
        if exc.status != 404:
            raise

    with open(mf.host_path, "rb") as fh:
        data = fh.read()
    resp = client.request(
        "POST",
        "/v1/files",
        body=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    file_uuid = str(json.loads(resp.read())["uuid"])
    logger.debug("Uploaded %s (%s)", mf.host_path, file_uuid)
    store.cache[("", cache_kind)] = {"uuid": file_uuid, "uploaded_at": time.time()}
    return file_uuid


def _build_payload(
    args: RunArgs,
    image_uuid: str,
    uploaded: dict[str, str],
    files: list[MappedFile],
    store: SessionStore,
) -> dict[str, object]:
    """Build the JSON payload for POST /v1/instances."""
    if args.shell:
        command = " ".join(args.command_args)
    else:
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


def _display_operation(
    op: dict[str, object],
    formatter: OutputFormatter,
) -> None:
    """Display an operation result using the given formatter."""
    duration_raw = op.get("duration")
    duration = (
        timedelta(seconds=duration_raw)  # type: ignore[arg-type]
        if duration_raw is not None
        else None
    )
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
        formatter(
            uuid=op["uuid"],
            kind=op.get("kind", ""),
            status=op["status"],
            duration=duration,
            exit_code=exit_code,
            error=op.get("error") or "",
            image=result.get("image") or "",
            tag=result.get("tag") or "",
            stdout=decode_stream(instance_result.get("stdout")),
            stderr=decode_stream(instance_result.get("stderr")),
        )
        formatter.flush()
        return

    if not isinstance(formatter, DefaultFormatter):
        raise RuntimeError(
            f"Unsupported formatter type: {type(formatter).__name__}, "
            "only json/json-pretty/default are supported"
        )

    # For DefaultFormatter, just only print stdout/stderr
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

    uploaded: dict[str, str] = {}
    for mf in expanded_files:
        uploaded[mf.host_path] = _upload_file(client, mf, store)

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
        formatter(uuid=op_uuid, status=op.get("status", "PENDING"))
        formatter.flush()
        return None

    # 5. Poll until terminal status
    sleep_time = 0.5
    try:
        time.sleep(sleep_time)
        sleep_time += sleep_time
        while True:
            resp = client.get(f"/v1/operations/{op_uuid}")
            op = json.loads(resp.read())
            if op["status"] in TERMINAL_STATUSES:
                break
            time.sleep(sleep_time)
            if sleep_time < 5:
                sleep_time += sleep_time
    except KeyboardInterrupt:
        try:
            client.delete(f"/v1/operations/{op_uuid}")
            logger.info("Cancelled operation %s", op_uuid)
        except (ApiError, KeyboardInterrupt, OSError):
            pass
        raise

    # 6. Cache terminal operation result
    store.cache[(op_uuid, "operation")] = op

    # 7. Display result
    _display_operation(op, formatter)

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

    metadata = op.get("metadata") or {}
    assert isinstance(metadata, dict)
    instance_result = metadata.get("result") or {}
    assert isinstance(instance_result, dict)
    state = instance_result.get("state") or {}
    assert isinstance(state, dict)
    exit_code = state.get("exit_code")
    if isinstance(exit_code, int):
        return exit_code
    if op["status"] != "SUCCESS":
        return 1
    return None
