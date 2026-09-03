from __future__ import annotations

import json
from contextvars import copy_context

import pytest
from conftest import ContreeTestClient
from contree_client.exceptions import APIStatusError
from contree_client.models import OperationEvent, OperationResponse, OperationSummary

from contree_cli import CLIENT, FORMATTER, SESSION_STORE
from contree_cli.arguments import parser
from contree_cli.cli.operation import (
    ACTIVE_STATUSES,
    CancelArgs,
    EventsArgs,
    ShowMultiArgs,
    WaitArgs,
    cmd_cancel,
    cmd_events,
    cmd_show_multi,
    cmd_wait,
)
from contree_cli.output import CSVFormatter, DefaultFormatter, JSONFormatter
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
    if kind == "instance":
        metadata = {
            "command": "echo hi",
            "image": "img-base",
            "shell": True,
            "result": None,
        }
    else:
        metadata = {
            "registry": {"url": "docker://docker.io/busybox:latest"},
            "tag": "busybox:latest",
        }
    return {
        "uuid": uuid,
        "kind": kind,
        "status": status,
        "error": error,
        "duration": duration,
        "metadata": metadata,
        "result": {"image": image, "tag": tag},
        "created_at": "2025-06-01T00:00:00Z",
    }


def mock_op(tc: ContreeTestClient, op: dict) -> None:
    tc.mock("get_operation_status", OperationResponse.from_dict(op))


def run_show_multi(
    tc: ContreeTestClient,
    ops: list[dict],
    *,
    formatter=None,
    store: SessionStore,
) -> int | None:
    for op in ops:
        mock_op(tc, op)
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
    cancel_outcomes: list[BaseException | None] | None = None,
) -> int | None:
    for page in list_pages or []:
        tc.mock(
            "list_operations",
            [OperationSummary.from_dict(op) for op in page],
        )
    for outcome in cancel_outcomes or []:
        if isinstance(outcome, BaseException):
            tc.mock("cancel_operation", error=outcome)
        else:
            tc.mock("cancel_operation", None)
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
        assert len(contree_client.calls_for("get_operation_status")) == 1

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
        calls = contree_client.calls_for("get_operation_status")
        assert len(calls) == 3
        out = capsys.readouterr().out
        assert "op-a" in out
        assert "op-b" in out
        assert "op-c" in out
        # One status fetch per UUID, in order
        for call, op in zip(calls, ops, strict=True):
            assert call.args == (op["uuid"],)

    def test_show_continues_on_api_error(
        self, contree_client, session_store, caplog, capsys
    ):
        # First UUID -> 404, then a successful one
        contree_client.mock(
            "get_operation_status", error=APIStatusError(404, "not found")
        )
        mock_op(contree_client, make_op("op-b"))

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
        mock_op(contree_client, make_op("op-from-history"))

        FORMATTER.set(CSVFormatter())
        SESSION_STORE.set(session_store)
        ctx = copy_context()
        args = ShowMultiArgs(uuids=["@2"])
        rc = ctx.run(cmd_show_multi, args)

        assert rc is None
        calls = contree_client.calls_for("get_operation_status")
        assert len(calls) == 1
        assert calls[0].args == ("op-from-history",)

    def test_show_raw_multi_uuid_emits_jsonl(
        self, contree_client, session_store, capsys
    ):
        # Multi-UUID `op show --raw` should produce one JSON line per
        # operation so the output streams cleanly into `jq -c`.
        import json as _json

        ops = [make_op("op-a"), make_op("op-b"), make_op("op-c")]
        for op in ops:
            mock_op(contree_client, op)
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
# op events
# ----------------------------------------------------------------------


def make_event(
    event_id: int, event_type: str, data: dict, *, spid: int = 1
) -> OperationEvent:
    return OperationEvent.from_dict(
        {
            "id": event_id,
            "ts": "2025-06-01T00:00:00Z",
            "type": event_type,
            "spid": spid,
            "data": data,
        }
    )


class TestCmdEvents:
    def test_default_formatter_is_not_jsonl(self, contree_client, capsys):
        """The default formatter is a table, not JSONL -- help text and
        behavior must agree on that."""
        contree_client.mock(
            "iter_operation_events",
            [make_event(1, "stdout", {"value": "hi", "encoding": "ascii"})],
        )
        FORMATTER.set(DefaultFormatter())
        ctx = copy_context()
        ctx.run(cmd_events, EventsArgs(uuids=["op-1"]))
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert lines
        for line in lines:
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)

    def test_json_output_is_jsonl(self, contree_client, capsys):
        contree_client.mock(
            "iter_operation_events",
            [
                make_event(1, "stdout", {"value": "hi", "encoding": "ascii"}),
                make_event(2, "exit", {"exit_code": 0}),
            ],
        )
        FORMATTER.set(JSONFormatter())
        ctx = copy_context()
        ctx.run(cmd_events, EventsArgs(uuids=["op-1"]))
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 2
        for line in lines:
            row = json.loads(line)
            assert row["uuid"] == "op-1"


# ----------------------------------------------------------------------
# op cancel
# ----------------------------------------------------------------------


class TestOperationCancel:
    def test_cancel_single_uuid(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a"],
                cancel_outcomes=[None],
            )
        assert rc is None
        calls = contree_client.calls_for("cancel_operation")
        assert len(calls) == 1
        assert calls[0].args == ("op-a",)
        assert "Cancelled operation op-a" in caplog.text

    def test_cancel_multiple_uuids(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a", "op-b", "op-c"],
                cancel_outcomes=[None, None, None],
            )
        assert rc is None
        calls = contree_client.calls_for("cancel_operation")
        assert [call.args for call in calls] == [("op-a",), ("op-b",), ("op-c",)]

    def test_cancel_continues_on_error(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                uuids=["op-a", "op-b"],
                cancel_outcomes=[APIStatusError(409, "conflict"), None],
            )
        assert rc == 1
        assert "Failed to cancel op-a" in caplog.text
        assert "Cancelled operation op-b" in caplog.text

    def test_cancel_requires_uuids_or_all(self, contree_client, caplog):
        with caplog.at_level("ERROR"):
            rc = run_cancel(contree_client)
        assert rc == 1
        assert "Provide at least one UUID" in caplog.text
        assert contree_client.calls == []

    def test_cancel_all_iterates_active_statuses(self, contree_client, caplog):
        # One op per active-status listing call. ACTIVE_STATUSES is a
        # frozenset, so the status <-> page pairing is nondeterministic;
        # pages are queued positionally and assertions stay unordered.
        list_pages = [[{"uuid": f"active-{i}"}] for i in range(len(ACTIVE_STATUSES))]
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                all_flag=True,
                list_pages=list_pages,
                cancel_outcomes=[None] * len(ACTIVE_STATUSES),
            )
        assert rc is None
        list_calls = contree_client.calls_for("list_operations")
        assert {call.kwargs["status"] for call in list_calls} == set(ACTIVE_STATUSES)
        cancel_calls = contree_client.calls_for("cancel_operation")
        assert sorted(call.args[0] for call in cancel_calls) == [
            f"active-{i}" for i in range(len(ACTIVE_STATUSES))
        ]
        for i in range(len(ACTIVE_STATUSES)):
            assert f"Cancelled operation active-{i}" in caplog.text

    def test_cancel_all_with_no_active(self, contree_client, caplog):
        list_pages = [[] for _ in ACTIVE_STATUSES]
        with caplog.at_level("INFO"):
            rc = run_cancel(
                contree_client,
                all_flag=True,
                list_pages=list_pages,
            )
        assert rc is None
        # Only listings, no cancels
        assert len(contree_client.calls_for("list_operations")) == len(ACTIVE_STATUSES)
        assert contree_client.calls_for("cancel_operation") == []
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
                cancel_outcomes=[None],
            )
        assert rc is None
        assert "--all overrides explicit UUIDs" in caplog.text
        # Only one cancel went out -- for pending-0, not the ignored UUIDs
        cancel_calls = contree_client.calls_for("cancel_operation")
        assert [call.args for call in cancel_calls] == [("pending-0",)]


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
        mock_op(contree_client, _wait_op("op-1", status="SUCCESS"))

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        rc = ctx.run(cmd_wait, WaitArgs(uuids=["op-1"], timeout=60))
        assert rc is None
        assert len(contree_client.calls_for("get_operation_status")) == 1

    def test_wait_failed_op_returns_exit_code_one(
        self, contree_client, monkeypatch, capsys
    ):
        monkeypatch.setattr("contree_cli.cli.operation.time.sleep", lambda _: None)
        mock_op(contree_client, _wait_op("op-fail", status="FAILED"))

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
        op["metadata"] = {
            "command": "false",
            "image": "img-base",
            "result": {"state": {"exit_code": 1}},
        }
        mock_op(contree_client, op)

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
        op["metadata"] = {
            "command": "exit 42",
            "image": "img-base",
            "result": {"state": {"exit_code": 42}},
        }
        mock_op(contree_client, op)

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
        mock_op(contree_client, _wait_op("op-slow", status="EXECUTING"))
        mock_op(contree_client, _wait_op("op-slow", status="EXECUTING"))

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
        contree_client.mock("list_operations", [])

        FORMATTER.set(JSONFormatter())
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("INFO"):
            rc = ctx.run(cmd_wait, WaitArgs(uuids=[], all=True, timeout=60))
        assert rc is None
        assert len(contree_client.calls_for("list_operations")) == len(ACTIVE_STATUSES)
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
