"""Configure authentication credentials.

Validates a token against the API (GET /v1/whoami) and saves it to the
config file under the specified profile. The token is prompted securely
via getpass if --token is not provided.

Supports two auth types:
  iam (default) — bearer token + project ID, default URL provided
  jwt (legacy)  — bearer token only, URL must be specified

Environment variable fallbacks during registration:
  CONTREE_TOKEN / NEBIUS_API_KEY     used when --token is omitted
  CONTREE_URL                        used when --url is omitted
  CONTREE_PROJECT / NEBIUS_AI_PROJECT used when --project is omitted (IAM)

Other commands ignore these variables; only ``contree auth`` reads
them. ``CONTREE_PROFILE`` selects the profile for any command.

Subcommands:
  profiles    List saved profiles (* marks active)
  switch NAME Switch the active profile
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from multiprocessing.pool import ThreadPool

from contree_cli import FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError, client_from_profile
from contree_cli.config import AuthType, Config, ConfigProfile
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)
PROFILE_CHECK_TIMEOUT = 2.0
PROFILE_CHECK_CONCURRENCY = 4
REQUIRED_PERMISSION = "list"

EPILOG = """\
for coding agents:
  `auth` verifies token with /v1/whoami before writing config
  mutates local config file and may prompt for token if omitted
  use `auth profiles` for read-only profile discovery
"""


@dataclass(frozen=True)
class AuthArgs(ArgumentsProtocol):
    token: str | None = None
    url: str | None = None
    auth_type: AuthType = AuthType.IAM
    project: str | None = None
    profile: str = "default"
    force: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> AuthArgs:
        return cls(
            token=ns.auth_token or None,
            url=ns.auth_url or None,
            auth_type=AuthType(ns.auth_type) if ns.auth_type else AuthType.IAM,
            project=ns.auth_project or None,
            profile=ns.profile or "default",
            force=ns.force,
        )


@dataclass(frozen=True)
class ProfilesArgs(ArgumentsProtocol):
    offline: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ProfilesArgs:
        return cls(offline=ns.offline)


@dataclass(frozen=True)
class SwitchArgs(ArgumentsProtocol):
    profile_name: str

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> SwitchArgs:
        return cls(profile_name=ns.profile_name)


@dataclass(frozen=True)
class RemoveArgs(ArgumentsProtocol):
    profile_name: str
    force: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> RemoveArgs:
        return cls(profile_name=ns.profile_name, force=ns.force)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        *FLAGS["token"], dest="auth_token", help="API token (prompted if omitted)"
    )
    p.add_argument(
        *FLAGS["url"],
        dest="auth_url",
        help="API base URL",
        default=None,
        type=lambda v: v.rstrip("/"),
    )
    p.add_argument(
        "--type",
        dest="auth_type",
        choices=list(AuthType),
        default=AuthType.IAM,
        help="Auth type",
    )
    p.add_argument(
        *FLAGS["project"],
        dest="auth_project",
        help="Project ID (IAM only)",
    )
    p.add_argument(*FLAGS["profile"], help="Profile name", default="default")
    p.add_argument(
        *FLAGS["force"],
        action="store_true",
        help="Overwrite existing profile without confirmation",
    )

    auth_sub = p.add_subparsers(dest="auth_action")

    profiles_parser = auth_sub.add_parser(
        "list",
        aliases=["ls", "profiles"],
        help="List saved profiles",
        description="List configured local profiles and active marker.",
        epilog="for coding agents: read-only command",
    )
    profiles_parser.add_argument(
        *FLAGS["offline"],
        action="store_true",
        help="Do not probe /v1/whoami; mark all profile statuses as offline",
    )
    profiles_parser.set_defaults(
        handler=cmd_list,
        load_args=ProfilesArgs,
    )

    switch_parser = auth_sub.add_parser(
        "switch",
        help="Switch active profile",
        description="Set [DEFAULT] profile in local config file.",
        epilog="for coding agents: mutates local config state",
    )
    switch_parser.set_defaults(handler=cmd_switch, load_args=SwitchArgs)
    switch_parser.add_argument("profile_name", help="Profile to activate")

    remove_parser = auth_sub.add_parser(
        "remove",
        aliases=["rm", "del"],
        help="Remove a saved profile",
        description="Delete a profile section from config.",
        epilog="for coding agents: mutates local config state",
    )
    remove_parser.add_argument("profile_name", help="Profile to remove")
    remove_parser.add_argument(
        *FLAGS["force"],
        action="store_true",
        help="Do not ask for confirmation",
    )
    remove_parser.set_defaults(handler=cmd_remove, load_args=RemoveArgs)

    return cmd_auth, AuthArgs


def _env_fallback(names: tuple[str, ...], *, what: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            logger.info("Using %s from %s", what, name)
            return value
    return None


def check_permission(payload: dict[str, object], permission: str) -> bool:
    perms = payload.get("permissions")
    if not isinstance(perms, dict):
        return False
    return bool(perms.get(permission))


def cmd_auth(args: AuthArgs) -> int | None:
    cfg = Config()
    exists = args.profile in cfg
    action = "Updating" if exists else "Setting"
    # Logs action ("Updating"/"Setting"), profile name, and auth type —
    # not the actual token value.
    # nosemgrep: python-logger-credential-disclosure
    logger.info(
        "%s token for profile %r (type: %s)", action, args.profile, args.auth_type
    )

    if exists and not args.force:
        answer = input(
            f"Profile {args.profile!r} already exists. Overwrite? [y/N] ",
        )
        if answer.lower() != "y":
            print("Aborted.")
            return 1

    # Token: --token > CONTREE_TOKEN > NEBIUS_API_KEY > interactive prompt
    token = args.token or _env_fallback(
        ("CONTREE_TOKEN", "NEBIUS_API_KEY"),
        what="token",
    )
    if token is None:
        token = getpass.getpass("Token: ")

    # URL: --url > CONTREE_URL > type-specific default > interactive prompt
    url = args.url or _env_fallback(("CONTREE_URL",), what="URL")
    if url is None:
        if args.auth_type == AuthType.IAM:
            url = Config.DEFAULT_IAM_URL
        else:
            url = input("URL: ").strip().rstrip("/")
            if not url:
                logger.error("URL is required for JWT auth")
                return 1

    # Project (IAM only): --project > CONTREE_PROJECT > NEBIUS_AI_PROJECT > prompt
    project: str | None = None
    if args.auth_type == AuthType.IAM:
        project = args.project or _env_fallback(
            ("CONTREE_PROJECT", "NEBIUS_AI_PROJECT"),
            what="project",
        )
        if project is None:
            project = input("Project ID: ").strip()
    profile = ConfigProfile(
        name=args.profile,
        token=token,
        url=url,
        auth_type=args.auth_type,
        project=project,
    )

    try:
        client = client_from_profile(profile)
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    try:
        resp = client.get("/v1/whoami")
        whoami = json.loads(resp.read() or b"{}")
    except ApiError as exc:
        # Logs the API error message, not the token itself.
        # nosemgrep: python-logger-credential-disclosure
        logger.error("Token verification failed: %s. Profile not changed.", exc)
        return 1
    except ValueError as exc:
        logger.error("Could not parse /v1/whoami response: %s", exc)
        return 1

    if not check_permission(whoami, REQUIRED_PERMISSION):
        project_label = profile.project or profile.url
        logger.warning(
            "Warning: token is valid but sandboxes are disabled on %s"
            " (no %r permission). The profile will be saved but no commands"
            " will work until the service is enabled.",
            project_label,
            REQUIRED_PERMISSION,
        )

    cfg[args.profile] = profile
    logger.info(
        "auth accepted, profile %r saved to -> %s",
        args.profile,
        cfg.path,
    )
    return None


def token_hash(token: str | None) -> str:
    if not token:
        return "<no token>"
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def cmd_list(args: ProfilesArgs) -> None:
    cfg = Config()
    logger.info("Configured profiles (* stands for active)")
    if not cfg:
        print("No profiles configured.")
        return

    active = cfg.resolve().name
    if active not in cfg:
        logger.warning(
            "Active profile %r does not exist in config",
            active,
        )

    def check_status(
        profile: ConfigProfile,
    ) -> tuple[ConfigProfile, str]:
        if not profile.token:
            return profile, "error"
        if args.offline:
            return profile, "offline mode"
        try:
            client = client_from_profile(
                profile,
                timeout=PROFILE_CHECK_TIMEOUT,
            )
        except ValueError:
            return profile, "no url"

        try:
            resp = client.get("/v1/whoami")
            payload = resp.read()
        except TimeoutError:
            return profile, "timeout"
        except Exception:
            return profile, "error"
        try:
            whoami = json.loads(payload or b"{}")
        except ValueError:
            return profile, "error"
        if not check_permission(whoami, REQUIRED_PERMISSION):
            return profile, "inactive"
        return profile, "ok"

    formatter = FORMATTER.get()
    profiles = list(cfg.values())
    with ThreadPool(PROFILE_CHECK_CONCURRENCY) as pool:
        for profile, status in pool.imap(check_status, profiles):
            formatter(
                name=profile.name,
                type=profile.auth_type,
                url=profile.url,
                project=profile.project or "",
                token_sha256=token_hash(profile.token),
                active=profile.name == active,
                status=status,
            )


def cmd_switch(args: SwitchArgs) -> None:
    cfg = Config()
    cfg.switch(args.profile_name)
    logger.info("Switched to profile %r", args.profile_name)


def cmd_remove(args: RemoveArgs) -> int | None:
    cfg = Config()
    if args.profile_name not in cfg:
        logger.error("Profile %r does not exist", args.profile_name)
        return 1
    if not args.force:
        answer = input(f"Remove profile {args.profile_name!r}? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return 1
    del cfg[args.profile_name]
    logger.info("Removed profile %r", args.profile_name)
    return None
