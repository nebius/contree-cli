from __future__ import annotations

from contextvars import copy_context

import pytest
from conftest import ContreeTestClient

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.arguments import parser
from contree_cli.cli.operation import (
    ACTIVE_STATUSES,
    CancelArgs,
    ShowMultiArgs,
    cmd_cancel,
    cmd_show_multi,
)
from contree_cli.output import CSVFormatter, JSONFormatter
from contree_cli.session import SessionStore


def make_op(
    uuid: str = "op-1",
    *,
    status: str = "SUCCESS",
    kind: str = "instance",
    duration: float = 1.5,
    error: str | None = None,
    image: str = "img-1",
    tag: str = "latest",
) -> dict:
    return {
        "uuid": uuid,
        "kind": kind,
        "status": status,
        "error": error,
        "duration": duration,
        "metadata": {"result": None},
        "result": {"image": image, "tag": tag, "duration": None},
        "created_at": "2025-06-01T00:00:00Z",
    }


def run_show_multi(
    tc: ContreeTestClient,
    ops: list[dict],
    *,
    formatter=None,
    store: SessionStore,
) -> int | None:
    for op in ops:
        tc.respond_json(op)
    FORMATTER.set(formatter or CSVFormatter())
    SESSION_STORE.set(store)
    ctx = copy_context()
    args = ShowMultiArgs(uuids=[op["uuid"] for op in ops])
    return ctx.run(cmd_show_multi, args)


def run_cancel(
    tc: ContreeTestClient,
    *,
    uuids: list[str] | None = None,
    all_flag: bool = False,
    list_pages: list[list[dict]] | None = None,
    delete_statuses: list[int] | None = None,
) -> int | None:
    if list_pages is not None:
        for page in list_pages:
            tc.respond_json(page)
    for status in delete_statuses or []:
        tc.respond(status=status, body=b"")
    CLIENT.set(tc)
    ctx = copy_context()
    args = CancelArgs(uuids=uuids or [], all=all_flag)
    return ctx.run(cmd_cancel, args)


# ----------------------------------------------------------------------
# argparse wiring
# ----------------------------------------------------------------------


class TestArgparseWiring:
    def test_op_alias_resolves_to_operation(self):
        ns = parser.parse_args(["op", "ls"])
        assert ns.command in ("operation", "op")
        assert ns.operation_action == "ls"

    def test_show_requires_at_least_one_uuid(self, capsys):
        with pytest.raises(SystemExit):
            parser.parse_args(["op", "show"])
        err = capsys.readouterr().err
        assert "uuids" in err.lower() or "required" in err.lower()

    def test_show_accepts_multiple_uuids(self):
        ns = parser.parse_args(["op", "show", "a", "b", "c"])
        assert ns.uuids == ["a", "b", "c"]
        assert ns.handler is cmd_show_multi

    def test_cancel_accepts_multiple_uuids(self):
        ns = parser.parse_args(["op", "cancel", "x", "y"])
        assert ns.uuids == ["x", "y"]
        assert ns.all is False
        assert ns.handler is cmd_cancel

    def test_cancel_all_flag(self):
        ns = parser.parse_args(["op", "cancel", "--all"])
        assert ns.all is True
        assert ns.uuids == []

    def test_list_delegates_to_cmd_list(self):
        from contree_cli.cli.operation import cmd_list

        ns = parser.parse_args(["op", "list", "-q"])
        assert ns.handler is cmd_list
        assert ns.quiet is True

    def test_list_ls_alias(self):
        from contree_cli.cli.operation import cmd_list

        ns = parser.parse_args(["op", "ls"])
        assert ns.handler is cmd_list

    def test_ps_shares_handler_with_op_list(self):
        """`contree ps` is a top-level shortcut for `contree op list`."""
        from contree_cli.cli.operation import cmd_list

        ns = parser.parse_args(["ps"])
        assert ns.handler is cmd_list

    def test_show_sh_alias(self):
        from contree_cli.cli.operation import cmd_show_multi

        ns = parser.parse_args(["op", "sh", "uuid-1"])
        assert ns.handler is cmd_show_multi
        assert ns.uuids == ["uuid-1"]

    def test_cancel_kill_alias(self):
        from contree_cli.cli.operation import cmd_cancel

        ns = parser.parse_args(["op", "kill", "uuid-1"])
        assert ns.handler is cmd_cancel
        assert ns.uuids == ["uuid-1"]

    def test_cancel_k_alias(self):
        from contree_cli.cli.operation import cmd_cancel

        ns = parser.parse_args(["op", "k", "uuid-1"])
        assert ns.handler is cmd_cancel
        assert ns.uuids == ["uuid-1"]


# ----------------------------------------------------------------------
# op show
# ----------------------------------------------------------------------


class TestOperationShow:
    def test_show_single_uuid(self, contree_client, session_store, capsys):
        rc = run_show_multi(
            contree_client,
            [make_op("op-a")],
            formatter=JSONFormatter(),
            store=session_store,
        )
        assert rc is None
        out = capsys.readouterr().out
        assert "op-a" in out
        assert contree_client.request_count == 1

    def test_show_multiple_uuids_issues_one_get_per_uuid(
        self, contree_client, session_store, capsys
    ):
        ops = [make_op("op-a"), make_op("op-b"), make_op("op-c")]
        rc = run_show_multi(
            contree_client,
            ops,
            formatter=JSONFormatter(),
            store=session_store,
        )
        assert rc is None
        assert contree_client.request_count == 3
        out = capsys.readouterr().out
        assert "op-a" in out
        assert "op-b" in out
        assert "op-c" in out
        # All three are GETs on /v1/operations/{uuid}
        for i, op in enumerate(ops):
            req = contree_client.get_request(i)
            assert req.method == "GET"
            assert req.path == f"/v1/operations/{op['uuid']}"

    def test_show_continues_on_api_error(
        self, contree_client, session_store, caplog, capsys
    ):
        # First UUID -> 404, then a successful one
        contree_client.respond(status=404, body=b"not found")
        contree_client.respond_json(make_op("op-b"))

        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        args = ShowMultiArgs(uuids=["op-a", "op-b"])

        with caplog.at_level("ERROR"):
            rc = ctx.run(cmd_show_multi, args)

        assert rc == 1
        assert "Failed to fetch op-a" in caplog.text
        out = capsys.readouterr().out
        # Second UUID still got rendered
        assert "op-b" in out

    def test_show_history_reference_uses_session_store(
        self, contree_client, session_store
    ):
        # Seed a history entry tied to a known op UUID, then reference it as @1
        session_store.set_image("img-1", kind="use", title="use img-1")
        session_store.set_image(
            "img-2",
            kind="run",
            title="echo hi",
            operation_uuid="op-from-history",
        )
        contree_client.respond_json(make_op("op-from-history"))

        FORMATTER.set(CSVFormatter())
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        args = ShowMultiArgs(uuids=["@2"])
        rc = ctx.run(cmd_show_multi, args)

        assert rc is None
        assert contree_client.request_count == 1
        assert contree_client.get_request(0).path == "/v1/operations/op-from-history"


# ----------------------------------------------------------------------
# op cancel
# ----------------------------------------------------------------------


class TestOperationCancel:
    def test_cancel_single_uuid(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a"],
                delete_statuses=[202],
            )
        assert rc is None
        assert contree_client.request_count == 1
        req = contree_client.get_request(0)
        assert req.method == "DELETE"
        assert req.path == "/v1/operations/op-a"
        assert "Cancelled operation op-a" in caplog.text

    def test_cancel_multiple_uuids(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a", "op-b", "op-c"],
                delete_statuses=[202, 202, 202],
            )
        assert rc is None
        assert contree_client.request_count == 3
        for i, uuid in enumerate(["op-a", "op-b", "op-c"]):
            req = contree_client.get_request(i)
            assert req.method == "DELETE"
            assert req.path == f"/v1/operations/{uuid}"

    def test_cancel_continues_on_error(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a", "op-b"],
                delete_statuses=[409, 202],
            )
        assert rc == 1
        assert "Failed to cancel op-a" in caplog.text
        assert "Cancelled operation op-b" in caplog.text

    def test_cancel_requires_uuids_or_all(self, contree_client, caplog):
        with caplog.at_level("ERROR"):
            rc = run_cancel(contree_client)
        assert rc == 1
        assert "Provide at least one UUID" in caplog.text
        assert contree_client.request_count == 0

    def test_cancel_all_iterates_active_statuses(self, contree_client, caplog):
        # One op per active status, then DELETE for each
        list_pages = [[{"uuid": f"{s.lower()}-0"}] for s in ACTIVE_STATUSES]
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                all_flag=True,
                list_pages=list_pages,
                delete_statuses=[202] * len(ACTIVE_STATUSES),
            )
        assert rc is None
        # 3 GETs + 3 DELETEs (one per active status)
        assert contree_client.request_count == 2 * len(ACTIVE_STATUSES)
        for status in ACTIVE_STATUSES:
            assert f"Cancelled operation {status.lower()}-0" in caplog.text

    def test_cancel_all_with_no_active(self, contree_client, caplog):
        list_pages = [[] for _ in ACTIVE_STATUSES]
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                all_flag=True,
                list_pages=list_pages,
            )
        assert rc is None
        # Only GETs, no DELETEs
        assert contree_client.request_count == len(ACTIVE_STATUSES)
        assert "No active operations" in caplog.text

    def test_cancel_all_overrides_explicit_uuids(self, contree_client, caplog):
        """--all wins; explicit UUIDs are ignored with a WARNING."""
        list_pages = [[{"uuid": "pending-0"}]] + [
            [] for _ in range(len(ACTIVE_STATUSES) - 1)
        ]
        with caplog.at_level("WARNING"):
            rc = run_cancel(
                contree_client,
                uuids=["ignored-1", "ignored-2"],
                all_flag=True,
                list_pages=list_pages,
                delete_statuses=[202],
            )
        assert rc is None
        assert "--all overrides explicit UUIDs" in caplog.text
        # Only one DELETE went out -- for pending-0, not the ignored UUIDs
        deletes = [r for r in contree_client.fake.requests if r.method == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0].path == "/v1/operations/pending-0"
