"""List and import sandbox images.

Without a subcommand, lists images (same as ``images list``).

Subcommands:
  list (ls)     List images with filtering and pagination
  import        Import image from a container registry
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from contree_cli import CLIENT, FORMATTER, ArgumentsProtocol, SetupResult
from contree_cli.client import ApiError
from contree_cli.types import (
    FLAGS,
    ArgumentsFormatter,
    isoformat_datetime,
    parse_datetime,
    parse_interval,
    positive_int,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
LIMIT_DEFAULT = 3000
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
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

    base_params: dict[str, str] = {}
    if args.prefix is not None:
        base_params["tag"] = args.prefix
    if args.uuid is not None:
        base_params["uuid"] = args.uuid
    if not args.all_images:
        base_params["tagged"] = "1"
    if args.since is not None:
        base_params["since"] = isoformat_datetime(args.since)
    if args.until is not None:
        base_params["until"] = isoformat_datetime(args.until)

    offset = 0
    emitted = 0
    while emitted < args.limit:
        page_size = min(PAGE_SIZE, args.limit - emitted)
        params = {
            **base_params,
            "offset": str(offset),
            "limit": str(page_size),
        }
        resp = client.get("/v1/images", params=params)
        data = json.loads(resp.read())
        images = data["images"]
        if not images:
            return
        for image in images:
            created_at = parse_datetime(image["created_at"])
            formatter(
                uuid=image["uuid"],
                created_at=created_at,
                tag=image.get("tag") or "",
            )
        emitted += len(images)
        if len(images) < page_size:
            return
        offset += len(images)
        if emitted < args.limit:
            logger.info(
                "Fetched %d images so far... (press Ctrl+C to break)",
                emitted,
            )

    # Hit the limit. Probe one extra record (offset=emitted, limit=1) to
    # detect truncation without re-fetching a full page.
    probe_params = {**base_params, "offset": str(offset), "limit": "1"}
    resp = client.get("/v1/images", params=probe_params)
    data = json.loads(resp.read())
    if data.get("images"):
        # Flush buffered output (e.g. TableFormatter) before the warning
        # so the truncation note appears AFTER the listing on screen.
        formatter.flush()
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

    # 1. Build credentials (prompt for password when --username given)
    credentials: dict[str, str] | None = None
    if args.username is not None:
        password = args.password or getpass.getpass("Registry password: ")
        credentials = {"username": args.username, "password": password}

    if credentials is not None:
        masked = credentials["password"][:3] + "***"
        cred_info = f"credentials {credentials['username']}:{masked}"
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
        registry: dict[str, object] = {"url": url}
        if credentials is not None:
            registry["credentials"] = credentials
        payload: dict[str, object] = {
            "registry": registry,
            "tag": tag,
        }
        if args.timeout is not None:
            payload["timeout"] = args.timeout
        resp = client.post_json("/v1/images/import", payload)
        data = json.loads(resp.read())
        op_uuids.append(data["uuid"])

    # 3. Poll every 5 seconds until all operations reach a terminal state
    pending = set(range(len(op_uuids)))
    failed = False
    try:
        while pending:
            time.sleep(5)
            for idx in list(pending):
                resp = client.get(f"/v1/operations/{op_uuids[idx]}")
                op = json.loads(resp.read())
                if op["status"] in TERMINAL_STATUSES:
                    pending.discard(idx)
                    if op["status"] != "SUCCESS":
                        failed = True
                    formatter(
                        uuid=op_uuids[idx],
                        status=op["status"],
                        registry_url=imports[idx][0],
                        image=(op.get("result") or {}).get("image", ""),
                    )
    except KeyboardInterrupt:
        # Cancel ALL operations on Ctrl+C
        for op_uuid in op_uuids:
            try:
                client.delete(f"/v1/operations/{op_uuid}")
                logger.info("Cancelled operation %s", op_uuid)
            except (ApiError, KeyboardInterrupt, OSError):
                pass
        raise

    return 1 if failed else None
