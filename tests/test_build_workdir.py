from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import ContreeTestClient
from test_build import BASE_IMG, NEW_IMG, build_events, make_op_success, make_spawn

from contree_cli.docker.context import BuildContext
from contree_cli.docker.kw_from import FromKeyword, seal_stage
from contree_cli.docker.kw_run import RunKeyword, finalize_pending
from contree_cli.docker.kw_workdir import WorkdirKeyword
from contree_cli.docker.local_context import LocalContext
from contree_cli.session import SessionStore


@pytest.fixture
def context(tmp_path):
    client = ContreeTestClient()
    with SessionStore(tmp_path / "session.db", "workdir-tests") as store:
        yield BuildContext(
            client=client,
            store=store,
            local=LocalContext.from_dir(tmp_path),
            last_image=BASE_IMG,
        )


def execute_run(context):
    name, response = make_spawn()
    context.client.mock(name, response)
    name, response = make_op_success(NEW_IMG)
    context.client.mock(
        "iter_operation_events", build_events(response, keepalive=False)
    )
    RunKeyword(parts=("true",)).execute(context)


@pytest.mark.parametrize(
    ("workdir", "cached"),
    [("/", False), ("/app", False), ("", False), ("/app", True)],
    ids=["root", "explicit", "inherited", "cached"],
)
def test_run_saves_workdir_and_sends_explicit_root(context, workdir, cached):
    context.workdir = workdir

    execute_run(context)
    if cached:
        context.last_image = BASE_IMG
        context.parent_hash = ""
        context.persisted_workdir = ""
        RunKeyword(parts=("true",)).execute(context)

    assert len(context.client.calls_for("spawn_instance")) == 1
    call = context.client.calls_for("spawn_instance")[0]
    assert call.kwargs["preserve_cwd"] is True
    assert call.kwargs["cwd"] == (workdir or ...)
    assert context.persisted_workdir == workdir
    with patch("contree_cli.docker.transfer.materialize") as materialize:
        finalize_pending(context)
    materialize.assert_not_called()


def test_trailing_workdir_is_materialized(context):
    WorkdirKeyword(path="/app").execute(context)

    with patch("contree_cli.docker.transfer.materialize") as materialize:
        finalize_pending(context)

    materialize.assert_called_once_with(context)


def test_relative_workdir_uses_image_default_once(context):
    with patch(
        "contree_cli.docker.transfer.read_image_workdir", return_value="/image/default"
    ) as read_workdir:
        WorkdirKeyword(path="child").execute(context)
        WorkdirKeyword(path="../next").execute(context)

    assert context.workdir == "/image/default/next"
    assert context.persisted_workdir == "/image/default"
    read_workdir.assert_called_once_with(context)


def test_explicit_root_never_resolves_image_default(context):
    with patch("contree_cli.docker.transfer.read_image_workdir") as read_workdir:
        WorkdirKeyword(path="/").execute(context)
        WorkdirKeyword(path="child").execute(context)

    assert context.workdir == "/child"
    read_workdir.assert_not_called()


@pytest.mark.parametrize("reference", ["parent", "0"])
def test_from_stage_inherits_workdir_and_env(context, reference):
    context.workdir = context.persisted_workdir = "/image/app"
    context.env = {"APP": "inherited"}
    context.persisted_env = dict(context.env)
    context.current_stage_alias = "parent"

    FromKeyword(image_ref=reference).execute(context)
    WorkdirKeyword(path="child").execute(context)

    assert context.workdir == "/image/app/child"
    assert context.persisted_workdir == "/image/app"
    assert context.env == {"APP": "inherited"}
    assert context.persisted_env == context.env


def test_from_external_image_resets_inherited_stage_state(context):
    context.workdir = context.persisted_workdir = "/previous"
    context.env = {"PREVIOUS": "value"}
    context.persisted_env = dict(context.env)
    context.client.mock("inspect_find_image_by_tag", NEW_IMG)

    FromKeyword(image_ref="tag:next").execute(context)

    assert context.workdir == ""
    assert context.persisted_workdir == ""
    assert context.env == {}
    assert context.persisted_env == {}


def test_sealed_stage_keeps_independent_environment(context):
    context.workdir = context.persisted_workdir = "/app"
    context.env = {"FIRST": "one"}
    context.persisted_env = dict(context.env)

    seal_stage(context)
    context.env["FIRST"] = "changed"

    assert context.stage_settings[BASE_IMG] == ("/app", {"FIRST": "one"})


def test_previous_cache_without_preserve_cwd_is_not_reused(context):
    context.workdir = "/app"
    old_chain = context.chain('RUN preserve_env=True shell=True parts=["true"]')
    context.store.set_image(BASE_IMG, kind="use", title="FROM")
    context.store.set_image_on_branch(
        f"layer:{context.short_hash(old_chain)}",
        BASE_IMG,
        kind="run",
        title="legacy RUN",
    )

    execute_run(context)

    assert len(context.client.calls_for("spawn_instance")) == 1
    assert context.last_image == NEW_IMG
