from __future__ import annotations

import io
import tarfile

import pytest

from contree_cli.docker.archive import (
    ArchiveBuffer,
    archive_is_directory,
    rewrite_image_archive,
)


@pytest.mark.parametrize("max_size", [1, 1024 * 1024])
def test_spooled_archive_members_can_be_rewound_for_upload(max_size):
    payload = b"archive member data"
    with ArchiveBuffer(max_size=max_size) as buffer:
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            member = tarfile.TarInfo("file")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        assert (buffer.name is not None) == (max_size == 1)
        buffer.seek(0)
        with tarfile.open(fileobj=buffer, mode="r:") as archive:
            extracted = archive.extractfile("file")
            assert extracted is not None
            with extracted:
                assert extracted.seekable()
                assert extracted.read(7) == payload[:7]
                assert extracted.seek(0) == 0
                assert extracted.read() == payload


@pytest.mark.parametrize("compression", ["w", "w:gz", "w:bz2"])
def test_empty_archive_reports_consistent_error(compression):
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode=compression):
        pass
    for action in (
        lambda: archive_is_directory(source),
        lambda: rewrite_image_archive(source, io.BytesIO()),
    ):
        with pytest.raises(ValueError, match="source archive is empty"):
            action()


def test_closed_archive_buffer_is_not_seekable():
    buffer = ArchiveBuffer(max_size=1024)
    buffer.close()
    with pytest.raises(ValueError):
        buffer.seekable()
