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
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from contree_cli import CLIENT, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError, ContreeClient, resolve_image, stream_response
from contree_cli.config import CLI_CONFIG_FILE, CliSettings
from contree_cli.session import SessionStore
from contree_cli.types import FLAGS

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
        help="Editor command (default: $EDITOR or vi)",
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
        resp = client.get("/v1/files", params={"sha256": sha})
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
    cli_defaults = CliSettings.load(CLI_CONFIG_FILE)
    editor = args.editor or os.environ.get("EDITOR") or cli_defaults.editor or "vi"
    logger.info("Opening %s in %s", tmp_file, editor)
    # $EDITOR may contain shell expressions (env vars, tilde, pipes),
    # e.g. "TERM=xterm vim" or "~/bin/editor". shlex.split would not
    # expand those — shell=True is required. The file path is quoted
    # via shlex.quote to prevent injection from the filename.
    # nosemgrep: subprocess-shell-true
    rc = subprocess.call(f"{editor} {shlex.quote(str(tmp_file))}", shell=True)
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
