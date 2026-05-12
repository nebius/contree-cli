from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from contree_cli.config import ConfigProfile
from contree_cli.session import (
    PendingFile,
    SessionStore,
    get_session_key,
)


class TestSessionStore:
    def test_session_none_initially(self, session_store: SessionStore):
        assert session_store.session is None

    def test_current_image_raises_when_no_session(self, session_store: SessionStore):
        with pytest.raises(SystemExit):
            _ = session_store.current_image

    def test_set_image_creates_session(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        s = session_store.session
        assert s is not None
        assert s.current_image == "img-1"
        assert s.last_kind == "use"
        assert s.session_key == "test"

    def test_active_branch_defaults_to_main(self, session_store: SessionStore):
        session_store.set_image("img-1")
        s = session_store.session
        assert s is not None
        assert s.active_branch == "main"

    def test_current_image_returns_latest(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        assert session_store.current_image == "img-2"

    def test_session_updated_at_is_set(self, session_store: SessionStore):
        session_store.set_image("img-1")
        s = session_store.session
        assert s is not None
        assert s.updated_at

    def test_session_key_property(self, session_store: SessionStore):
        assert session_store.session_key == "test"


class TestHistoryChain:
    def test_first_entry_has_null_parent(self, session_store: SessionStore):
        session_store.set_image("img-1")
        conn = session_store._conn
        row = conn.execute(
            "SELECT parent_id FROM session_history WHERE session_key = ?",
            ("test",),
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_second_entry_links_to_first(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_image("img-2")
        conn = session_store._conn
        rows = conn.execute(
            "SELECT id, image_uuid, parent_id FROM session_history "
            "WHERE session_key = ? ORDER BY id",
            ("test",),
        ).fetchall()
        assert len(rows) == 2
        first_id, _, first_parent = rows[0]
        _, _, second_parent = rows[1]
        assert first_parent is None
        assert second_parent == first_id

    def test_branch_pointer_advances(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_image("img-2")
        session_store.set_image("img-3")
        conn = session_store._conn
        row = conn.execute(
            "SELECT history_id FROM session_branches "
            "WHERE session_key = ? AND branch_name = 'main'",
            ("test",),
        ).fetchone()
        assert row is not None
        img = conn.execute(
            "SELECT image_uuid FROM session_history WHERE id = ?",
            (row[0],),
        ).fetchone()
        assert img is not None
        assert img[0] == "img-3"


class TestWALMode:
    def test_wal_mode_enabled(self, tmp_path: Path):
        db = tmp_path / "wal_test.db"
        store = SessionStore(db, "k")
        try:
            mode = store._conn.execute(
                "PRAGMA journal_mode",
            ).fetchone()
            assert mode is not None
            assert mode[0] == "wal"
        finally:
            store.close()


class TestRollback:
    def test_rollback_one_step(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        entry = session_store.rollback(1)
        assert entry.image_uuid == "img-1"
        assert session_store.current_image == "img-1"

    def test_rollback_two_steps(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        session_store.set_image("img-3", kind="run")
        entry = session_store.rollback(2)
        assert entry.image_uuid == "img-1"

    def test_rollback_too_far_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="only 0 ancestors"):
            session_store.rollback(1)

    def test_rollback_no_session_raises(self, session_store: SessionStore):
        with pytest.raises(ValueError, match="No active session"):
            session_store.rollback(1)

    def test_rollback_zero_raises(self, session_store: SessionStore):
        session_store.set_image("img-1")
        with pytest.raises(ValueError, match="must be >= 1"):
            session_store.rollback(0)


class TestNavigate:
    def test_absolute_jump(self, session_store: SessionStore):
        hid1 = session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        session_store.set_image("img-3", kind="run")
        entry = session_store.navigate(hid1)
        assert entry.image_uuid == "img-1"
        assert session_store.current_image == "img-1"

    def test_absolute_jump_nonexistent_raises(
        self,
        session_store: SessionStore,
    ):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="not found"):
            session_store.navigate(9999)

    def test_absolute_jump_wrong_session_raises(self, tmp_path: Path):
        from contree_cli.session import SessionStore

        db = tmp_path / "nav.db"
        store_a = SessionStore(db, "session-a")
        store_b = SessionStore(db, "session-b")
        try:
            hid = store_a.set_image("img-1", kind="use")
            store_b.set_image("img-x", kind="use")
            with pytest.raises(ValueError, match="not found"):
                store_b.navigate(hid)
        finally:
            store_a.close()
            store_b.close()

    def test_relative_backward(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        entry = session_store.navigate(-1)
        assert entry.image_uuid == "img-1"

    def test_zero_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="must not be 0"):
            session_store.navigate(0)

    def test_no_session_raises(self, session_store: SessionStore):
        with pytest.raises(ValueError, match="No active session"):
            session_store.navigate(-1)

    def test_forward_one_step(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        session_store.set_image("img-3", kind="run")
        # Go back 2, then forward 1
        session_store.navigate(-2)
        assert session_store.current_image == "img-1"
        entry = session_store.navigate_forward(1)
        assert entry.image_uuid == "img-2"

    def test_forward_two_steps(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        session_store.set_image("img-3", kind="run")
        session_store.navigate(-2)
        entry = session_store.navigate_forward(2)
        assert entry.image_uuid == "img-3"

    def test_forward_past_tip_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        with pytest.raises(ValueError, match="only 0 children"):
            session_store.navigate_forward(1)

    def test_forward_at_branch_point_picks_latest(
        self,
        session_store: SessionStore,
    ):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        # Go back and create a second child (branching point)
        session_store.navigate(-1)
        session_store.set_image("img-3", kind="run")
        # Now go back again and forward — should pick img-3 (latest id)
        session_store.navigate(-1)
        entry = session_store.navigate_forward(1)
        assert entry.image_uuid == "img-3"

    def test_forward_no_session_raises(self, session_store: SessionStore):
        with pytest.raises(ValueError, match="No active session"):
            session_store.navigate_forward(1)

    def test_forward_zero_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="must be >= 1"):
            session_store.navigate_forward(0)


class TestCreateBranch:
    def test_create_branch_from_active(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.create_branch("feature")
        branches = session_store.list_branches()
        names = [b[0] for b in branches]
        assert "feature" in names
        assert "main" in names

    def test_create_branch_from_specific(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.create_branch("feature")
        session_store.create_branch("hotfix", from_branch="feature")
        names = [b[0] for b in session_store.list_branches()]
        assert "hotfix" in names

    def test_create_branch_duplicate_raises(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.create_branch("feature")
        with pytest.raises(ValueError, match="already exists"):
            session_store.create_branch("feature")

    def test_create_branch_missing_source_raises(self, session_store: SessionStore):
        session_store.set_image("img-1")
        with pytest.raises(ValueError, match="does not exist"):
            session_store.create_branch("feature", from_branch="nonexistent")

    def test_create_branch_no_session_raises(self, session_store: SessionStore):
        with pytest.raises(ValueError, match="No active session"):
            session_store.create_branch("feature")


class TestListBranches:
    def test_empty_when_no_session(self, session_store: SessionStore):
        assert session_store.list_branches() == []

    def test_single_branch(self, session_store: SessionStore):
        session_store.set_image("img-1")
        branches = session_store.list_branches()
        assert branches == [("main", True)]

    def test_multiple_branches(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.create_branch("feature")
        branches = session_store.list_branches()
        assert ("feature", False) in branches
        assert ("main", True) in branches


class TestSwitchBranch:
    def test_switch_branch(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_image("img-2")
        session_store.create_branch("feature")
        entry = session_store.switch_branch("feature")
        assert entry.image_uuid == "img-2"
        s = session_store.session
        assert s is not None
        assert s.active_branch == "feature"

    def test_switch_nonexistent_raises(self, session_store: SessionStore):
        session_store.set_image("img-1")
        with pytest.raises(ValueError, match="does not exist"):
            session_store.switch_branch("nonexistent")


class TestListSessions:
    def test_empty(self, session_store: SessionStore):
        assert session_store.list_sessions() == []

    def test_multiple_sessions(self, tmp_path: Path):
        db = tmp_path / "multi.db"
        store1 = SessionStore(db, "session-a")
        store2 = SessionStore(db, "session-b")
        try:
            store1.set_image("img-1", kind="use")
            store2.set_image("img-2", kind="run")
            sessions = store1.list_sessions()
            keys = [s.session_key for s in sessions]
            assert "session-a" in keys
            assert "session-b" in keys
        finally:
            store1.close()
            store2.close()


class TestFindSession:
    def test_exact_match(self, tmp_path: Path):
        db = tmp_path / "find.db"
        store = SessionStore(db, "default_myproject")
        try:
            store.set_image("img-1")
            found = store.find_session("default_myproject")
            assert found.session_key == "default_myproject"
        finally:
            store.close()

    def test_suffix_match(self, tmp_path: Path):
        db = tmp_path / "find.db"
        store = SessionStore(db, "default_myproject")
        try:
            store.set_image("img-1")
            found = store.find_session("myproject")
            assert found.session_key == "default_myproject"
        finally:
            store.close()

    def test_not_found_raises(self, session_store: SessionStore):
        session_store.set_image("img-1")
        with pytest.raises(ValueError, match="not found"):
            session_store.find_session("nonexistent")

    def test_ambiguous_raises(self, tmp_path: Path):
        db = tmp_path / "find.db"
        store1 = SessionStore(db, "profile1_proj")
        store2 = SessionStore(db, "profile2_proj")
        try:
            store1.set_image("img-1")
            store2.set_image("img-2")
            with pytest.raises(ValueError, match="Ambiguous"):
                store1.find_session("proj")
        finally:
            store1.close()
            store2.close()


class TestHistoryDag:
    def test_empty(self, session_store: SessionStore):
        entries, branches = session_store.history_dag()
        assert entries == []
        assert branches == {}

    def test_linear_history(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        entries, branches = session_store.history_dag()
        assert len(entries) == 2
        assert entries[0].image_uuid == "img-1"
        assert entries[1].image_uuid == "img-2"
        # Branch label on tip only
        assert entries[1].id in branches
        assert "main" in branches[entries[1].id]

    def test_branched_history(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run")
        session_store.create_branch("feature")
        entries, branches = session_store.history_dag()
        # Both branches point to same tip (img-2)
        tip_id = entries[-1].id
        assert "main" in branches[tip_id]
        assert "feature" in branches[tip_id]


class TestGetHistoryEntry:
    def test_valid_entry(self, session_store: SessionStore):
        session_store.set_image("img-1")
        entries, _ = session_store.history_dag()
        entry = session_store._get_history_entry(entries[0].id)
        assert entry.image_uuid == "img-1"

    def test_invalid_id_raises(self, session_store: SessionStore):
        with pytest.raises(ValueError, match="not found"):
            session_store._get_history_entry(9999)


class TestGetSessionKey:
    def test_with_env_var(self):
        with patch.dict(os.environ, {"CONTREE_SESSION": "mysess"}):
            key = get_session_key("default")
        assert key == "mysess"

    def test_with_override_beats_env_var(self):
        with patch.dict(os.environ, {"CONTREE_SESSION": "mysess"}):
            key = get_session_key("default", override="flag")
        assert key == "flag"

    def test_without_env_var(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pathlib.Path.cwd", return_value=Path("/tmp/myproj")),
        ):
            key = get_session_key("default")
        assert key.startswith("myproj+")
        suffix = key.split("+", 1)[1]
        assert len(suffix) == 8
        assert all(ch in "0123456789abcdef" for ch in suffix)


class TestGetDbPath:
    def test_default_profile(self):
        p = ConfigProfile(name="default", url="", token=None)
        assert p.session_db_path.name == "default.db"
        assert p.session_db_path.parent.name == "sessions"
        assert p.session_db_path.parent.parent.name == "cli"

    def test_named_profile(self):
        p = ConfigProfile(name="staging", url="", token=None)
        assert p.session_db_path.name == "staging.db"
        assert p.session_db_path.parent.name == "sessions"


class TestPendingFiles:
    def test_no_session_returns_empty(self, session_store: SessionStore):
        assert session_store.pending_files() == []

    def test_add_and_list(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /etc/config.ini",
        )
        session_store.add_pending_file(hid, "/etc/config.ini", "file-uuid-1")
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0] == PendingFile(
            instance_path="/etc/config.ini",
            file_uuid="file-uuid-1",
            uid=0,
            gid=0,
            mode="0644",
        )

    def test_add_with_custom_attrs(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /app/script.sh",
        )
        session_store.add_pending_file(
            hid,
            "/app/script.sh",
            "file-uuid-2",
            uid=1000,
            gid=1000,
            mode="0755",
        )
        files = session_store.pending_files()
        assert files[0].uid == 1000
        assert files[0].gid == 1000
        assert files[0].mode == "0755"

    def test_same_path_edited_twice_latest_wins(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid1 = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /etc/config.ini",
        )
        session_store.add_pending_file(hid1, "/etc/config.ini", "uuid-old")
        hid2 = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /etc/config.ini",
        )
        session_store.add_pending_file(hid2, "/etc/config.ini", "uuid-new")
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].file_uuid == "uuid-new"

    def test_multiple_files(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid1 = session_store.set_image("img-1", kind="file", title="Change file /a.txt")
        session_store.add_pending_file(hid1, "/a.txt", "uuid-a")
        hid2 = session_store.set_image("img-1", kind="file", title="Change file /b.txt")
        session_store.add_pending_file(hid2, "/b.txt", "uuid-b")
        files = session_store.pending_files()
        assert len(files) == 2
        paths = {f.instance_path for f in files}
        assert paths == {"/a.txt", "/b.txt"}

    def test_not_included_after_run(self, session_store: SessionStore):
        """After a run, pending files are no longer returned."""
        session_store.set_image("img-1", kind="use")
        hid = session_store.set_image("img-1", kind="file", title="Change file /a.txt")
        session_store.add_pending_file(hid, "/a.txt", "uuid-a")
        assert len(session_store.pending_files()) == 1
        # Simulate a successful run creating a new image
        session_store.set_image("img-2", kind="run", title="echo hello")
        assert session_store.pending_files() == []

    def test_reappear_after_rollback(self, session_store: SessionStore):
        """After rollback past a run, pending files reappear."""
        session_store.set_image("img-1", kind="use")
        hid = session_store.set_image("img-1", kind="file", title="Change file /a.txt")
        session_store.add_pending_file(hid, "/a.txt", "uuid-a")
        session_store.set_image("img-2", kind="run", title="echo hello")
        assert session_store.pending_files() == []
        # Rollback past the run
        session_store.rollback(1)
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].instance_path == "/a.txt"

    def test_clear_returns_count(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid1 = session_store.set_image("img-1", kind="file", title="Change file /a.txt")
        session_store.add_pending_file(hid1, "/a.txt", "uuid-a")
        hid2 = session_store.set_image("img-1", kind="file", title="Change file /b.txt")
        session_store.add_pending_file(hid2, "/b.txt", "uuid-b")
        cleared = session_store.clear_pending_files()
        assert cleared == 2

    def test_clear_no_session_returns_zero(self, session_store: SessionStore):
        assert session_store.clear_pending_files() == 0

    def test_branch_isolation(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        hid = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /main.txt",
        )
        session_store.add_pending_file(hid, "/main.txt", "uuid-main")
        session_store.create_branch("feature")
        session_store.switch_branch("feature")
        # Feature branch shares history — file is visible here too
        # (both branches point to same tip with the file entry)
        feat_files = session_store.pending_files()
        assert len(feat_files) == 1
        # Add a file on feature branch
        hid2 = session_store.set_image(
            "img-1",
            kind="file",
            title="Change file /feat.txt",
        )
        session_store.add_pending_file(hid2, "/feat.txt", "uuid-feat")
        assert len(session_store.pending_files()) == 2
        # Switch back to main — only sees /main.txt
        session_store.switch_branch("main")
        files = session_store.pending_files()
        assert len(files) == 1
        assert files[0].instance_path == "/main.txt"


class TestCwd:
    def test_cwd_empty_initially(self, session_store: SessionStore):
        session_store.set_image("img-1")
        assert session_store.get_cwd() == ""

    def test_set_and_get_cwd(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        assert session_store.get_cwd() == "/app"

    def test_cwd_stored_in_history(self, session_store: SessionStore):
        """set_cwd creates a kind='cd' history entry."""
        session_store.set_image("img-1")
        session_store.set_cwd("/etc")
        entries, _ = session_store.history_dag()
        cd_entries = [e for e in entries if e.kind == "cd"]
        assert len(cd_entries) == 1
        assert cd_entries[0].title == "/etc"

    def test_cwd_no_session_returns_empty(self, session_store: SessionStore):
        assert session_store.get_cwd() == ""

    def test_set_cwd_no_session_is_noop(self, session_store: SessionStore):
        session_store.set_cwd("/app")  # Should not raise
        assert session_store.get_cwd() == ""

    def test_cwd_survives_set_image(self, session_store: SessionStore):
        """set_image should not reset cwd."""
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        session_store.set_image("img-2", kind="run")
        assert session_store.get_cwd() == "/app"

    def test_cwd_rollback(self, session_store: SessionStore):
        """Rolling back past a cd should reset cwd."""
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        session_store.set_image("img-2", kind="run")
        assert session_store.get_cwd() == "/app"
        # Rollback past the run and cd entries
        session_store.rollback(2)
        assert session_store.get_cwd() == ""

    def test_multiple_cd_last_wins(self, session_store: SessionStore):
        """The most recent cd in history determines cwd."""
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        session_store.set_cwd("/tmp")
        assert session_store.get_cwd() == "/tmp"


class TestResolvePath:
    """SessionStore.resolve_path centralises path resolution."""

    def test_relative_joined_with_cwd(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        assert session_store.resolve_path("src") == "/app/src"

    def test_relative_dotdot(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_cwd("/app/src")
        assert session_store.resolve_path("../lib") == "/app/lib"

    def test_relative_no_cwd_uses_root(self, session_store: SessionStore):
        session_store.set_image("img-1")
        assert session_store.resolve_path("etc") == "/etc"

    def test_empty_returns_cwd(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        assert session_store.resolve_path("") == "/app"

    def test_empty_no_cwd_returns_root(self, session_store: SessionStore):
        session_store.set_image("img-1")
        assert session_store.resolve_path("") == "/"

    def test_absolute_clean_passes_through(self, session_store: SessionStore):
        session_store.set_image("img-1")
        session_store.set_cwd("/app")
        assert session_store.resolve_path("/etc/hosts") == "/etc/hosts"

    def test_absolute_with_dotdot_normalized(
        self,
        session_store: SessionStore,
    ):
        session_store.set_image("img-1")
        assert session_store.resolve_path("/tmp/../etc/hosts") == "/etc/hosts"

    def test_absolute_with_double_slash_normalized(
        self,
        session_store: SessionStore,
    ):
        session_store.set_image("img-1")
        assert session_store.resolve_path("/tmp//hosts") == "/tmp/hosts"

    def test_absolute_with_dot_normalized(
        self,
        session_store: SessionStore,
    ):
        session_store.set_image("img-1")
        assert session_store.resolve_path("/tmp/./hosts") == "/tmp/hosts"

    def test_absolute_trailing_dotdot(self, session_store: SessionStore):
        session_store.set_image("img-1")
        assert session_store.resolve_path("/app/src/..") == "/app"


class TestShellHistory:
    def test_empty_initially(self, session_store: SessionStore):
        assert session_store.load_shell_history() == []

    def test_add_and_load(self, session_store: SessionStore):
        session_store.add_shell_history("ls /etc")
        session_store.add_shell_history("cat /etc/hosts")
        lines = session_store.load_shell_history()
        assert lines == ["ls /etc", "cat /etc/hosts"]

    def test_order_preserved(self, session_store: SessionStore):
        for i in range(5):
            session_store.add_shell_history(f"cmd-{i}")
        lines = session_store.load_shell_history()
        assert lines == [f"cmd-{i}" for i in range(5)]

    def test_session_isolation(self, tmp_path: Path):
        """History from one session is not visible in another."""
        db = tmp_path / "iso.db"
        s1 = SessionStore(db, "sess-a")
        s2 = SessionStore(db, "sess-b")
        try:
            s1.add_shell_history("only-in-a")
            s2.add_shell_history("only-in-b")
            assert s1.load_shell_history() == ["only-in-a"]
            assert s2.load_shell_history() == ["only-in-b"]
        finally:
            s1.close()
            s2.close()

    def test_trim_respects_max(self, session_store: SessionStore):
        # Temporarily lower the limit for testing
        original = SessionStore.MAX_SHELL_HISTORY
        SessionStore.MAX_SHELL_HISTORY = 5
        try:
            for i in range(10):
                session_store.add_shell_history(f"line-{i}")
            session_store.trim_shell_history()
            lines = session_store.load_shell_history()
            assert len(lines) == 5
            # Should keep the 5 newest
            assert lines == [f"line-{i}" for i in range(5, 10)]
        finally:
            SessionStore.MAX_SHELL_HISTORY = original

    def test_trim_noop_when_under_limit(self, session_store: SessionStore):
        session_store.add_shell_history("one")
        session_store.add_shell_history("two")
        session_store.trim_shell_history()
        assert session_store.load_shell_history() == ["one", "two"]


class TestImageCache:
    def test_set_and_get(self, session_store: SessionStore):
        cache = session_store.cache
        cache["img-1", "files:/etc/"] = [{"path": "/etc/hosts"}]
        result = cache["img-1", "files:/etc/"]
        assert result == [{"path": "/etc/hosts"}]

    def test_missing_key_raises(self, session_store: SessionStore):
        with pytest.raises(KeyError):
            _ = session_store.cache["no-such", "missing"]

    def test_contains(self, session_store: SessionStore):
        cache = session_store.cache
        assert ("img-1", "files:/etc/") not in cache
        cache["img-1", "files:/etc/"] = []
        assert ("img-1", "files:/etc/") in cache

    def test_contains_non_tuple(self, session_store: SessionStore):
        assert "not-a-tuple" not in session_store.cache

    def test_get_with_default(self, session_store: SessionStore):
        cache = session_store.cache
        assert cache.get(("img-1", "files:/etc/")) is None
        assert cache.get(("img-1", "files:/etc/"), 42) == 42
        cache["img-1", "files:/etc/"] = [1, 2, 3]
        assert cache.get(("img-1", "files:/etc/")) == [1, 2, 3]

    def test_overwrite(self, session_store: SessionStore):
        cache = session_store.cache
        cache["img-1", "images"] = [{"uuid": "old"}]
        cache["img-1", "images"] = [{"uuid": "new"}]
        assert cache["img-1", "images"] == [{"uuid": "new"}]

    def test_persists_across_instances(self, session_store: SessionStore):
        """New ImageCache instances see the same data."""
        session_store.cache["img-1", "files:/"] = ["root"]
        cache2 = session_store.cache
        assert cache2["img-1", "files:/"] == ["root"]

    def test_global_image_list(self, session_store: SessionStore):
        """The image list cache uses empty-string UUID."""
        cache = session_store.cache
        images = [{"uuid": "aaa", "tag": "common/python"}]
        cache["", "images"] = images
        assert cache["", "images"] == images

    def test_small_value_stored_as_json_prefix(self, session_store: SessionStore):
        """Values under threshold use json: prefix."""
        cache = session_store.cache
        cache["img-1", "small"] = {"key": "val"}
        row = session_store._conn.execute(
            "SELECT value FROM image_cache WHERE image_uuid='img-1' AND kind='small'",
        ).fetchone()
        assert row["value"].startswith("json:")

    def test_large_value_stored_as_gzip_prefix(self, session_store: SessionStore):
        """Values over threshold use gzip: prefix."""
        cache = session_store.cache
        big = {"data": "x" * 2000}
        cache["img-1", "big"] = big
        row = session_store._conn.execute(
            "SELECT value FROM image_cache WHERE image_uuid='img-1' AND kind='big'",
        ).fetchone()
        assert row["value"].startswith("gzip:")
        assert cache["img-1", "big"] == big

    def test_legacy_plain_json_readable(self, session_store: SessionStore):
        """Pre-prefix entries (plain JSON) can still be read."""
        session_store._conn.execute(
            "INSERT INTO image_cache (image_uuid, kind, value) VALUES (?, ?, ?)",
            ("img-1", "legacy", '{"old": true}'),
        )
        session_store._conn.commit()
        assert session_store.cache["img-1", "legacy"] == {"old": True}

    def test_iter(self, session_store: SessionStore):
        cache = session_store.cache
        cache["img-1", "a"] = 1
        cache["img-2", "b"] = 2
        keys = list(cache)
        assert ("img-1", "a") in keys
        assert ("img-2", "b") in keys

    def test_len(self, session_store: SessionStore):
        cache = session_store.cache
        assert len(cache) == 0
        cache["img-1", "a"] = 1
        assert len(cache) == 1
        cache["img-2", "b"] = 2
        assert len(cache) == 2

    def test_delete(self, session_store: SessionStore):
        cache = session_store.cache
        cache["img-1", "a"] = 1
        assert len(cache) == 1
        del cache["img-1", "a"]
        assert len(cache) == 0

    def test_delete_missing_raises(self, session_store: SessionStore):
        with pytest.raises(KeyError):
            del session_store.cache["no", "key"]


class TestBranchTip:
    def test_existing_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        entry = session_store.branch_tip("main")
        assert entry.image_uuid == "img-1"

    def test_nonexistent_branch_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="does not exist"):
            session_store.branch_tip("nonexistent")


class TestDeleteBranch:
    def test_delete_existing_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_branch("feature")
        session_store.delete_branch("feature")
        branches = dict(session_store.list_branches())
        assert "feature" not in branches

    def test_delete_active_branch_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="Cannot delete the active branch"):
            session_store.delete_branch("main")

    def test_delete_nonexistent_branch_raises(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        with pytest.raises(ValueError, match="does not exist"):
            session_store.delete_branch("nonexistent")


class TestPruneBranches:
    def test_prune_detached_and_disposable(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_detached_branch("op-1", "det")
        session_store.create_disposable_branch("op-2", "disp")
        removed = session_store.prune_branches()
        assert "detached-op-1" in removed
        assert "disposable-op-2" in removed
        branches = dict(session_store.list_branches())
        assert "detached-op-1" not in branches
        assert "disposable-op-2" not in branches

    def test_prune_keeps_named_branches(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_branch("feature")
        session_store.create_detached_branch("op-1", "det")
        removed = session_store.prune_branches()
        assert "detached-op-1" in removed
        branches = dict(session_store.list_branches())
        assert "feature" in branches

    def test_prune_empty(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        removed = session_store.prune_branches()
        assert removed == []

    def test_prune_keeps_active_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        # Even if active branch name starts with detached-, it should be kept
        removed = session_store.prune_branches()
        assert removed == []


class TestCreateDetachedBranch:
    def test_creates_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        name = session_store.create_detached_branch("op-abc", "test title")
        assert name == "detached-op-abc"
        branches = dict(session_store.list_branches())
        assert "detached-op-abc" in branches

    def test_creates_history_entry(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_detached_branch("op-abc", "test title")
        entry = session_store.branch_tip("detached-op-abc")
        assert entry.kind == "run-detached"
        assert entry.title == "test title"
        assert entry.operation_uuid == "op-abc"


class TestCreateDisposableBranch:
    def test_creates_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        name = session_store.create_disposable_branch("op-xyz", "test title")
        assert name == "disposable-op-xyz"
        branches = dict(session_store.list_branches())
        assert "disposable-op-xyz" in branches

    def test_creates_history_entry(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_disposable_branch("op-xyz", "test title")
        entry = session_store.branch_tip("disposable-op-xyz")
        assert entry.kind == "run-disposable"
        assert entry.title == "test title"
        assert entry.operation_uuid == "op-xyz"


class TestSetImageOnBranch:
    def test_set_image_on_explicit_branch(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        session_store.create_branch("feature")
        session_store.set_image_on_branch(
            "feature", "img-feat", kind="run", title="test"
        )
        entry = session_store.branch_tip("feature")
        assert entry.image_uuid == "img-feat"
        assert entry.kind == "run"
        # Active branch should still be on img-1
        assert session_store.current_image == "img-1"

    def test_set_image_on_new_branch(self, session_store: SessionStore):
        """set_image_on_branch creates branch entry if it doesn't exist."""
        session_store.set_image("img-1", kind="use")
        session_store.set_image_on_branch(
            "new-branch", "img-new", kind="run", title="new"
        )
        entry = session_store.branch_tip("new-branch")
        assert entry.image_uuid == "img-new"


class TestHistoryDagFor:
    def test_cross_session_query(self, tmp_path: Path):
        db = tmp_path / "dag_for.db"
        store_a = SessionStore(db, "session-a")
        store_b = SessionStore(db, "session-b")
        try:
            store_a.set_image("img-a1", kind="use")
            store_a.set_image("img-a2", kind="run")
            store_b.set_image("img-b1", kind="use")
            # Query store_a's history from store_b
            entries, _branch_map = store_b.history_dag_for("session-a")
            assert len(entries) == 2
            assert entries[0].image_uuid == "img-a1"
            assert entries[1].image_uuid == "img-a2"
        finally:
            store_a.close()
            store_b.close()

    def test_empty_session(self, session_store: SessionStore):
        entries, branch_map = session_store.history_dag_for("nonexistent")
        assert entries == []
        assert branch_map == {}


class TestHistoryDepth:
    def test_zero_when_no_session(self, session_store: SessionStore):
        assert session_store.history_depth() == 0

    def test_depth_matches_entries(self, session_store: SessionStore):
        session_store.set_image("img-1", kind="use")
        assert session_store.history_depth() == 1
        session_store.set_image("img-2", kind="run")
        assert session_store.history_depth() == 2
        session_store.set_image("img-3", kind="run")
        assert session_store.history_depth() == 3


class TestConcurrentStores:
    def test_two_stores_on_same_db_do_not_lock(self, tmp_path: Path):
        # Two `contree shell` tabs share one per-profile SQLite file.
        # WAL + busy_timeout must let interleaved writes succeed.
        db = tmp_path / "shared.db"
        store_a = SessionStore(db, "sess-a")
        store_b = SessionStore(db, "sess-b")
        try:
            for i in range(20):
                store_a.set_image(f"img-a-{i}", kind="run")
                store_b.set_image(f"img-b-{i}", kind="run")
                store_a.cache[(f"img-a-{i}", "list:/etc")] = {"n": i}
                store_b.cache[(f"img-b-{i}", "list:/etc")] = {"n": i}
            assert store_a.history_depth() == 20
            assert store_b.history_depth() == 20
        finally:
            store_a.close()
            store_b.close()
