import contextvars
import logging
import sys
from collections.abc import Callable
from dataclasses import replace

import contree_cli.config as config_mod
from contree_cli import CLIENT, FORMATTER, PROFILE, SESSION_STORE, ArgumentsProtocol
from contree_cli.arguments import parser
from contree_cli.client import ApiError, client_from_profile
from contree_cli.config import Config
from contree_cli.log import setup_logging
from contree_cli.output import FORMATTERS
from contree_cli.session import SessionStore, get_session_key

log = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) == 1:
        parser.print_help()
        exit(0)

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

    # auth creates its own client and can work with nonexistent profiles
    if args.command not in ("auth",):
        if profile.name not in cfg:
            log.error(
                "Profile %r does not exist. Run `contree auth` first.",
                profile.name,
            )
            exit(1)
        try:
            client = client_from_profile(profile)
        except ValueError as exc:
            log.error("%s", exc)
            exit(1)
        CLIENT.set(client)

    formatter = FORMATTERS[args.output_format]()

    session_key = get_session_key(profile.name, override=args.session_key)
    db_path = profile.session_db_path

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
