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
    WaitArgs,
    cmd_cancel,
    cmd_show_multi,
    cmd_wait,
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

    def test_show_raw_multi_uuid_emits_jsonl(
        self, contree_client, session_store, capsys
    ):
        # Multi-UUID `op show --raw` should produce one JSON line per
        # operation so the output streams cleanly into `jq -c`.
        import json as _json

        ops = [make_op("op-a"), make_op("op-b"), make_op("op-c")]
        for op in ops:
            contree_client.respond_json(op)
        FORMATTER.set(JSONFormatter())
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        rc = ctx.run(
            cmd_show_multi,
            ShowMultiArgs(uuids=[op["uuid"] for op in ops], raw=True),
        )
        assert rc is None
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        parsed = [_json.loads(line) for line in lines]
        assert [p["uuid"] for p in parsed] == ["op-a", "op-b", "op-c"]


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


# ----------------------------------------------------------------------
# op wait
# ----------------------------------------------------------------------


def _wait_op(uuid: str, status: str = "SUCCESS", duration: float = 1.0) -> dict:
    return {
        "uuid": uuid,
        "kind": "instance",
        "status": status,
        "duration": duration,
        "error": None,
    }


class TestOperationWait:
    def test_argparse_wait_alias(self):
        ns = parser.parse_args(["op", "w", "op-1"])
        assert ns.handler is cmd_wait
        assert ns.uuids == ["op-1"]

    def test_argparse_wait_default_timeout(self):
        ns = parser.parse_args(["op", "wait", "op-1"])
        assert ns.timeout == 60

    def test_wait_returns_none_on_terminal_success(self, contree_client, monkeypatch):
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        contree_client.respond_json(_wait_op("op-1", status="SUCCESS"))

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-1"], timeout=60))
        assert rc is None
        assert contree_client.request_count == 1

    def test_wait_failed_op_returns_exit_code_one(
        self, contree_client, monkeypatch, capsys
    ):
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        contree_client.respond_json(_wait_op("op-fail", status="FAILED"))

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-fail"], timeout=60))
        assert rc == 1
        import json as _json

        data = _json.loads(capsys.readouterr().out)
        assert data["status"] == "FAILED"
        assert data["timed_out"] is False

    def test_wait_success_with_nonzero_exit_code_preserves_status(
        self, contree_client, monkeypatch, capsys
    ):
        """Operation status is the server's word; it is NOT promoted to
        FAILED when the sandbox process exited non-zero. The exit_code
        is shown separately and propagated to the CLI's exit code so
        `op wait && next-step` still composes correctly."""
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        op = _wait_op("op-false", status="SUCCESS")
        op["metadata"] = {"result": {"state": {"exit_code": 1}}}
        contree_client.respond_json(op)

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-false"], timeout=60))
        assert rc == 1
        import json as _json

        data = _json.loads(capsys.readouterr().out)
        assert data["status"] == "SUCCESS"
        assert data["exit_code"] == 1
        assert data["timed_out"] is False

    def test_wait_propagates_specific_exit_code(self, contree_client, monkeypatch):
        """Like `session wait`, propagate the actual process exit code so
        `op wait foo && next-step` composes correctly with the underlying
        sandbox command's status."""
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        op = _wait_op("op-42", status="SUCCESS")
        op["metadata"] = {"result": {"state": {"exit_code": 42}}}
        contree_client.respond_json(op)

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-42"], timeout=60))
        assert rc == 42

    def test_wait_emits_timed_out_column(
        self, contree_client, monkeypatch, capsys, caplog
    ):
        # `time.monotonic` returns a value past the deadline on the second
        # call, simulating a real-world timeout without sleeping.
        clock = iter([0.0, 0.0, 0.5, 100.0, 100.0, 100.0, 100.0])
        monkeypatch.setattr(
            "contree_cli.cli.operation.time.monotonic", lambda: next(clock)
        )
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        # Poll: returns EXECUTING (not terminal). Second fetch (post-deadline)
        # picks up the same op for the timed-out row.
        contree_client.respond_json(_wait_op("op-slow", status="EXECUTING"))
        contree_client.respond_json(_wait_op("op-slow", status="EXECUTING"))

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("WARNING"):
            rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-slow"], timeout=1))

        assert rc == 1
        import json as _json

        data = _json.loads(capsys.readouterr().out)
        assert data["uuid"] == "op-slow"
        assert data["status"] == "EXECUTING"
        assert data["timed_out"] is True
        assert "Timeout" in caplog.text

    def test_wait_no_args_no_all_errors(self, contree_client, caplog):
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("ERROR"):
            rc = ctx.run(cmd_wait, WaitArgs(uuids=[], all=False, timeout=60))
        assert rc == 1
        assert "at least one UUID" in caplog.text

    def test_wait_all_with_no_active(self, contree_client, monkeypatch, caplog):
        # list_active returns no UUIDs after polling each ACTIVE_STATUS once.
        for _ in ACTIVE_STATUSES:
            contree_client.respond_json([])

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("INFO"):
            rc = ctx.run(cmd_wait, WaitArgs(uuids=[], all=True, timeout=60))
        assert rc is None
        assert "No active operations to wait for" in caplog.text


# ----------------------------------------------------------------------
# argparse + from_args integration -- the parsing/resolution itself is
# tested exhaustively in tests/test_refs.py; here we just verify each
# subcommand's argparse Namespace flows through resolve_operation_uuids() so it
# accepts whitespace-joined UUID strings (a common agent quoting bug).
# ----------------------------------------------------------------------


UUID_A = "019e3fb6-e2d8-7350-a8f9-8b2b5ebfda7f"
UUID_B = "019e3fb6-e447-760d-b7ab-62ef51f91b1f"
UUID_C = "019e3fb6-e5c3-7184-96f1-f7d56453a193"


class TestArgsFromNamespace:
    def test_wait_one_quoted_string_of_uuids(self, session_store):
        ns = parser.parse_args(["op", "wait", f"{UUID_A} {UUID_B} {UUID_C}"])
        SESSION_STORE.set(session_store)
        args = copy_context().run(WaitArgs.from_args, ns)
        assert args.uuids == [UUID_A, UUID_B, UUID_C]

    def test_cancel_one_quoted_string_of_uuids(self, session_store):
        ns = parser.parse_args(["op", "cancel", f"{UUID_A} {UUID_B}"])
        SESSION_STORE.set(session_store)
        args = copy_context().run(CancelArgs.from_args, ns)
        assert args.uuids == [UUID_A, UUID_B]

    def test_show_one_quoted_string_of_uuids(self, session_store):
        ns = parser.parse_args(["op", "show", f"{UUID_A} {UUID_B}"])
        SESSION_STORE.set(session_store)
        args = copy_context().run(ShowMultiArgs.from_args, ns)
        assert args.uuids == [UUID_A, UUID_B]

    def test_show_resolves_history_ref_to_real_uuid(self, session_store):
        # @N is no longer passed through verbatim -- from_args resolves
        # it against the active session and returns the real UUID.
        session_store.set_image("img-1", kind="use")
        session_store.set_image("img-2", kind="run", operation_uuid=UUID_A)
        ns = parser.parse_args(["op", "show", "@2"])
        SESSION_STORE.set(session_store)
        args = copy_context().run(ShowMultiArgs.from_args, ns)
        assert args.uuids == [UUID_A]

    def test_wait_with_garbage_uuid_raises(self, session_store):
        ns = parser.parse_args(["op", "wait", "definitely-not-uuid"])
        SESSION_STORE.set(session_store)
        with pytest.raises(ValueError, match="Invalid operation reference"):
            copy_context().run(WaitArgs.from_args, ns)
