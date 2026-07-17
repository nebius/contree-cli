"""``COPY [--from=...] [--chown=...] [--chmod=...] SRC... DEST``.

Local sources are uploaded from the build context and attached to the
next RUN. ``--from=<stage|index|image>`` sources are exported from the
referenced image as a tar archive, uploaded as a single file and
unpacked by an extraction RUN inside the sandbox, so ownership, modes
and symlinks survive natively. For now the extraction needs
``/bin/sh``, ``tar``, ``cp`` and ``mv`` in the target image (busybox
suffices; ``FROM scratch`` targets cannot receive ``COPY --from``) -
a temporary limitation that goes away once the backend unpacks
archives itself.
"""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import IO, ClassVar, TypeVar

from contree_client.exceptions import NotFoundError

from contree_cli.cli.run import upload_files

from .context import BuildContext, PendingFile
from .keyword import DockerKeyword
from .kw_run import RunKeyword

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=DockerKeyword)

SCRATCH_DIR = "/.contree-build"
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
    """Resolve sources via ``LocalContext``, upload, append to ``ctx.pending``."""
    sub_sources = tuple(ctx.substitute(s) for s in sources)
    sub_dest = ctx.substitute(dest)
    sub_chown = ctx.substitute(chown)
    sub_chmod = ctx.substitute(chmod)

    if not posixpath.isabs(sub_dest):
        sub_dest = posixpath.normpath(posixpath.join(ctx.workdir or "/", sub_dest))

    uid, gid = parse_chown(sub_chown)
    mode_override = parse_chmod(sub_chmod)

    mapped = ctx.local.collect(
        sub_sources,
        sub_dest,
        uid=uid,
        gid=gid,
        mode_override=mode_override,
    )
    if not mapped:
        return

    uploaded = upload_files(ctx.client, mapped, ctx.store)
    for mf in mapped:
        ctx.pending.append(
            PendingFile(
                instance_path=mf.instance_path,
                file_uuid=uploaded[mf.host_path],
                sha256=mf.sha256(),
                uid=mf.uid,
                gid=mf.gid,
                mode=f"{mf.mode:04o}",
            )
        )


def resolve_stage_image(ctx: BuildContext, ref: str) -> str:
    """Map a ``--from`` reference to an image UUID.

    Numeric references address sealed stages by position, names go
    through the alias registry, anything else is treated as an
    external image reference (docker parity: ``--from=image:tag``).
    """
    if ref.isdigit():
        index = int(ref)
        if index >= len(ctx.stage_images):
            raise ValueError(
                f"COPY --from={ref}: stage index out of range"
                f" ({len(ctx.stage_images)} stage(s) sealed so far)"
            )
        return ctx.stage_images[index]
    if ref in ctx.stages:
        return ctx.stages[ref]
    try:
        return ctx.client.resolve_image(ref)
    except NotFoundError:
        raise ValueError(
            f"COPY --from={ref}: unknown build stage and no such image"
        ) from None


def fetch_archive(
    ctx: BuildContext,
    stage_ref: str,
    image_uuid: str,
    src: str,
    buffer: IO[bytes],
) -> None:
    """Fill *buffer* with the tar export of *src* from *image_uuid*."""
    try:
        for chunk in ctx.client.inspect_image_archive(image_uuid, src):
            buffer.write(chunk)
    except NotFoundError:
        raise ValueError(
            f"COPY --from={stage_ref}: {src} not found in {image_uuid}"
        ) from None
    buffer.seek(0)


def archive_root(buffer: IO[bytes], src: str) -> tuple[str, bool]:
    """Peek the buffered tar: its root member name and directory-ness.

    Archiving a directory yields members rooted at the directory
    basename (``/etc`` -> ``etc/hosts``); a single file yields one
    entry named after the file. The peek only drives the extraction
    command - the archive itself is uploaded untouched.
    """
    with tarfile.open(fileobj=buffer, mode="r:") as tar:
        member = tar.next()
        if member is None:
            raise ValueError(f"COPY --from: empty archive for {src}")
        root = member.name.split("/", 1)[0]
        is_dir = member.isdir() or member.name.rstrip("/") != root
    buffer.seek(0)
    return root, is_dir


def copy_from_image(
    ctx: BuildContext,
    from_stage: str,
    sources: tuple[str, ...],
    dest: str,
    chown: str,
    chmod: str,
) -> None:
    """Stage a ``COPY --from`` directive as one extraction layer.

    Each source is exported from the referenced image as a tar
    archive, uploaded once (deduplicated) and attached under
    ``/.contree-build``; a single RUN then unpacks every archive into
    place and removes the scratch directory. Ownership and modes come
    from the tar unless ``--chown``/``--chmod`` override them.
    """
    stage_ref = ctx.substitute(from_stage)
    image_uuid = resolve_stage_image(ctx, stage_ref)

    sub_sources = tuple(ctx.substitute(s) for s in sources)
    sub_dest = ctx.substitute(dest)
    dest_is_dir = sub_dest.endswith("/") or len(sub_sources) > 1
    if not posixpath.isabs(sub_dest):
        sub_dest = posixpath.join(ctx.workdir or "/", sub_dest)
    sub_dest = posixpath.normpath(sub_dest)

    sub_chown = ctx.substitute(chown)
    sub_chmod = ctx.substitute(chmod)
    uid, gid = parse_chown(sub_chown)
    mode_override = parse_chmod(sub_chmod)

    script: list[str] = []
    for index, raw_src in enumerate(sub_sources):
        src = posixpath.normpath(posixpath.join("/", raw_src))
        tar_path = f"{SCRATCH_DIR}/copy-{index}.tar"
        extract_dir = f"{SCRATCH_DIR}/extract-{index}"

        # Small archives stay in memory; big ones spill to a temp file.
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY) as buffer:
            fetch_archive(ctx, stage_ref, image_uuid, src, buffer)
            root, is_dir = archive_root(buffer, src)
            stored = ctx.client.ensure_file(buffer)

        ctx.pending.append(
            PendingFile(
                instance_path=tar_path,
                file_uuid=str(stored.uuid),
                sha256=str(stored.sha256),
                uid=0,
                gid=0,
                mode="0600",
            )
        )

        unpacked = f"{extract_dir}/{root}"
        script.append(f"mkdir -p {shlex.quote(extract_dir)}")
        script.append(f"tar -xf {shlex.quote(tar_path)} -C {shlex.quote(extract_dir)}")
        if sub_chown:
            script.append(f"chown -R {uid}:{gid} {shlex.quote(unpacked)}")
        if mode_override is not None:
            flag = "-R " if is_dir else ""
            script.append(f"chmod {flag}{mode_override:o} {shlex.quote(unpacked)}")
        if is_dir:
            # Docker copies the CONTENTS of a directory source.
            script.append(f"mkdir -p {shlex.quote(sub_dest)}")
            script.append(f"cp -a {shlex.quote(unpacked)}/. {shlex.quote(sub_dest)}/")
        else:
            if dest_is_dir:
                target = posixpath.join(sub_dest, posixpath.basename(src))
            else:
                target = sub_dest
            script.append(f"mkdir -p {shlex.quote(posixpath.dirname(target) or '/')}")
            script.append(f"mv {shlex.quote(unpacked)} {shlex.quote(target)}")

    script.append(f"rm -rf {shlex.quote(SCRATCH_DIR)}")
    command = " && ".join(script)

    # The extraction runs as root regardless of an active USER (docker
    # COPY semantics); --chown above handles ownership.
    saved_user = ctx.user
    ctx.user = ""
    try:
        RunKeyword(parts=(command,), shell_form=True).execute(ctx)
    finally:
        ctx.user = saved_user


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
