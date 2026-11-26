"""Materialize Dockerfile files and metadata through a keep-alive instance."""

from __future__ import annotations

import copy
import logging
import posixpath
import tarfile
import time
from dataclasses import dataclass, replace
from typing import IO, Any

from contree_client.models import FileItem, FileSpec

from .archive import ArchiveBuffer, ArchiveInfo, rewrite_image_archive
from .context import BUILD_TIMEOUT_DEFAULT, BuildContext, PendingFile
from .metadata import inspect_destination, restore_metadata

logger = logging.getLogger(__name__)

MAX_ARCHIVE_ENTRIES = 10_000
CLEANUP_TIMEOUT = 10


@dataclass(frozen=True)
class StagedArchive:
    path: str
    file_uuid: str
    sha256: str
    info: ArchiveInfo
    owner: tuple[int, int] | None = None
    preserve_root: bool = True

    def cache_repr(self) -> dict[str, Any]:
        return {
            "archive": self.sha256,
            "path": self.path,
            "owner": self.owner,
            "preserve_root": self.preserve_root,
            "metadata": [
                (
                    entry.path,
                    entry.uid,
                    entry.gid,
                    entry.mode,
                    entry.is_symlink,
                    entry.is_directory,
                )
                for entry in self.info.entries
            ],
        }


@dataclass(frozen=True)
class StagedLink:
    path: str
    target: str
    symbolic: bool
    uid: int
    gid: int
    mode: int = 0

    def cache_repr(self) -> dict[str, Any]:
        return {
            "link": self.path,
            "target": self.target,
            "symbolic": self.symbolic,
            "uid": self.uid,
            "gid": self.gid,
            "mode": self.mode,
        }


def stage_archive(
    ctx: BuildContext,
    content: IO[bytes],
    info: ArchiveInfo,
    path: str,
    owner: tuple[int, int] | None,
    *,
    preserve_root: bool = True,
) -> None:
    if path != "/" and len(info.entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError(
            f"COPY archive has {len(info.entries)} entries; the guest limit is "
            f"{MAX_ARCHIVE_ENTRIES}, including the directory root header"
        )
    if path != "/":
        stored = ctx.client.ensure_file(content)
        ctx.archives.append(
            StagedArchive(
                path, str(stored.uuid), str(stored.sha256), info, owner, preserve_root
            )
        )
        return
    metadata = {entry.path: entry for entry in info.entries}
    links: list[StagedLink] = []
    deferred_links: set[str] = set()
    with tarfile.open(fileobj=content, mode="r:") as source:
        groups: dict[str, list[tarfile.TarInfo]] = {}
        for member in source.getmembers():
            name = posixpath.normpath(member.name)
            if name == ".":
                continue
            entry = metadata[name]
            uid, gid = owner or (entry.uid, entry.gid)
            root = name.split("/", 1)[0]
            if "/" not in name and member.issym():
                links.append(StagedLink("/" + name, member.linkname, True, uid, gid))
            elif member.islnk() and (
                member.linkname.split("/", 1)[0] != root
                or member.linkname in deferred_links
            ):
                deferred_links.add(name)
                links.append(
                    StagedLink(
                        "/" + name, "/" + member.linkname, False, uid, gid, entry.mode
                    )
                )
            elif "/" not in name and member.isreg():
                extracted = source.extractfile(member)
                if extracted is None:
                    raise ValueError(f"COPY archive has no content for {name}")
                with extracted:
                    stored = ctx.client.ensure_file(extracted)
                ctx.pending.append(
                    PendingFile(
                        "/" + name,
                        str(stored.uuid),
                        str(stored.sha256),
                        uid,
                        gid,
                        f"{entry.mode:04o}",
                        replace_existing=info.is_directory,
                    )
                )
            else:
                groups.setdefault(root, []).append(member)
        group_names = {
            root: {posixpath.normpath(member.name) for member in members}
            for root, members in groups.items()
        }
        for link in links:
            parents = link.path.lstrip("/").split("/")[:-1]
            if not parents:
                continue
            members = groups.setdefault(parents[0], [])
            names = group_names.setdefault(parents[0], set())
            for index in range(1, len(parents) + 1):
                name = "/".join(parents[:index])
                if name not in names:
                    parent = tarfile.TarInfo(name)
                    parent.type = tarfile.DIRTYPE
                    parent.mode = 0o755
                    members.insert(index - 1, parent)
                    names.add(name)
        for root, members in groups.items():
            with (
                ArchiveBuffer(max_size=8 * 1024 * 1024) as subtree,
                ArchiveBuffer(max_size=8 * 1024 * 1024) as prepared,
            ):
                with tarfile.open(fileobj=subtree, mode="w") as output:
                    for member in members:
                        original_entry = metadata.get(posixpath.normpath(member.name))
                        restored = copy.copy(member)
                        if original_entry is not None:
                            restored.uid, restored.gid = (
                                original_entry.uid,
                                original_entry.gid,
                            )
                            restored.mode = original_entry.mode
                        if member.isreg():
                            extracted = source.extractfile(member)
                            if extracted is None:
                                raise ValueError(
                                    f"COPY archive has no content for {member.name}"
                                )
                            with extracted:
                                output.addfile(restored, extracted)
                        else:
                            output.addfile(restored)
                subtree.seek(0)
                child_info = rewrite_image_archive(subtree, prepared)
                stage_archive(
                    ctx, prepared, child_info, "/" + root, owner, preserve_root=False
                )
    ctx.archives.extend(links)


def wait_ready(ctx: BuildContext, operation: str) -> str:
    events = ctx.client.follow_operation_events(operation, timeout=ctx.timeout or None)
    try:
        for event in events:
            if str(event.type) == "lifecycle" and event.spid == 1:
                data = event.to_dict()["data"]
                cwd = data.get("cwd")
                if not isinstance(cwd, str) or not cwd.startswith("/"):
                    raise RuntimeError("Instance readiness event has no absolute cwd")
                return cwd
            if event.type == "completion":
                raise RuntimeError(f"Build instance {operation} ended before readiness")
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()
    raise RuntimeError(f"Build instance {operation} has no readiness event")


def stop_instance(ctx: BuildContext, operation: str) -> dict[str, Any]:
    ctx.client.operation_subprocess_kill(operation, 1, signal="TERM")
    result = ctx.client.wait_operation(
        operation, timeout=ctx.timeout or None, follow_events=False
    ).to_dict()
    if result.get("status") != "SUCCESS":
        raise RuntimeError(
            f"Build instance {operation} failed: "
            f"{result.get('error') or result.get('status')}"
        )
    return result


def cancel_instance(ctx: BuildContext, operation: str) -> None:
    try:
        ctx.client.bounded(time.monotonic() + CLEANUP_TIMEOUT).cancel_operation(
            operation
        )
    except Exception:
        logger.warning("Could not cancel build instance %s", operation, exc_info=True)


def read_image_workdir(ctx: BuildContext) -> str:
    key = (ctx.last_image, "docker_workdir")
    cached = ctx.store.cache.get(key)
    if isinstance(cached, str) and cached.startswith("/"):
        return cached
    execution = replace(
        ctx,
        client=ctx.client.bounded(
            time.monotonic() + (ctx.timeout or BUILD_TIMEOUT_DEFAULT)
        ),
    )
    spawned = execution.client.spawn_instance(
        "", ctx.last_image, shell=False, disposable=True, timeout=ctx.timeout or ...
    )
    operation = str(spawned.uuid)
    try:
        cwd = wait_ready(execution, operation)
        stop_instance(execution, operation)
    except BaseException:
        cancel_instance(ctx, operation)
        raise
    ctx.store.cache[key] = cwd
    return cwd


def run_metadata_command(
    ctx: BuildContext, operation: str, command: str, args: list[str]
) -> None:
    spid = ctx.client.operation_subprocess_create(
        operation, command, args=args, shell=False, cwd="/", uid=0, gid=0
    )
    events = ctx.client.follow_operation_events(
        operation, spid=spid, timeout=ctx.timeout or None
    )
    try:
        for event in events:
            if event.type == "exit":
                data = event.to_dict()["data"]
                if data.get("code") != 0 or data.get("timed_out"):
                    raise RuntimeError(
                        f"COPY metadata command {command!r} failed: {data}"
                    )
                return
            if event.type == "completion":
                break
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()
    raise RuntimeError(f"COPY metadata command {command!r} has no exit event")


def materialize(ctx: BuildContext) -> None:
    chain = ctx.chain("MATERIALIZE api-v4 preserve_env preserve_cwd")
    branch = f"layer:{ctx.short_hash(chain)}"
    if ctx.try_cache_hit(branch) is None:
        deadline = time.monotonic() + (ctx.timeout or BUILD_TIMEOUT_DEFAULT)
        execution = replace(ctx, client=ctx.client.bounded(deadline))
        destinations: dict[str, dict[str, FileItem]] = {}
        files = {}
        for item in ctx.pending:
            if (
                item.replace_existing
                and inspect_destination(execution, item.instance_path, destinations)
                is not None
            ):
                continue
            files[item.instance_path] = FileSpec(
                uuid=item.file_uuid, uid=item.uid, gid=item.gid, mode=item.mode
            )
        spawned = execution.client.spawn_instance(
            "",
            ctx.last_image,
            shell=False,
            disposable=False,
            timeout=ctx.timeout or ...,
            cwd=ctx.workdir or ...,
            env=dict(ctx.env) if ctx.env else ...,
            preserve_env=True,
            preserve_cwd=True,
            files=files or ...,
        )
        operation = str(spawned.uuid)
        try:
            cwd = wait_ready(execution, operation)
            for item in ctx.pending:
                if item.instance_path in files:
                    continue
                existing = inspect_destination(
                    execution, item.instance_path, destinations
                )
                uid, gid, mode = (
                    (existing.uid, existing.gid, existing.mode & 0o7777)
                    if existing is not None and existing.is_regular
                    else (0, 0, 0o644)
                )
                execution.client.apply_instance_file(
                    operation,
                    item.file_uuid,
                    item.instance_path,
                    unpack=False,
                    replace=True,
                )
                owner_changed = (item.uid, item.gid) != (uid, gid)
                if owner_changed:
                    run_metadata_command(
                        execution,
                        operation,
                        "chown",
                        ["-h", f"{item.uid}:{item.gid}", item.instance_path],
                    )
                if int(item.mode, 8) != mode or (owner_changed and mode & 0o6000):
                    run_metadata_command(
                        execution, operation, "chmod", [item.mode, item.instance_path]
                    )
            for archive in ctx.archives:
                if isinstance(archive, StagedLink):
                    flags = "-sfnT" if archive.symbolic else "-fT"
                    run_metadata_command(
                        execution,
                        operation,
                        "ln",
                        [flags, "--", archive.target, archive.path],
                    )
                    if archive.uid or archive.gid:
                        run_metadata_command(
                            execution,
                            operation,
                            "chown",
                            ["-h", f"{archive.uid}:{archive.gid}", archive.path],
                        )
                    if not archive.symbolic and archive.mode & 0o7000:
                        run_metadata_command(
                            execution,
                            operation,
                            "chmod",
                            [f"{archive.mode:o}", archive.path],
                        )
                    continue
                execution.client.apply_instance_file(
                    operation,
                    archive.file_uuid,
                    archive.path,
                    unpack=True,
                    replace=True,
                    merge=True,
                )
                restore_metadata(execution, operation, archive)
            result = stop_instance(execution, operation)
            image = (result.get("result") or {}).get("image")
            if not image:
                raise RuntimeError("Build instance succeeded without a result image")
            ctx.commit_layer(
                branch,
                str(image),
                kind="run",
                title="COPY/metadata",
                operation_uuid=operation,
            )
            ctx.store.cache[(str(image), "docker_workdir")] = cwd
        except BaseException:
            cancel_instance(ctx, operation)
            raise
    ctx.parent_hash = chain
    ctx.pending.clear()
    ctx.archives.clear()
    ctx.persisted_env = dict(ctx.env)
    ctx.persisted_workdir = ctx.workdir
