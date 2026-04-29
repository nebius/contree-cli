import os
from unittest.mock import patch

import pytest

from contree_cli.mapped_file import MappedFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_stat_result(*, st_uid=1000, st_gid=1000, st_mode=0o100644):
    """Build an os.stat_result with controllable uid/gid/mode."""
    # os.stat_result expects a 10-element tuple:
    # (mode, ino, dev, nlink, uid, gid, size, atime, mtime, ctime)
    return os.stat_result((st_mode, 0, 0, 1, st_uid, st_gid, 0, 0, 0, 0))


FAKE_STAT = _fake_stat_result(st_uid=1000, st_gid=2000, st_mode=0o100755)


# ---------------------------------------------------------------------------
# Host-path only (all defaults from stat)
# ---------------------------------------------------------------------------


class TestHostOnly:
    def test_defaults_from_stat(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/some/file")

        assert mf.host_path == "/some/file"
        assert mf.instance_path == "/some/file"
        assert mf.uid == 1000
        assert mf.gid == 2000
        assert mf.mode == 0o755


# ---------------------------------------------------------------------------
# Host + instance_path
# ---------------------------------------------------------------------------


class TestInstancePath:
    def test_instance_path_overridden(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/host/path:/sandbox/path")

        assert mf.host_path == "/host/path"
        assert mf.instance_path == "/sandbox/path"
        assert mf.uid == 1000
        assert mf.gid == 2000
        assert mf.mode == 0o755


# ---------------------------------------------------------------------------
# Tagged options
# ---------------------------------------------------------------------------


class TestTaggedOptions:
    def test_uid_only(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/a:u42")

        assert mf.host_path == "/a"
        assert mf.instance_path == "/a"
        assert mf.uid == 42
        assert mf.gid == 2000
        assert mf.mode == 0o755

    def test_gid_only(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/a:g99")

        assert mf.gid == 99
        assert mf.uid == 1000

    def test_mode_only(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/a:m0644")

        assert mf.mode == 0o644
        assert mf.uid == 1000
        assert mf.gid == 2000

    def test_uid_and_gid(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/a:u1000:g1000")

        assert mf.uid == 1000
        assert mf.gid == 1000
        assert mf.mode == 0o755

    def test_instance_path_with_mode(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf = MappedFile.parse("/a:/b:m0755")

        assert mf.instance_path == "/b"
        assert mf.mode == 0o755
        assert mf.uid == 1000
        assert mf.gid == 2000

    def test_tags_in_any_order(self):
        with patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT):
            mf1 = MappedFile.parse("/a:/b:u0:g0:m0644")
            mf2 = MappedFile.parse("/a:/b:m0644:g0:u0")
            mf3 = MappedFile.parse("/a:/b:g0:m0644:u0")

        assert mf1 == mf2 == mf3


# ---------------------------------------------------------------------------
# All explicit — no stat call
# ---------------------------------------------------------------------------


class TestAllExplicit:
    def test_all_fields(self):
        mf = MappedFile.parse("/host:/inst:u0:g0:m644")

        assert mf.host_path == "/host"
        assert mf.instance_path == "/inst"
        assert mf.uid == 0
        assert mf.gid == 0
        assert mf.mode == 0o644

    def test_no_stat_called(self):
        with patch("contree_cli.mapped_file.os.stat") as mock_stat:
            MappedFile.parse("/nonexistent:/x:u0:g0:m755")

        mock_stat.assert_not_called()

    def test_mode_octal_parsing(self):
        mf = MappedFile.parse("/a:/b:u0:g0:m700")
        assert mf.mode == 0o700

        mf2 = MappedFile.parse("/a:/b:u0:g0:m777")
        assert mf2.mode == 0o777

        mf3 = MappedFile.parse("/a:/b:u0:g0:m600")
        assert mf3.mode == 0o600


# ---------------------------------------------------------------------------
# Named uid/gid resolution
# ---------------------------------------------------------------------------


class TestNameResolution:
    def test_named_uid(self):
        pw_entry = type("pw", (), {"pw_uid": 501})()
        with (
            patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT),
            patch("contree_cli.mapped_file.pwd.getpwnam", return_value=pw_entry),
        ):
            mf = MappedFile.parse("/a:ualice")

        assert mf.uid == 501

    def test_unknown_uid_name_falls_back_to_root(self):
        with (
            patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT),
            patch(
                "contree_cli.mapped_file.pwd.getpwnam",
                side_effect=KeyError("no such user"),
            ),
        ):
            mf = MappedFile.parse("/a:unosuchuser")

        assert mf.uid == 0

    def test_named_gid(self):
        gr_entry = type("gr", (), {"gr_gid": 50})()
        with (
            patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT),
            patch("contree_cli.mapped_file.grp.getgrnam", return_value=gr_entry),
        ):
            mf = MappedFile.parse("/a:gstaff")

        assert mf.gid == 50

    def test_unknown_gid_name_falls_back_to_root(self):
        with (
            patch("contree_cli.mapped_file.os.stat", return_value=FAKE_STAT),
            patch(
                "contree_cli.mapped_file.grp.getgrnam",
                side_effect=KeyError("no such group"),
            ),
        ):
            mf = MappedFile.parse("/a:gnosuchgroup")

        assert mf.gid == 0


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrors:
    def test_empty_string(self):
        with pytest.raises(ValueError, match="host_path is required"):
            MappedFile.parse("")

    def test_host_file_missing_when_stat_needed(self):
        with pytest.raises(ValueError, match="cannot stat"):
            MappedFile.parse("/no/such/file/exists")

    def test_unknown_field(self):
        with pytest.raises(ValueError, match="unknown field"):
            MappedFile.parse("/a:badfield")

    def test_duplicate_instance_path(self):
        with pytest.raises(ValueError, match="duplicate instance path"):
            MappedFile.parse("/a:/b:/c")

    def test_invalid_mode_falls_back_to_zero(self):
        mf = MappedFile.parse("/a:/b:u0:g0:mxyz")
        assert mf.mode == 0

    def test_non_octal_mode_falls_back_to_zero(self):
        # "999" is not valid octal
        mf = MappedFile.parse("/a:/b:u0:g0:m999")
        assert mf.mode == 0


# ---------------------------------------------------------------------------
# Dataclass properties
# ---------------------------------------------------------------------------


class TestDataclass:
    def test_frozen(self):
        mf = MappedFile.parse("/a:/b:u0:g0:m644")
        with pytest.raises(AttributeError):
            mf.host_path = "/other"

    def test_equality(self):
        a = MappedFile.parse("/a:/b:u0:g0:m644")
        b = MappedFile.parse("/a:/b:u0:g0:m644")
        assert a == b

    def test_inequality(self):
        a = MappedFile.parse("/a:/b:u0:g0:m644")
        b = MappedFile.parse("/a:/b:u0:g0:m755")
        assert a != b
