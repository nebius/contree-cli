from __future__ import annotations

import json
from contextvars import copy_context

from conftest import ContreeTestClient, FakeResponse

from contree_cli import FORMATTER, SESSION_STORE
from contree_cli.cli.ls import (
    LsArgs,
    cmd_ls,
)
from contree_cli.output import (
    CSVFormatter,
    DefaultFormatter,
    JSONFormatter,
    TableFormatter,
)
from contree_cli.session import SessionStore


def _make_file(
    path: str = "/etc/hosts",
    size: int = 128,
    mode: int = 0o644,
    owner: str = "root",
    group: str = "root",
    mtime: int = 1700000000,
    is_dir: bool = False,
    is_symlink: bool = False,
) -> dict:
    return {
        "path": path,
        "size": size,
        "mode": mode,
        "owner": owner,
        "group": group,
        "mtime": mtime,
        "is_dir": is_dir,
        "is_symlink": is_symlink,
    }


def _run_cmd(
    tc: ContreeTestClient,
    files: list[dict] | None = None,
    *,
    store: SessionStore,
    text: str | None = None,
    image: str = "a1b2c3d4-5678-9abc-def0-111111111111",
    path: str = "/etc",
    formatter=None,
    images_response: dict | None = None,
):
    """Run cmd_ls with mocked responses and a session store."""
    if images_response is not None:
        tc.respond_json(images_response)

    if text is not None:
        # Manually add a text response (not JSON)
        tc.fake.responses.append(FakeResponse(body=text.encode()))
    else:
        tc.respond_json({"path": path, "files": files or []})

    FORMATTER.set(formatter or CSVFormatter())
    store.set_image(image, kind="test")
    SESSION_STORE.set(store)
    ctx = copy_context()

    args = LsArgs(path=path)
    ctx.run(cmd_ls, args)


class TestCmdLs:
    def test_request_path(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [_make_file()],
            store=session_store,
            image="a1b2c3d4-5678-9abc-def0-111111111111",
            path="/etc",
        )
        paths = contree_client.request_paths
        assert len(paths) == 1
        assert "/v1/inspect/a1b2c3d4-5678-9abc-def0-111111111111/list" in paths[0]
        assert "path=%2Fetc" in paths[0]

    def test_outputs_file_entries(self, contree_client, session_store, capsys):
        files = [
            _make_file(path="/etc/hosts", size=128),
            _make_file(path="/etc/passwd", size=256),
        ]
        _run_cmd(contree_client, files, store=session_store)
        out = capsys.readouterr().out
        assert "/etc/hosts" in out
        assert "/etc/passwd" in out

    def test_tag_resolves_then_inspects(self, contree_client, session_store):
        images_resp = {"images": [{"uuid": "resolved-uuid", "tag": "latest"}]}
        _run_cmd(
            contree_client,
            [_make_file()],
            store=session_store,
            image="tag:latest",
            images_response=images_resp,
        )
        paths = contree_client.request_paths
        assert len(paths) == 2
        assert "tag=latest" in paths[0]
        assert "/v1/inspect/resolved-uuid/list" in paths[1]

    def test_empty_directory(self, contree_client, session_store, capsys):
        _run_cmd(contree_client, [], store=session_store)
        assert capsys.readouterr().out == ""

    def test_symlink_type(self, contree_client, session_store, capsys):
        files = [_make_file(path="/etc/link", is_symlink=True)]
        _run_cmd(contree_client, files, store=session_store)
        out = capsys.readouterr().out
        assert "/etc/link" in out
        assert ",l\r\n" in out or ",l\n" in out

    def test_directory_type(self, contree_client, session_store, capsys):
        files = [_make_file(path="/etc/conf.d", is_dir=True)]
        _run_cmd(contree_client, files, store=session_store)
        out = capsys.readouterr().out
        assert "/etc/conf.d" in out
        assert ",d\r\n" in out or ",d\n" in out

    def test_regular_file_type(self, contree_client, session_store, capsys):
        files = [_make_file(path="/etc/hosts")]
        _run_cmd(contree_client, files, store=session_store)
        out = capsys.readouterr().out
        assert ",-\r\n" in out or ",-\n" in out

    def test_mode_octal_format(self, contree_client, session_store, capsys):
        files = [_make_file(mode=0o755)]
        _run_cmd(contree_client, files, store=session_store)
        out = capsys.readouterr().out
        assert "755" in out

    def test_json_output(self, contree_client, session_store, capsys):
        files = [_make_file(path="/bin/sh", size=42)]
        _run_cmd(contree_client, files, store=session_store, formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["path"] == "/bin/sh"
        assert parsed["size"] == 42

    def test_table_output(self, contree_client, session_store, capsys):
        files = [_make_file(), _make_file(path="/etc/passwd")]
        fmt = TableFormatter()
        _run_cmd(contree_client, files, store=session_store, formatter=fmt)
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 3  # header + 2 rows
        assert "PATH" in lines[0]

    def test_owner_fallback_to_uid(self, contree_client, session_store, capsys):
        f = _make_file()
        del f["owner"]
        f["uid"] = 1000
        _run_cmd(contree_client, [f], store=session_store)
        out = capsys.readouterr().out
        assert "1000" in out

    def test_group_fallback_to_gid(self, contree_client, session_store, capsys):
        f = _make_file()
        del f["group"]
        f["gid"] = 1000
        _run_cmd(contree_client, [f], store=session_store)
        out = capsys.readouterr().out
        assert "1000" in out

    def test_default_formatter_prints_server_text(
        self, contree_client, session_store, capsys
    ):
        server_text = "-rw-r--r--  root  root  128  2023-11-14 22:13  /etc/hosts\n"
        _run_cmd(
            contree_client,
            text=server_text,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        assert capsys.readouterr().out == server_text

    def test_default_formatter_passes_text_param(self, contree_client, session_store):
        server_text = "-rw-r--r--  root  root  128  /etc/hosts\n"
        _run_cmd(
            contree_client,
            text=server_text,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        paths = contree_client.request_paths
        assert len(paths) == 1
        assert "text=1" in paths[0]
        assert "path=%2Fetc" in paths[0]

    def test_explicit_format_no_text_param(self, contree_client, session_store):
        _run_cmd(
            contree_client,
            [_make_file()],
            store=session_store,
            formatter=JSONFormatter(),
        )
        paths = contree_client.request_paths
        assert len(paths) == 1
        assert "text=1" not in paths[0]

    def test_default_formatter_empty(self, contree_client, session_store, capsys):
        _run_cmd(
            contree_client, text="", store=session_store, formatter=DefaultFormatter()
        )
        assert capsys.readouterr().out == ""

    def test_explicit_format_not_overridden(
        self, contree_client, session_store, capsys
    ):
        files = [_make_file()]
        _run_cmd(contree_client, files, store=session_store, formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["path"] == "/etc/hosts"

    def test_json_format_caches_result(self, contree_client, session_store):
        """Second call with same args should not hit the API."""
        files = [_make_file()]
        _run_cmd(contree_client, files, store=session_store)
        assert contree_client.request_count == 1

        # Second call -- should be served from cache
        _run_cmd(contree_client, files, store=session_store)
        assert contree_client.request_count == 1  # no new request (cached)

    def test_default_formatter_not_cached(self, contree_client, session_store):
        """DefaultFormatter (text=1) path always hits API."""
        text = "-rw-r--r--  root  root  128  /etc/hosts\n"
        _run_cmd(
            contree_client,
            text=text,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        assert contree_client.request_count == 1

        _run_cmd(
            contree_client,
            text=text,
            store=session_store,
            formatter=DefaultFormatter(),
        )
        assert contree_client.request_count == 2  # new request (not cached)
