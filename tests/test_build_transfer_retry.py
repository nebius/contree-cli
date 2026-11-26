"""Build file transfers stop after instance expiry or a bounded retry budget."""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from contree_client.exceptions import APIConnectionError, APIStatusError
from contree_client.runtime import RequestSpec, ResponseData, RetryPolicy

from contree_cli.client import CliClient, ClientBase


@pytest.fixture
def transfer_client():
    client = CliClient(
        "token",
        retry=RetryPolicy(max_attempts=None, retry_unsafe=True, delays=(0.0,)),
    )
    yield client
    client.close()


def apply_transfer(client, *, deadline=None, unpack=True):
    client.bounded(
        time.monotonic() + 30 if deadline is None else deadline
    ).apply_instance_file(
        "operation",
        "file",
        "/dst",
        unpack=unpack,
        replace=True,
        merge=True if unpack else ...,
    )


def test_expired_instance_is_not_retried(transfer_client):
    policy = transfer_client.retry
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(410, "Instance ended")
        ) as request,
        pytest.raises(APIStatusError) as error,
    ):
        apply_transfer(transfer_client)
    assert error.value.status == 410
    assert request.call_count == 1
    assert transfer_client.retry is policy
    assert 410 in policy.statuses
    assert policy.max_attempts is None


def test_transient_failure_retries_with_the_same_deadline(transfer_client):
    deadline = time.monotonic() + 30
    with patch.object(
        CliClient,
        "request",
        side_effect=[APIStatusError(503, "Unavailable"), ResponseData(204, {}, b"")],
    ) as request:
        apply_transfer(transfer_client, deadline=deadline)
    assert request.call_count == 2
    assert all(call.args[0].deadline == deadline for call in request.call_args_list)


def test_transient_retries_stop_at_remaining_instance_deadline(
    transfer_client, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr("contree_client.base.time.monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr("contree_client.base.time.sleep", advance)
    transfer_client.retry = RetryPolicy(
        max_attempts=None, retry_unsafe=True, delays=(10.0,)
    )
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(503, "Unavailable")
        ) as request,
        pytest.raises(TimeoutError),
    ):
        apply_transfer(transfer_client, deadline=102.0)
    assert request.call_count == 1
    assert clock[0] == 102.0


def test_expired_deadline_prevents_request(transfer_client):
    with patch.object(CliClient, "request") as request, pytest.raises(TimeoutError):
        apply_transfer(transfer_client, deadline=time.monotonic() - 1)
    request.assert_not_called()


@pytest.mark.parametrize("attempts", [None, 3])
def test_transient_retries_have_a_finite_attempt_budget(transfer_client, attempts):
    transfer_client.retry = RetryPolicy(
        max_attempts=attempts, retry_unsafe=True, delays=(0.0,)
    )
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(503, "Unavailable")
        ) as request,
        pytest.raises(APIStatusError),
    ):
        apply_transfer(transfer_client)
    assert request.call_count == (attempts or 10)


LIFECYCLE_CALLS = (
    ("operation_subprocess_create", ("operation", "chown"), {}),
    ("operation_subprocess_kill", ("operation", 1), {"signal": "TERM"}),
    ("cancel_operation", ("operation",), {}),
    ("wait_operation", ("operation",), {"follow_events": False}),
)


@pytest.mark.parametrize("method,args,kwargs", LIFECYCLE_CALLS)
def test_lifecycle_transient_failures_have_finite_retries(
    transfer_client, method, args, kwargs
):
    deadline = time.monotonic() + 30
    client = transfer_client.bounded(deadline)
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(503, "Unavailable")
        ) as request,
        pytest.raises(APIStatusError),
    ):
        getattr(client, method)(*args, **kwargs)
    assert request.call_count == 10
    assert all(call.args[0].deadline == deadline for call in request.call_args_list)
    assert transfer_client.request_deadline is None
    assert transfer_client.retry.max_attempts is None


@pytest.mark.parametrize("method,args,kwargs", LIFECYCLE_CALLS)
def test_lifecycle_retries_stop_at_the_instance_deadline(
    transfer_client, monkeypatch, method, args, kwargs
):
    clock = [100.0]
    monkeypatch.setattr("contree_client.base.time.monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr("contree_client.base.time.sleep", advance)
    transfer_client.retry = RetryPolicy(
        max_attempts=None, retry_unsafe=True, delays=(10.0,)
    )
    client = transfer_client.bounded(102.0)
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(503, "Unavailable")
        ) as request,
        pytest.raises(TimeoutError),
    ):
        getattr(client, method)(*args, **kwargs)
    assert request.call_count == 1
    assert clock[0] == 102.0


@pytest.mark.parametrize("request_deadline", [None, 90.0, 110.0])
def test_stream_uses_earliest_deadline_without_mutating_request(
    transfer_client, request_deadline
):
    client = transfer_client.bounded(100.0)
    spec = RequestSpec("GET", "/events", deadline=request_deadline)
    with patch.object(ClientBase, "stream", return_value=iter([b"event"])) as stream:
        assert list(client.stream(spec)) == [b"event"]
    expected = 100.0 if request_deadline is None else min(100.0, request_deadline)
    assert stream.call_args.args[0].deadline == expected
    assert spec.deadline == request_deadline


def test_event_reconnect_status_probe_has_a_deadline(transfer_client, monkeypatch):
    clock = [100.0]
    deadline = 100.2
    monkeypatch.setattr("contree_client.base.time.monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    def unavailable(spec):
        assert spec.deadline == deadline
        if clock[0] >= deadline:
            raise TimeoutError("Status request expired")
        raise APIStatusError(503, "Unavailable")

    monkeypatch.setattr("contree_client.base.time.sleep", advance)
    client = transfer_client.bounded(deadline)
    with (
        patch.object(
            ClientBase, "stream", side_effect=APIConnectionError("Disconnected")
        ),
        patch.object(ClientBase, "request", side_effect=unavailable) as request,
        pytest.raises(TimeoutError),
    ):
        list(client.follow_operation_events("operation"))
    assert request.call_count <= 4
    assert all(call.args[0].deadline == deadline for call in request.call_args_list)


def test_cleanup_has_a_new_budget_and_preserves_original_failure(transfer_client):
    from contree_cli.docker.transfer import cancel_instance

    expired = transfer_client.bounded(time.monotonic() - 1)
    original_error = ValueError("Archive transfer failed")
    with (
        patch.object(
            CliClient, "request", side_effect=APIStatusError(503, "Unavailable")
        ) as request,
        pytest.raises(ValueError) as raised,
    ):
        try:
            raise original_error
        except ValueError:
            cancel_instance(SimpleNamespace(client=expired), "operation")
            raise
    assert raised.value is original_error
    assert request.call_count == 10
    assert request.call_args.args[0].deadline > time.monotonic()
    assert expired.request_deadline < time.monotonic()
