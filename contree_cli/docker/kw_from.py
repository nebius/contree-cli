"""``FROM image[:tag] [AS name]`` - set the base image for the build."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import ClassVar

from contree_cli.cli.images import normalize_registry_url
from contree_cli.client import ApiError, resolve_image

from .context import BuildContext
from .keyword import DockerKeyword

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})


@dataclass(frozen=True, repr=False)
class FromKeyword(DockerKeyword):
    NAME: ClassVar[str] = "FROM"
    image_ref: str = ""
    alias: str = ""

    def __repr__(self) -> str:
        if self.alias:
            return f"FROM {self.image_ref} AS {self.alias}"
        return f"FROM {self.image_ref}"

    @classmethod
    def parse(cls, args_text: str) -> FromKeyword:
        raw = args_text.strip()
        if not raw:
            raise ValueError("FROM requires an image reference")
        parts = raw.split()
        if len(parts) == 1:
            return cls(image_ref=parts[0], alias="")
        if len(parts) == 3 and parts[1].upper() == "AS":
            return cls(image_ref=parts[0], alias=parts[2])
        raise ValueError(f"invalid FROM syntax: {raw!r}")

    def serialize(self) -> str:
        return f"FROM {self.image_ref}" + (f" AS {self.alias}" if self.alias else "")

    def execute(self, ctx: BuildContext) -> None:
        ref = ctx.substitute(self.image_ref)
        image_uuid = resolve_or_import(ctx, ref)

        from_hash = hashlib.sha256(f"FROM:{image_uuid}".encode()).hexdigest()
        branch_name = f"layer:{BuildContext.short_hash(from_hash)}"

        ctx.pending.clear()
        cached = ctx.try_cache_hit(branch_name)
        if cached is not None:
            logger.info("CACHED: %r -> %s", self, cached)
            ctx.parent_hash = from_hash
            return

        ctx.commit_layer(
            branch_name,
            image_uuid,
            kind="use",
            title=f"FROM {ref}",
        )
        ctx.parent_hash = from_hash


def resolve_or_import(ctx: BuildContext, ref: str) -> str:
    """Resolve ``ref`` to a UUID, importing from a registry on miss."""
    try:
        return resolve_image(ctx.client, ref)
    except ApiError as exc:
        if exc.status != 404:
            raise

    url = normalize_registry_url(ref)
    tag = ref if not ref.startswith("docker://") else url.removeprefix("docker://")
    logger.info("FROM auto-import %s as tag %s", url, tag)

    payload: dict[str, object] = {"registry": {"url": url}, "tag": tag}
    if ctx.timeout:
        payload["timeout"] = ctx.timeout
    resp = ctx.client.post_json("/v1/images/import", payload)
    op = json.loads(resp.read())
    op_uuid: str = op["uuid"]

    try:
        return wait_import(ctx, op_uuid, tag)
    except KeyboardInterrupt:
        with contextlib.suppress(ApiError, OSError):
            ctx.client.delete(f"/v1/operations/{op_uuid}")
        raise


def wait_import(ctx: BuildContext, op_uuid: str, tag: str) -> str:
    delay = 1.0
    while True:
        time.sleep(delay)
        resp = ctx.client.get(f"/v1/operations/{op_uuid}")
        op = json.loads(resp.read())
        if op["status"] in TERMINAL_STATUSES:
            break
        if delay < 5:
            delay += delay
    if op["status"] != "SUCCESS":
        raise RuntimeError(
            f"image import {tag!r} ended with {op['status']}"
            + (f": {op.get('error', '')}" if op.get("error") else "")
        )
    result = op.get("result") or {}
    image = result.get("image")
    if not image:
        raise RuntimeError(f"image import {tag!r} returned no image")
    return str(image)
