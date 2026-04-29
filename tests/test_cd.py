from __future__ import annotations

from contextvars import copy_context

from conftest import ContreeTestClient

from contree_cli import SESSION_STORE
from contree_cli.cli.cd import CdArgs, cmd_cd
from contree_cli.session import SessionStore

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"

LISTING = [{"name": ".", "type": "dir"}]


def _run_cmd(
    store: SessionStore,
    path: str | None = None,
    tc: ContreeTestClient | None = None,
) -> int | None:
    SESSION_STORE.set(store)
    if tc is not None:
        tc.respond_json(LISTING)
    ctx = copy_context()
    args = CdArgs(path=path)
    return ctx.run(cmd_cd, args)


class TestCdPrint:
    def test_no_arg_prints_cwd(self, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="use")
        session_store.set_cwd("/app")
        _run_cmd(session_store)
        assert capsys.readouterr().out.strip() == "/app"

    def test_no_arg_defaults_to_root(self, session_store, capsys):
        session_store.set_image(IMG_UUID, kind="use")
        _run_cmd(session_store)
        assert capsys.readouterr().out.strip() == "/"


class TestCdAbsolute:
    def test_absolute_path(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        _run_cmd(session_store, "/usr/local", contree_client)
        assert session_store.get_cwd() == "/usr/local"

    def test_absolute_path_normalised(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        _run_cmd(session_store, "/usr//local/../bin", contree_client)
        assert session_store.get_cwd() == "/usr/bin"

    def test_root(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        session_store.set_cwd("/app")
        _run_cmd(session_store, "/", contree_client)
        assert session_store.get_cwd() == "/"


class TestCdRelative:
    def test_relative_from_root(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        _run_cmd(session_store, "app", contree_client)
        assert session_store.get_cwd() == "/app"

    def test_relative_from_subdir(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        session_store.set_cwd("/usr")
        _run_cmd(session_store, "local/bin", contree_client)
        assert session_store.get_cwd() == "/usr/local/bin"

    def test_dotdot(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        session_store.set_cwd("/usr/local/bin")
        _run_cmd(session_store, "..", contree_client)
        assert session_store.get_cwd() == "/usr/local"

    def test_dot(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        session_store.set_cwd("/app")
        _run_cmd(session_store, ".", contree_client)
        assert session_store.get_cwd() == "/app"


class TestCdValidation:
    def test_nonexistent_path_fails(self, session_store, contree_client):
        session_store.set_image(IMG_UUID, kind="use")
        contree_client.respond(status=404, body=b"not found")
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        rc = ctx.run(cmd_cd, CdArgs(path="/nonexistent"))
        assert rc == 1
        assert session_store.get_cwd() != "/nonexistent"

    def test_no_image_skips_validation(self, session_store, contree_client):
        """cd with an image but no CLIENT should still set cwd."""
        session_store.set_image(IMG_UUID, kind="use")
        _run_cmd(session_store, "/app", contree_client)
        assert session_store.get_cwd() == "/app"
