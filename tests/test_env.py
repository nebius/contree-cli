from __future__ import annotations

from pathlib import Path

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.env import EnvArgs, cmd_env
from contree_cli.session import SessionStore


class TestEnvSet:
    def test_set_single(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        rc = cmd_env(EnvArgs(vars=["FOO=bar"]))
        assert rc is None
        assert session_store.get_env() == {"FOO": "bar"}

    def test_set_multiple(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        rc = cmd_env(EnvArgs(vars=["A=1", "B=2"]))
        assert rc is None
        assert session_store.get_env() == {"A": "1", "B": "2"}

    def test_set_overwrite(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        cmd_env(EnvArgs(vars=["FOO=old"]))
        cmd_env(EnvArgs(vars=["FOO=new"]))
        assert session_store.get_env() == {"FOO": "new"}

    def test_set_invalid_format(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        rc = cmd_env(EnvArgs(vars=["NOEQUALS"]))
        assert rc == 1

    def test_set_with_equals_in_value(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        cmd_env(EnvArgs(vars=["CMD=a=b=c"]))
        assert session_store.get_env() == {"CMD": "a=b=c"}


class TestEnvUnset:
    def test_unset(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        session_store.set_env("FOO", "bar")
        rc = cmd_env(EnvArgs(vars=["FOO"], delete=True))
        assert rc is None
        assert session_store.get_env() == {}

    def test_unset_multiple(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        session_store.set_env("A", "1")
        session_store.set_env("B", "2")
        session_store.set_env("C", "3")
        cmd_env(EnvArgs(vars=["A", "C"], delete=True))
        assert session_store.get_env() == {"B": "2"}

    def test_unset_no_keys(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        rc = cmd_env(EnvArgs(vars=[], delete=True))
        assert rc == 1


class TestEnvList:
    def test_list_empty(self, session_store: SessionStore, capsys) -> None:
        SESSION_STORE.set(session_store)
        rc = cmd_env(EnvArgs(vars=[]))
        assert rc is None
        assert "No session environment" in capsys.readouterr().out

    def test_list_with_vars(self, session_store: SessionStore) -> None:
        SESSION_STORE.set(session_store)
        session_store.set_env("PATH", "/usr/bin")
        session_store.set_env("DEBUG", "1")

        rows: list[dict[str, object]] = []

        class Capture:
            def __call__(self, **kw: object) -> None:
                rows.append(kw)

            def flush(self) -> None:
                pass

        FORMATTER.set(Capture())
        cmd_env(EnvArgs(vars=[]))
        assert len(rows) == 2
        keys = {str(r["key"]) for r in rows}
        assert keys == {"DEBUG", "PATH"}


class TestSessionStoreEnv:
    def test_get_set_unset(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path / "env.db", "test-env")
        assert store.get_env() == {}
        store.set_env("A", "1")
        store.set_env("B", "2")
        assert store.get_env() == {"A": "1", "B": "2"}
        store.unset_env("A")
        assert store.get_env() == {"B": "2"}
        store.close()

    def test_env_per_session(self, tmp_path: Path) -> None:
        s1 = SessionStore(tmp_path / "env.db", "session-1")
        s2 = SessionStore(tmp_path / "env.db", "session-2")
        s1.set_env("FOO", "from-s1")
        s2.set_env("FOO", "from-s2")
        assert s1.get_env() == {"FOO": "from-s1"}
        assert s2.get_env() == {"FOO": "from-s2"}
        s1.close()
        s2.close()

    def test_delete_session_cleans_env(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path / "env.db", "doomed")
        store.set_image("fake-uuid", kind="test")
        store.set_env("X", "1")
        assert store.get_env() == {"X": "1"}
        store.delete_session("doomed")
        assert store.get_env() == {}
        store.close()
