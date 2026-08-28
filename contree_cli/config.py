"""CLI-owned configuration on top of ``contree_client.profiles``.

Profile parsing, the auth.ini/cli.ini merge and the resolution
precedence (explicit name > ``CONTREE_PROFILE`` > active) live in the
library; this module keeps what only the CLI needs: the writable
profile store (atomic 0600 save, active-profile switching, deletion
with session-DB cleanup), the ``[cli]`` settings section, the editor
choice and the session database layout.
"""

from __future__ import annotations

import configparser
import logging
import os
import shutil
import stat
from collections.abc import Iterator, MutableMapping
from pathlib import Path

from contree_client.profiles import (
    AUTH_TYPE_IAM,
    AUTH_TYPE_JWT,
    DEFAULT_IAM_URL,
    Profile,
    ProfileError,
    load_profiles,
    resolve_profile,
)

from .migrations import run_migrations

__all__ = [
    "AUTH_TYPE_IAM",
    "AUTH_TYPE_JWT",
    "CLI_CONFIG_FILE",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "CONTREE_HOME",
    "DEFAULT_IAM_URL",
    "EDITOR",
    "SETTINGS",
    "Config",
    "Profile",
    "get_default_path",
    "remove_session_db",
    "session_db_path",
]

log = logging.getLogger(__name__)


def get_default_path(env: str, default: str | Path) -> Path:
    return Path(os.getenv(env) or default).expanduser()


XDG_CONFIG_HOME = get_default_path("XDG_CONFIG_HOME", "~/.config")
CONTREE_HOME = get_default_path("CONTREE_HOME", XDG_CONFIG_HOME / "contree")
CONFIG_DIR = CONTREE_HOME
CONFIG_FILE = CONTREE_HOME / "auth.ini"
CLI_CONFIG_FILE = CONTREE_HOME / "cli.ini"

# Parsed at import time. Paths are fixed by CONTREE_HOME (env), so the
# ``[cli]`` section is available before argparse runs and can supply
# defaults that beat hardcoded ones but still lose to flags.
SETTINGS = configparser.ConfigParser()
SETTINGS.read([CLI_CONFIG_FILE, CONFIG_FILE])

# Default editor for ``contree file edit`` when ``--editor`` is not given.
# Resolved once at import time. Priority: $EDITOR > cli.ini > vim > nano > vi.
EDITOR = (
    os.environ.get("EDITOR")
    or SETTINGS.get("cli", "editor", fallback=None)
    or shutil.which("vim")
    or shutil.which("nano")
    or "vi"
)


def session_db_path(profile_name: str) -> Path:
    """Per-profile session database location (CLI-owned layout).

    ``CONTREE_SESSION_DB`` overrides the computed path entirely.
    """
    override = os.getenv("CONTREE_SESSION_DB")
    if override:
        return Path(override).expanduser()
    return CONTREE_HOME / "cli" / "sessions" / f"{profile_name}.db"


def remove_session_db(profile_name: str) -> None:
    db = session_db_path(profile_name)
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        p.unlink(missing_ok=True)


class Config(MutableMapping[str, Profile]):
    """Writable INI-backed profile store over the library reader.

    Dict-like: ``cfg[name]``, ``cfg[name] = profile``,
    ``del cfg[name]``, ``name in cfg``, ``len(cfg)``, iteration.
    """

    def __init__(self, path: Path | None = None) -> None:
        run_migrations(CONTREE_HOME)
        self.__path = path or CONFIG_FILE
        self.__profiles: dict[str, Profile] = {}
        self.__active: str = "default"
        self._load()

    @property
    def path(self) -> Path:
        return self.__path

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        log.debug("Loading profiles for %s", self.__path)
        self.__profiles, self.__active = load_profiles(self.__path)

    def _save(self) -> None:
        cp = configparser.ConfigParser()
        cp["DEFAULT"]["profile"] = self.__active
        for profile in self.__profiles.values():
            profile.save(cp)
        self.__path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0o600 from the start so the token is never readable
        # by other users — even between create() and chmod().
        fd = os.open(
            self.__path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w") as f:
            cp.write(f)
        os.chmod(self.__path, stat.S_IRUSR | stat.S_IWUSR)

    # -- MutableMapping interface --------------------------------------------

    def __contains__(self, name: object) -> bool:
        return name in self.__profiles

    def __getitem__(self, name: str) -> Profile:
        return self.__profiles[name]

    def __setitem__(self, name: str, profile: Profile) -> None:
        assert name == profile.name, "profile name must match key"
        self.__profiles[name] = profile
        self._save()

    def __delitem__(self, name: str) -> None:
        if name not in self.__profiles:
            raise KeyError(name)
        self.__profiles.pop(name)
        if self.__active == name:
            self.__active = next(iter(self.__profiles), "default")
        self._save()
        remove_session_db(name)

    def __len__(self) -> int:
        return len(self.__profiles)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__profiles)

    # -- profile management --------------------------------------------------

    @property
    def current(self) -> Profile:
        return self.__profiles[self.__active]

    @current.setter
    def current(self, profile: Profile) -> None:
        self.__active = profile.name
        self._save()

    def resolve(self, profile_override: str | None = None) -> Profile:
        """Resolve the active profile by name.

        The library owns the precedence (*profile_override* >
        ``CONTREE_PROFILE`` > config default). A missing profile
        yields a credential-less stub instead of an error so local
        commands still run and main() reports remote ones itself.
        Credentials come strictly from the saved profile; runtime
        commands do not read tokens, URLs, or project IDs from the
        environment. To register/refresh credentials from env vars use
        ``contree auth``.
        """
        try:
            return resolve_profile(profile_override, path=self.__path)
        except ProfileError:
            name = (
                profile_override or os.environ.get("CONTREE_PROFILE") or self.__active
            )
            return Profile(name=name, url="", token=None)

    def switch(self, name: str) -> None:
        """Set the active profile."""
        if name not in self.__profiles:
            raise ValueError(f"profile {name!r} does not exist")
        self.__active = name
        self._save()
