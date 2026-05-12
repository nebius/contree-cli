"""``ADD [--chown=...] [--chmod=...] SRC... DEST`` - file/dir variant of COPY.

URL fetches and tar extraction (the parts of ``ADD`` that distinguish it from
``COPY``) are not supported in the MVP - those inputs emit a warning and are
skipped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import ClassVar

from .context import BuildContext
from .keyword import DockerKeyword
from .kw_copy import parse_copy_like, stage_copy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AddKeyword(DockerKeyword):
    NAME: ClassVar[str] = "ADD"
    sources: tuple[str, ...] = field(default_factory=tuple)
    dest: str = ""
    chown: str = ""
    chmod: str = ""
    from_stage: str = ""

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

        url_sources = [s for s in self.sources if is_url(s)]
        if url_sources:
            for url in url_sources:
                logger.warning("ADD URL %s not supported, skipping", url)
            local_sources = tuple(s for s in self.sources if not is_url(s))
            if not local_sources:
                return
            stage_copy(ctx, local_sources, self.dest, self.chown, self.chmod)
            return

        stage_copy(ctx, self.sources, self.dest, self.chown, self.chmod)


def is_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "ftp://"))
