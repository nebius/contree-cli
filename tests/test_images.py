from __future__ import annotations

import json
from contextvars import copy_context
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient
from contree_client.models import Image, OperationResponse

from contree_cli import CLIENT, FORMATTER
from contree_cli.cli.images import (
    LIMIT_DEFAULT,
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
    """Run cmd_images against a mocked image stream."""
    _mock_images(tc, images)

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
        calls = contree_client.calls_for("iter_images")
        assert calls[0].kwargs["tag"] == "ubuntu"

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

    def test_unknown_field_dropped(self, contree_client, capsys):
        """Fields absent from the typed Image model (e.g. ``size``,
        ``digest``) are dropped by the contree-client parser; only the
        model's known fields reach the row."""
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
        assert parsed["uuid"] == "ggg"
        assert parsed["tag"] == "v4"
        assert "size" not in parsed
        assert "digest" not in parsed

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
        calls = contree_client.calls_for("iter_images")
        assert calls[0].kwargs["uuid"] == "abc-123"

    def test_default_tagged_only_param(self, contree_client):
        _run_cmd(contree_client, [])
        calls = contree_client.calls_for("iter_images")
        assert calls[0].kwargs["tagged"] is True

    def test_all_param_disables_tagged_filter(self, contree_client):
        _run_cmd(contree_client, [], all_images=True)
        calls = contree_client.calls_for("iter_images")
        assert calls[0].kwargs["tagged"] is False

    def test_since_param(self, contree_client):
        """The parsed datetime goes to the client as-is; the library
        owns the wire formatting (format_time_param)."""
        from datetime import datetime

        _run_cmd(contree_client, [], since="1h")
        calls = contree_client.calls_for("iter_images")
        assert isinstance(calls[0].kwargs["since"], datetime)

    def test_until_param(self, contree_client):
        from datetime import datetime

        _run_cmd(contree_client, [], until="2025-01-01")
        calls = contree_client.calls_for("iter_images")
        assert isinstance(calls[0].kwargs["until"], datetime)


def _make_image(i: int) -> dict:
    return {"uuid": f"uuid-{i}", "tag": None, "created_at": "2025-01-01T00:00:00Z"}


def _mock_images(tc: ContreeTestClient, images: list[dict]) -> None:
    tc.mock("iter_images", [Image.from_dict(image) for image in images])


class TestImagesPagination:
    """Offset pagination is the library's job (iter_images); the CLI
    contract is a single iterator pass with the record budget
    forwarded as limit and truncation detected via one extra item."""

    def test_single_iterator_pass(self, contree_client, capsys):
        images = [_make_image(i) for i in range(5)]
        _run_cmd(contree_client, images)
        assert len(contree_client.calls_for("iter_images")) == 1

    def test_limit_forwarded_with_probe(self, contree_client):
        _run_cmd(contree_client, [], limit=7)
        calls = contree_client.calls_for("iter_images")
        assert calls[0].kwargs["limit"] == 8

    def test_empty_stream(self, contree_client, capsys):
        """No output when the stream is empty."""
        _run_cmd(contree_client, [])
        assert capsys.readouterr().out == ""

    def test_all_images_emitted(self, contree_client, capsys):
        images = [_make_image(i) for i in range(25)]
        _run_cmd(contree_client, images)
        out = capsys.readouterr().out
        assert out.count("uuid-") == 25

    def test_progress_logged_per_page(self, contree_client, caplog):
        """Every consumed page reports progress, so a long listing does
        not look hung while the next page loads."""
        import logging

        images = [_make_image(i) for i in range(1500)]
        with caplog.at_level(logging.INFO, logger="contree_cli.cli.images"):
            _run_cmd(contree_client, images, limit=3000)
        progress = [
            r.getMessage() for r in caplog.records if "loading more" in r.getMessage()
        ]
        assert progress == ["Fetched 1000 images, loading more..."]

    def test_default_limit_matches_constant(self):
        assert LIMIT_DEFAULT > 0
        assert ImagesArgs().limit == LIMIT_DEFAULT

    def test_limit_truncates_with_warning(self, contree_client, caplog):
        """An extra record past --limit means truncation -> warning."""
        import logging

        _mock_images(contree_client, [_make_image(i) for i in range(6)])

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=5))
        msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("truncated" in m and "--limit=5" in m for m in msgs)

    def test_limit_warning_after_table_flush(self, contree_client, caplog, capsys):
        """TableFormatter buffer is flushed before the truncation warning."""
        import logging

        _mock_images(contree_client, [_make_image(i) for i in range(6)])

        FORMATTER.set(TableFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=5))

        out = capsys.readouterr().out
        # Table content must be printed (i.e. flushed) before the handler
        # logs the warning. Verify the table is on stdout already.
        assert "uuid-0" in out
        assert "uuid-4" in out

    def test_limit_no_warning_when_stream_fits(self, contree_client, caplog):
        """Exactly --limit records -> no warning."""
        import logging

        _mock_images(contree_client, [_make_image(i) for i in range(5)])

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        with caplog.at_level(logging.WARNING, logger="contree_cli.cli.images"):
            ctx.run(cmd_images, ImagesArgs(limit=5))
        warns = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("truncated" in r.getMessage() for r in warns)

    def test_limit_emits_only_limit_records(self, contree_client, capsys):
        """The stream past --limit is not emitted."""
        _mock_images(contree_client, [_make_image(i) for i in range(10)])

        FORMATTER.set(CSVFormatter())
        ctx = copy_context()
        ctx.run(cmd_images, ImagesArgs(limit=3))

        out = capsys.readouterr().out
        # 1 header row + 3 data rows.
        assert len(out.strip().splitlines()) == 4


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


def _op_response(
    uuid: str, status: str = "PENDING", image: str = ""
) -> OperationResponse:
    result = {"image": image} if image else {}
    return OperationResponse.from_dict(
        {"uuid": uuid, "status": status, "result": result}
    )


def _run_import(tc: ContreeTestClient, refs: list[str], *, formatter=None, **kwargs):
    """Run cmd_import with mocked client and time.sleep."""
    FORMATTER.set(formatter or CSVFormatter())
    ctx = copy_context()
    args = ImportArgs(refs=refs, **kwargs)
    with patch("contree_cli.cli.images.time.sleep"):
        return ctx.run(cmd_import, args)


class TestCmdImport:
    def test_single_import_success(self, contree_client, capsys):
        contree_client.mock("import_image", "op-1")
        contree_client.mock(
            "get_operation_status", _op_response("op-1", "SUCCESS", "img-1")
        )

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc is None
        assert len(contree_client.calls_for("import_image")) == 1
        polls = contree_client.calls_for("get_operation_status")
        assert polls[0].args == ("op-1",)
        out = capsys.readouterr().out
        assert "op-1" in out

    def test_normalized_url_in_request(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("get_operation_status", _op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest"])

        call = contree_client.calls_for("import_image")[0]
        registry = call.args[0]
        assert registry.url == "docker://docker.io/library/ubuntu:latest"
        assert registry.credentials is ...
        assert call.kwargs["tag"] == "ubuntu:latest"
        assert call.kwargs["timeout"] is ...

    def test_timeout_included_in_request(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("get_operation_status", _op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest"], timeout=60)

        call = contree_client.calls_for("import_image")[0]
        assert call.kwargs["timeout"] == 60

    def test_brace_expansion_multiple(self, contree_client, capsys):
        # 3 import operations
        contree_client.mock("import_image", "op-1")
        contree_client.mock("import_image", "op-2")
        contree_client.mock("import_image", "op-3")
        # 3 poll responses (all terminal on first poll)
        contree_client.mock(
            "get_operation_status", _op_response("op-1", "SUCCESS", "img-1")
        )
        contree_client.mock(
            "get_operation_status", _op_response("op-2", "SUCCESS", "img-2")
        )
        contree_client.mock(
            "get_operation_status", _op_response("op-3", "SUCCESS", "img-3")
        )

        rc = _run_import(contree_client, ["ubuntu:{latest,noble,jammy}"])

        assert rc is None
        assert len(contree_client.calls_for("import_image")) == 3
        assert len(contree_client.calls_for("get_operation_status")) == 3
        out = capsys.readouterr().out
        assert "op-1" in out
        assert "op-2" in out
        assert "op-3" in out

    def test_polls_until_terminal(self, contree_client, capsys):
        contree_client.mock("import_image", "op-1")
        # First poll: still pending
        contree_client.mock("get_operation_status", _op_response("op-1", "EXECUTING"))
        # Second poll: done
        contree_client.mock(
            "get_operation_status", _op_response("op-1", "SUCCESS", "img-1")
        )

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc is None
        assert len(contree_client.calls_for("import_image")) == 1
        assert len(contree_client.calls_for("get_operation_status")) == 2

    def test_failed_import_returns_1(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("get_operation_status", _op_response("op-1", "FAILED"))

        rc = _run_import(contree_client, ["ubuntu:latest"])

        assert rc == 1

    def test_keyboard_interrupt_cancels_all(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("import_image", "op-2")
        contree_client.mock("cancel_operation", None)

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

        cancels = contree_client.calls_for("cancel_operation")
        assert [c.args for c in cancels] == [("op-1",), ("op-2",)]

    def test_explicit_tag(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("get_operation_status", _op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ubuntu:latest?tag=myubuntu:test"])

        call = contree_client.calls_for("import_image")[0]
        assert call.args[0].url == "docker://docker.io/library/ubuntu:latest"
        assert call.kwargs["tag"] == "myubuntu:test"

    def test_ghcr_implicit_tag(self, contree_client):
        contree_client.mock("import_image", "op-1")
        contree_client.mock("get_operation_status", _op_response("op-1", "SUCCESS"))

        _run_import(contree_client, ["ghcr.io/ubuntu/ubuntu:latest"])

        call = contree_client.calls_for("import_image")[0]
        assert call.args[0].url == "docker://ghcr.io/ubuntu/ubuntu:latest"
        assert call.kwargs["tag"] == "ubuntu/ubuntu:latest"
