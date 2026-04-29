from __future__ import annotations

import json
from contextvars import copy_context
from unittest.mock import patch

from conftest import ContreeTestClient, FakeResponse

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.use import UseArgs, cmd_use
from contree_cli.output import DefaultFormatter, JSONFormatter
from contree_cli.session import SessionStore


def _run_cmd(
    tc: ContreeTestClient,
    args: UseArgs,
    *,
    store: SessionStore,
    responses: list[FakeResponse] | None = None,
    formatter=None,
):
    """Run cmd_use with mocked HTTP client and real SessionStore."""
    if responses:
        tc.fake.responses.extend(responses)

    SESSION_STORE.set(store)
    FORMATTER.set(formatter or DefaultFormatter())
    ctx = copy_context()

    created: list[SessionStore] = []
    _orig = SessionStore.__init__

    def _tracking_init(self, db_path, session_key):
        _orig(self, db_path, session_key)
        created.append(self)

    with patch.object(SessionStore, "__init__", _tracking_init):
        rc = ctx.run(cmd_use, args)

    for s in created:
        s.close()
    return rc


class TestUseWithImage:
    def test_stores_image(self, contree_client, session_store):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=False)
        _run_cmd(contree_client, args, store=session_store)
        assert session_store.current_image == "a1b2c3d4-5678-9abc-def0-111111111111"

    def test_outputs_session_key(self, contree_client, session_store, capsys):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=False)
        rc = _run_cmd(contree_client, args, store=session_store)
        assert rc is None
        out = capsys.readouterr().out
        assert "CONTREE_SESSION" in out
        assert "test" in out

    def test_outputs_fish_syntax(self, contree_client, session_store, capsys):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=False)
        with patch.dict("os.environ", {"SHELL": "/usr/bin/fish"}):
            _run_cmd(contree_client, args, store=session_store)
        out = capsys.readouterr().out
        assert out.strip() == "set -gx CONTREE_SESSION test"

    def test_resolves_tag(self, contree_client, session_store):
        args = UseArgs(image="tag:latest", new=False)
        images_resp = FakeResponse.json(
            {"images": [{"uuid": "resolved-uuid"}]},
        )
        _run_cmd(
            contree_client,
            args,
            store=session_store,
            responses=[images_resp],
        )
        assert session_store.current_image == "resolved-uuid"


class TestUseNoArgs:
    def test_shows_session_info_json(self, contree_client, session_store, capsys):
        session_store.set_image("a1b2c3d4-5678-9abc-def0-111111111111", kind="use")
        args = UseArgs(image=None, new=False)
        rc = _run_cmd(
            contree_client,
            args,
            store=session_store,
            formatter=JSONFormatter(),
        )
        assert rc is None
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["session_key"] == "test"
        assert parsed["active_branch"] == "main"
        assert parsed["current_image"] == "a1b2c3d4-5678-9abc-def0-111111111111"
        assert parsed["last_kind"] == "use"

    def test_no_session_returns_error(self, contree_client, session_store, capsys):
        args = UseArgs(image=None, new=False)
        rc = _run_cmd(contree_client, args, store=session_store)
        assert rc == 1
        err = capsys.readouterr().err
        assert "No active session" in err


class TestUseNew:
    def test_new_creates_different_session(
        self, contree_client, session_store, profile, capsys
    ):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=True)
        _run_cmd(contree_client, args, store=session_store)
        out = capsys.readouterr().out
        assert "CONTREE_SESSION" in out
        # Key should NOT be the original "test" key
        assert "test" not in out

    def test_new_warns_on_tty(self, contree_client, session_store, profile, caplog):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=True)
        with (
            caplog.at_level("WARNING"),
            patch("sys.stdout.isatty", return_value=True),
        ):
            _run_cmd(contree_client, args, store=session_store)
        assert "Session is not active until exported" in caplog.text
        assert "eval" in caplog.text
        assert "(contree use -N IMAGE)" in caplog.text

    def test_new_warns_fish_eval(self, contree_client, session_store, profile, caplog):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=True)
        with (
            caplog.at_level("WARNING"),
            patch("sys.stdout.isatty", return_value=True),
            patch.dict("os.environ", {"SHELL": "/usr/bin/fish"}),
        ):
            _run_cmd(contree_client, args, store=session_store)
        assert "eval (contree use -N IMAGE)" in caplog.text

    def test_new_no_warning_when_piped(
        self, contree_client, session_store, profile, caplog
    ):
        args = UseArgs(image="a1b2c3d4-5678-9abc-def0-111111111111", new=True)
        with (
            caplog.at_level("WARNING"),
            patch("sys.stdout.isatty", return_value=False),
        ):
            _run_cmd(contree_client, args, store=session_store)
        assert caplog.text == ""

    def test_new_without_image_returns_error(
        self, contree_client, session_store, capsys
    ):
        args = UseArgs(image=None, new=True)
        rc = _run_cmd(contree_client, args, store=session_store)
        assert rc == 1
        err = capsys.readouterr().err
        assert "--new requires an IMAGE" in err
