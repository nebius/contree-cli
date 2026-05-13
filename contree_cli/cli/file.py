"""Manage files in the session image.

Subcommands:
  edit (e)  Download a file from the session image, open it in $EDITOR
            (or vi), and upload the modified version as a pending file
            attachment. The change takes effect on the next `run`.

  cp        Copy a local file into the session image as a pending file
            attachment. The file is uploaded immediately but injected
            into the sandbox on the next `run`.

Pending files are branch-aware — switching branches changes which
files are visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from contree_cli import (
    CLIENT,
    FORMATTER,
    SESSION_STORE,
    ArgumentsProtocol,
    SetupResult,
)
from contree_cli.client import ApiError, ContreeClient, resolve_image, stream_response
from contree_cli.config import EDITOR
from contree_cli.session import SessionStore
from contree_cli.types import (
    FLAGS,
    isoformat_datetime,
    parse_datetime,
    parse_interval,
    positive_int,
)

logger = logging.getLogger(__name__)

EPILOG = """\
for coding agents:
  mutating command (stages pending file changes for next run)
  file edit PATH uses local editor and uploads on change
  file cp SRC DEST uploads local file and stages DEST path
"""


@dataclass(frozen=True)
class FileEditArgs(ArgumentsProtocol):
    path: str
    editor: str | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> FileEditArgs:
        return cls(path=ns.path, editor=getattr(ns, "editor", None))


@dataclass(frozen=True)
class FileCpArgs(ArgumentsProtocol):
    src: str
    dest: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> FileCpArgs:
        return cls(src=ns.src, dest=ns.dest)


FILE_LIST_LIMIT_DEFAULT = 1000
FILE_LIST_PAGE_SIZE = 1000


@dataclass(frozen=True)
class FileListArgs(ArgumentsProtocol):
    since: datetime | None = None
    until: datetime | None = None
    limit: int = FILE_LIST_LIMIT_DEFAULT
    quiet: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> FileListArgs:
        return cls(
            since=getattr(ns, "since", None),
            until=getattr(ns, "until", None),
            limit=getattr(ns, "limit", FILE_LIST_LIMIT_DEFAULT),
            quiet=bool(getattr(ns, "quiet", False)),
        )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    sub = p.add_subparsers(dest="file_action", required=True)
    edit_p = sub.add_parser(
        "edit",
        aliases=["e"],
        help="Edit a file in the session image",
        description=(
            "Edit a remote file via a local editor. The updated content is "
            "uploaded and staged as a pending file for the next run."
        ),
        epilog=(
            "for coding agents:\n"
            "  mutates session state (adds pending file entry)\n"
            "  does not apply immediately; effect appears on next `contree run`"
        ),
    )
    edit_p.add_argument(
        *FLAGS["editor"],
        default=EDITOR,
        help=f"Editor command (default: {EDITOR})",
    )
    edit_p.add_argument("path", help="Path inside image")
    edit_p.set_defaults(handler=cmd_file_edit, load_args=FileEditArgs)

    cp_p = sub.add_parser(
        "cp",
        help="Copy a local file into the session image",
        description=(
            "Upload a local file and stage it as a pending attachment to be "
            "injected on the next run."
        ),
        epilog=(
            "for coding agents:\n"
            "  mutates session state (adds pending file entry)\n"
            "  destination path is inside sandbox filesystem"
        ),
    )
    cp_p.add_argument("src", help="Local file path")
    cp_p.add_argument("dest", help="Destination path inside image")
    cp_p.set_defaults(handler=cmd_file_cp, load_args=FileCpArgs)

    ls_p = sub.add_parser(
        "ls",
        aliases=["list"],
        help="List uploaded files (joined with local cache)",
        description=(
            "List remote files uploaded to the project and, when present in"
            " the local upload cache, show what produced them under the"
            " 'source' column: either an absolute host path (for run --file"
            " / COPY uploads) or a URL (for ADD URL).\n"
            "\n"
            "source is THIS-MACHINE ONLY: the mapping lives in the local"
            " CLI cache ($CONTREE_HOME/cli/sessions/<profile>.db) and is"
            " never synced. Files uploaded from a different host, by a"
            " teammate, or before tracking landed will show an empty source"
            " -- that is expected, not a bug. Use the remote uuid or sha256"
            " for cross-machine identity."
        ),
        epilog=(
            "examples:\n"
            "  contree file ls\n"
            "  contree file ls --since 1d\n"
            "  contree file ls --limit 5000\n"
            "  contree file ls -q              # uuid + sha256 + source\n"
            "  contree -f json file ls\n"
        ),
    )
    ls_p.add_argument(
        *FLAGS["since"],
        type=parse_interval,
        help=parse_interval.__doc__,
    )
    ls_p.add_argument(
        *FLAGS["until"],
        type=parse_interval,
        help="Show files before. " + str(parse_interval.__doc__),
    )
    ls_p.add_argument(
        *FLAGS["limit"],
        type=positive_int,
        default=FILE_LIST_LIMIT_DEFAULT,
        help="Stop after this many files and warn if more are available",
    )
    ls_p.add_argument(
        *FLAGS["quiet"],
        action="store_true",
        help=(
            "Emit only uuid, sha256, and source columns. source is populated"
            " only for files uploaded from this very machine."
        ),
    )
    ls_p.set_defaults(handler=cmd_file_ls, load_args=FileListArgs)

    return cmd_file_edit, FileEditArgs


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(256 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _upload_and_record(
    client: ContreeClient,
    store: SessionStore,
    local_path: Path,
    instance_path: str,
    title: str,
) -> str:
    """Upload a local file (with dedup) and record as pending."""
    sha = _file_sha256(local_path)
    try:
        resp = client.get(f"/v1/files/{sha}")
        file_uuid = json.loads(resp.read())["uuid"]
        logger.info("File already exists on server (%s)", file_uuid)
    except ApiError as exc:
        if exc.status != 404:
            raise
        with open(local_path, "rb") as fh:
            resp = client.request(
                "POST",
                "/v1/files",
                body=fh,
                headers={"Content-Type": "application/octet-stream"},
            )
            file_uuid = json.loads(resp.read())["uuid"]
        logger.info("Uploaded %s (%s)", instance_path, file_uuid)

    history_id = store.set_image(
        store.current_image,
        kind="file",
        title=title,
    )
    store.add_pending_file(history_id, instance_path, str(file_uuid))
    logger.info(
        "Pending: %s -> %s (will be included in next run)",
        instance_path,
        file_uuid,
    )
    return str(file_uuid)


def cmd_file_edit(args: FileEditArgs) -> int | None:
    client = CLIENT.get()
    store = SESSION_STORE.get()
    image_uuid = resolve_image(client, store.current_image)

    # 1. Download to temp file (or create empty)
    tmp_dir = Path(tempfile.mkdtemp(prefix="contree-"))
    filename = Path(args.path).name or "file"
    tmp_file = tmp_dir / filename
    try:
        resp = client.get(
            f"/v1/inspect/{image_uuid}/download",
            params={"path": args.path},
        )
        with tmp_file.open("wb") as f:
            for chunk in stream_response(resp):
                f.write(chunk)
        logger.info("Downloaded %s to %s", args.path, tmp_file)
    except ApiError as exc:
        if exc.status != 404:
            raise
        tmp_file.write_bytes(b"")
        logger.info("File %s not found, creating empty file", args.path)

    # 2. Record original hash, open editor
    original_hash = _file_sha256(tmp_file)
    logger.info("Opening %s in %s", tmp_file, args.editor)
    # $EDITOR may contain shell expressions (env vars, tilde, pipes),
    # e.g. "TERM=xterm vim" or "~/bin/editor". shlex.split would not
    # expand those, shell=True is required. The file path is quoted
    # via shlex.quote to prevent injection from the filename.
    # nosemgrep: subprocess-shell-true
    rc = subprocess.call(f"{args.editor} {shlex.quote(str(tmp_file))}", shell=True)
    if rc != 0:
        logger.error("Editor exited with code %d", rc)
        return 1

    # 3. Check for changes
    new_hash = _file_sha256(tmp_file)
    if new_hash == original_hash:
        logger.info("No changes detected, skipping upload")
        return None

    # 4+5. Upload (with dedup) and record pending file
    _upload_and_record(
        client,
        store,
        tmp_file,
        args.path,
        title=f"Change file {args.path}",
    )
    return None


def cmd_file_cp(args: FileCpArgs) -> int | None:
    client = CLIENT.get()
    store = SESSION_STORE.get()
    local_path = Path(args.src)
    if not local_path.is_file():
        logger.error("File not found: %s", args.src)
        return 1
    _upload_and_record(
        client,
        store,
        local_path,
        args.dest,
        title=f"Change file {args.dest}",
    )
    return None


def cmd_file_ls(args: FileListArgs) -> int | None:
    client = CLIENT.get()
    store = SESSION_STORE.get()
    formatter = FORMATTER.get()

    sources = store.cache.local_file_paths()

    params: dict[str, str] = {}
    if args.since is not None:
        params["since"] = isoformat_datetime(args.since)
    if args.until is not None:
        params["until"] = isoformat_datetime(args.until)

    offset = 0
    emitted = 0
    while emitted < args.limit:
        page_size = min(FILE_LIST_PAGE_SIZE, args.limit - emitted)
        page = {**params, "offset": str(offset), "limit": str(page_size)}
        resp = client.get("/v1/files", params=page)
        data = json.loads(resp.read())
        files = data.get("files", [])
        if not files:
            return None
        for entry in files:
            uuid_str = entry.get("uuid")
            source = sources.get(uuid_str, "") if isinstance(uuid_str, str) else ""
            if args.quiet:
                formatter(
                    uuid=uuid_str,
                    sha256=entry.get("sha256", ""),
                    source=source,
                )
                continue
            row: dict[str, object] = {}
            for key, value in entry.items():
                if isinstance(value, (dict, list)):
                    continue
                if key in {"created_at", "updated_at"} and isinstance(value, str):
                    value = parse_datetime(value)
                row[key] = value
            row["source"] = source
            formatter(**row)
        emitted += len(files)
        if len(files) < page_size:
            return None
        offset += len(files)

    probe = {**params, "offset": str(offset), "limit": "1"}
    resp = client.get("/v1/files", params=probe)
    data = json.loads(resp.read())
    if data.get("files"):
        formatter.flush()
        logger.warning(
            "Output truncated at --limit=%d files; more results are"
            " available. Raise --limit or narrow with --since/--until.",
            args.limit,
        )
    return None
