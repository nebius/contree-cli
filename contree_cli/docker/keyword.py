"""Base class for Dockerfile keywords plus shared helpers."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import ClassVar

from .context import BuildContext

logger = logging.getLogger(__name__)

SUB_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def substitute(text: str, env: dict[str, str]) -> str:
    """Expand ``$VAR`` / ``${VAR}`` against ``env``. Missing names expand to ''."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, "")

    return SUB_RE.sub(repl, text)


def parse_command_form(rest: str) -> tuple[list[str], bool]:
    """Parse the argument to ``RUN``/``COPY``/``ADD``/``CMD``.

    Returns ``(parts, shell_form)`` where ``shell_form`` is ``True`` when the
    directive used the bare shell syntax. ``parts`` for shell-form contains a
    single joined string. JSON exec-form returns the list as-is.
    """
    stripped = rest.lstrip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise ValueError(f"invalid JSON exec-form: {rest!r}") from exc
        if not isinstance(parsed, list) or not all(isinstance(p, str) for p in parsed):
            raise ValueError(f"exec-form must be a list of strings: {rest!r}")
        return list(parsed), False
    return [rest], True


def parse_keyval_pairs(rest: str) -> dict[str, str]:
    """Parse ``KEY1=VAL1 KEY2=VAL2`` or the single-pair form ``KEY VAL``.

    Quoted values are supported via ``shlex``-style splitting on whitespace.
    """
    import shlex

    tokens = shlex.split(rest)
    if not tokens:
        return {}
    if "=" not in tokens[0]:
        # Legacy form: ENV KEY VALUE (whole rest after first token is the value)
        key = tokens[0]
        value = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
        return {key: value.strip()}
    pairs: dict[str, str] = {}
    for t in tokens:
        if "=" not in t:
            raise ValueError(f"expected KEY=VALUE, got {t!r}")
        k, _, v = t.partition("=")
        pairs[k] = v
    return pairs


@dataclass(frozen=True, repr=False)
class DockerKeyword:
    """Base class. Subclasses implement ``parse``, ``serialize``, ``execute``.

    ``__repr__`` is overridden in every subclass to render the directive as
    it would appear in a Dockerfile, so build logs look like the original
    source.
    """

    NAME: ClassVar[str] = ""

    @classmethod
    def parse(cls, args_text: str) -> DockerKeyword:
        raise NotImplementedError

    def serialize(self) -> str:
        """Stable text used for layer hashing."""
        raise NotImplementedError

    def execute(self, ctx: BuildContext) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        return self.NAME or self.__class__.__name__
