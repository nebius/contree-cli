from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from conftest import make_file_item
from contree_client.exceptions import NotFoundError
from contree_client.models import DirectoryList

from contree_cli.docker.archive import ArchiveEntry, ArchiveInfo
from contree_cli.docker.metadata import (
    METADATA_BATCH_BYTES,
    METADATA_BATCH_PATHS,
    metadata_batches,
    restore_metadata,
)
from contree_cli.docker.transfer import StagedArchive


def metadata_context(listings: dict[str, list[dict[str, object]]] | None = None):
    client = Mock()
    listings = listings or {}
    client.inspect_image_list.side_effect = lambda image, parent: (
        DirectoryList.from_dict({"path": parent, "files": listings.get(parent, [])})
    )
    return SimpleNamespace(client=client, last_image="base-image")


def metadata_archive(entries, owner=None, preserve_root=True, path="/dst"):
    return StagedArchive(
        path=path,
        file_uuid="archive-uuid",
        sha256="archive-sha",
        info=ArchiveInfo(True, tuple(entries)),
        owner=owner,
        preserve_root=preserve_root,
    )


@pytest.mark.parametrize("owner", [None, (0, 0), (2000, 2000)])
def test_existing_destination_root_keeps_owner_and_mode(owner):
    ctx = metadata_context(
        {
            "/": [make_file_item("dst", is_dir=True, uid=1000, gid=1000, mode=0o700)],
            "/dst": [
                make_file_item("sub", is_dir=True, uid=1000, gid=1000, mode=0o700)
            ],
        }
    )
    archive = metadata_archive(
        [
            ArchiveEntry(".", 0, 0, 0o755, False, True),
            ArchiveEntry("sub", 0, 0, 0o750, False, True),
        ],
        owner,
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    uid, gid = owner or (0, 0)
    assert [call.args[2:] for call in command.call_args_list] == [
        ("chown", ["-h", f"{uid}:{gid}", "/dst/sub"]),
        ("chmod", ["700", "/dst"]),
    ]


@pytest.mark.parametrize(
    ("existing", "owner"),
    [(True, (0, 0)), (False, (1234, 1235))],
    ids=["existing-root-owner", "new-source-owner"],
)
def test_split_root_child_restores_source_metadata(existing, owner):
    ctx = metadata_context(
        {"/": [make_file_item("dst", is_dir=True, uid=1000, gid=1000, mode=0o700)]}
        if existing
        else {}
    )
    uid, gid = owner
    archive = metadata_archive(
        [ArchiveEntry(".", uid, gid, 0o755, False, True)], preserve_root=False
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_called_once_with(
        ctx, "operation", "chown", ["-h", f"{uid}:{gid}", "/dst"]
    )


@pytest.mark.parametrize("owner", [None, (0, 0), (2000, 2001)])
def test_new_destination_root_defaults_to_root_while_children_keep_source_owner(owner):
    ctx = metadata_context()
    archive = metadata_archive(
        [
            ArchiveEntry(".", 1234, 1235, 0o755, False, True),
            ArchiveEntry("sub", 1234, 1235, 0o755, False, True),
        ],
        owner=owner,
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    expected = []
    if owner is None:
        expected = [("chown", ["-h", "1234:1235", "/dst/sub"])]
    elif owner != (0, 0):
        expected = [("chown", ["-h", "2000:2001", "/dst/sub", "/dst"])]
    assert [call.args[2:] for call in command.call_args_list] == expected


def test_new_root_owned_files_and_default_directories_need_no_commands():
    ctx = metadata_context()
    archive = metadata_archive(
        [
            ArchiveEntry(".", 0, 0, 0o755, False, True),
            ArchiveEntry("sub", 0, 0, 0o755, False, True),
            ArchiveEntry("file", 0, 0, 0o640, False),
            ArchiveEntry("link", 0, 0, 0o777, True),
        ]
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_not_called()
    ctx.client.inspect_image_list.assert_called_once_with("base-image", "/")


def test_new_directory_keeps_mode_applied_by_archive_when_parent_missing():
    ctx = metadata_context()
    ctx.client.inspect_image_list.side_effect = NotFoundError(404, "missing parent")
    archive = metadata_archive([ArchiveEntry("sub", 0, 0, 0o710, False, True)])

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_not_called()


def test_directory_special_modes_are_restored_after_owner_changes():
    ctx = metadata_context(
        {
            "/": [make_file_item("dst", is_dir=True, uid=1000, gid=1000, mode=0o2700)],
            "/dst": [
                make_file_item("sub", is_dir=True, uid=1000, gid=1000, mode=0o755)
            ],
        }
    )
    archive = metadata_archive(
        [
            ArchiveEntry(".", 0, 0, 0o755, False, True),
            ArchiveEntry("sub", 0, 0, 0o2755, False, True),
        ]
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    assert [call.args[2:] for call in command.call_args_list] == [
        ("chown", ["-h", "0:0", "/dst/sub"]),
        ("chmod", ["2755", "/dst/sub"]),
        ("chmod", ["2700", "/dst"]),
    ]


def test_directory_listings_are_cached_per_parent():
    ctx = metadata_context()
    archive = metadata_archive(
        [ArchiveEntry(f"sub{index}", 0, 0, 0o755, False, True) for index in range(500)]
    )

    restore_metadata(ctx, "operation", archive)

    ctx.client.inspect_image_list.assert_called_once_with("base-image", "/")


@pytest.mark.parametrize("owner", [None, (0, 0)])
def test_new_directories_restore_root_group_under_setgid_destination(owner):
    ctx = metadata_context(
        {"/": [make_file_item("dst", is_dir=True, gid=1234, mode=0o2755)]}
    )
    archive = metadata_archive(
        [
            ArchiveEntry(".", 0, 0, 0o755, False, True),
            ArchiveEntry("sub", 0, 0, 0o755, False, True),
            ArchiveEntry("sub/nested", 0, 0, 0o755, False, True),
            ArchiveEntry("sub/file", 0, 0, 0o644, False),
        ],
        owner=owner,
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    assert [call.args[2:] for call in command.call_args_list] == [
        ("chown", ["-h", "0:0", "/dst/sub/nested", "/dst/sub"]),
        ("chmod", ["2755", "/dst"]),
    ]
    assert [call.args[1] for call in ctx.client.inspect_image_list.call_args_list] == [
        "/",
        "/dst",
    ]


def test_existing_child_without_setgid_stops_group_inheritance():
    ctx = metadata_context(
        {
            "/": [make_file_item("dst", is_dir=True, gid=1234, mode=0o2755)],
            "/dst": [make_file_item("sub", is_dir=True, gid=5678, mode=0o755)],
        }
    )
    archive = metadata_archive([ArchiveEntry("sub/nested", 0, 0, 0o755, False, True)])

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_not_called()


def test_new_destination_and_staged_files_inherit_setgid_parent():
    ctx = metadata_context(
        {"/": [make_file_item("parent", is_dir=True, gid=1234, mode=0o2755)]}
    )
    archive = metadata_archive(
        [
            ArchiveEntry(".", 0, 0, 0o755, False, True),
            ArchiveEntry("sub", 0, 0, 0o755, False, True),
            ArchiveEntry("sub/file", 0, 0, 0o644, False),
            ArchiveEntry("link", 0, 0, 0o777, True),
        ],
        path="/parent/new/dst",
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    assert [call.args[2:] for call in command.call_args_list] == [
        (
            "chown",
            [
                "-h",
                "0:0",
                "/parent/new/dst/link",
                "/parent/new/dst/sub/file",
                "/parent/new/dst/sub",
                "/parent/new/dst",
            ],
        )
    ]
    assert [call.args[1] for call in ctx.client.inspect_image_list.call_args_list] == [
        "/",
        "/parent",
    ]


def test_nearest_existing_setgid_directory_controls_new_descendant_group():
    ctx = metadata_context(
        {
            "/": [make_file_item("dst", is_dir=True, gid=1234, mode=0o2755)],
            "/dst": [make_file_item("sub", is_dir=True, gid=5678, mode=0o2755)],
        }
    )
    archive = metadata_archive(
        [
            ArchiveEntry("sub", 0, 0, 0o755, False, True),
            ArchiveEntry("sub/nested", 0, 0, 0o755, False, True),
        ],
        owner=(0, 5678),
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_not_called()


def test_absent_destination_prunes_nested_directory_lookups():
    ctx = metadata_context()
    archive = metadata_archive(
        [ArchiveEntry(".", 0, 0, 0o755, False, True)]
        + [
            entry
            for index in range(200)
            for entry in (
                ArchiveEntry(f"branch{index}", 0, 0, 0o755, False, True),
                ArchiveEntry(f"branch{index}/sub", 0, 0, 0o755, False, True),
            )
        ]
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    command.assert_not_called()
    ctx.client.inspect_image_list.assert_called_once_with("base-image", "/")


def test_ownership_batches_finish_before_special_modes_are_restored():
    ctx = metadata_context()
    count = 1000
    archive = metadata_archive(
        [ArchiveEntry(f"file{index}", 0, 0, 0o4755, False) for index in range(count)],
        owner=(1000, 1001),
    )

    with patch("contree_cli.docker.transfer.run_metadata_command") as command:
        restore_metadata(ctx, "operation", archive)

    calls = [call.args[2:] for call in command.call_args_list]
    chowns = [args for name, args in calls if name == "chown"]
    chmods = [args for name, args in calls if name == "chmod"]
    assert len(chowns) == len(chmods) == 8
    assert all(name == "chown" for name, args in calls[:8])
    assert all(name == "chmod" for name, args in calls[8:])
    assert all(args[:2] == ["-h", "1000:1001"] for args in chowns)
    assert all(len(args[2:]) <= METADATA_BATCH_PATHS for args in chowns)
    assert sum(len(args[2:]) for args in chowns) == count
    assert sum(len(args[1:]) for args in chmods) == count


def test_metadata_batches_bound_encoded_argument_bytes():
    paths = [f"/dst/{index}" + "é" * 1000 for index in range(40)]

    batches = list(metadata_batches(paths))

    assert [path for batch in batches for path in batch] == paths
    assert all(
        sum(len(path.encode("utf-8")) + 1 for path in batch) <= METADATA_BATCH_BYTES
        for batch in batches
    )


def test_metadata_rejects_paths_outside_destination():
    ctx = metadata_context()
    archive = metadata_archive([ArchiveEntry("../outside", 1000, 1000, 0o644, False)])

    with pytest.raises(ValueError, match="escapes destination"):
        restore_metadata(ctx, "operation", archive)
