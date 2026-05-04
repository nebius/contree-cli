"""Tests for the transitional ``contree_cli.migrations`` module.

DELETE-ME together with ``contree_cli/migrations.py`` (target: 0.7.0).
"""

from __future__ import annotations

from contree_cli.migrations import run_migrations


class TestRunMigrations:
    def test_renames_legacy_dir_and_reorganises(self, tmp_path):
        legacy = tmp_path / "contree-cli"
        legacy.mkdir()
        (legacy / "config.ini").write_text("[DEFAULT]\nprofile = default\n")
        (legacy / "sessions-default.db").write_bytes(b"sqlite-bytes")
        (legacy / "sessions-default.db-wal").write_bytes(b"wal")
        (legacy / "skills.db").write_bytes(b"skills-bytes")

        home = tmp_path / "contree"
        run_migrations(home)

        assert not legacy.exists()
        assert (home / "auth.ini").exists()
        assert not (home / "config.ini").exists()
        assert (
            home / "cli" / "sessions" / "default.db"
        ).read_bytes() == b"sqlite-bytes"
        assert (home / "cli" / "sessions" / "default.db-wal").exists()
        assert (home / "cli" / "skills.db").read_bytes() == b"skills-bytes"
        assert not (home / "sessions-default.db").exists()
        assert not (home / "skills.db").exists()

    def test_no_op_when_legacy_missing(self, tmp_path):
        home = tmp_path / "contree"
        run_migrations(home)
        assert not home.exists()

    def test_no_op_when_new_auth_already_exists(self, tmp_path):
        legacy = tmp_path / "contree-cli"
        legacy.mkdir()
        (legacy / "config.ini").write_text("legacy")

        home = tmp_path / "contree"
        home.mkdir()
        (home / "auth.ini").write_text("already-there")

        run_migrations(home)

        assert legacy.exists()
        assert (home / "auth.ini").read_text() == "already-there"

    def test_migrates_credentials_with_pre_existing_flat_session(self, tmp_path):
        """A pre-existing sessions-*.db in home (e.g. auto-created by an
        earlier broken release) must NOT block credential migration."""
        legacy = tmp_path / "contree-cli"
        legacy.mkdir()
        (legacy / "config.ini").write_text("[DEFAULT]\nprofile = default\n")

        home = tmp_path / "contree"
        home.mkdir()
        (home / "sessions-default.db").write_bytes(b"fresh-empty-db")

        run_migrations(home)

        assert (home / "auth.ini").exists()
        assert (
            home / "cli" / "sessions" / "default.db"
        ).read_bytes() == b"fresh-empty-db"
        assert not (home / "sessions-default.db").exists()

    def test_flat_to_nested_runs_without_legacy(self, tmp_path):
        """No legacy dir, but flat layout in home → reorganise only."""
        home = tmp_path / "contree"
        home.mkdir()
        (home / "auth.ini").write_text("[DEFAULT]\nprofile = default\n")
        (home / "sessions-default.db").write_bytes(b"db")
        (home / "skills.db").write_bytes(b"skills")

        run_migrations(home)

        assert (home / "cli" / "sessions" / "default.db").exists()
        assert (home / "cli" / "skills.db").exists()
        assert not (home / "sessions-default.db").exists()
        assert not (home / "skills.db").exists()

    def test_skips_target_collision(self, tmp_path):
        """If destination already has a file with the same name, keep the
        source untouched rather than clobbering."""
        home = tmp_path / "contree"
        home.mkdir()
        (home / "cli").mkdir()
        (home / "cli" / "skills.db").write_bytes(b"new-skills")
        (home / "skills.db").write_bytes(b"old-skills")

        run_migrations(home)

        assert (home / "skills.db").read_bytes() == b"old-skills"
        assert (home / "cli" / "skills.db").read_bytes() == b"new-skills"
