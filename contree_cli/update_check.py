"""PyPI update check, rate-limited to once per day.

State file at ``$CONTREE_HOME/cli/version_check.json``::

    {
      "last_check": 1762555200.0,
      "latest_version": "0.5.0",
      "current_version": "0.4.2"
    }

``last_check`` is a Unix epoch timestamp; storing seconds keeps the
freshness check trivial (one subtraction) and immune to timezone /
ISO-format quirks.

Network errors, malformed cache files, and parse failures are swallowed:
the update check must never break a user's command.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from contree_cli import config
from contree_cli.client import CLI_USER_AGENT, cli_version


class UpdateChecker:
    """Encapsulates the state file + PyPI probe + outdated-version warning.

    All side effects (filesystem, network, logging) are guarded so that a
    failure in update-checking can never break the user's command.
    """

    PYPI_URL = "https://pypi.org/pypi/contree-cli/json"
    CHECK_INTERVAL = timedelta(days=1)
    NETWORK_TIMEOUT = 2.0
    OPT_OUT_ENV = "CONTREE_NO_UPDATE_CHECK"
    STATE_PATH = config.CONTREE_HOME / "cli" / "version_check.json"
    # Capture leading digits of each dot-separated component; anything
    # past the digits (``a1``, ``-rc.1``, etc.) marks a pre-release.
    COMPONENT_REGEX = re.compile(r"\d+")

    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        current_version: str = cli_version(),
    ) -> None:
        self.state_path = state_path
        self.current_version = current_version
        self.latest_version: str | None = None

    def read_state(self) -> dict[str, object]:
        """Load the cache file. Any schema violation -> empty dict.

        A wrong-typed field (e.g. ``last_check`` saved as an ISO string
        by an older release) makes the whole entry untrustworthy; the
        next refresh just rewrites it.
        """
        try:
            with self.state_path.open() as f:
                data = json.load(f)
            assert isinstance(data, dict)
            assert isinstance(data.get("last_check", 0), (int, float))
            assert isinstance(data.get("latest_version", ""), str)
        except Exception:
            return {}
        return data

    def write_state(self, state: dict[str, object]) -> None:
        with suppress(OSError):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, indent=1))

    def parse_version(self, value: str) -> tuple[tuple[int, int], ...]:
        """Parse ``value`` into a sortable tuple of ``(number, rank)``.

        ``rank`` is ``1`` for a clean numeric component and ``0`` for a
        pre-release suffix (``a1``, ``-rc.1``, …). With this encoding,
        ``0.4.2a1`` < ``0.4.2`` < ``0.4.21`` as expected. Components with
        no digits at all are dropped.
        """
        parts: list[tuple[int, int]] = []
        for raw in value.split("."):
            match = self.COMPONENT_REGEX.search(raw)
            if not match:
                continue
            tail = raw[match.end() :]
            parts.append((int(match.group()), 0 if tail else 1))
        return tuple(parts)

    def fetch_latest_version(self) -> str | None:
        try:
            request = urllib.request.Request(
                self.PYPI_URL,
                headers={
                    "User-Agent": CLI_USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(  # nosemgrep
                request, timeout=self.NETWORK_TIMEOUT
            ) as resp:
                payload = json.loads(resp.read())
        except Exception:
            return None
        info = payload.get("info") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return None
        version = info.get("version")
        return version if isinstance(version, str) else None

    @property
    def enabled(self) -> bool:
        return self.OPT_OUT_ENV not in os.environ and self.current_version != "editable"

    def is_cache_fresh(self, state: dict[str, object]) -> bool:
        """True if ``state['last_check']`` is within ``CHECK_INTERVAL``."""
        last_check = state.get("last_check")
        if not isinstance(last_check, (int, float)):
            return False
        return time.time() - last_check < self.CHECK_INTERVAL.total_seconds()

    def refresh(self) -> None:
        """Read the cache once, refetch from PyPI if stale.

        Populates ``self.latest_version`` with whatever we know after
        this call (cached value, freshly fetched value, or ``None``).
        :meth:`is_latest` then decides whether to log based purely on
        in-memory state — no further file IO.
        """
        if not self.enabled:
            return

        state = self.read_state()
        cached = state.get("latest_version")
        if isinstance(cached, str):
            self.latest_version = cached

        if self.is_cache_fresh(state):
            return

        latest = self.fetch_latest_version()
        if latest is None:
            # Network failed; keep whatever was cached.
            return

        self.latest_version = latest
        self.write_state(
            {
                "last_check": time.time(),
                "latest_version": latest,
                "current_version": self.current_version,
            }
        )

    def is_latest(self) -> bool:
        """Return True if the installed version is at or ahead of the cached
        ``latest_version``.

        Returns True when checks are disabled or ``latest_version`` is
        unknown so callers default to "no warning" in those cases. Pure
        decision based on in-memory state populated by :meth:`refresh`;
        never touches the network or filesystem.
        """
        if not self.enabled or self.latest_version is None:
            return True
        return self.parse_version(self.current_version) >= self.parse_version(
            self.latest_version,
        )
