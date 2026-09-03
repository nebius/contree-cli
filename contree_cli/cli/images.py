"""List and import sandbox images.

Without a subcommand, lists images (same as ``images list``).

Subcommands:
  list (ls)     List images with filtering and pagination
  import        Import image from a container registry
"""

from __future__ import annotations

import argparse
import getpass
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from contree_client.exceptions import APIStatusError
from contree_client.models import (
    TERMINAL_STATUSES,
    ImageImportRegistry,
    ImageImportRegistryCredentials,
)

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.types import (
    FLAGS,
    ArgumentsFormatter,
    parse_interval,
    positive_int,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
LIMIT_DEFAULT = 3000
DOCKER_HUB = "docker.io"

EPILOG = """\
examples:
  contree images --prefix=ubuntu
  contree images list --all
  contree images import ubuntu:latest
  contree images import ubuntu:{latest,noble,jammy}
  contree images import ghcr.io/owner/image:tag

for coding agents:
  `images` / `images list` is read-only
  `images import` spawns async import operations and polls until completion
  supports brace expansion for batch imports
  Ctrl+C cancels all active import operations
"""

IMPORT_EPILOG = """\
examples:
  contree images import ubuntu:latest
  contree images import --timeout 600 ubuntu:latest
  contree images import docker.io/ubuntu:latest
  contree images import docker://docker.io/ubuntu:latest
  contree images import ghcr.io/ubuntu/ubuntu:latest
  contree images import ubuntu:{latest,noble,jammy}

for coding agents:
  mutating command — creates import operations
  all formats are normalised to docker://registry/path:tag
  polls every 5 seconds until all operations complete
  Ctrl+C cancels all active import operations
"""


# ---------------------------------------------------------------------------
# Args dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImagesArgs(ArgumentsProtocol):
    prefix: str | None = None
    uuid: str | None = None
    all_images: bool = False
    since: datetime | None = None
    until: datetime | None = None
    limit: int = LIMIT_DEFAULT

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ImagesArgs:
        return cls(
            prefix=getattr(ns, "prefix", None),
            uuid=getattr(ns, "uuid", None),
            all_images=getattr(ns, "all_images", False),
            since=getattr(ns, "since", None),
            until=getattr(ns, "until", None),
            limit=getattr(ns, "limit", LIMIT_DEFAULT),
        )


@dataclass(frozen=True)
class ImportArgs(ArgumentsProtocol):
    refs: list[str] = field(default_factory=list)
    username: str | None = None
    password: str | None = None
    timeout: int | None = None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> ImportArgs:
        return cls(
            refs=ns.refs,
            username=ns.username,
            password=ns.password,
            timeout=ns.timeout,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def expand_braces(ref: str) -> list[str]:
    """Expand ``name:{a,b,c}`` into ``[name:a, name:b, name:c]``."""
    start = ref.find("{")
    if start == -1:
        return [ref]
    end = ref.find("}", start)
    if end == -1:
        return [ref]
    prefix = ref[:start]
    suffix = ref[end + 1 :]
    return [prefix + alt + suffix for alt in ref[start + 1 : end].split(",")]


def normalize_registry_url(ref: str) -> str:
    """Normalise an image reference to ``docker://registry/path:tag``.

    When the ``docker://`` scheme is already present the URL is treated as
    fully-qualified and returned unchanged.  Otherwise Docker Hub
    single-segment paths get the ``library/`` prefix automatically.
    """
    if ref.startswith("docker://"):
        return ref

    raw = ref

    parts = raw.split("/")

    if len(parts) == 1:
        # Bare image name, e.g. "ubuntu:latest"
        registry = DOCKER_HUB
        image_path = f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0]:
        # Explicit registry, e.g. "docker.io/ubuntu:latest" or
        # "ghcr.io/owner/image:tag"
        registry = parts[0]
        remaining = "/".join(parts[1:])
        if registry == DOCKER_HUB and "/" not in remaining:
            image_path = f"library/{remaining}"
        else:
            image_path = remaining
    else:
        # Multi-segment Docker Hub path, e.g. "myuser/myimage:tag"
        registry = DOCKER_HUB
        image_path = "/".join(parts)

    # Ensure a tag is present
    last_segment = image_path.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        image_path += ":latest"

    return f"docker://{registry}/{image_path}"


# ---------------------------------------------------------------------------
# Parser setup
# ---------------------------------------------------------------------------


def _add_list_args(p: argparse.ArgumentParser) -> None:
    """Add the shared listing/filter arguments to *p*."""
    p.add_argument(*FLAGS["prefix"], help="Filter by tag prefix")
    p.add_argument(*FLAGS["uuid"], help="Filter by image UUID")
    p.add_argument(
        *FLAGS["all"],
        action="store_true",
        dest="all_images",
        help="Include untagged images (default: tagged only)",
    )
    p.add_argument(
        *FLAGS["since"],
        type=parse_interval,
        help=parse_interval.__doc__,
    )
    p.add_argument(
        *FLAGS["until"],
        type=parse_interval,
        help="Show images before. " + str(parse_interval.__doc__),
    )
    p.add_argument(
        *FLAGS["limit"],
        type=positive_int,
        default=LIMIT_DEFAULT,
        help="Stop after this many images and warn if more are available",
    )


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    # Parent-level list args mirror the subcommand so `contree images
    # --prefix …` works without typing `list`.
    _add_list_args(p)

    sub = p.add_subparsers(dest="images_action")

    # images list / images ls
    list_p = sub.add_parser(
        "list",
        aliases=["ls"],
        help="List images",
        formatter_class=ArgumentsFormatter,
    )
    _add_list_args(list_p)
    list_p.set_defaults(handler=cmd_images, load_args=ImagesArgs)

    # images import
    import_p = sub.add_parser(
        "import",
        help="Import image from container registry",
        epilog=IMPORT_EPILOG,
        formatter_class=ArgumentsFormatter,
    )
    import_p.add_argument(
        "refs",
        nargs="+",
        help="Image references (supports brace expansion)",
    )
    import_p.add_argument(
        *FLAGS["username"],
        default=None,
        help="Registry username (enables credentials)",
    )
    import_p.add_argument(
        *FLAGS["password"],
        default=None,
        help="Registry password (prompted securely if --username given)",
    )
    import_p.add_argument(
        *FLAGS["timeout"],
        type=int,
        default=None,
        help="Import timeout in seconds",
    )
    import_p.set_defaults(handler=cmd_import, load_args=ImportArgs)

    return cmd_images, ImagesArgs


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def cmd_images(args: ImagesArgs) -> None:
    client = CLIENT.get()
    formatter = FORMATTER.get()

    hit_limit = False
    # Fetch one extra record past the budget so truncation is
    # detectable and the warning below can fire.
    images = client.iter_images(
        tag=args.prefix,
        uuid=args.uuid,
        tagged=not args.all_images,
        since=args.since,
        until=args.until,
        page_size=PAGE_SIZE,
        limit=args.limit + 1,
    )
    for emitted, image in enumerate(images):
        if emitted >= args.limit:
            hit_limit = True
            break
        formatter(**image.to_dict())
        # Buffering formatters (table) print nothing until the end, so
        # each consumed page reports progress while the next one loads.
        if (emitted + 1) % PAGE_SIZE == 0:
            logger.info("Fetched %d images, loading more...", emitted + 1)
    formatter.flush()

    if hit_limit:
        logger.warning(
            "Output truncated at --limit=%d images; more results are"
            " available. Raise --limit or narrow with"
            " --prefix/--since/--until.",
            args.limit,
        )


def _parse_explicit_tag(ref: str) -> tuple[str, str | None]:
    """Split ``ref?tag=VALUE`` into ``(ref, VALUE)`` or ``(ref, None)``."""
    if "?tag=" in ref:
        base, tag = ref.split("?tag=", 1)
        return base, tag
    return ref, None


def _derive_tag(ref: str) -> str:
    """Decanonize: strip scheme and registry host, keep namespace + image.

    ``docker://docker.io/library/ubuntu:latest`` → ``ubuntu:latest``
    ``docker://docker.io/nimlang/nim:latest`` → ``nimlang/nim:latest``
    ``docker://ghcr.io/owner/image:tag`` → ``owner/image:tag``
    ``ubuntu:latest`` → ``ubuntu:latest``
    """
    clean = ref.removeprefix("docker://")
    # Remove registry host (first segment if it contains a dot)
    parts = clean.split("/", 1)
    if len(parts) == 2 and "." in parts[0]:
        clean = parts[1]
    # Remove default "library/" prefix from Docker Hub
    clean = clean.removeprefix("library/")
    return clean


def cmd_import(args: ImportArgs) -> int | None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("error",))

    # 1. Build credentials (prompt for password when --username given)
    credentials: ImageImportRegistryCredentials | None = None
    if args.username is not None:
        password = args.password or getpass.getpass("Registry password: ")
        credentials = ImageImportRegistryCredentials(
            username=args.username,
            password=password,
        )
        cred_info = f"credentials {args.username}:{password[:3]}***"
    else:
        cred_info = "anonymous credentials"

    # 2. Expand braces, normalise URLs, derive tags
    imports: list[tuple[str, str]] = []  # (url, tag)
    for ref in args.refs:
        for expanded in expand_braces(ref):
            base, explicit_tag = _parse_explicit_tag(expanded)
            url = normalize_registry_url(base)
            tag = explicit_tag if explicit_tag is not None else _derive_tag(base)
            logger.info(
                "Starting import %s with tag %s and %s",
                url,
                tag,
                cred_info,
            )
            imports.append((url, tag))

    # 3. Issue all POST /v1/images/import requests up-front
    op_uuids: list[str] = []
    for url, tag in imports:
        registry = ImageImportRegistry(
            url=url,
            credentials=credentials if credentials is not None else ...,
        )
        op_uuids.append(
            client.import_image(
                registry,
                tag=tag,
                timeout=args.timeout if args.timeout is not None else ...,
            )
        )

    # 3. Poll every 5 seconds until all operations reach a terminal state
    pending = set(range(len(op_uuids)))
    failed = False
    try:
        while pending:
            time.sleep(5)
            for idx in list(pending):
                op = client.get_operation_status(op_uuids[idx]).to_dict()
                if op["status"] in TERMINAL_STATUSES:
                    pending.discard(idx)
                    if op["status"] != "SUCCESS":
                        failed = True
                    formatter(
                        **{
                            **op,
                            "uuid": op_uuids[idx],
                            "registry_url": imports[idx][0],
                            "image": (op.get("result") or {}).get("image", ""),
                        }
                    )
    except KeyboardInterrupt:
        # Cancel ALL operations on Ctrl+C
        for op_uuid in op_uuids:
            try:
                client.cancel_operation(op_uuid)
                logger.info("Cancelled operation %s", op_uuid)
            except (APIStatusError, KeyboardInterrupt, OSError):
                pass
        raise

    return 1 if failed else None
