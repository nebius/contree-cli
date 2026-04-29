from __future__ import annotations

from contextvars import copy_context

import pytest
from conftest import ContreeTestClient

from contree_cli import CLIENT
from contree_cli.cli.kill import ACTIVE_STATUSES, KillArgs, cmd_kill
from contree_cli.client import ApiError


def _run_cmd(tc: ContreeTestClient, uuid, *, status=202):
    tc.respond(status=status, body=b"")
    ctx = copy_context()

    args = KillArgs(uuid=uuid)
    ctx.run(cmd_kill, args)


class TestCmdKill:
    def test_sends_delete(self, contree_client):
        _run_cmd(contree_client, "op-123")
        req = contree_client.get_request(0)
        assert req.method == "DELETE"
        assert req.path == "/v1/operations/op-123"

    def test_logs_cancellation(self, contree_client, caplog):
        with caplog.at_level("INFO"):
            _run_cmd(contree_client, "op-456")
        assert "Cancelled operation op-456" in caplog.text

    def test_not_found_raises(self, contree_client):
        contree_client.respond(status=404, body=b"nope")
        ctx = copy_context()
        args = KillArgs(uuid="bad-uuid")
        with pytest.raises(ApiError) as exc_info:
            ctx.run(cmd_kill, args)
        assert exc_info.value.status == 404

    def test_conflict_raises(self, contree_client):
        contree_client.respond(status=409, body=b"already done")
        ctx = copy_context()
        args = KillArgs(uuid="done-op")
        with pytest.raises(ApiError) as exc_info:
            ctx.run(cmd_kill, args)
        assert exc_info.value.status == 409


# ---------------------------------------------------------------------------
# --all
# ---------------------------------------------------------------------------


def _ops_for_status(status, count):
    return [{"uuid": f"{status.lower()}-{i}"} for i in range(count)]


def _run_kill_all(ops_by_status, *, delete_failures=None):
    """Run cmd_kill --all with mocked list + delete responses.

    ops_by_status: dict mapping status string to list of op dicts
    delete_failures: set of UUIDs that should return 409
    """
    delete_failures = delete_failures or set()
    tc = ContreeTestClient()

    # For each active status, one GET page (possibly empty)
    for status in ACTIVE_STATUSES:
        ops = ops_by_status.get(status, [])
        tc.respond_json(ops)

    # Collect all UUIDs in order and queue delete responses
    for status in ACTIVE_STATUSES:
        for op in ops_by_status.get(status, []):
            if op["uuid"] in delete_failures:
                tc.respond(status=409, body=b"conflict")
            else:
                tc.respond(status=202, body=b"")

    CLIENT.set(tc)
    ctx = copy_context()
    args = KillArgs(all=True)

    rc = ctx.run(cmd_kill, args)
    return tc, rc


class TestKillAll:
    def test_kills_all_active(self, caplog):
        ops = {
            "PENDING": _ops_for_status("PENDING", 1),
            "EXECUTING": _ops_for_status("EXECUTING", 1),
        }
        with caplog.at_level("INFO"):
            tc, rc = _run_kill_all(ops)
        assert rc is None
        # 3 GETs (one per status) + 2 DELETEs
        assert tc.request_count == 5
        assert "Cancelled operation pending-0" in caplog.text
        assert "Cancelled operation executing-0" in caplog.text

    def test_no_active_operations(self, caplog):
        with caplog.at_level("INFO"):
            tc, rc = _run_kill_all({})
        assert rc is None
        assert "No active operations" in caplog.text
        # Only 3 GETs, no DELETEs
        assert tc.request_count == 3

    def test_partial_failure(self, caplog):
        ops = {
            "PENDING": _ops_for_status("PENDING", 2),
        }
        with caplog.at_level("INFO"):
            _, rc = _run_kill_all(
                ops,
                delete_failures={"pending-1"},
            )
        assert rc == 1
        assert "Cancelled operation pending-0" in caplog.text
        assert "Failed to cancel pending-1" in caplog.text

    def test_queries_all_statuses(self):
        tc, _ = _run_kill_all({})
        paths = tc.request_paths
        for status in ACTIVE_STATUSES:
            assert any(f"status={status}" in p for p in paths), f"{status} not queried"
