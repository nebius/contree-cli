"""Prepare deterministic COPY archives without executing guest commands."""

from __future__ import annotations

import copy
import os
import posixpath
import stat
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import IO

from .dockerignore import may_include_descendants
from .local_context import LocalContext, matches_default_excludes


class ArchiveBuffer(tempfile.SpooledTemporaryFile[bytes]):
    """Provide tarfile seekability for memory and disk spools on Python 3.10."""

    def seekable(self) -> bool:
        self.tell()
        return True


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    uid: int
    gid: int
    mode: int
    is_symlink: bool
    is_directory: bool = False


@dataclass(frozen=True)
class ArchiveInfo:
    is_directory: bool
    entries: tuple[ArchiveEntry, ...]


def canonical_member(
    member: tarfile.TarInfo, mode_override: int | None
) -> tuple[tarfile.TarInfo, ArchiveEntry]:
    """Keep restoration metadata separately from the runtime archive format."""
    member = copy.copy(member)
    mode = member.mode if mode_override is None or member.issym() else mode_override
    entry = ArchiveEntry(
        member.name, member.uid, member.gid, mode, member.issym(), member.isdir()
    )
    member.mode = mode & 0o777
    if member.isreg():
        member.type = tarfile.REGTYPE
        member.sparse = None
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    member.mtime = 0
    member.pax_headers = {}
    return member, entry


def local_source(local: LocalContext, source: str) -> Path:
    """Validate an explicit source's target and retain its requested basename."""
    path = Path(os.path.abspath(local.root / source))
    try:
        path.relative_to(local.root)
        path.resolve().relative_to(local.root)
    except ValueError:
        raise ValueError(f"COPY/ADD source escapes context: {source!r}") from None
    return path


def local_archive_paths(
    local: LocalContext, path: Path, *, follow_symlink: bool = False
) -> Iterator[Path]:
    """Retain excluded parents only when an included descendant needs them."""
    resolved = path.resolve() if follow_symlink else path.parent.resolve() / path.name
    relative_paths = tuple(
        candidate.relative_to(local.root).as_posix()
        for candidate in (path, resolved)
        if candidate != local.root
    )
    if any(matches_default_excludes(relative) for relative in relative_paths):
        return
    ignored = any(local.is_ignored(relative) for relative in relative_paths)
    if ignored and any(
        not may_include_descendants(relative, local.dockerignore)
        for relative in relative_paths
    ):
        return
    status = path.stat() if follow_symlink else path.lstat()
    if not stat.S_ISDIR(status.st_mode):
        if not ignored:
            yield path
        return
    children = (
        included
        for child in sorted(path.iterdir())
        for included in local_archive_paths(local, child)
    )
    if ignored:
        first = next(children, None)
        if first is None:
            return
        yield path
        yield first
    else:
        yield path
    yield from children


def write_local_archive(
    local: LocalContext,
    source: str,
    output: IO[bytes],
    *,
    mode_override: int | None = None,
    name_override: str | None = None,
) -> ArchiveInfo | None:
    """Dereference an explicit source and preserve symlinks inside directories."""
    path = local_source(local, source)
    selected = local_archive_paths(local, path, follow_symlink=True)
    first = next(selected, None)
    if first is None:
        return None
    source_stat = path.stat()
    is_directory = stat.S_ISDIR(source_stat.st_mode)
    name = archive_leaf_name(path.name, is_directory, name_override)
    entries: list[ArchiveEntry] = []
    hardlinks: dict[tuple[int, int], str] = {}
    output.seek(0)
    output.truncate()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for selected_path in chain((first,), selected):
            write_local_entry(
                archive,
                selected_path,
                selected_path.relative_to(path).as_posix() if is_directory else name,
                mode_override,
                entries,
                hardlinks,
                follow_symlink=selected_path == path,
            )
    output.seek(0)
    return ArchiveInfo(is_directory, tuple(entries))


def write_local_entry(
    archive: tarfile.TarFile,
    path: Path,
    name: str,
    mode_override: int | None,
    entries: list[ArchiveEntry],
    hardlinks: dict[tuple[int, int], str],
    *,
    follow_symlink: bool = False,
) -> None:
    status = path.stat() if follow_symlink else path.lstat()
    member = tarfile.TarInfo(name)
    member.mode = stat.S_IMODE(status.st_mode)
    if stat.S_ISDIR(status.st_mode):
        member.type = tarfile.DIRTYPE
    elif stat.S_ISLNK(status.st_mode):
        member.type = tarfile.SYMTYPE
        member.linkname = os.readlink(path)
    elif stat.S_ISREG(status.st_mode):
        key = (status.st_dev, status.st_ino)
        if status.st_nlink > 1 and key in hardlinks:
            member.type = tarfile.LNKTYPE
            member.linkname = hardlinks[key]
        else:
            member.size = status.st_size
            hardlinks[key] = name
    else:
        raise ValueError(f"COPY/ADD source has unsupported file type: {path}")
    member, entry = canonical_member(member, mode_override)
    entries.append(entry)
    if member.isreg():
        with path.open("rb") as content:
            archive.addfile(member, content)
    else:
        archive.addfile(member)


def archive_path(name: str) -> str:
    """Validate an archive path before removing redundant dot components."""
    if name.startswith("/") or ".." in name.split("/") or "\x00" in name:
        raise ValueError(f"COPY archive contains an unsafe path: {name!r}")
    return posixpath.normpath(name)


def archive_leaf_name(name: str, is_directory: bool, name_override: str | None) -> str:
    if name_override is None:
        return name
    if is_directory:
        raise ValueError("Directory archives cannot rename their root")
    if (
        not name_override
        or name_override in {".", ".."}
        or "/" in name_override
        or "\x00" in name_override
    ):
        raise ValueError(f"Invalid COPY destination filename: {name_override!r}")
    return name_override


def archive_is_directory(source: IO[bytes]) -> bool:
    """Identify a directory archive without consuming the input stream."""
    source.seek(0)
    try:
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            first = next(iter(archive), None)
            if first is None:
                raise ValueError("COPY --from: source archive is empty")
            name = archive_path(first.name)
            return first.isdir() or "/" in name
    finally:
        source.seek(0)


def rewrite_image_archive(
    source: IO[bytes],
    output: IO[bytes],
    *,
    mode_override: int | None = None,
    name_override: str | None = None,
) -> ArchiveInfo:
    """Remove a directory archive's root prefix and rewrite internal hardlinks."""
    source.seek(0)
    output.seek(0)
    output.truncate()
    entries: list[ArchiveEntry] = []
    regular_paths: set[str] = set()
    symlink_paths: set[str] = set()
    with (
        tarfile.open(fileobj=source, mode="r:*") as original,
        tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as result,
    ):
        first = next(iter(original), None)
        if first is None:
            raise ValueError("COPY --from: source archive is empty")
        first_name = archive_path(first.name)
        root = first_name.split("/", 1)[0]
        is_directory = first.isdir() or "/" in first_name
        leaf = archive_leaf_name(root, is_directory, name_override)
        for member in original:
            source_name = archive_path(member.name)
            if (
                root != "."
                and source_name != root
                and not source_name.startswith(root + "/")
            ):
                raise ValueError("COPY --from: archive contains multiple roots")
            if not is_directory and source_name != root:
                raise ValueError("COPY --from: file archive contains extra entries")
            name = strip_archive_root(source_name, root) if is_directory else leaf
            parents = name.split("/")[:-1]
            if any(
                "/".join(parents[:i]) in symlink_paths
                for i in range(1, len(parents) + 1)
            ):
                raise ValueError(f"COPY archive traverses a symlink: {source_name!r}")
            rewritten = copy.copy(member)
            rewritten.name = name
            if member.islnk():
                target = archive_path(member.linkname)
                rewritten.linkname = strip_archive_root(target, root)
                if rewritten.linkname not in regular_paths:
                    raise ValueError(
                        "COPY archive hardlink target is not an earlier file: "
                        f"{target!r}"
                    )
                regular_paths.add(name)
            elif member.issym():
                symlink_paths.add(name)
            elif member.isreg():
                regular_paths.add(name)
            elif not member.isdir():
                raise ValueError(
                    f"COPY archive has unsupported file type: {source_name!r}"
                )
            rewritten, entry = canonical_member(rewritten, mode_override)
            entries.append(entry)
            if member.isreg():
                content = original.extractfile(member)
                if content is None:
                    raise ValueError(
                        f"COPY archive has no file content: {source_name!r}"
                    )
                with content:
                    result.addfile(rewritten, content)
            else:
                result.addfile(rewritten)
    source.seek(0)
    output.seek(0)
    return ArchiveInfo(is_directory, tuple(entries))


def strip_archive_root(name: str, root: str) -> str:
    if root == ".":
        return name
    if name == root:
        return "."
    if not name.startswith(root + "/"):
        raise ValueError(f"COPY archive path is outside its root: {name!r}")
    return name[len(root) + 1 :]
