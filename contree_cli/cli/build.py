"""Build an image from a Dockerfile.

Reads the Dockerfile at the given path (default ``<CONTEXT>/Dockerfile``)
and applies each directive against an isolated build session keyed by
the absolute path of the context directory. Successful layers are
materialised as branches named ``layer:<chain-hash>`` so that
re-running the same Dockerfile reuses prior work.

Supported directives (MVP): FROM, RUN, COPY, ADD (without URL/tar),
WORKDIR, ENV, ARG, USER. Other Dockerfile directives parse cleanly
but are skipped with a warning (CMD, ENTRYPOINT, LABEL, EXPOSE,
VOLUME, STOPSIGNAL, MAINTAINER, HEALTHCHECK, ONBUILD, SHELL).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from contree_cli import (
    CLIENT,
    FORMATTER,
    PROFILE,
    SESSION_STORE,
    ArgumentsProtocol,
    SetupResult,
)
from contree_cli.docker import (
    ArgKeyword,
    BuildContext,
    DockerKeyword,
    FromKeyword,
    LocalContext,
    RunKeyword,
    parse_dockerfile,
)
from contree_cli.docker.context import BUILD_TIMEOUT_DEFAULT
from contree_cli.session import SessionStore
from contree_cli.types import FLAGS

logger = logging.getLogger(__name__)

EPILOG = """\
examples:
  contree build .
  contree build . --tag myimage:latest
  contree build --dockerfile ./Dockerfile.test ./app
  contree build --build-arg VERSION=1.2 .
  contree build --no-cache .

for coding agents:
  mutating command, may create operations against the API
  layer cache is per-context (session keyed by abspath(context))
  use --no-cache to bypass cached layers and rebuild from scratch
"""


@dataclass(frozen=True)
class BuildArgs(ArgumentsProtocol):
    context: str = "."
    dockerfile: str = ""
    tag: str = ""
    build_args: tuple[str, ...] = field(default_factory=tuple)
    no_cache: bool = False
    timeout: int = BUILD_TIMEOUT_DEFAULT

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> BuildArgs:
        return cls(
            context=ns.context or ".",
            dockerfile=ns.dockerfile or "",
            tag=ns.tag or "",
            build_args=tuple(ns.build_arg or ()),
            no_cache=bool(ns.no_cache),
            timeout=ns.timeout,
        )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "context",
        nargs="?",
        default=".",
        help="Build context directory",
    )
    p.add_argument(
        *FLAGS["dockerfile"],
        default="",
        metavar="PATH",
        help="Dockerfile path (default: <context>/Dockerfile)",
    )
    p.add_argument(
        *FLAGS["tag_name"],
        default="",
        metavar="NAME[:TAG]",
        help="Tag the final image",
    )
    p.add_argument(
        *FLAGS["build_arg"],
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Build-time variable (repeatable)",
    )
    p.add_argument(
        *FLAGS["no_cache"],
        action="store_true",
        help="Ignore cached layers and rebuild",
    )
    p.add_argument(
        *FLAGS["timeout"],
        type=int,
        default=BUILD_TIMEOUT_DEFAULT,
        help="Timeout in seconds for each RUN step",
    )
    return cmd_build, BuildArgs


def cmd_build(args: BuildArgs) -> int | None:
    context_dir = Path(args.context).expanduser().resolve()
    if not context_dir.is_dir():
        logger.error("context %s is not a directory", context_dir)
        return 1

    dockerfile_path = (
        Path(args.dockerfile).expanduser()
        if args.dockerfile
        else context_dir / "Dockerfile"
    )
    if not dockerfile_path.is_file():
        logger.error("Dockerfile %s not found", dockerfile_path)
        return 1

    text = dockerfile_path.read_text()
    try:
        directives = parse_dockerfile(text)
    except ValueError as exc:
        logger.error("Dockerfile parse error: %s", exc)
        return 1

    if not validate_first_directive(directives):
        logger.error("Dockerfile must contain a FROM directive")
        return 1

    build_args = parse_build_args(args.build_args)

    profile = PROFILE.get()
    client = CLIENT.get()
    session_key = make_session_key(context_dir)
    store = SessionStore(profile.session_db_path, session_key)
    SESSION_STORE.set(store)

    ctx = BuildContext(
        client=client,
        store=store,
        local=LocalContext.from_dir(context_dir),
        build_args=build_args,
        no_cache=args.no_cache,
        timeout=args.timeout,
    )

    try:
        for kw in directives:
            kw.execute(ctx)
        finalize_pending(ctx)
    except Exception as exc:
        logger.error("build failed: %s", exc)
        return 1

    if not ctx.last_image:
        logger.error("build produced no image")
        return 1

    if args.tag:
        client.patch_json(
            f"/v1/images/{ctx.last_image}/tag",
            {"tag": args.tag},
        )
        logger.info("tagged %s as %s", ctx.last_image, args.tag)

    formatter = FORMATTER.get()
    formatter(
        image=ctx.last_image,
        tag=args.tag,
        session=session_key,
    )
    formatter.flush()
    return None


def validate_first_directive(directives: list[DockerKeyword]) -> bool:
    for d in directives:
        if isinstance(d, FromKeyword):
            return True
        if isinstance(d, ArgKeyword):
            continue
        return False
    return False


def parse_build_args(items: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--build-arg expected KEY=VALUE, got {item!r}")
        k, _, v = item.partition("=")
        out[k] = v
    return out


def make_session_key(context_dir: Path) -> str:
    digest = hashlib.sha256(str(context_dir).encode()).hexdigest()
    return f"build:{digest[:16]}"


def finalize_pending(ctx: BuildContext) -> None:
    """If COPY/ADD left files pending, commit them via a trivial RUN."""
    if not ctx.pending:
        return
    closer = RunKeyword(parts=(":",), shell_form=True)
    closer.execute(ctx)
