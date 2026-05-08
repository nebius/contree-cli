"""PyPI update check, rate-limited to once per day.

State file at ``$CONTREE_HOME/cli/version_check.json``::

    {
      "last_check": "2026-05-08T12:00:00+00:00",
      "latest_version": "0.5.0",
      "current_version": "0.4.2"
    }

Network errors, malformed cache files, and parse failures are swallowed:
the update check must never break a user's command.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contree_cli import config
from contree_cli.client import CLI_USER_AGENT, cli_version

log = logging.getLogger(__name__)


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

    def __init__(
        self,
        *,
        state_path: Path = STATE_PATH,
        current_version: str = cli_version(),
    ) -> None:
        self.state_path = state_path
        self.current_version = current_version

    def read_state(self) -> dict[str, str]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, state: dict[str, str]) -> None:
        with suppress(OSError):
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, indent=2) + "\n")

    @staticmethod
    def parse_version(value: str) -> tuple[int, ...]:
        """Parse a PEP-440-ish version into a comparable tuple of ints.

        Strips non-numeric suffixes per component (e.g. ``0a1`` -> ``0``)
        so pre-releases compare as their numeric prefix. Good enough to
        decide "is X newer than Y" for normal releases.
        """
        parts: list[int] = []
        for component in value.split("."):
            digits = ""
            for ch in component:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            if digits:
                parts.append(int(digits))
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

    def warn_if_outdated(self, latest: str) -> None:
        if self.parse_version(latest) > self.parse_version(self.current_version):
            log.warning(
                "A new version of contree-cli is available: %s (installed: %s)."
                " Upgrade with `uv tool install -U contree-cli` or"
                " `pip install -U contree-cli`.",
                latest,
                self.current_version,
            )

    @property
    def enabled(self) -> bool:
        return (
            not os.environ.get(self.OPT_OUT_ENV) and self.current_version != "editable"
        )

    def is_state_fresh(self) -> bool:
        """True if the cache file's mtime is within ``CHECK_INTERVAL``.

        Uses ``Path.stat()`` (works on Windows and POSIX) to avoid
        reading/parsing the JSON on the hot path. Missing files,
        permission errors, or any OSError is treated as "not fresh".
        """
        try:
            mtime = self.state_path.stat().st_mtime
        except OSError:
            return False
        age_seconds = datetime.now(timezone.utc).timestamp() - mtime
        return age_seconds < self.CHECK_INTERVAL.total_seconds()

    def refresh(self) -> None:
        """Refresh cached PyPI state, rate-limited to ``CHECK_INTERVAL``.

        Decides freshness from the cache file's mtime to avoid the
        JSON read+parse on the common case. If stale, probes PyPI and
        rewrites ``state_path``.
        """
        if not self.enabled:
            return
        if self.is_state_fresh():
            return

        latest = self.fetch_latest_version()
        if latest is None:
            return

        self.write_state(
            {
                "last_check": datetime.now(timezone.utc).isoformat(),
                "latest_version": latest,
                "current_version": self.current_version,
            }
        )

    def check(self) -> None:
        """Log a warning if cached ``latest_version`` is newer than current.

        Pure read; never touches the network or rewrites state. Pair
        with :meth:`refresh` to first ensure the cache is up to date.
        """
        if not self.enabled:
            return

        state = self.read_state()
        cached = state.get("latest_version")
        if isinstance(cached, str):
            self.warn_if_outdated(cached)
