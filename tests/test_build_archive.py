from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path

import pytest

from contree_cli.docker.archive import (
    archive_is_directory,
    rewrite_image_archive,
    write_local_archive,
)
from contree_cli.docker.local_context import LocalContext


def members(buffer):
    with tarfile.open(fileobj=buffer, mode="r:") as archive:
        return {member.name: member for member in archive}


def input_archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o751
            member.uid = 123
            member.gid = 456
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                member.linkname = value
                archive.addfile(member)
    buffer.seek(0)
    return buffer


def test_local_directory_keeps_empty_dirs_and_symlinks(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "empty").mkdir()
    (source / "file").write_bytes(b"hello")
    (source / "dangling").symlink_to("missing")
    (source / "linked-dir").symlink_to("empty", target_is_directory=True)
    output = io.BytesIO()

    info = write_local_archive(LocalContext.from_dir(tmp_path), "src", output)

    assert info is not None
    assert info.is_directory
    archived = members(output)
    assert set(archived) == {".", "empty", "file", "dangling", "linked-dir"}
    assert archived["empty"].isdir()
    assert archived["dangling"].issym()
    assert archived["dangling"].linkname == "missing"
    assert archived["linked-dir"].issym()


def test_local_ignore_and_deterministic_headers(tmp_path):
    (tmp_path / ".dockerignore").write_text("src/skip\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "keep").write_bytes(b"yes")
    (source / "skip").mkdir()
    (source / "skip" / "secret").write_bytes(b"no")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_bytes(b"no")
    local = LocalContext.from_dir(tmp_path)
    first = io.BytesIO()
    second = io.BytesIO()

    write_local_archive(local, "src", first)
    os.utime(source / "keep", (100, 100))
    write_local_archive(local, "src", second)

    assert first.getvalue() == second.getvalue()
    assert set(members(first)) == {".", "keep"}


def test_ignored_local_source_returns_none(tmp_path):
    (tmp_path / ".dockerignore").write_text("src\n")
    (tmp_path / "src").mkdir()
    output = io.BytesIO()
    assert write_local_archive(LocalContext.from_dir(tmp_path), "src", output) is None
    assert output.getvalue() == b""


@pytest.mark.parametrize("source", ["tree", "alias"])
@pytest.mark.parametrize(
    "extra_rules",
    ["", "!tree/keep.log\n", "!tree/cache*/keep.txt\n"],
)
def test_unreadable_ignored_directory_is_not_traversed(
    tmp_path, monkeypatch, source, extra_rules
):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "keep.log").write_bytes(b"included bytes")
    (tree / "drop.log").write_bytes(b"must not upload")
    blocked = tree / "volume-data"
    blocked.mkdir()
    (blocked / "secret").write_bytes(b"must not upload")
    (tmp_path / "alias").symlink_to("tree", target_is_directory=True)
    (tmp_path / ".dockerignore").write_text(
        "tree/*.log\n!tree/keep.log\ntree/volume-data/\n" + extra_rules
    )
    original_iterdir = Path.iterdir

    def checked_iterdir(path):
        if path.resolve() == blocked:
            raise PermissionError("volume-data cannot be read")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", checked_iterdir)
    output = io.BytesIO()

    assert write_local_archive(LocalContext.from_dir(tmp_path), source, output)

    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == [".", "keep.log"]
        assert archive.extractfile("keep.log").read() == b"included bytes"
    assert b"must not upload" not in output.getvalue()


@pytest.mark.parametrize(
    "exception",
    ["tree/volume-data/", "tree/volume-data/keep.txt", "tree/*-data/keep.txt"],
)
def test_reincluded_unreadable_directory_reports_permission_error(
    tmp_path, monkeypatch, exception
):
    tree = tmp_path / "tree"
    tree.mkdir()
    blocked = tree / "volume-data"
    blocked.mkdir()
    (tmp_path / ".dockerignore").write_text(
        "tree/*.log\n!tree/keep.log\ntree/volume-data/\n" + f"!{exception}\n"
    )
    original_iterdir = Path.iterdir

    def checked_iterdir(path):
        if path == blocked:
            raise PermissionError("volume-data cannot be read")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", checked_iterdir)

    with pytest.raises(PermissionError, match="volume-data cannot be read"):
        write_local_archive(LocalContext.from_dir(tmp_path), "tree", io.BytesIO())


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("assets/keep.txt", ["keep.txt"]),
        ("assets", [".", "keep.txt"]),
        (".", [".", "assets", "assets/keep.txt"]),
    ],
)
@pytest.mark.parametrize("parent_pattern", ["assets", "assets/"])
def test_negation_reincludes_file_below_ignored_parent(
    tmp_path, source, expected, parent_pattern
):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "keep.txt").write_bytes(b"included bytes")
    (assets / "drop.txt").write_bytes(b"must not upload")
    (assets / "unused").mkdir()
    (assets / "unused" / "secret").write_bytes(b"must not upload")
    (tmp_path / ".dockerignore").write_text(f"{parent_pattern}\n!assets/keep.txt\n")
    output = io.BytesIO()

    info = write_local_archive(LocalContext.from_dir(tmp_path), source, output)

    assert info is not None
    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == expected
        assert archive.extractfile(expected[-1]).read() == b"included bytes"
    assert b"must not upload" not in output.getvalue()


@pytest.mark.parametrize("source", ["assets/keep.txt", "assets"])
def test_later_parent_exclusion_prevents_file_reinclusion(tmp_path, source):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "keep.txt").write_bytes(b"must not upload")
    (tmp_path / ".dockerignore").write_text("!assets/keep.txt\nassets/\n")
    output = io.BytesIO()

    assert write_local_archive(LocalContext.from_dir(tmp_path), source, output) is None
    assert output.getvalue() == b""


@pytest.mark.parametrize("source", ["alias/keep.txt", "alias"])
def test_negation_checks_both_requested_and_resolved_paths(tmp_path, source):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "keep.txt").write_bytes(b"included bytes")
    (assets / "drop.txt").write_bytes(b"must not upload")
    (tmp_path / "alias").symlink_to("assets", target_is_directory=True)
    (tmp_path / ".dockerignore").write_text(
        "assets/\n!assets/keep.txt\nalias/\n!alias/keep.txt\n"
    )
    output = io.BytesIO()

    assert write_local_archive(LocalContext.from_dir(tmp_path), source, output)
    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.extractfile("keep.txt").read() == b"included bytes"
        assert "drop.txt" not in archive.getnames()
    assert b"must not upload" not in output.getvalue()


@pytest.mark.parametrize("exception", ["assets/keep.txt", "alias/keep.txt"])
def test_negation_cannot_bypass_other_path_exclusion(tmp_path, exception):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "keep.txt").write_bytes(b"must not upload")
    (tmp_path / "alias").symlink_to("assets", target_is_directory=True)
    (tmp_path / ".dockerignore").write_text(f"assets/\nalias/\n!{exception}\n")
    output = io.BytesIO()

    assert (
        write_local_archive(LocalContext.from_dir(tmp_path), "alias/keep.txt", output)
        is None
    )
    assert output.getvalue() == b""


def test_negation_does_not_override_default_exclusions(tmp_path):
    (tmp_path / "keep.txt").write_bytes(b"included bytes")
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "secret").write_bytes(b"must not upload")
    (tmp_path / ".dockerignore").write_text("!**\n")
    output = io.BytesIO()

    write_local_archive(LocalContext.from_dir(tmp_path), ".", output)

    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == [".", "keep.txt"]
        assert archive.extractfile("keep.txt").read() == b"included bytes"
    assert b"must not upload" not in output.getvalue()
    assert (
        write_local_archive(
            LocalContext.from_dir(tmp_path), ".hidden/secret", io.BytesIO()
        )
        is None
    )


def test_local_file_and_chmod_metadata(tmp_path):
    (tmp_path / "file").write_bytes(b"data")
    output = io.BytesIO()
    info = write_local_archive(
        LocalContext.from_dir(tmp_path), "file", output, mode_override=0o4755
    )
    assert info is not None
    assert not info.is_directory
    assert info.entries[0].mode == 0o4755
    assert info.entries[0].uid == info.entries[0].gid == 0
    assert members(output)["file"].mode == 0o755


def test_local_hardlinks_refer_to_preceding_file(tmp_path):
    (tmp_path / "one").write_bytes(b"same")
    os.link(tmp_path / "one", tmp_path / "two")
    output = io.BytesIO()
    write_local_archive(LocalContext.from_dir(tmp_path), ".", output)
    archived = members(output)
    assert archived["one"].isreg()
    assert archived["two"].islnk()
    assert archived["two"].linkname == "one"


def test_leaf_symlink_does_not_read_external_target(tmp_path):
    root = tmp_path / "context"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"must not upload")
    (root / "link").symlink_to(secret)
    output = io.BytesIO()
    with pytest.raises(ValueError, match="escapes context"):
        write_local_archive(LocalContext.from_dir(root), "link", output)
    assert b"must not upload" not in output.getvalue()


def test_explicit_symlink_copies_target_bytes_under_requested_basename(tmp_path):
    (tmp_path / "target").write_bytes(b"target bytes")
    (tmp_path / "link").symlink_to("target")
    output = io.BytesIO()

    info = write_local_archive(LocalContext.from_dir(tmp_path), "link", output)

    assert info is not None
    assert not info.is_directory
    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == ["link"]
        assert archive.getmember("link").isreg()
        assert archive.extractfile("link").read() == b"target bytes"


def test_explicit_directory_symlink_copies_contents_and_preserves_nested_links(
    tmp_path,
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "file").write_bytes(b"target bytes")
    (target / "nested").symlink_to("file")
    (tmp_path / "link").symlink_to("target", target_is_directory=True)
    output = io.BytesIO()

    info = write_local_archive(LocalContext.from_dir(tmp_path), "link", output)

    assert info is not None
    assert info.is_directory
    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == [".", "file", "nested"]
        assert archive.extractfile("file").read() == b"target bytes"
        assert archive.getmember("nested").issym()
        assert archive.getmember("nested").linkname == "file"


@pytest.mark.parametrize("ignored", ["target/secret", "alias/secret", "target"])
def test_explicit_source_through_directory_symlink_obeys_ignore_rules(
    tmp_path, ignored
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret").write_bytes(b"must not upload")
    (tmp_path / "alias").symlink_to("target", target_is_directory=True)
    (tmp_path / ".dockerignore").write_text(ignored + "\n")
    output = io.BytesIO()

    info = write_local_archive(LocalContext.from_dir(tmp_path), "alias/secret", output)

    assert info is None
    assert output.getvalue() == b""


@pytest.mark.parametrize("ignored", ["target/secret", "alias/secret"])
def test_directory_symlink_walk_obeys_requested_and_resolved_ignore_rules(
    tmp_path, ignored
):
    target = tmp_path / "target"
    target.mkdir()
    (target / "secret").write_bytes(b"must not upload")
    (target / "public").write_bytes(b"public bytes")
    (tmp_path / "alias").symlink_to("target", target_is_directory=True)
    (tmp_path / ".dockerignore").write_text(ignored + "\n")
    output = io.BytesIO()

    write_local_archive(LocalContext.from_dir(tmp_path), "alias", output)

    assert set(members(output)) == {".", "public"}
    assert b"must not upload" not in output.getvalue()


def test_explicit_symlink_cannot_bypass_ignored_target(tmp_path):
    (tmp_path / "secret").write_bytes(b"must not upload")
    (tmp_path / "alias").symlink_to("secret")
    (tmp_path / ".dockerignore").write_text("secret\n")
    output = io.BytesIO()

    assert write_local_archive(LocalContext.from_dir(tmp_path), "alias", output) is None
    assert output.getvalue() == b""


def test_nested_symlink_can_reference_outside_context_without_reading_target(tmp_path):
    root = tmp_path / "context"
    root.mkdir()
    secret = tmp_path / "secret"
    secret.write_bytes(b"must not upload")
    (root / "link").symlink_to(secret)
    output = io.BytesIO()

    write_local_archive(LocalContext.from_dir(root), ".", output)

    assert members(output)["link"].issym()
    assert b"must not upload" not in output.getvalue()


@pytest.mark.parametrize("source", ["../context-other/file", "../file", "escape/file"])
def test_local_source_cannot_escape_context(tmp_path, source):
    root = tmp_path / "context"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes context"):
        write_local_archive(LocalContext.from_dir(root), source, io.BytesIO())


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="OS cannot create FIFO files")
def test_local_special_file_is_rejected(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="unsupported file type"):
        write_local_archive(LocalContext.from_dir(tmp_path), ".", io.BytesIO())


def test_image_directory_prefix_and_hardlinks_are_rewritten():
    source = input_archive(
        [
            ("etc", tarfile.DIRTYPE, ""),
            ("etc/empty", tarfile.DIRTYPE, ""),
            ("etc/hosts", tarfile.REGTYPE, b"localhost"),
            ("etc/alias", tarfile.LNKTYPE, "etc/hosts"),
            ("etc/link", tarfile.SYMTYPE, "../other"),
        ]
    )
    output = io.BytesIO()
    info = rewrite_image_archive(source, output)
    archived = members(output)
    assert info.is_directory
    assert set(archived) == {".", "empty", "hosts", "alias", "link"}
    assert archived["alias"].linkname == "hosts"
    assert archived["link"].linkname == "../other"
    assert all(entry.uid == 123 and entry.gid == 456 for entry in info.entries)
    assert all(member.uid == member.gid == 0 for member in archived.values())


def test_image_file_retains_basename_and_content():
    output = io.BytesIO()
    info = rewrite_image_archive(
        input_archive([("app", tarfile.REGTYPE, b"binary")]), output
    )
    assert not info.is_directory
    with tarfile.open(fileobj=output, mode="r:") as archive:
        assert archive.getnames() == ["app"]
        assert archive.extractfile("app").read() == b"binary"


def test_image_file_can_be_renamed():
    source = input_archive([("app", tarfile.REGTYPE, b"binary")])
    assert not archive_is_directory(source)
    assert source.tell() == 0
    output = io.BytesIO()
    info = rewrite_image_archive(source, output, name_override="other")
    assert info.entries[0].path == "other"
    assert set(members(output)) == {"other"}


def test_local_file_can_be_renamed(tmp_path):
    (tmp_path / "file").write_bytes(b"data")
    output = io.BytesIO()
    info = write_local_archive(
        LocalContext.from_dir(tmp_path), "file", output, name_override="renamed"
    )
    assert info is not None
    assert info.entries[0].path == "renamed"
    assert set(members(output)) == {"renamed"}


@pytest.mark.parametrize("name", ["", ".", "..", "../other", "/absolute"])
def test_rename_cannot_escape_archive(name):
    with pytest.raises(ValueError, match="destination filename"):
        rewrite_image_archive(
            input_archive([("app", tarfile.REGTYPE, b"binary")]),
            io.BytesIO(),
            name_override=name,
        )


def test_empty_image_archive_is_rejected():
    with pytest.raises(ValueError, match="archive is empty"):
        rewrite_image_archive(input_archive([]), io.BytesIO())


def test_image_root_archive_keeps_relative_children():
    output = io.BytesIO()
    info = rewrite_image_archive(
        input_archive(
            [(".", tarfile.DIRTYPE, ""), ("./app", tarfile.REGTYPE, b"binary")]
        ),
        output,
    )
    assert info.is_directory
    assert set(members(output)) == {".", "app"}


@pytest.mark.parametrize("path", ["/absolute", "root/../escape", "../escape"])
def test_image_archive_unsafe_path_is_rejected(path):
    with pytest.raises(ValueError, match="unsafe path"):
        rewrite_image_archive(
            input_archive([(path, tarfile.REGTYPE, b"bad")]), io.BytesIO()
        )


def test_image_archive_external_hardlink_is_rejected():
    with pytest.raises(ValueError, match="outside its root"):
        rewrite_image_archive(
            input_archive(
                [("root", tarfile.DIRTYPE, ""), ("root/link", tarfile.LNKTYPE, "other")]
            ),
            io.BytesIO(),
        )


def test_image_archive_symlink_parent_is_rejected():
    with pytest.raises(ValueError, match="traverses a symlink"):
        rewrite_image_archive(
            input_archive(
                [
                    ("root", tarfile.DIRTYPE, ""),
                    ("root/link", tarfile.SYMTYPE, "/elsewhere"),
                    ("root/link/file", tarfile.REGTYPE, b"bad"),
                ]
            ),
            io.BytesIO(),
        )


def test_image_archive_multiple_roots_are_rejected():
    with pytest.raises(ValueError, match="multiple roots"):
        rewrite_image_archive(
            input_archive(
                [("root", tarfile.DIRTYPE, ""), ("other", tarfile.REGTYPE, b"bad")]
            ),
            io.BytesIO(),
        )
