"""List files in the session image.

Uses the /inspect/ API to list directory contents without spawning an
instance. Defaults to the session working directory (set via `cd`).

In default format, the API returns a pre-formatted text listing. In
structured formats (json, csv, etc.) the response is cached per
(image, path) for instant repeat queries.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, cast

from contree_client.runtime import RequestSpec, error_for_response

from contree_cli import CLIENT, FORMATTER, SESSION_STORE, ArgumentsProtocol, SetupResult
from contree_cli.output import DefaultFormatter

EPILOG = """\
for coding agents:
  read-only command (inspect API, no instance spawn)
  defaults to session cwd when PATH is omitted
  use -o json for cacheable structured listings
"""


def setup_parser(p: argparse.ArgumentParser) -> SetupResult:
    p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path inside image (defaults to session cwd)",
    )
    return cmd_ls, LsArgs


@dataclass(frozen=True)
class LsArgs(ArgumentsProtocol):
    path: str | None

    @classmethod
    def from_args(cls, ns: argparse.Namespace) -> LsArgs:
        return cls(path=ns.path)


def cmd_ls(args: LsArgs) -> None:
    client = CLIENT.get()
    formatter = FORMATTER.get()
    formatter.configure(tail=("type",))
    store = SESSION_STORE.get()
    image = store.current_image
    uuid = client.resolve_image(image)
    if args.path is not None:
        path = store.resolve_path(args.path)
    else:
        path = store.get_cwd() or "/"

    if isinstance(formatter, DefaultFormatter):
        # The pre-formatted text listing (?text=1) has no typed method
        # in contree-client; issue the request through the raw spec.
        response = client.call(
            RequestSpec(
                method="GET",
                path=f"/inspect/{uuid}/list",
                query={"path": path, "text": "1"},
                idempotent=True,
            )
        )
        if response.status != 200:
            raise error_for_response(response)
        sys.stdout.write(response.body.decode())
        return

    cache_key = (uuid, f"list:{path}")
    cached = store.cache.get(cache_key)
    if cached is not None:
        data = cast("dict[str, Any]", cached)
    else:
        data = client.inspect_image_list(uuid, path).to_dict()
        store.cache[cache_key] = data
    for f in data["files"]:
        if f.get("is_dir"):
            ftype = "d"
        elif f.get("is_symlink"):
            ftype = "l"
        else:
            ftype = "-"
        formatter(
            **{
                **f,
                "owner": f.get("owner") or f.get("uid", ""),
                "group": f.get("group") or f.get("gid", ""),
                "type": ftype,
            }
        )
