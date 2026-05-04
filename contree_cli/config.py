from __future__ import annotations

import configparser
import logging
import os
import stat
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

CONTREE_HOME = Path(os.getenv("CONTREE_HOME", "~/.config/contree")).expanduser()
CONFIG_DIR = CONTREE_HOME
CONFIG_FILE = CONTREE_HOME / "auth.ini"
CLI_CONFIG_FILE = CONTREE_HOME / "cli.ini"


@dataclass(frozen=True)
class CliSettings:
    """Optional user defaults from cli.ini ([cli] section)."""

    log_level: str | None = None
    output_format: str | None = None
    editor: str | None = None

    @classmethod
    def load(cls, path: Path) -> CliSettings:
        cp = configparser.ConfigParser()
        if path.exists():
            log.debug("Loading CLI defaults from %s", path)
            cp.read(path)
        section: configparser.SectionProxy | dict[str, str] = (
            cp["cli"] if cp.has_section("cli") else {}
        )
        return cls(
            log_level=section.get("log_level") or None,
            output_format=section.get("format") or None,
            editor=section.get("editor") or None,
        )


class AuthType(str, Enum):
    IAM = "iam"
    JWT = "jwt"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, repr=False)
class ConfigProfile:
    name: str
    url: str
    token: str | None
    auth_type: AuthType = AuthType.JWT
    project: str | None = None

    def __repr__(self) -> str:
        masked = "***" if self.token else None
        return (
            f"{self.__class__.__name__}(name={self.name!r}, url={self.url!r},"
            f" token={masked!r}, auth_type={self.auth_type!r},"
            f" project={self.project!r})"
        )

    @property
    def session_db_path(self) -> Path:
        return CONTREE_HOME / "cli" / "sessions" / f"{self.name}.db"

    def remove_session_db(self) -> None:
        db = self.session_db_path
        for suffix in ("", "-wal", "-shm"):
            p = db.with_name(db.name + suffix)
            p.unlink(missing_ok=True)


class Config(MutableMapping[str, ConfigProfile]):
    """INI-backed profile store.

    Dict-like: ``cfg[name]``, ``cfg[name] = profile``,
    ``del cfg[name]``, ``name in cfg``, ``len(cfg)``, iteration.
    """

    DEFAULT_IAM_URL = "https://api.studio.nebius.com/sandboxes"
    PROFILE_PREFIX = "profile:"

    def __init__(self, path: Path | None = None) -> None:
        from contree_cli.migrations import run_migrations

        run_migrations(CONTREE_HOME)
        self.__path = path or CONFIG_FILE
        self.__profiles: dict[str, ConfigProfile] = {}
        self.__active: str = "default"
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        cp = configparser.ConfigParser()
        log.debug("Loading config from %s", self.__path)
        cp.read(self.__path)
        self.__active = cp.defaults().get("profile", "default")
        self.__profiles.clear()
        for section in cp.sections():
            if section.startswith(self.PROFILE_PREFIX):
                p = self._parse_profile(cp, section)
                self.__profiles[p.name] = p

    @classmethod
    def _parse_profile(
        cls,
        cp: configparser.ConfigParser,
        section: str,
    ) -> ConfigProfile:
        auth_type = AuthType(cp.get(section, "type", fallback=AuthType.JWT.value))
        default_url = cls.DEFAULT_IAM_URL if auth_type == AuthType.IAM else ""
        return ConfigProfile(
            name=section[len(cls.PROFILE_PREFIX) :],
            token=cp.get(section, "token", fallback=None),
            url=cp.get(section, "url", fallback=default_url),
            auth_type=auth_type,
            project=cp.get(section, "project", fallback=None),
        )

    def _save(self) -> None:
        cp = configparser.ConfigParser()
        cp["DEFAULT"]["profile"] = self.__active
        for profile in self.__profiles.values():
            section = self.PROFILE_PREFIX + profile.name
            cp.add_section(section)
            if profile.token is not None:
                cp.set(section, "token", profile.token)
            cp.set(section, "url", profile.url.rstrip("/"))
            cp.set(section, "type", profile.auth_type)
            if profile.project is not None:
                cp.set(section, "project", profile.project)
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

    def __getitem__(self, name: str) -> ConfigProfile:
        return self.__profiles[name]

    def __setitem__(self, name: str, profile: ConfigProfile) -> None:
        assert name == profile.name, "profile name must match key"
        self.__profiles[name] = profile
        self._save()

    def __delitem__(self, name: str) -> None:
        if name not in self.__profiles:
            raise KeyError(name)
        profile = self.__profiles.pop(name)
        if self.__active == name:
            self.__active = next(iter(self.__profiles), "default")
        self._save()
        profile.remove_session_db()

    def __len__(self) -> int:
        return len(self.__profiles)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__profiles)

    # -- profile management --------------------------------------------------

    @property
    def current(self) -> ConfigProfile:
        return self.__profiles[self.__active]

    @current.setter
    def current(self, profile: ConfigProfile) -> None:
        self.__active = profile.name
        self._save()

    def resolve(self, profile_override: str | None = None) -> ConfigProfile:
        """Resolve the active profile with env-var overrides.

        Priority: *profile_override* > ``CONTREE_PROFILE`` > config default.
        Per-field: ``CONTREE_TOKEN`` / ``CONTREE_URL`` / ``CONTREE_PROJECT``
        override the stored values.
        """
        name = profile_override or os.environ.get("CONTREE_PROFILE") or self.__active
        env_token = os.environ.get("CONTREE_TOKEN")
        env_url = os.environ.get("CONTREE_URL")
        env_project = os.environ.get("CONTREE_PROJECT")

        if name in self.__profiles:
            p = self.__profiles[name]
            return ConfigProfile(
                name=name,
                token=env_token or p.token,
                url=env_url or p.url,
                auth_type=p.auth_type,
                project=env_project or p.project,
            )
        return ConfigProfile(
            name=name,
            token=env_token,
            url=env_url or "",
            auth_type=AuthType.JWT,
            project=env_project,
        )

    def switch(self, name: str) -> None:
        """Set the active profile."""
        if name not in self.__profiles:
            raise ValueError(f"profile {name!r} does not exist")
        self.__active = name
        self._save()
