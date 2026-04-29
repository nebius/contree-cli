from __future__ import annotations

import json
from contextvars import copy_context

import pytest
from conftest import ContreeTestClient

from contree_cli import SESSION_STORE
from contree_cli.cli.tag import TagArgs, cmd_tag
from contree_cli.client import ApiError
from contree_cli.session import SessionStore

IMG_UUID = "a1b2c3d4-5678-9abc-def0-111111111111"
IMG_UUID_2 = "a1b2c3d4-0000-0000-0000-000000000002"
IMG_UUID_3 = "a1b2c3d4-0000-0000-0000-000000000003"

_DEFAULT_TAG_BODY = {
    "uuid": IMG_UUID,
    "tag": "latest",
    "created_at": "2025-01-01T00:00:00Z",
}


def _run_cmd(
    tc: ContreeTestClient,
    image_ref=IMG_UUID,
    tag="latest",
    *,
    delete=False,
    status=200,
    body=None,
):
    tc.respond_json(body or _DEFAULT_TAG_BODY, status=status)
    ctx = copy_context()
    args = TagArgs(tag=tag, image_ref=image_ref, delete=delete)
    result = ctx.run(cmd_tag, args)
    return result


class TestCmdTag:
    def test_sends_patch(self, contree_client):
        _run_cmd(contree_client, IMG_UUID, "v1.0")
        req = contree_client.get_request(0)
        assert req.method == "PATCH"
        assert req.path == f"/v1/images/{IMG_UUID}/tag"

    def test_request_body(self, contree_client):
        _run_cmd(contree_client, IMG_UUID, "latest")
        req = contree_client.get_request(0)
        assert json.loads(req.body) == {"tag": "latest"}

    def test_returns_none_on_success(self, contree_client):
        result = _run_cmd(contree_client)
        assert result is None

    def test_logs_success(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            _run_cmd(contree_client, IMG_UUID_2, "prod")
        assert f"Tagged image {IMG_UUID_2} as prod" in caplog.text

    def test_not_found_raises(self, contree_client):
        contree_client.respond(status=404, body=b"image not found")
        ctx = copy_context()
        args = TagArgs(tag="latest", image_ref="bad-uuid")
        with pytest.raises(ApiError) as exc_info:
            ctx.run(cmd_tag, args)
        assert exc_info.value.status == 404


class TestCmdTagDelete:
    def test_sends_delete(self, contree_client):
        _run_cmd(contree_client, IMG_UUID, delete=True)
        req = contree_client.get_request(0)
        assert req.method == "DELETE"
        assert req.path == f"/v1/images/{IMG_UUID}/tag?tag=latest"

    def test_returns_none_on_success(self, contree_client):
        result = _run_cmd(contree_client, delete=True)
        assert result is None

    def test_logs_removal(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            _run_cmd(contree_client, IMG_UUID_3, delete=True)
        assert f"Removed tag 'latest' from image {IMG_UUID_3}" in caplog.text

    def test_delete_includes_tag_in_query(self, contree_client):
        result = _run_cmd(contree_client, IMG_UUID, "mytag", delete=True)
        req = contree_client.get_request(0)
        assert req.method == "DELETE"
        assert req.path == f"/v1/images/{IMG_UUID}/tag?tag=mytag"
        assert result is None


class TestCmdTagCurrentImage:
    def test_tags_current_session_image(self, contree_client, session_store):
        session_store.set_image(IMG_UUID, kind="test")
        SESSION_STORE.set(session_store)
        contree_client.respond_json(_DEFAULT_TAG_BODY)
        ctx = copy_context()
        args = TagArgs(tag="my-tag", image_ref=None)
        result = ctx.run(cmd_tag, args)
        assert result is None
        req = contree_client.get_request(0)
        assert req.method == "PATCH"
        assert req.path == f"/v1/images/{IMG_UUID}/tag"

    def test_no_session_returns_error(self, contree_client, tmp_path):
        store = SessionStore(tmp_path / "empty.db", "no-image")
        SESSION_STORE.set(store)
        ctx = copy_context()
        args = TagArgs(tag="my-tag", image_ref=None)
        result = ctx.run(cmd_tag, args)
        assert result == 1
        store.close()

    def test_from_args_one_arg(self) -> None:
        import argparse

        ns = argparse.Namespace(args=["my-tag"], delete=False)
        args = TagArgs.from_args(ns)
        assert args.tag == "my-tag"
        assert args.image_ref is None

    def test_from_args_two_args(self) -> None:
        import argparse

        ns = argparse.Namespace(args=[IMG_UUID, "my-tag"], delete=False)
        args = TagArgs.from_args(ns)
        assert args.tag == "my-tag"
        assert args.image_ref == IMG_UUID
