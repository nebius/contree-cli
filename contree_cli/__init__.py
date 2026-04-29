from __future__ import annotations

import argparse
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from contree_cli.client import ContreeClient
    from contree_cli.config import ConfigProfile
    from contree_cli.output import OutputFormatter
    from contree_cli.session import SessionStore

PROFILE: ContextVar[ConfigProfile] = ContextVar("PROFILE")
CLIENT: ContextVar[ContreeClient] = ContextVar("CLIENT")
FORMATTER: ContextVar[OutputFormatter] = ContextVar("FORMATTER")
SESSION_STORE: ContextVar[SessionStore] = ContextVar("SESSION_STORE")
IN_SHELL: ContextVar[bool] = ContextVar("IN_SHELL", default=False)


class ArgumentsProtocol(Protocol):
    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ArgumentsProtocol: ...


Handler = Callable[..., int | None]
SetupResult = tuple[Handler, type[ArgumentsProtocol]]
