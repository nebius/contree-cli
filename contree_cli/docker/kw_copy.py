"""Copy local and image sources through the archive API."""

from __future__ import annotations

import json
import posixpath
import shlex
from collections import deque
from dataclasses import dataclass, field
from typing import IO, ClassVar, TypeVar

from contree_client.exceptions import NotFoundError
from contree_client.models import FileItem

from .archive import (
    ArchiveBuffer,
    archive_is_directory,
    local_source,
    rewrite_image_archive,
    write_local_archive,
)
from .context import BuildContext, resolve_stage_ref
from .keyword import DockerKeyword
from .kw_from import resolve_or_import
from .metadata import inspect_destination
from .transfer import materialize, stage_archive

T = TypeVar("T", bound=DockerKeyword)

SPOOL_MAX_MEMORY = 8 * 1024 * 1024


@dataclass(frozen=True, repr=False)
class CopyKeyword(DockerKeyword):
    NAME: ClassVar[str] = "COPY"
    sources: tuple[str, ...] = field(default_factory=tuple)
    dest: str = ""
    chown: str = ""
    chmod: str = ""
    from_stage: str = ""

    def __repr__(self) -> str:
        return format_copy_like("COPY", self)

    @classmethod
    def parse(cls, args_text: str) -> CopyKeyword:
        return parse_copy_like(cls, args_text, "COPY")

    def serialize(self) -> str:
        return (
            f"COPY from={self.from_stage} chown={self.chown} chmod={self.chmod} "
            f"sources={json.dumps(list(self.sources))} dest={self.dest}"
        )

    def execute(self, ctx: BuildContext) -> None:
        if self.from_stage:
            copy_from_image(
                ctx,
                self.from_stage,
                self.sources,
                self.dest,
                self.chown,
                self.chmod,
            )
            return
        stage_copy(ctx, self.sources, self.dest, self.chown, self.chmod)


def parse_copy_like(cls: type[T], args_text: str, label: str) -> T:
    """Shared parser for COPY and ADD shell-style syntax."""
    raw = args_text.strip()
    if not raw:
        raise ValueError(f"{label} requires SRC and DEST")
    stripped = raw.lstrip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"invalid JSON exec-form: {raw!r}") from exc
        if (
            not isinstance(parsed, list)
            or len(parsed) < 2
            or not all(isinstance(p, str) for p in parsed)
        ):
            raise ValueError(f"{label} exec-form must be a list of >=2 strings")
        return cls(sources=tuple(parsed[:-1]), dest=parsed[-1])  # type: ignore[call-arg]

    tokens = shlex.split(raw)
    chown = ""
    chmod = ""
    from_stage = ""
    positional: list[str] = []
    for t in tokens:
        if t.startswith("--chown="):
            chown = t.partition("=")[2]
        elif t.startswith("--chmod="):
            chmod = t.partition("=")[2]
        elif t.startswith("--from="):
            from_stage = t.partition("=")[2]
        elif t.startswith("--"):
            raise ValueError(f"unknown {label} option: {t!r}")
        else:
            positional.append(t)
    if len(positional) < 2:
        raise ValueError(f"{label} requires at least one source and a destination")
    return cls(  # type: ignore[call-arg]
        sources=tuple(positional[:-1]),
        dest=positional[-1],
        chown=chown,
        chmod=chmod,
        from_stage=from_stage,
    )


def stage_copy(
    ctx: BuildContext,
    sources: tuple[str, ...],
    dest: str,
    chown: str,
    chmod: str,
) -> None:
    """Upload source archives and commit one COPY layer without a guest shell."""
    if ctx.pending or ctx.archives:
        materialize(ctx)
    sub_sources = tuple(ctx.substitute(source) for source in sources)
    sub_dest = ctx.substitute(dest)
    if not posixpath.isabs(sub_dest):
        sub_dest = posixpath.join(ctx.resolve_workdir(), sub_dest)
    owner = parse_chown(ctx.substitute(chown)) if chown else None
    mode = parse_chmod(ctx.substitute(chmod))
    for source in sub_sources:
        if ctx.pending or ctx.archives:
            materialize(ctx)
        path = local_source(ctx.local, source)
        directory = path.is_dir()
        target, name = archive_destination(
            ctx, sub_dest, path.name, directory, len(sub_sources) > 1
        )
        with ArchiveBuffer(max_size=SPOOL_MAX_MEMORY) as buffer:
            info = write_local_archive(
                ctx.local, source, buffer, mode_override=mode, name_override=name
            )
            if info is None:
                continue
            stage_archive(ctx, buffer, info, target, owner)
    if ctx.pending or ctx.archives:
        materialize(ctx)


def archive_destination(
    ctx: BuildContext, dest: str, source_name: str, directory: bool, multiple: bool
) -> tuple[str, str | None]:
    destination_directory = dest.endswith(("/", "/.", "/..")) or multiple
    dest, existing_directory = resolve_destination(ctx, dest)
    if directory:
        return dest, None
    destination_directory = destination_directory or existing_directory
    target = posixpath.join(dest, source_name) if destination_directory else dest
    return posixpath.dirname(target) or "/", posixpath.basename(target)


def resolve_destination(ctx: BuildContext, dest: str) -> tuple[str, bool]:
    """Resolve image symlinks before processing subsequent parent components."""
    listings: dict[str, dict[str, FileItem]] = {}
    components = deque(dest.split("/"))
    resolved: list[str] = []
    entries: list[FileItem | None] = []
    missing_depth: int | None = None
    links = 0
    while components:
        component = components.popleft()
        if not component or component == ".":
            continue
        if entries and entries[-1] is not None and not entries[-1].is_dir:
            raise ValueError(f"COPY destination parent is not a directory: {dest}")
        if component == "..":
            if resolved:
                resolved.pop()
                entries.pop()
            if missing_depth is not None and len(resolved) < missing_depth:
                missing_depth = None
            continue
        path = "/" + "/".join((*resolved, component))
        entry = (
            inspect_destination(ctx, path, listings) if missing_depth is None else None
        )
        if entry is not None and entry.is_symlink:
            links += 1
            if links > 40:
                raise ValueError(f"COPY destination has too many symlinks: {dest}")
            if posixpath.isabs(entry.symlink_to):
                resolved.clear()
                entries.clear()
                missing_depth = None
            components.extendleft(reversed(entry.symlink_to.split("/")))
            continue
        resolved.append(component)
        entries.append(entry)
        if entry is None and missing_depth is None:
            missing_depth = len(resolved)
    return "/" + "/".join(resolved), not entries or (
        entries[-1] is not None and entries[-1].is_dir
    )


def resolve_stage_image(ctx: BuildContext, ref: str) -> str:
    """Map a ``--from`` reference to an image UUID.

    Numeric references address sealed stages by position, names go
    through the alias registry, anything else is treated as an
    external image reference (docker parity: ``--from=image:tag``),
    auto-imported from a registry on a resolve miss just like ``FROM``.
    """
    local = resolve_stage_ref(ctx, ref)
    if local is not None:
        return local
    return resolve_or_import(ctx, ref)


def fetch_archive(
    ctx: BuildContext,
    stage_ref: str,
    image_uuid: str,
    src: str,
    buffer: IO[bytes],
) -> None:
    """Fill *buffer* with the tar export of *src* from *image_uuid*."""
    try:
        for chunk in ctx.client.inspect_image_archive(
            image_uuid, src, compressed=False
        ):
            buffer.write(chunk)
    except NotFoundError:
        raise ValueError(
            f"COPY --from={stage_ref}: {src} not found in {image_uuid}"
        ) from None
    buffer.seek(0)


def copy_from_image(
    ctx: BuildContext,
    from_stage: str,
    sources: tuple[str, ...],
    dest: str,
    chown: str,
    chmod: str,
) -> None:
    """Download image paths and apply normalized archives through guest RPC."""
    if ctx.pending or ctx.archives:
        materialize(ctx)
    stage_ref = ctx.substitute(from_stage)
    image_uuid = resolve_stage_image(ctx, stage_ref)
    sub_sources = tuple(ctx.substitute(source) for source in sources)
    sub_dest = ctx.substitute(dest)
    if not posixpath.isabs(sub_dest):
        sub_dest = posixpath.join(ctx.resolve_workdir(), sub_dest)
    owner = parse_chown(ctx.substitute(chown)) if chown else None
    mode = parse_chmod(ctx.substitute(chmod))
    for source in sub_sources:
        if ctx.pending or ctx.archives:
            materialize(ctx)
        source = posixpath.normpath(posixpath.join("/", source))
        with (
            ArchiveBuffer(max_size=SPOOL_MAX_MEMORY) as downloaded,
            ArchiveBuffer(max_size=SPOOL_MAX_MEMORY) as prepared,
        ):
            fetch_archive(ctx, stage_ref, image_uuid, source, downloaded)
            directory = archive_is_directory(downloaded)
            target, name = archive_destination(
                ctx,
                sub_dest,
                posixpath.basename(source),
                directory,
                len(sub_sources) > 1,
            )
            info = rewrite_image_archive(
                downloaded, prepared, mode_override=mode, name_override=name
            )
            stage_archive(ctx, prepared, info, target, owner)
    if ctx.pending or ctx.archives:
        materialize(ctx)


def parse_chown(spec: str) -> tuple[int, int]:
    if not spec:
        return 0, 0
    user, _, group = spec.partition(":")
    uid = resolve_id(user) if user else 0
    gid = resolve_id(group) if group else uid
    return uid, gid


def parse_chmod(spec: str) -> int | None:
    if not spec:
        return None
    try:
        return int(spec, 8)
    except ValueError:
        raise ValueError(f"invalid chmod value: {spec!r}") from None


def resolve_id(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def format_copy_like(name: str, kw: object) -> str:
    flags: list[str] = []
    chown = getattr(kw, "chown", "")
    chmod = getattr(kw, "chmod", "")
    from_stage = getattr(kw, "from_stage", "")
    if from_stage:
        flags.append(f"--from={from_stage}")
    if chown:
        flags.append(f"--chown={chown}")
    if chmod:
        flags.append(f"--chmod={chmod}")
    sources = list(getattr(kw, "sources", ()))
    dest = getattr(kw, "dest", "")
    return " ".join([name, *flags, *sources, dest])
