from __future__ import annotations

import json
from contextvars import copy_context
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient

from contree_cli import CLIENT, FORMATTER
from contree_cli.cli.images import (
    LIMIT_DEFAULT,
    PAGE_SIZE,
    ImagesArgs,
    ImportArgs,
    _derive_tag,
    _parse_explicit_tag,
    cmd_images,
    cmd_import,
    expand_braces,
    normalize_registry_url,
)
from contree_cli.output import CSVFormatter, JSONFormatter, TableFormatter
from contree_cli.types import parse_interval


def _run_cmd(tc: ContreeTestClient, images, *, formatter=None, **kwargs):
    """Run cmd_images with a single-page response."""
    return _run_cmd_pages(tc, [images], formatter=formatter, **kwargs)


def _run_cmd_pages(tc: ContreeTestClient, pages, *, formatter=None, **kwargs):
    """Run cmd_images with multiple pages of responses."""
    for page in pages:
        tc.respond_json({"images": page})

    FORMATTER.set(formatter or CSVFormatter())
    ctx = copy_context()

    if "since" in kwargs and isinstance(kwargs["since"], str):
        kwargs["since"] = parse_interval(kwargs["since"])
    if "until" in kwargs and isinstance(kwargs["until"], str):
        kwargs["until"] = parse_interval(kwargs["until"])

    args = ImagesArgs(**kwargs)
    ctx.run(cmd_images, args)


class TestCmdImages:
    def test_lists_images(self, contree_client, capsys):
        images = [
            {"uuid": "aaa", "tag": "latest", "created_at": "2025-01-01T00:00:00Z"},
            {"uuid": "bbb", "tag": None, "created_at": "2025-01-02T00:00:00Z"},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "aaa" in out
        assert "latest" in out
        assert "bbb" in out

    def test_prefix_passed_as_tag_param(self, contree_client):
        _run_cmd(contree_client, [], prefix="ubuntu")
        assert "tag=ubuntu" in contree_client.request_paths[0]

    def test_null_tag_shown_as_empty(self, contree_client, capsys):
        images = [
            {"uuid": "ccc", "tag": None, "created_at": "2025-01-01T00:00:00Z"},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        lines = out.splitlines()
        data_line = lines[1]
        assert "ccc" in data_line
        # null tag renders as empty in CSV: two consecutive delimiters
        # around its position.
        assert ",," in data_line

    def test_empty_list(self, contree_client, capsys):
        _run_cmd(contree_client, [])
        assert capsys.readouterr().out == ""

    def test_json_output(self, contree_client, capsys):
        images = [
            {"uuid": "ddd", "tag": "v1", "created_at": "2025-06-01T00:00:00Z"},
        ]
        _run_cmd(contree_client, images, formatter=JSONFormatter())
        line = capsys.readouterr().out.strip()
        parsed = json.loads(line)
        assert parsed["uuid"] == "ddd"
        assert parsed["tag"] == "v1"

    def test_table_output(self, contree_client, capsys):
        images = [
            {"uuid": "eee", "tag": "v2", "created_at": "2025-06-01T00:00:00Z"},
            {"uuid": "fff", "tag": "v3", "created_at": "2025-06-02T00:00:00Z"},
        ]
        fmt = TableFormatter()
        _run_cmd(contree_client, images, formatter=fmt)
        fmt.flush()
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 3  # header + 2 rows
        assert "UUID" in lines[0]

    def test_unknown_field_passes_through(self, contree_client, capsys):
        """New server fields (e.g. ``size``, ``digest``) reach the row as-is."""
        images = [
            {
                "uuid": "ggg",
                "tag": "v4",
                "created_at": "2025-06-01T00:00:00Z",
                "size": 12345,
                "digest": "sha256:abcd",
            },
        ]
        _run_cmd(contree_client, images, formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out.strip())
        assert parsed["size"] == 12345
        assert parsed["digest"] == "sha256:abcd"

    def test_nested_fields_skipped(self, contree_client, capsys):
        images = [
            {
                "uuid": "hhh",
                "tag": "v5",
                "created_at": "2025-06-01T00:00:00Z",
                "metadata": {"foo": "bar"},
                "tags": ["a", "b"],
            },
        ]
        _run_cmd(contree_client, images, formatter=JSONFormatter())
        parsed = json.loads(capsys.readouterr().out.strip())
        assert "metadata" not in parsed
        assert "tags" not in parsed
        assert parsed["uuid"] == "hhh"


class TestImagesParams:
    def test_uuid_param(self, contree_client):
        _run_cmd(contree_client, [], uuid="abc-123")
        assert "uuid=abc-123" in contree_client.request_paths[0]

    def test_default_tagged_only_param(self, contree_client):
        _run_cmd(contree_client, [])
        assert "tagged=1" in contree_client.request_paths[0]

    def test_all_param_disables_tagged_filter(self, contree_client):
        _run_cmd(contree_client, [], all_images=True)
        assert "tagged=1" not in contree_client.request_paths[0]

    def test_since_param(self, contree_client):
        _run_cmd(contree_client, [], since="1h")
        path = contree_client.request_paths[0]
        assert "since=" in path

    def test_until_param(self, contree_client):
        _run_cmd(contree_client, [], until="2025-01-01")
        path = contree_client.request_paths[0]
        assert "until=" in path


def _make_image(i: int) -> dict:
    return {"uuid": f"uuid-{i}", "tag": None, "created_at": "2025-01-01T00:00:00Z"}


class TestImagesPagination:
    def test_single_page_partial(self, contree_client, capsys):
        """A page smaller than PAGE_SIZE means no further requests."""
        images = [_make_image(i) for i in range(5)]
        _run_cmd(contree_client, images)
        assert contree_client.request_count == 1

    def test_two_full_pages(self, contree_client, capsys):
        """Two full pages + an empty third page."""
        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        page2 = [_make_image(i) for i in range(PAGE_SIZE, PAGE_SIZE + 3)]
        _run_cmd_pages(contree_client, [page1, page2])
        assert contree_client.request_count == 2
        out = capsys.readouterr().out
        assert f"uuid-{PAGE_SIZE + 2}" in out

    def test_offset_increments(self, contree_client):
        """Each page request increments offset by PAGE_SIZE."""
        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        page2 = []
        _run_cmd_pages(contree_client, [page1, page2])
        paths = contree_client.request_paths
        assert "offset=0" in paths[0]
        assert f"offset={PAGE_SIZE}" in paths[1]

    def test_empty_first_page(self, contree_client, capsys):
        """No output and only one request when first page is empty."""
        _run_cmd(contree_client, [])
        assert contree_client.request_count == 1
        assert capsys.readouterr().out == ""

    def test_all_images_emitted(self, contree_client, capsys):
        """All images across pages appear in output."""
        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        page2 = [_make_image(i) for i in range(PAGE_SIZE, PAGE_SIZE + 5)]
        _run_cmd_pages(contree_client, [page1, page2])
        out = capsys.readouterr().out
        assert out.count("uuid-") == PAGE_SIZE + 5

    def test_pages_flushed_progressively(self, contree_client, capsys):
        """Each full page is flushed as it completes (streaming output)."""
        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        page2 = [_make_image(i) for i in range(PAGE_SIZE, PAGE_SIZE * 2)]
        page3 = [_make_image(i) for i in range(PAGE_SIZE * 2, PAGE_SIZE * 2 + 3)]
        _run_cmd_pages(contree_client, [page1, page2, page3])
        out = capsys.readouterr().out
        assert f"uuid-{PAGE_SIZE - 1}" in out
        assert f"uuid-{PAGE_SIZE * 2 - 1}" in out
        assert f"uuid-{PAGE_SIZE * 2 + 2}" in out

    def test_default_limit_matches_constant(self):
        assert LIMIT_DEFAULT > 0
        assert ImagesArgs().limit == LIMIT_DEFAULT

    def test_limit_truncates_with_warning(self, contree_client, caplog):
        """Hitting --limit triggers a probe; non-empty probe -> warning."""
        import logging

        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        contree_client.respond_json({"images": page1})
        contree_client.respond_json({"images": [_make_image(PAGE_SIZE)]})

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=PAGE_SIZE))
        msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("truncated" in m and f"--limit={PAGE_SIZE}" in m for m in msgs)
        assert contree_client.request_count == 2

    def test_limit_probe_uses_skip_of_one(self, contree_client):
        """Probe is a single-record request, not a full page."""
        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        contree_client.respond_json({"images": page1})
        contree_client.respond_json({"images": []})

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        ctx.run(cmd_images, ImagesArgs(limit=PAGE_SIZE))

        probe_path = contree_client.request_paths[1]
        assert "limit=1" in probe_path
        assert f"offset={PAGE_SIZE}" in probe_path

    def test_limit_warning_after_table_flush(self, contree_client, caplog, capsys):
        """TableFormatter buffer is flushed before the truncation warning."""
        import logging

        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        contree_client.respond_json({"images": page1})
        contree_client.respond_json({"images": [_make_image(PAGE_SIZE)]})

        FORMATTER.set(TableFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=PAGE_SIZE))

        out = capsys.readouterr().out
        # Table content must be printed (i.e. flushed) before the handler
        # logs the warning. Verify the table is on stdout already.
        assert "uuid-0" in out
        assert f"uuid-{PAGE_SIZE - 1}" in out

    def test_limit_no_warning_when_no_more(self, contree_client, caplog):
        """Empty probe response -> no warning."""
        import logging

        page1 = [_make_image(i) for i in range(PAGE_SIZE)]
        contree_client.respond_json({"images": page1})
        contree_client.respond_json({"images": []})

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=PAGE_SIZE))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("truncated" in r.getMessage() for r in warns)

    def test_limit_smaller_than_page_size_emits_only_limit_records(
        self, contree_client, capsys
    ):
        """--limit < PAGE_SIZE: caller emits exactly limit records from the page."""
        contree_client.respond_json(
            {"images": [_make_image(i) for i in range(PAGE_SIZE)]}
        )

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        ctx.run(cmd_images, ImagesArgs(limit=3))

        out = capsys.readouterr().out
        # 1 header row + 3 data rows.
        assert len(out.strip().splitlines()) == 4

    def test_progress_not_logged_for_single_short_page(self, contree_client, caplog):
        """Final/only partial page does not emit progress (output covers it)."""
        import logging

        images = [_make_image(i) for i in range(5)]
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.images"):
            _run_cmd(contree_client, images)
        assert not any("images so far" in r.getMessage() for r in caplog.records)


class TestImagesCreatedAtFormats:
    """Verify created_at parsing with various ISO 8601 formats from the API."""

    def test_fractional_seconds_microseconds(self, contree_client, capsys):
        images = [
            {"uuid": "ts-1", "tag": None, "created_at": "2026-02-16T21:25:30.265927Z"},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "ts-1" in out

    def test_fractional_seconds_milliseconds(self, contree_client, capsys):
        images = [
            {"uuid": "ts-2", "tag": None, "created_at": "2025-03-15T10:00:00.123Z"},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "ts-2" in out

    def test_explicit_utc_offset(self, contree_client, capsys):
        ts = "2026-02-25T16:16:28.984413+00:00"
        images = [
            {"uuid": "ts-3", "tag": None, "created_at": ts},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "ts-3" in out

    def test_whole_seconds_z_suffix(self, contree_client, capsys):
        images = [
            {"uuid": "ts-4", "tag": None, "created_at": "2025-01-01T00:00:00Z"},
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "ts-4" in out

    def test_mixed_formats_in_single_page(self, contree_client, capsys):
        images = [
            {"uuid": "mix-1", "tag": "a", "created_at": "2025-01-01T00:00:00Z"},
            {"uuid": "mix-2", "tag": "b", "created_at": "2026-02-16T21:25:30.265927Z"},
            {
                "uuid": "mix-3",
                "tag": "c",
                "created_at": "2025-07-04T12:00:00.500+00:00",
            },
        ]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert "mix-1" in out
        assert "mix-2" in out
        assert "mix-3" in out


# ---------------------------------------------------------------------------
# expand_braces
# ---------------------------------------------------------------------------


class TestExpandBraces:
    def test_no_braces(self):
        assert expand_braces("ubuntu:latest") == ["ubuntu:latest"]

    def test_single_expansion(self):
        assert expand_braces("ubuntu:{latest,noble}") == [
            "ubuntu:latest",
            "ubuntu:noble",
        ]

    def test_triple_expansion(self):
        assert expand_braces("ubuntu:{latest,noble,jammy}") == [
            "ubuntu:latest",
            "ubuntu:noble",
            "ubuntu:jammy",
        ]

    def test_no_closing_brace(self):
        assert expand_braces("ubuntu:{latest") == ["ubuntu:{latest"]

    def test_empty_braces(self):
        assert expand_braces("ubuntu:{}") == ["ubuntu:"]

    def test_single_item_in_braces(self):
        assert expand_braces("ubuntu:{latest}") == ["ubuntu:latest"]


# ---------------------------------------------------------------------------
# normalize_registry_url
# ---------------------------------------------------------------------------


class TestNormalizeRegistryUrl:
    def test_bare_name_with_tag(self):
        assert (
            normalize_registry_url("ubuntu:latest")
            == "docker://docker.io/library/ubuntu:latest"
        )

    def test_bare_name_no_tag(self):
        assert (
            normalize_registry_url("ubuntu")
            == "docker://docker.io/library/ubuntu:latest"
        )

    def test_dockerhub_explicit(self):
        assert (
            normalize_registry_url("docker.io/ubuntu:latest")
            == "docker://docker.io/library/ubuntu:latest"
        )

    def test_dockerhub_with_scheme(self):
        assert (
            normalize_registry_url("docker://docker.io/ubuntu:latest")
            == "docker://docker.io/ubuntu:latest"
        )

    def test_ghcr(self):
        assert (
            normalize_registry_url("ghcr.io/ubuntu/ubuntu:latest")
            == "docker://ghcr.io/ubuntu/ubuntu:latest"
        )

    def test_user_slash_image(self):
        assert (
            normalize_registry_url("myuser/myimage:v1")
            == "docker://docker.io/myuser/myimage:v1"
        )

    def test_user_slash_image_no_tag(self):
        assert (
            normalize_registry_url("myuser/myimage")
            == "docker://docker.io/myuser/myimage:latest"
        )

    def test_dockerhub_with_scheme_and_library(self):
        assert (
            normalize_registry_url("docker://docker.io/library/ubuntu:latest")
            == "docker://docker.io/library/ubuntu:latest"
        )


# ---------------------------------------------------------------------------
# _parse_explicit_tag / _derive_tag
# ---------------------------------------------------------------------------


class TestParseExplicitTag:
    def test_no_tag(self):
        assert _parse_explicit_tag("ubuntu:latest") == ("ubuntu:latest", None)

    def test_with_tag(self):
        assert _parse_explicit_tag("ubuntu:latest?tag=myubuntu:test") == (
            "ubuntu:latest",
            "myubuntu:test",
        )

    def test_docker_scheme_with_tag(self):
        assert _parse_explicit_tag(
            "docker://docker.io/ubuntu:latest?tag=custom:v1"
        ) == ("docker://docker.io/ubuntu:latest", "custom:v1")


class TestDeriveTag:
    def test_bare_name(self):
        assert _derive_tag("ubuntu:latest") == "ubuntu:latest"

    def test_dockerhub(self):
        assert _derive_tag("docker.io/ubuntu:latest") == "ubuntu:latest"

    def test_dockerhub_with_scheme(self):
        assert _derive_tag("docker://docker.io/ubuntu:latest") == "ubuntu:latest"

    def test_ghcr(self):
        assert _derive_tag("ghcr.io/ubuntu/ubuntu:latest") == "ubuntu/ubuntu:latest"

    def test_user_slash_image(self):
        assert _derive_tag("myuser/myimage:v1") == "myuser/myimage:v1"


# ---------------------------------------------------------------------------
# cmd_import
# ---------------------------------------------------------------------------


def _op_response(uuid: str, status: str = "PENDING", image: str = ""):
    result = {"image": image} if image else {}
    return {"uuid": uuid, "status": status, "result": result}


def _run_import(tc: ContreeTestClient, refs: list[str], *, formatter=None, **kwargs):
    """Run cmd_import with mocked HTTP and time.sleep."""
    FORMATTER.set(formatter or CSVFormatter())
    ctx = copy_context()
    args = ImportArgs(refs=refs, **kwargs)
    with patch("contree_cli.cli.images.time.sleep"):
        return ctx.run(cmd_import, args)


class TestCmdImport:
    def test_single_import_success(self, contree_client, capsys):
        # POST response (operation created)
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        # GET poll response (terminal)
        contree_client.respond_json(
            _op_response("op-1", "SUCCESS", image="img-1"),
        )

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc is None
        paths = contree_client.request_paths
        assert "/v1/images/import" in paths[0]
        assert "/v1/operations/op-1" in paths[1]
        out = capsys.readouterr().out
        assert "op-1" in out

    def test_normalized_url_in_post_body(self, contree_client):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json(_op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest"])

        req = contree_client.get_request(0)
        payload = json.loads(req.body)
        assert payload["registry"]["url"] == "docker://docker.io/library/ubuntu:latest"
        assert payload["tag"] == "ubuntu:latest"
        assert "timeout" not in payload

    def test_timeout_included_in_post_body(self, contree_client):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json(_op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest"], timeout=60)

        req = contree_client.get_request(0)
        payload = json.loads(req.body)
        assert payload["timeout"] == 60

    def test_brace_expansion_multiple(self, contree_client, capsys):
        # 3 POST responses
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json({"uuid": "op-2"}, status=201)
        contree_client.respond_json({"uuid": "op-3"}, status=201)
        # 3 poll responses (all terminal on first poll)
        contree_client.respond_json(_op_response("op-1", "SUCCESS", "img-1"))
        contree_client.respond_json(_op_response("op-2", "SUCCESS", "img-2"))
        contree_client.respond_json(_op_response("op-3", "SUCCESS", "img-3"))

        rc = _run_import(contree_client, ["ubuntu:{latest,noble,jammy}"])

        assert rc is None
        # 3 POSTs + 3 GETs = 6 requests
        assert contree_client.request_count == 6
        out = capsys.readouterr().out
        assert "op-1" in out
        assert "op-2" in out
        assert "op-3" in out

    def test_polls_until_terminal(self, contree_client, capsys):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        # First poll: still pending
        contree_client.respond_json(_op_response("op-1", "EXECUTING"))
        # Second poll: done
        contree_client.respond_json(_op_response("op-1", "SUCCESS", "img-1"))

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc is None
        # 1 POST + 2 GETs
        assert contree_client.request_count == 3

    def test_failed_import_returns_1(self, contree_client):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json(
            _op_response("op-1", "FAILED"),
        )

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc == 1

    def test_keyboard_interrupt_cancels_all(self, contree_client):
        # POST responses
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json({"uuid": "op-2"}, status=201)
        # DELETE responses for cancellation
        contree_client.respond(status=200)
        contree_client.respond(status=200)

        FORMATTER.set(CSVFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        args = ImportArgs(refs=["ubuntu:latest", "nginx:latest"])

        with (
            patch(
                "contree_cli.cli.images.time.sleep",
                side_effect=KeyboardInterrupt,
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            ctx.run(cmd_import, args)

        # 2 POSTs + 2 DELETEs (cancellations)
        assert contree_client.request_count == 4
        paths = contree_client.request_paths
        assert "/v1/operations/op-1" in paths[2]
        assert "/v1/operations/op-2" in paths[3]

    def test_explicit_tag(self, contree_client):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json(_op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest?tag=myubuntu:test"])

        req = contree_client.get_request(0)
        payload = json.loads(req.body)
        assert payload["registry"]["url"] == "docker://docker.io/library/ubuntu:latest"
        assert payload["tag"] == "myubuntu:test"

    def test_ghcr_implicit_tag(self, contree_client):
        contree_client.respond_json({"uuid": "op-1"}, status=201)
        contree_client.respond_json(_op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ghcr.io/ubuntu/ubuntu:latest"])

        req = contree_client.get_request(0)
        payload = json.loads(req.body)
        assert payload["registry"]["url"] == "docker://ghcr.io/ubuntu/ubuntu:latest"
        assert payload["tag"] == "ubuntu/ubuntu:latest"
