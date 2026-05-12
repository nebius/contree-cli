"""``USER name[:group]`` - run subsequent commands as the given user."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .context import BuildContext
from .keyword import DockerKeyword


@dataclass(frozen=True)
class UserKeyword(DockerKeyword):
    NAME: ClassVar[str] = "USER"
    spec: str = ""

    @classmethod
    def parse(cls, args_text: str) -> UserKeyword:
        raw = args_text.strip()
        if not raw:
            raise ValueError("USER requires a name")
        return cls(spec=raw)

    def serialize(self) -> str:
        return f"USER {self.spec}"

    def execute(self, ctx: BuildContext) -> None:
        ctx.user = ctx.substitute(self.spec)
