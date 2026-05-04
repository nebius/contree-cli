import argparse
import contextvars
import logging
import sys
from collections.abc import Callable
from dataclasses import replace

import contree_cli.config as config_mod
from contree_cli import CLIENT, FORMATTER, PROFILE, SESSION_STORE, ArgumentsProtocol
from contree_cli.arguments import parser
from contree_cli.client import ApiError, client_from_profile
from contree_cli.config import CLI_CONFIG_FILE, CliSettings, Config
from contree_cli.log import setup_logging
from contree_cli.output import FORMATTERS
from contree_cli.session import SessionStore, get_session_key

log = logging.getLogger(__name__)


def apply_defaults(p: argparse.ArgumentParser, **defaults: object) -> None:
    """Apply defaults to *p* and every nested subparser.

    argparse subparsers maintain their own default tables and will
    override main-parser defaults for arguments they declare (e.g.
    ``--editor`` on ``file edit``), so we have to walk into them.
    """
    p.set_defaults(**defaults)
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                apply_defaults(sub, **defaults)


def main() -> None:
    if len(sys.argv) == 1:
        parser.print_help()
        exit(0)

    cli_defaults = CliSettings.load(CLI_CONFIG_FILE)
    overrides: dict[str, object] = {}
    if cli_defaults.log_level:
        overrides["log_level"] = cli_defaults.log_level
    if cli_defaults.output_format:
        overrides["output_format"] = cli_defaults.output_format
    if cli_defaults.editor:
        overrides["editor"] = cli_defaults.editor
    if overrides:
        apply_defaults(parser, **overrides)

    args = parser.parse_args()
    setup_logging(level=getattr(logging, args.log_level.upper(), logging.INFO))

    config_mod.CONFIG_FILE = args.config_path
    config_mod.CONFIG_DIR = args.config_path.parent

    cfg = Config(args.config_path)
    profile = cfg.resolve(profile_override=args.profile)

    # CLI flags override resolved profile fields
    if args.token:
        profile = replace(profile, token=args.token)
    if args.url:
        profile = replace(profile, url=args.url)
    if args.project:
        profile = replace(profile, project=args.project)

    # Local-only commands don't need a client or a configured profile:
    # auth bootstraps its own; agent/man/skill operate purely on local files.
    LOCAL_COMMANDS = ("auth", "agent", "man", "skill")
    needs_client = args.command not in LOCAL_COMMANDS

    if needs_client and profile.name not in cfg:
        log.error(
            "Profile %r does not exist. Run `contree auth` first.",
            profile.name,
        )
        exit(1)

    if needs_client:
        try:
            client = client_from_profile(profile)
        except ValueError as exc:
            log.error("%s", exc)
            exit(1)
        CLIENT.set(client)

    formatter = FORMATTERS[args.output_format]()

    session_key = get_session_key(profile.name, override=args.session_key)
    db_path = profile.session_db_path
    log.debug("Running in session: %s", session_key)

    with SessionStore(db_path, session_key) as store:
        PROFILE.set(profile)
        FORMATTER.set(formatter)
        SESSION_STORE.set(store)
        ctx = contextvars.copy_context()

        loader: type[ArgumentsProtocol] = args.load_args
        handler: Callable[[ArgumentsProtocol], int | None] = args.handler

        try:
            exit_code = ctx.run(handler, loader.from_args(args))
        except ApiError as exc:
            log.error("%s", exc)
            exit(1)
        except KeyboardInterrupt:
            log.error("User interrupted")
            exit(1)
        finally:
            formatter.flush()

    exit(exit_code or 0)


if __name__ == "__main__":
    main()
