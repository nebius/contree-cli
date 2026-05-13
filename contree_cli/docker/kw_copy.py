"""``COPY [--chown=...] [--chmod=...] SRC... DEST`` - stage files into the build."""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
from dataclasses import dataclass, field
from typing import ClassVar, TypeVar

from contree_cli.cli.run import upload_files

from .context import BuildContext, PendingFile
from .keyword import DockerKeyword

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=DockerKeyword)


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
            f"COPY chown={self.chown} chmod={self.chmod} "
            f"sources={json.dumps(list(self.sources))} dest={self.dest}"
        )

    def execute(self, ctx: BuildContext) -> None:
        if self.from_stage:
            logger.warning("COPY --from=%s not supported, skipping", self.from_stage)
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
