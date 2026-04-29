"""Manage session environment variables.

Session env vars are applied to every `contree run` automatically.
Per-run `-e` flags override session env vars with the same key.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from contree_cli import FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  contree env                                  list session env vars
  contree env PATH=/root/.cargo/bin:$PATH      set PATH
  contree env DEBUG=1 DB_HOST=localhost         set multiple
  contree env -d PATH                          unset PATH
  contree env -d PATH DEBUG                    unset multiple
"""


@dataclass(frozen=True)
class EnvArgs(ArgumentsProtocol):
    vars: list[str]
    delete: bool = False

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> EnvArgs:
        return cls(vars=ns.vars or [], delete=ns.delete)


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "vars",
        nargs="*",
        metavar="KEY=VALUE",
        help="Environment variables to set (or keys to delete with -d)",
    )
    p.add_argument(
        *FLAGS["delete"],
        action="store_true",
        help="Unset the specified environment variables",
    )
    return cmd_env, EnvArgs


def cmd_env(args: EnvArgs) -> int | None:
    store = SESSION_STORE.get()

    if args.delete:
        if not args.vars:
            logger.error("Specify keys to unset")
            return 1
        store.unset_env(*args.vars)
        for key in args.vars:
            logger.info("Unset %s", key)
        return None

    if args.vars:
        for pair in args.vars:
            key, sep, value = pair.partition("=")
            if not sep:
                logger.error("Invalid format %r, expected KEY=VALUE", pair)
                return 1
            store.set_env(key, value)
            logger.info("Set %s=%s", key, value)
        return None

    # List
    formatter = FORMATTER.get()
    env = store.get_env()
    if not env:
        print("No session environment variables set.")
        return None
    for key, value in env.items():
        formatter(key=key, value=value)
    return None
