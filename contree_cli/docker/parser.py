"""Parse a Dockerfile into a list of ``DockerKeyword`` instances."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

from .keyword import DockerKeyword
from .kw_add import AddKeyword
from .kw_arg import ArgKeyword
from .kw_copy import CopyKeyword
from .kw_env import EnvKeyword
from .kw_from import FromKeyword
from .kw_run import RunKeyword
from .kw_skipped import SkippedKeyword
from .kw_user import UserKeyword
from .kw_workdir import WorkdirKeyword

logger = logging.getLogger(__name__)


KEYWORDS: Mapping[str, type[DockerKeyword]] = MappingProxyType(
    {
        "FROM": FromKeyword,
        "RUN": RunKeyword,
        "COPY": CopyKeyword,
        "ADD": AddKeyword,
        "WORKDIR": WorkdirKeyword,
        "ENV": EnvKeyword,
        "ARG": ArgKeyword,
        "USER": UserKeyword,
    }
)


SKIPPED_NAMES = frozenset(
    {
        "CMD",
        "ENTRYPOINT",
        "LABEL",
        "EXPOSE",
        "VOLUME",
        "STOPSIGNAL",
        "MAINTAINER",
        "HEALTHCHECK",
        "ONBUILD",
        "SHELL",
    }
)


def parse_dockerfile(text: str) -> list[DockerKeyword]:
    """Tokenise ``text`` into directives.

    Joins backslash-continued lines, drops comment/blank lines, then
    dispatches by leading keyword.
    """
    merged = join_continuations(text)
    result: list[DockerKeyword] = []
    for raw in merged:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        keyword = head.upper()
        if keyword in KEYWORDS:
            result.append(KEYWORDS[keyword].parse(rest))
        elif keyword in SKIPPED_NAMES:
            result.append(SkippedKeyword.of(keyword, rest))
        else:
            raise ValueError(f"unknown Dockerfile directive: {head!r}")
    return result


def join_continuations(text: str) -> list[str]:
    """Merge lines ending with ``\\`` into single logical lines."""
    out: list[str] = []
    buf: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.endswith("\\"):
            buf.append(line[:-1])
            continue
        if buf:
            buf.append(line)
            out.append(" ".join(s.strip() for s in buf))
            buf = []
        else:
            out.append(line)
    if buf:
        out.append(" ".join(s.strip() for s in buf))
    return out
