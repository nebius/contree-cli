from __future__ import annotations

from contextvars import copy_context

from conftest import ContreeTestClient
from contree_client.exceptions import ContreeAPIError
from contree_client.models import OperationSummary

from contree_cli import CLIENT
from contree_cli.cli.operation import ACTIVE_STATUSES, CancelArgs, cmd_cancel


def _run_cmd(tc: ContreeTestClient, uuid):
    tc.mock("cancel_operation", None)
    ctx = copy_context()

    args = CancelArgs(uuids=[uuid])
    ctx.run(cmd_cancel, args)


class TestCmdKill:
    def test_sends_cancel(self, contree_client):
        _run_cmd(contree_client, "op-123")
        calls = contree_client.calls_for("cancel_operation")
        assert len(calls) == 1
        assert calls[0].args == ("op-123",)

    def test_logs_cancellation(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            _run_cmd(contree_client, "op-456")
        assert "Cancelled operation op-456" in caplog.text

    def test_not_found_logs_and_sets_exit(self, contree_client, caplog):
        contree_client.mock("cancel_operation", error=ContreeAPIError(404, "nope"))
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("ERROR"):
            rc = ctx.run(cmd_cancel, CancelArgs(uuids=["bad-uuid"]))
        assert rc == 1
        assert "Failed to cancel bad-uuid" in caplog.text

    def test_conflict_logs_and_sets_exit(self, contree_client, caplog):
        contree_client.mock(
            "cancel_operation", error=ContreeAPIError(409, "already done")
        )
        CLIENT.set(contree_client)
        ctx = copy_context()
        with caplog.at_level("ERROR"):
            rc = ctx.run(cmd_cancel, CancelArgs(uuids=["done-op"]))
        assert rc == 1


# ---------------------------------------------------------------------------
# --all
# ---------------------------------------------------------------------------


def _ops_for_status(status, count):
    return [{"uuid": f"{status.lower()}-{i}"} for i in range(count)]


def _run_kill_all(pages, *, cancel_failures=None):
    """Run cmd_cancel --all with mocked list + cancel responses.

    ``list_active`` queries each ACTIVE_STATUS once; the frozenset
    iteration order is nondeterministic, so ``pages`` are queued
    positionally (call order), not per status. Cancellations follow
    the page order, so ``cancel_failures`` matches by UUID.
    """
    cancel_failures = cancel_failures or set()
    tc = ContreeTestClient()

    # One page per active-status listing call; pad with empties.
    queued = list(pages)
    while len(queued) < len(ACTIVE_STATUSES):
        queued.append([])
    for page in queued:
        tc.mock(
            "list_operations",
            [OperationSummary.from_dict(op) for op in page],
        )

    # Queue one cancel outcome per collected UUID, in page order.
    for page in queued:
        for op in page:
            if op["uuid"] in cancel_failures:
                tc.mock(
                    "cancel_operation",
                    error=ContreeAPIError(409, "conflict"),
                )
            else:
                tc.mock("cancel_operation", None)

    CLIENT.set(tc)
    ctx = copy_context()
    args = CancelArgs(uuids=[], all=True)

    rc = ctx.run(cmd_cancel, args)
    return tc, rc


class TestKillAll:
    def test_kills_all_active(self, caplog):
        pages = [
            _ops_for_status("PENDING", 1),
            _ops_for_status("EXECUTING", 1),
        ]
        with caplog.at_level("INFO"):
            tc, rc = _run_kill_all(pages)
        assert rc is None
        # 3 listing calls (one per status) + 2 cancels
        assert len(tc.calls_for("list_operations")) == len(ACTIVE_STATUSES)
        assert len(tc.calls_for("cancel_operation")) == 2
        assert "Cancelled operation pending-0" in caplog.text
        assert "Cancelled operation executing-0" in caplog.text

    def test_no_active_operations(self, caplog):
        with caplog.at_level("INFO"):
            tc, rc = _run_kill_all([])
        assert rc is None
        assert "No active operations" in caplog.text
        # Only listings, no cancels
        assert len(tc.calls_for("list_operations")) == len(ACTIVE_STATUSES)
        assert tc.calls_for("cancel_operation") == []

    def test_partial_failure(self, caplog):
        pages = [_ops_for_status("PENDING", 2)]
        with caplog.at_level("INFO"):
            _, rc = _run_kill_all(
                pages,
                cancel_failures={"pending-1"},
            )
        assert rc == 1
        assert "Cancelled operation pending-0" in caplog.text
        assert "Failed to cancel pending-1" in caplog.text

    def test_queries_all_statuses(self):
        tc, _ = _run_kill_all([])
        statuses = {call.kwargs["status"] for call in tc.calls_for("list_operations")}
        assert statuses == set(ACTIVE_STATUSES)
