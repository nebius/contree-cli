"""Keywords that the MVP recognises but does not implement."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .context import BuildContext
from .keyword import DockerKeyword

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class SkippedKeyword(DockerKeyword):
    name: str = ""
    raw: str = ""

    def __repr__(self) -> str:
        return f"{self.name} {self.raw}".rstrip()

    @classmethod
    def of(cls, name: str, raw: str) -> SkippedKeyword:
        return cls(name=name.upper(), raw=raw)

    def serialize(self) -> str:
        return f"{self.name}:{self.raw}"

    def execute(self, ctx: BuildContext) -> None:
        logger.warning("directive %s not supported, skipping", self.name)
