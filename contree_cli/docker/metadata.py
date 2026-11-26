"""Restore COPY ownership and modes without changing an existing destination root."""

from __future__ import annotations

import posixpath
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from contree_client.exceptions import NotFoundError
from contree_client.models import FileItem

from .context import BuildContext

if TYPE_CHECKING:
    from .transfer import StagedArchive


METADATA_BATCH_PATHS = 128
METADATA_BATCH_BYTES = 16 * 1024


def metadata_batches(paths: list[str]) -> Iterator[list[str]]:
    batch: list[str] = []
    size = 0
    for path in paths:
        path_size = len(path.encode("utf-8")) + 1
        if batch and (
            len(batch) >= METADATA_BATCH_PATHS
            or size + path_size > METADATA_BATCH_BYTES
        ):
            yield batch
            batch = []
            size = 0
        batch.append(path)
        size += path_size
    if batch:
        yield batch


def inspect_destination(
    ctx: BuildContext, path: str, listings: dict[str, dict[str, FileItem]]
) -> FileItem | None:
    parent = posixpath.dirname(path)
    if parent not in listings:
        try:
            listing = ctx.client.inspect_image_list(ctx.last_image, parent)
        except NotFoundError:
            listings[parent] = {}
        else:
            listings[parent] = {
                posixpath.normpath(posixpath.join(parent, item.path)): item
                for item in listing.files
            }
    return listings[parent].get(path)


@dataclass(frozen=True)
class DirectoryState:
    existing: FileItem | None
    uid: int = 0
    gid: int = 0
    mode: int = 0o755


def directory_state(
    ctx: BuildContext,
    path: str,
    listings: dict[str, dict[str, FileItem]],
    states: dict[str, DirectoryState],
) -> DirectoryState:
    """Infer ownership before merge applies directory modes in postorder."""
    if path in states:
        return states[path]
    if path == "/":
        return DirectoryState(None)
    parent = posixpath.dirname(path)
    parent_state = directory_state(ctx, parent, listings, states)
    existing = (
        inspect_destination(ctx, path, listings)
        if parent == "/" or parent_state.existing is not None
        else None
    )
    if existing is not None and (not existing.is_dir or existing.is_symlink):
        existing = None
    if existing is not None:
        state = DirectoryState(existing, existing.uid, existing.gid, existing.mode)
    else:
        setgid = parent_state.mode & 0o2000
        state = DirectoryState(
            None, gid=parent_state.gid if setgid else 0, mode=0o755 | setgid
        )
    states[path] = state
    return state


def restore_metadata(ctx: BuildContext, operation: str, archive: StagedArchive) -> None:
    from .transfer import run_metadata_command

    root = posixpath.normpath(archive.path)
    if not posixpath.isabs(root):
        raise ValueError(f"COPY metadata destination is not absolute: {root}")
    listings: dict[str, dict[str, FileItem]] = {}
    states: dict[str, DirectoryState] = {}
    staging_parent = directory_state(ctx, posixpath.dirname(root), listings, states)
    staging_gid = staging_parent.gid if staging_parent.mode & 0o2000 else 0
    owners: dict[tuple[int, int], list[str]] = {}
    modes: dict[int, list[str]] = {}
    for entry in reversed(archive.info.entries):
        path = posixpath.normpath(posixpath.join(root, entry.path))
        if path != root and not path.startswith(root.rstrip("/") + "/"):
            raise ValueError(f"COPY metadata path escapes destination: {entry.path}")
        state = (
            directory_state(ctx, path, listings, states) if entry.is_directory else None
        )
        existing = state.existing if state is not None else None
        if path == root and archive.preserve_root and existing is not None:
            mode = existing.mode & 0o7777
            if mode != (entry.mode & 0o777):
                modes.setdefault(mode, []).append(path)
            continue
        owner = archive.owner or (entry.uid, entry.gid)
        if path == root and archive.preserve_root and archive.owner is None:
            owner = (0, 0)
        current_owner = (
            (state.uid, state.gid) if state is not None else (0, staging_gid)
        )
        if owner != current_owner:
            owners.setdefault(owner, []).append(path)
        if entry.is_symlink:
            continue
        mode = entry.mode & 0o7777
        if mode & 0o7000:
            modes.setdefault(mode, []).append(path)
    for (uid, gid), paths in owners.items():
        for batch in metadata_batches(paths):
            run_metadata_command(
                ctx, operation, "chown", ["-h", f"{uid}:{gid}", *batch]
            )
    for mode, paths in modes.items():
        for batch in metadata_batches(paths):
            run_metadata_command(ctx, operation, "chmod", [f"{mode:o}", *batch])
