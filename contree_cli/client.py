"""CLI-side glue over the contree-client library.

Transport, typed API methods, models, SSE parsing, retries
(RetryPolicy), operation helpers (wait_operation,
follow_operation_events), image reference resolution, stream payload
decoding and User-Agent composition all live in ``contree_client``.
This module keeps only what is genuinely CLI-specific:

- ``CliClient``: the auto-detected transport backend announcing the
  CLI identity in the User-Agent.
- ``client_from_profile``: build a client from the CLI config profile
  with the CLI retry policy.
- ``cli_version`` / ``CLI_USER_AGENT``: version reporting.
"""

from __future__ import annotations

import json
import logging
import platform
import sys
from importlib.metadata import PackageNotFoundError, distribution
from typing import TYPE_CHECKING, Any

from contree_client.profiles import AUTH_TYPE_IAM, Profile
from contree_client.runtime import RetryPolicy

if TYPE_CHECKING:
    # The auto-detected backend class is a runtime variable, invalid
    # as a static base; the checker sees the abstract sync interface
    # instead (construction sites carry an ignore[abstract]).
    from contree_client.base import ContreeSyncClient as ClientBase
else:
    from contree_client.sync import ContreeClient as ClientBase

log = logging.getLogger(__name__)


def cli_version() -> str:
    try:
        dist = distribution("contree-cli")
    except PackageNotFoundError:
        return "editable"
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            if json.loads(raw).get("dir_info", {}).get("editable"):
                return "editable"
        except ValueError:
            pass
    return dist.version


CLI_IDENTITY = f"contree-cli/{cli_version()}"

# Standalone User-Agent for requests that do not go through the API
# client (the PyPI update check) and for `contree --version`.
CLI_USER_AGENT = (
    f"{CLI_IDENTITY} "
    f"Python/{'.'.join(map(str, sys.version_info))} "
    f"{platform.platform()} "
)


class CliClient(ClientBase):
    """Contree client that announces the CLI in the User-Agent.

    The library composes the header from its own product tokens; the
    ``identity`` kwarg prepends the application token, so server logs
    attribute the traffic to contree-cli without losing the library
    and transport tokens.
    """

    def __init__(self, token: str, **kwargs: Any) -> None:
        kwargs.setdefault("identity", CLI_IDENTITY)
        super().__init__(token, **kwargs)


def client_from_profile(
    profile: Profile,
    timeout: float | None = 300.0,
) -> CliClient:
    """Create a client for a config profile.

    The CLI retries transient failures without a budget: the retry
    loop ends only on success, a non-retryable response, or Ctrl+C.
    The guards stay CLI-side for the friendly messages and because
    the library's from_profile checks neither the IAM project nor
    empty-string tokens (Profile.save round-trips None as "").
    """
    if not profile.token:
        raise ValueError(
            f"No token configured for profile {profile.name!r}."
            " Run `contree auth` first."
        )
    if profile.auth_type == AUTH_TYPE_IAM and not profile.project:
        raise ValueError(
            f"No project configured for IAM profile {profile.name!r}."
            " Run `contree auth` first."
        )
    if profile.auth_type != AUTH_TYPE_IAM and not profile.url:
        raise ValueError(
            f"No URL configured for JWT profile {profile.name!r}."
            " Run `contree auth` or pass --url."
        )
    return CliClient.from_profile(
        profile,
        timeout=timeout,
        retry=RetryPolicy(max_attempts=None),
    )
