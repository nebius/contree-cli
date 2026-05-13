"""Local build context: the host directory + ``.dockerignore`` filter.

Encapsulates everything we need to assemble the set of files that will be
uploaded to the API as part of a build: the root directory, the parsed
``.dockerignore`` rules, and the directory-walking logic that turns
``COPY``/``ADD`` source specs into concrete ``MappedFile`` entries.
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from contree_cli.cli.run import DEFAULT_FILE_EXCLUDES
from contree_cli.mapped_file import MappedFile

from .dockerignore import DockerignoreRule, is_ignored, parse_dockerignore


@dataclass(frozen=True)
class LocalContext:
    """Read-only handle for the local build context directory."""

    root: Path
    dockerignore: tuple[DockerignoreRule, ...] = field(default_factory=tuple)

    @classmethod
    def from_dir(cls, root: Path) -> LocalContext:
        return cls(root=root.resolve(), dockerignore=parse_dockerignore(root))

    def is_ignored(self, rel_path: str) -> bool:
        if is_ignored(rel_path, self.dockerignore):
            return True
        return matches_default_excludes(rel_path)

    def collect(
        self,
        sources: tuple[str, ...],
        dest: str,
        *,
        uid: int,
        gid: int,
        mode_override: int | None,
    ) -> list[MappedFile]:
        """Walk every source, return ``MappedFile`` rows for upload."""
        mapped: list[MappedFile] = []
        for src in sources:
            host_path = (self.root / src).resolve()
            if not str(host_path).startswith(str(self.root)):
                raise ValueError(f"COPY/ADD source escapes context: {src!r}")
            mapped.extend(self.walk(host_path, dest, sources, uid, gid, mode_override))
        return mapped

    def walk(
        self,
        host_path: Path,
        dest: str,
        sources: tuple[str, ...],
        uid: int,
        gid: int,
        mode_override: int | None,
    ) -> list[MappedFile]:
        if host_path.is_file():
            return self.walk_file(host_path, dest, sources, uid, gid, mode_override)
        if host_path.is_dir():
            return self.walk_dir(host_path, dest, uid, gid, mode_override)
        raise FileNotFoundError(f"COPY/ADD source not found: {host_path}")

    def walk_file(
        self,
        host_path: Path,
        dest: str,
        sources: tuple[str, ...],
        uid: int,
        gid: int,
        mode_override: int | None,
    ) -> list[MappedFile]:
        rel = host_path.relative_to(self.root).as_posix()
        if self.is_ignored(rel):
            return []
        if dest.endswith("/") or len(sources) > 1:
            instance_path = posixpath.join(dest.rstrip("/"), host_path.name)
        else:
            instance_path = dest
        mode = (
            mode_override
            if mode_override is not None
            else (host_path.stat().st_mode & 0o7777)
        )
        return [
            MappedFile(
                host_path=str(host_path),
                instance_path=instance_path,
                uid=uid,
                gid=gid,
                mode=mode,
            )
        ]

    def walk_dir(
        self,
        host_path: Path,
        dest: str,
        uid: int,
        gid: int,
        mode_override: int | None,
    ) -> list[MappedFile]:
        base = dest.rstrip("/") or "/"
        result: list[MappedFile] = []
        for root, dirs, files in os.walk(str(host_path), topdown=True):
            rel_root = os.path.relpath(root, str(self.root))
            rel_root_posix = "" if rel_root == "." else rel_root.replace(os.sep, "/")
            dirs[:] = [
                d
                for d in dirs
                if not self.is_ignored(
                    d if not rel_root_posix else f"{rel_root_posix}/{d}"
                )
            ]
            for name in files:
                rel_file = name if not rel_root_posix else f"{rel_root_posix}/{name}"
                if self.is_ignored(rel_file):
                    continue
                full = os.path.join(root, name)
                if not os.path.isfile(full):
                    continue
                # Path of the file relative to the *source* dir so that
                # directory copies preserve their internal layout under DEST.
                rel_to_source = os.path.relpath(full, str(host_path))
                rel_to_source_posix = rel_to_source.replace(os.sep, "/")
                instance_path = f"{base.rstrip('/')}/{rel_to_source_posix}"
                mode = (
                    mode_override
                    if mode_override is not None
                    else (os.stat(full).st_mode & 0o7777)
                )
                result.append(
                    MappedFile(
                        host_path=full,
                        instance_path=instance_path,
                        uid=uid,
                        gid=gid,
                        mode=mode,
                    )
                )
        return result


def matches_default_excludes(rel_path: str) -> bool:
    parts = rel_path.split("/")
    for pattern in DEFAULT_FILE_EXCLUDES:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False
