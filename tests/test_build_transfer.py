from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from contree_cli.docker.archive import ArchiveEntry, ArchiveInfo
from contree_cli.docker.context import BuildContext, PendingFile
from contree_cli.docker.kw_copy import copy_from_image, stage_copy
from contree_cli.docker.local_context import LocalContext
from contree_cli.docker.transfer import (
    StagedArchive,
    StagedLink,
    materialize,
    read_image_workdir,
    restore_metadata,
    stage_archive,
    wait_ready,
)
from contree_cli.session import SessionStore

BASE = "11111111-1111-1111-1111-111111111111"
RESULT = "22222222-2222-2222-2222-222222222222"


def event(kind: str, data: dict[str, object], spid: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        type=kind,
        spid=spid,
        to_dict=lambda: {"type": kind, "spid": spid, "data": data},
    )


def event_stream(
    events: list[SimpleNamespace], closed: list[bool]
) -> Iterator[SimpleNamespace]:
    try:
        yield from events
    finally:
        closed.append(True)


def response(status: str = "SUCCESS", image: str | None = RESULT) -> SimpleNamespace:
    return SimpleNamespace(
        to_dict=lambda: {
            "status": status,
            "metadata": {"result": {"state": {"exit_code": None, "signal": 15}}},
            "result": {"image": image},
        }
    )


@contextmanager
def build_context(tmp_path: Path):
    with SessionStore(tmp_path / "session.db", "transfer-tests") as store:
        client = Mock()
        client.bounded.return_value = client
        client.inspect_image_list.return_value = SimpleNamespace(files=[])
        client.spawn_instance.return_value = SimpleNamespace(uuid="operation-1")
        client.wait_operation.return_value = response()
        closed: list[bool] = []
        client.follow_operation_events.return_value = event_stream(
            [event("lifecycle", {"cwd": "/inherited"})], closed
        )
        ctx = BuildContext(
            client=client,
            store=store,
            local=LocalContext.from_dir(tmp_path),
            last_image=BASE,
            env={"MODE": "build", "EMPTY": ""},
            workdir="/",
            timeout=42,
        )
        yield ctx, client, closed


def staged_archive(
    entries: tuple[ArchiveEntry, ...] = (), owner: tuple[int, int] | None = None
) -> StagedArchive:
    return StagedArchive(
        path="/destination",
        file_uuid="archive-uuid",
        sha256="archive-sha",
        info=ArchiveInfo(True, entries),
        owner=owner,
    )


def test_materialize_uses_api_without_a_shell(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        ctx.pending.append(PendingFile("/file", "file-uuid", "file-sha", 7, 8, "0640"))
        ctx.archives.append(staged_archive())

        materialize(ctx)

        client.spawn_instance.assert_called_once()
        args, kwargs = client.spawn_instance.call_args
        assert args == ("", BASE)
        assert kwargs["shell"] is False
        assert kwargs["disposable"] is False
        assert kwargs["cwd"] == "/"
        assert kwargs["preserve_env"] is True
        assert kwargs["preserve_cwd"] is True
        assert kwargs["env"] == {"MODE": "build", "EMPTY": ""}
        assert kwargs["timeout"] == 42
        assert kwargs["files"]["/file"].to_dict() == {
            "uuid": "file-uuid",
            "uid": 7,
            "gid": 8,
            "mode": "0640",
        }
        client.apply_instance_file.assert_called_once_with(
            "operation-1",
            "archive-uuid",
            "/destination",
            unpack=True,
            replace=True,
            merge=True,
        )
        client.operation_subprocess_kill.assert_called_once_with(
            "operation-1", 1, signal="TERM"
        )
        client.wait_operation.assert_called_once_with(
            "operation-1", timeout=42, follow_events=False
        )
        assert closed == [True]
        assert ctx.last_image == RESULT
        assert ctx.store.current_image == RESULT
        assert ctx.store.cache[(RESULT, "docker_workdir")] == "/inherited"
        assert not ctx.pending
        assert not ctx.archives
        assert ctx.persisted_env == ctx.env
        assert ctx.persisted_workdir == "/"
        client.cancel_operation.assert_not_called()
        client.operation_subprocess_create.assert_not_called()


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([], "no readiness event"),
        ([event("completion", {})], "ended before readiness"),
        ([event("lifecycle", {"cwd": "relative"})], "no absolute cwd"),
        ([event("lifecycle", {"cwd": None})], "no absolute cwd"),
    ],
)
def test_readiness_failure_closes_stream(tmp_path, events, message):
    with build_context(tmp_path) as (ctx, client, closed):
        client.follow_operation_events.return_value = event_stream(events, closed)
        with pytest.raises(RuntimeError, match=message):
            wait_ready(ctx, "operation-1")
        assert closed == [True]


def test_readiness_ignores_other_processes(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        client.follow_operation_events.return_value = event_stream(
            [
                event("spawn", {"cwd": "/wrong"}, 2),
                event("lifecycle", {"cwd": "/wrong"}, 2),
                event("lifecycle", {"cwd": "/correct"}),
                event("completion", {}),
            ],
            closed,
        )
        assert wait_ready(ctx, "operation-1") == "/correct"
        assert closed == [True]


@pytest.mark.parametrize(
    "failure", [RuntimeError("upload failed"), KeyboardInterrupt()]
)
def test_failed_transfer_cancels_without_publishing(tmp_path, failure):
    with build_context(tmp_path) as (ctx, client, closed):
        archive = staged_archive()
        ctx.archives.append(archive)
        client.apply_instance_file.side_effect = failure

        with pytest.raises(type(failure)) as caught:
            materialize(ctx)

        assert caught.value is failure
        client.cancel_operation.assert_called_once_with("operation-1")
        client.operation_subprocess_kill.assert_not_called()
        assert ctx.store.session is None
        assert ctx.last_image == BASE
        assert ctx.parent_hash == ""
        assert ctx.archives == [archive]
        assert ctx.persisted_env == {}
        assert closed == [True]


@pytest.mark.parametrize(
    ("outcome", "message"),
    [(response("FAILED"), "failed"), (response(image=None), "without a result image")],
)
def test_failed_finalization_does_not_publish(tmp_path, outcome, message):
    with build_context(tmp_path) as (ctx, client, closed):
        client.wait_operation.return_value = outcome
        with pytest.raises(RuntimeError, match=message):
            materialize(ctx)
        client.cancel_operation.assert_called_once_with("operation-1")
        assert ctx.store.session is None
        assert ctx.last_image == BASE
        assert closed == [True]


def test_cleanup_failure_keeps_original_error(tmp_path, caplog):
    with build_context(tmp_path) as (ctx, client, closed):
        ctx.archives.append(staged_archive())
        failure = RuntimeError("upload failed")
        client.apply_instance_file.side_effect = failure
        client.cancel_operation.side_effect = RuntimeError("cancel failed")
        with pytest.raises(RuntimeError, match="upload failed") as caught:
            materialize(ctx)
        assert caught.value is failure
        assert "Could not cancel build instance" in caplog.text
        assert ctx.store.session is None
        assert closed == [True]


def test_materialize_reuses_cached_layer(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        archive = staged_archive()
        ctx.archives.append(archive)
        materialize(ctx)
        cached = BuildContext(
            client=client,
            store=ctx.store,
            local=ctx.local,
            last_image=BASE,
            env={"MODE": "build", "EMPTY": ""},
            workdir="/",
            archives=[archive],
        )

        materialize(cached)

        client.spawn_instance.assert_called_once()
        client.apply_instance_file.assert_called_once()
        assert cached.last_image == RESULT
        assert not cached.archives
        assert cached.persisted_env == cached.env
        assert cached.persisted_workdir == "/"
        assert cached.parent_hash == ctx.parent_hash
        assert closed == [True]


def test_metadata_restoration_uses_direct_processes(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        archive = staged_archive(
            (
                ArchiveEntry(".", 12, 13, 0o2755, False, True),
                ArchiveEntry("file", 12, 13, 0o4750, False),
                ArchiveEntry("link", 12, 13, 0o777, True),
            )
        )
        client.operation_subprocess_create.side_effect = [2, 3, 4]
        client.follow_operation_events.side_effect = [
            event_stream([event("exit", {"code": 0}, spid)], closed)
            for spid in range(2, 5)
        ]

        restore_metadata(ctx, "operation-1", archive)

        commands = client.operation_subprocess_create.call_args_list
        assert [(call.args[1], call.kwargs["args"]) for call in commands] == [
            (
                "chown",
                [
                    "-h",
                    "12:13",
                    "/destination/link",
                    "/destination/file",
                ],
            ),
            ("chmod", ["4750", "/destination/file"]),
            ("chmod", ["2755", "/destination"]),
        ]
        assert all(call.kwargs["shell"] is False for call in commands)
        assert all(call.kwargs["uid"] == call.kwargs["gid"] == 0 for call in commands)
        assert all(call.kwargs["cwd"] == "/" for call in commands)
        assert len(closed) == 3


def test_explicit_root_owner_needs_no_command_for_new_root_owned_file(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        archive = staged_archive((ArchiveEntry("file", 99, 100, 0o644, False),), (0, 0))
        client.operation_subprocess_create.return_value = 2
        client.follow_operation_events.return_value = event_stream(
            [event("exit", {"code": 0}, 2)], closed
        )
        restore_metadata(ctx, "operation-1", archive)
        client.operation_subprocess_create.assert_not_called()
        assert closed == []


@pytest.mark.parametrize("exit_data", [{"code": 1}, {"code": 0, "timed_out": True}])
def test_metadata_failure_cancels_layer(tmp_path, exit_data):
    with build_context(tmp_path) as (ctx, client, closed):
        ctx.archives.append(
            staged_archive((ArchiveEntry("file", 99, 100, 0o644, False),))
        )
        client.operation_subprocess_create.return_value = 2
        client.follow_operation_events.side_effect = [
            event_stream([event("lifecycle", {"cwd": "/"})], closed),
            event_stream([event("exit", exit_data, 2)], closed),
        ]
        with pytest.raises(RuntimeError, match=r"metadata command.*failed"):
            materialize(ctx)
        client.cancel_operation.assert_called_once_with("operation-1")
        assert ctx.store.session is None
        assert closed == [True, True]


def test_inherited_workdir_probe_is_disposable_and_cached(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        client.wait_operation.return_value = response(image=None)
        assert read_image_workdir(ctx) == "/inherited"
        assert read_image_workdir(ctx) == "/inherited"
        client.spawn_instance.assert_called_once_with(
            "", BASE, shell=False, disposable=True, timeout=42
        )
        client.operation_subprocess_kill.assert_called_once_with(
            "operation-1", 1, signal="TERM"
        )
        assert ctx.store.cache[(BASE, "docker_workdir")] == "/inherited"
        assert ctx.store.session is None
        assert ctx.last_image == BASE
        assert closed == [True]


def test_failed_workdir_probe_does_not_cache(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        client.follow_operation_events.return_value = event_stream([], closed)
        with pytest.raises(RuntimeError, match="no readiness event"):
            read_image_workdir(ctx)
        assert ctx.store.cache.get((BASE, "docker_workdir")) is None
        client.cancel_operation.assert_called_once_with("operation-1")
        assert closed == [True]


def archive_member(
    path: str,
    kind: bytes = tarfile.REGTYPE,
    target: str = "",
    mode: int = 0o644,
    uid: int = 0,
    gid: int = 0,
) -> tarfile.TarInfo:
    member = tarfile.TarInfo(path)
    member.type = kind
    member.linkname = target
    member.mode = mode
    member.uid = uid
    member.gid = gid
    if member.isreg():
        member.size = 4
    return member


def archive_content(members: list[tarfile.TarInfo]) -> tuple[io.BytesIO, ArchiveInfo]:
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        for member in members:
            archive.addfile(member, io.BytesIO(b"data") if member.isreg() else None)
    content.seek(0)
    return content, ArchiveInfo(
        True,
        tuple(
            ArchiveEntry(
                member.name, member.uid, member.gid, member.mode, member.issym()
            )
            for member in members
        ),
    )


def capture_uploads(client: Mock) -> list[bytes]:
    uploads: list[bytes] = []

    def upload(content):
        data = content.read()
        uploads.append(data)
        return SimpleNamespace(
            uuid=f"file-{len(uploads)}", sha256=hashlib.sha256(data).hexdigest()
        )

    client.ensure_file.side_effect = upload
    return uploads


@pytest.mark.parametrize("owner", [None, (7, 8)])
def test_new_root_file_uses_spawn_metadata_without_guest_tools(
    tmp_path, monkeypatch, owner
):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        uploads = capture_uploads(client)
        content, info = archive_content(
            [
                archive_member(".", tarfile.DIRTYPE, mode=0o755),
                archive_member("program", mode=0o4751, uid=12, gid=13),
            ]
        )
        stage_archive(ctx, content, info, "/", owner)
        assert uploads == [b"data"]
        assert not ctx.archives
        (pending,) = ctx.pending
        assert pending.instance_path == "/program"
        assert (pending.uid, pending.gid) == (owner or (12, 13))
        assert pending.mode == "4751"
        commands = Mock()
        monkeypatch.setattr(
            "contree_cli.docker.transfer.run_metadata_command", commands
        )
        materialize(ctx)
        files = client.spawn_instance.call_args.kwargs["files"]
        assert set(files) == {"/program"}
        assert files["/program"].uid == pending.uid
        assert files["/program"].gid == pending.gid
        assert files["/program"].mode == "4751"
        client.apply_instance_file.assert_not_called()
        commands.assert_not_called()


def test_root_split_keeps_child_directories_and_empty_directories(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        uploads = capture_uploads(client)
        content, info = archive_content(
            [
                archive_member(".", tarfile.DIRTYPE, mode=0o755),
                archive_member("app", tarfile.DIRTYPE, mode=0o750),
                archive_member("app/file", mode=0o640),
                archive_member("empty", tarfile.DIRTYPE, mode=0o700),
            ]
        )
        stage_archive(ctx, content, info, "/", None)
        assert not ctx.pending
        assert [entry.path for entry in ctx.archives] == ["/app", "/empty"]
        members = []
        for data in uploads:
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                members.append([(entry.name, entry.mode) for entry in archive])
        assert members == [[(".", 0o750), ("file", 0o640)], [(".", 0o700)]]
        materialize(ctx)
        assert [call.args[2] for call in client.apply_instance_file.call_args_list] == [
            "/app",
            "/empty",
        ]


def test_root_split_defers_symlinks_and_cross_directory_hardlinks(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        capture_uploads(client)
        content, info = archive_content(
            [
                archive_member(".", tarfile.DIRTYPE),
                archive_member("a", tarfile.DIRTYPE),
                archive_member("a/file"),
                archive_member("b", tarfile.DIRTYPE),
                archive_member("b/copy", tarfile.LNKTYPE, "a/file"),
                archive_member("shortcut", tarfile.SYMTYPE, "a/file"),
            ]
        )
        stage_archive(ctx, content, info, "/", None)
        assert [entry.path for entry in ctx.archives[:2]] == ["/a", "/b"]
        links = ctx.archives[2:]
        assert all(isinstance(entry, StagedLink) for entry in links)
        assert [(entry.path, entry.target, entry.symbolic) for entry in links] == [
            ("/b/copy", "/a/file", False),
            ("/shortcut", "a/file", True),
        ]


def test_root_split_resolves_chained_cross_directory_hardlinks(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        capture_uploads(client)
        content, info = archive_content(
            [
                archive_member(".", tarfile.DIRTYPE),
                archive_member("a", tarfile.DIRTYPE),
                archive_member("a/file"),
                archive_member("b", tarfile.DIRTYPE),
                archive_member("b/first", tarfile.LNKTYPE, "a/file"),
                archive_member("b/second", tarfile.LNKTYPE, "b/first"),
            ]
        )
        stage_archive(ctx, content, info, "/", None)
        links = [entry for entry in ctx.archives if isinstance(entry, StagedLink)]
        assert [entry.path for entry in links] == ["/b/first", "/b/second"]
        assert links[0].target == "/a/file"
        assert links[1].target in {"/a/file", "/b/first"}


def test_root_split_creates_implicit_link_parents(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        uploads = capture_uploads(client)
        content, info = archive_content(
            [
                archive_member("file"),
                archive_member("sub/deep/link", tarfile.LNKTYPE, "file"),
            ]
        )
        stage_archive(ctx, content, info, "/", None)
        assert ctx.pending[0].instance_path == "/file"
        assert [entry.path for entry in ctx.archives] == ["/sub", "/sub/deep/link"]
        with tarfile.open(fileobj=io.BytesIO(uploads[1])) as archive:
            assert [(member.name, member.isdir()) for member in archive] == [
                (".", True),
                ("deep", True),
            ]


def test_root_cache_distinguishes_file_metadata_and_link_targets(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        capture_uploads(client)
        content, info = archive_content(
            [
                archive_member("program", mode=0o4751, uid=12, gid=13),
                archive_member("alias", tarfile.SYMTYPE, "program"),
            ]
        )
        stage_archive(ctx, content, info, "/", None)
        original = ctx.chain("copy")
        ctx.pending[0].mode = "0751"
        assert ctx.chain("copy") != original
        ctx.pending[0].mode = "4751"
        assert ctx.chain("copy") == original
        ctx.pending[0].uid = 99
        assert ctx.chain("copy") != original
        ctx.pending[0].uid = 12
        assert ctx.chain("copy") == original
        ctx.archives[0] = replace(ctx.archives[0], target="different")
        assert ctx.chain("copy") != original


def test_root_links_use_literal_targets_and_restore_special_mode(tmp_path):
    with build_context(tmp_path) as (ctx, client, closed):
        ctx.archives.extend(
            [
                StagedLink("/copy", "/original", False, 12, 13, 0o4751),
                StagedLink("/shortcut", "-relative-target", True, 0, 0, 0o777),
            ]
        )
        client.operation_subprocess_create.side_effect = [2, 3, 4, 5]
        client.follow_operation_events.side_effect = [
            event_stream([event("lifecycle", {"cwd": "/"})], closed),
            *[
                event_stream([event("exit", {"code": 0}, spid)], closed)
                for spid in range(2, 6)
            ],
        ]
        materialize(ctx)
        calls = client.operation_subprocess_create.call_args_list
        assert [(call.args[1], call.kwargs["args"]) for call in calls] == [
            ("ln", ["-fT", "--", "/original", "/copy"]),
            ("chown", ["-h", "12:13", "/copy"]),
            ("chmod", ["4751", "/copy"]),
            ("ln", ["-sfnT", "--", "-relative-target", "/shortcut"]),
        ]
        assert all(call.kwargs["shell"] is False for call in calls)
        assert closed == [True] * 5
        client.apply_instance_file.assert_not_called()


def test_root_link_cache_distinguishes_special_mode():
    link = StagedLink("/copy", "/original", False, 12, 13, 0o4751)
    assert link.cache_repr() != replace(link, mode=0o751).cache_repr()


@pytest.mark.parametrize("source_kind", ["local", "image"])
def test_root_multiple_sources_preserve_symlink_then_file_order(
    tmp_path, monkeypatch, source_kind
):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        uploads = capture_uploads(client)
        client.inspect_image_list.return_value = SimpleNamespace(
            files=[SimpleNamespace(path="a", is_regular=False)]
        )
        client.follow_operation_events.side_effect = lambda *args, **kwargs: iter(
            [event("lifecycle", {"cwd": "/"})]
        )
        commands = Mock()
        client.attach_mock(commands, "metadata")
        monkeypatch.setattr(
            "contree_cli.docker.transfer.run_metadata_command", commands
        )
        if source_kind == "local":
            first = tmp_path / "dir1"
            second = tmp_path / "dir2"
            first.mkdir()
            second.mkdir()
            (first / "a").symlink_to("original")
            (second / "a").write_bytes(b"data")
            (second / "a").chmod(0o666)
            stage_copy(ctx, ("dir1", "dir2"), "/", "", "")
        else:
            ctx.stages["source"] = BASE
            first, info = archive_content(
                [
                    archive_member("dir1", tarfile.DIRTYPE),
                    archive_member("dir1/a", tarfile.SYMTYPE, "original"),
                ]
            )
            second, info = archive_content(
                [
                    archive_member("dir2", tarfile.DIRTYPE),
                    archive_member("dir2/a"),
                ]
            )
            assert info.is_directory
            client.inspect_image_archive.side_effect = [
                iter([first.getvalue()]),
                iter([second.getvalue()]),
            ]
            copy_from_image(ctx, "source", ("/dir1", "/dir2"), "/", "", "")

        assert client.spawn_instance.call_count == 2
        assert all(
            call.kwargs["files"] is Ellipsis
            for call in client.spawn_instance.call_args_list
        )
        assert uploads == [b"data"]
        client.apply_instance_file.assert_called_once_with(
            "operation-1", "file-1", "/a", unpack=False, replace=True
        )
        expected_commands = [("ln", ["-sfnT", "--", "original", "/a"])]
        if source_kind == "local":
            expected_commands.append(("chmod", ["0666", "/a"]))
        assert [(call.args[2], call.args[3]) for call in commands.call_args_list] == (
            expected_commands
        )
        operations = {
            "spawn_instance",
            "metadata",
            "apply_instance_file",
            "operation_subprocess_kill",
        }
        assert [call[0] for call in client.mock_calls if call[0] in operations] == [
            "spawn_instance",
            "metadata",
            "operation_subprocess_kill",
            "spawn_instance",
            "apply_instance_file",
            *(["metadata"] if source_kind == "local" else []),
            "operation_subprocess_kill",
        ]


@pytest.mark.parametrize("files, allowed", [(9_999, True), (10_000, False)])
def test_archive_entry_limit_includes_root_header(tmp_path, files, allowed):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        capture_uploads(client)
        content, info = archive_content(
            [archive_member(".", tarfile.DIRTYPE)]
            + [archive_member(f"file-{index}") for index in range(files)]
        )
        if allowed:
            stage_archive(ctx, content, info, "/destination", None)
            client.ensure_file.assert_called_once()
            assert len(ctx.archives) == 1
        else:
            with pytest.raises(ValueError, match=r"10001 entries.*10000"):
                stage_archive(ctx, content, info, "/destination", None)
            client.ensure_file.assert_not_called()
            assert not ctx.archives


def test_existing_root_symlink_uses_raw_replacement_without_spawn_attachment(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        client.inspect_image_list.return_value = SimpleNamespace(
            files=[SimpleNamespace(path="a", is_regular=False)]
        )
        uploads = capture_uploads(client)
        content, info = archive_content(
            [archive_member(".", tarfile.DIRTYPE), archive_member("a")]
        )
        stage_archive(ctx, content, info, "/", None)
        materialize(ctx)
        assert client.spawn_instance.call_args.kwargs["files"] is Ellipsis
        assert uploads == [b"data"]
        client.apply_instance_file.assert_called_once_with(
            "operation-1", "file-1", "/a", unpack=False, replace=True
        )
        client.operation_subprocess_create.assert_not_called()


def test_root_replacement_restores_root_owner_and_source_mode(tmp_path, monkeypatch):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        client.inspect_image_list.return_value = SimpleNamespace(
            files=[
                SimpleNamespace(
                    path="program", uid=1000, gid=1000, mode=0o4755, is_regular=True
                )
            ]
        )
        ctx.pending.append(
            PendingFile("/program", "file", "sha", 0, 0, "0640", replace_existing=True)
        )
        commands = Mock()
        monkeypatch.setattr(
            "contree_cli.docker.transfer.run_metadata_command", commands
        )
        materialize(ctx)
        assert [(call.args[2], call.args[3]) for call in commands.call_args_list] == [
            ("chown", ["-h", "0:0", "/program"]),
            ("chmod", ["0640", "/program"]),
        ]


def test_root_split_applies_archive_limit_per_child(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        uploads = capture_uploads(client)
        members = [archive_member(".", tarfile.DIRTYPE)]
        for child in ("a", "b"):
            members.append(archive_member(child, tarfile.DIRTYPE))
            members.extend(archive_member(f"{child}/{index}") for index in range(5_000))
        content, info = archive_content(members)
        assert len(info.entries) == 10_003
        stage_archive(ctx, content, info, "/", None)
        assert len(uploads) == 2
        assert [archive.path for archive in ctx.archives] == ["/a", "/b"]
        for uploaded in uploads:
            with tarfile.open(fileobj=io.BytesIO(uploaded)) as archive:
                assert len(archive.getmembers()) == 5_001
                assert archive.getmembers()[0].name == "."


def test_new_root_files_use_one_spawn_without_metadata_processes(tmp_path):
    with build_context(tmp_path) as state:
        ctx, client = state[:2]
        capture_uploads(client)
        content, info = archive_content(
            [archive_member(".", tarfile.DIRTYPE)]
            + [archive_member(f"file-{index}", mode=0o755) for index in range(200)]
        )
        stage_archive(ctx, content, info, "/", (42, 42))
        materialize(ctx)
        files = client.spawn_instance.call_args.kwargs["files"]
        assert len(files) == 200
        assert all(
            (spec.uid, spec.gid, spec.mode) == (42, 42, "0755")
            for spec in files.values()
        )
        client.spawn_instance.assert_called_once()
        client.inspect_image_list.assert_called_once_with(BASE, "/")
        client.apply_instance_file.assert_not_called()
        client.operation_subprocess_create.assert_not_called()
