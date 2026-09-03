"""``FROM image[:tag] [AS name]`` - set the base image for the build."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from dataclasses import dataclass
from typing import ClassVar

from contree_client.exceptions import APIStatusError
from contree_client.models import ImageImportRegistry

from contree_cli.cli.images import normalize_registry_url

from .context import BuildContext, resolve_stage_ref
from .keyword import DockerKeyword
from .kw_run import RunKeyword

logger = logging.getLogger(__name__)


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
        if ctx.last_image:
            seal_stage(ctx)
        ctx.current_stage_alias = self.alias
        ctx.env.clear()
        ctx.workdir = "/"
        ctx.user = ""

        ref = ctx.substitute(self.image_ref)
        image_uuid = resolve_stage_ref(ctx, ref)
        if image_uuid is None:
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


def seal_stage(ctx: BuildContext) -> None:
    """Close the stage in progress before the next FROM starts.

    Pending files exist only as attachments for a future RUN, so a
    stage that ends with COPY/ADD is committed through the same
    trivial closer that finalize_pending uses; the sealed image then
    becomes addressable via ``COPY --from=<alias|index>``.
    """
    if ctx.pending:
        # Sealing just commits already-uploaded files; it must run as
        # root regardless of the stage's active USER (matching how
        # copy_from_image's own extraction RUN clears ctx.user).
        saved_user = ctx.user
        ctx.user = ""
        try:
            RunKeyword(parts=(":",), shell_form=True).execute(ctx)
        finally:
            ctx.user = saved_user
    ctx.stage_images.append(ctx.last_image)
    if ctx.current_stage_alias:
        ctx.stages[ctx.current_stage_alias] = ctx.last_image
    logger.info(
        "stage %s sealed -> %s",
        ctx.current_stage_alias or len(ctx.stage_images) - 1,
        ctx.last_image,
    )


def resolve_or_import(ctx: BuildContext, ref: str) -> str:
    """Resolve ``ref`` to a UUID, importing from a registry on miss."""
    try:
        return ctx.client.resolve_image(ref)
    except APIStatusError as exc:
        if exc.status != 404:
            raise

    url = normalize_registry_url(ref)
    tag = ref if not ref.startswith("docker://") else url.removeprefix("docker://")
    logger.info("FROM auto-import %s as tag %s", url, tag)

    op_uuid = ctx.client.import_image(
        ImageImportRegistry(url=url),
        tag=tag,
        timeout=ctx.timeout if ctx.timeout else ...,
    )

    try:
        return wait_import(ctx, op_uuid, tag)
    except KeyboardInterrupt:
        with contextlib.suppress(APIStatusError, OSError):
            ctx.client.cancel_operation(op_uuid)
        raise


def wait_import(ctx: BuildContext, op_uuid: str, tag: str) -> str:
    op = ctx.client.wait_operation(op_uuid).to_dict()
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
