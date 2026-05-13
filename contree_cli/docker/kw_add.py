"""``ADD [--chown=...] [--chmod=...] SRC... DEST`` - file/dir variant of COPY.

URL sources are streamed straight from the upstream socket into
``POST /v1/files`` (no local disk copy) and cached by URL with their
HTTP validators so the next build reuses the remote ``file_uuid``
whenever the upstream's ``ETag``/``Last-Modified``/``Content-MD5`` still
matches. Local sources fall through to the same walker that ``COPY``
uses. Tar auto-extraction is not implemented.
"""

from __future__ import annotations

import json
import logging
import posixpath
from dataclasses import dataclass, field
from typing import ClassVar

from .context import BuildContext, PendingFile
from .keyword import DockerKeyword
from .kw_copy import (
    format_copy_like,
    parse_chmod,
    parse_chown,
    parse_copy_like,
    stage_copy,
)
from .url_fetch import FetchedUrl, fetch_and_upload, is_url, url_basename

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class AddKeyword(DockerKeyword):
    NAME: ClassVar[str] = "ADD"
    sources: tuple[str, ...] = field(default_factory=tuple)
    dest: str = ""
    chown: str = ""
    chmod: str = ""
    from_stage: str = ""

    def __repr__(self) -> str:
        return format_copy_like("ADD", self)

    @classmethod
    def parse(cls, args_text: str) -> AddKeyword:
        return parse_copy_like(cls, args_text, "ADD")

    def serialize(self) -> str:
        return (
            f"ADD chown={self.chown} chmod={self.chmod} "
            f"sources={json.dumps(list(self.sources))} dest={self.dest}"
        )

    def execute(self, ctx: BuildContext) -> None:
        if self.from_stage:
            logger.warning("ADD --from=%s not supported, skipping", self.from_stage)
            return

        sub_dest = ctx.substitute(self.dest)
        if not posixpath.isabs(sub_dest):
            sub_dest = posixpath.normpath(posixpath.join(ctx.workdir or "/", sub_dest))

        local_sources: list[str] = []
        url_sources: list[str] = []
        for raw in self.sources:
            value = ctx.substitute(raw)
            (url_sources if is_url(value) else local_sources).append(value)

        if url_sources:
            for url, fetched in stage_urls(
                ctx,
                tuple(url_sources),
                sub_dest,
                chown=ctx.substitute(self.chown),
                chmod=ctx.substitute(self.chmod),
                multi_source=len(self.sources) > 1,
            ):
                if fetched.cache_state == "head":
                    logger.info("CACHED: %r (HEAD validators match): %s", self, url)
                elif fetched.cache_state == "get-304":
                    logger.info("CACHED: %r (GET 304 Not Modified): %s", self, url)

        if local_sources:
            stage_copy(
                ctx,
                tuple(local_sources),
                self.dest,
                self.chown,
                self.chmod,
            )


def stage_urls(
    ctx: BuildContext,
    urls: tuple[str, ...],
    dest: str,
    *,
    chown: str,
    chmod: str,
    multi_source: bool,
) -> list[tuple[str, FetchedUrl]]:
    """Stream each URL into ``POST /v1/files`` and stage a pending file.

    Returns ``[(url, FetchedUrl), ...]`` so the caller can decide how to
    log the outcome (``fetched.cache_state`` tells whether the upstream
    was downloaded or short-circuited via HEAD/304).
    """
    uid, gid = parse_chown(chown)
    mode_override = parse_chmod(chmod)
    dest_is_dir = dest.endswith("/") or multi_source

    fetches: list[tuple[str, FetchedUrl]] = []
    for url in urls:
        fetched = fetch_and_upload(url, ctx.client, ctx.store, timeout=ctx.timeout)
        if dest_is_dir:
            instance_path = posixpath.join(dest.rstrip("/"), url_basename(url))
        else:
            instance_path = dest
        mode = mode_override if mode_override is not None else 0o644
        ctx.pending.append(
            PendingFile(
                instance_path=instance_path,
                file_uuid=fetched.file_uuid,
                sha256=fetched.sha256,
                uid=uid,
                gid=gid,
                mode=f"{mode:04o}",
            )
        )
        fetches.append((url, fetched))
    return fetches
