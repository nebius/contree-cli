from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import ContreeTestClient
from test_build import (
    BASE_IMG,
    NEW_IMG,
    NEW_IMG_2,
    make_keepalive_success,
    make_op_success,
    make_spawn,
    make_tag_lookup,
    run_build,
    write_dockerfile,
)

from contree_cli.cli.build import BuildArgs, make_session_key
from contree_cli.docker.context import BuildContext
from contree_cli.docker.local_context import LocalContext
from contree_cli.session import SessionStore


@pytest.mark.parametrize(
    ("directives", "commands", "environments"),
    [
        ("ENV MODE=corp\nRUN true", ["true"], [{"MODE": "corp"}]),
        ("ENV MODE=corp", [""], [{"MODE": "corp"}]),
        (
            "ENV MODE=first\nRUN true\nENV MODE=last",
            ["true", ""],
            [{"MODE": "first"}, {"MODE": "last"}],
        ),
        (
            "ENV MODE=corp\nRUN true\nENV MODE=corp",
            ["true"],
            [{"MODE": "corp"}],
        ),
        ("ENV MODE=\nUSER missing", [""], [{"MODE": ""}]),
        (
            "ARG SECRET=build-only\nARG REGISTRY=corp\nENV REGISTRY=$REGISTRY",
            [""],
            [{"REGISTRY": "corp"}],
        ),
        ("ARG SECRET=build-only", [], []),
    ],
)
def test_build_persists_only_env(tmp_path, directives, commands, environments):
    write_dockerfile(tmp_path, f"FROM tag:base\n{directives}\n")
    client = ContreeTestClient()
    mocks = [make_tag_lookup(BASE_IMG)]
    for index, command in enumerate(commands):
        success = make_op_success if command else make_keepalive_success
        mocks.extend([make_spawn(f"op-{index}"), success(NEW_IMG, f"op-{index}")])

    result = run_build(
        client, BuildArgs(context=str(tmp_path)), mocks, tmp_path / "sessions.db"
    )

    assert result is None
    spawns = client.calls_for("spawn_instance")
    assert [call.args[0] for call in spawns] == commands
    assert [call.kwargs["env"] for call in spawns] == environments
    assert all(call.kwargs["preserve_env"] is True for call in spawns)


@pytest.mark.parametrize("second_from", ["tag:base", "first"])
def test_stage_seals_env_and_resets_tracking(tmp_path, second_from):
    write_dockerfile(
        tmp_path,
        "FROM tag:base AS first\nENV FIRST=corp\nUSER missing\n"
        f"FROM {second_from}\nENV SECOND=corp\nRUN true\n",
    )
    mocks = [make_tag_lookup(BASE_IMG), make_spawn(), make_keepalive_success(NEW_IMG)]
    if second_from == "tag:base":
        mocks.append(make_tag_lookup(BASE_IMG))
    mocks.extend([make_spawn("op-2"), make_op_success(NEW_IMG_2, "op-2")])
    client = ContreeTestClient()

    result = run_build(
        client, BuildArgs(context=str(tmp_path)), mocks, tmp_path / "sessions.db"
    )

    assert result is None
    first, second = client.calls_for("spawn_instance")
    assert first.args == ("", BASE_IMG)
    assert first.kwargs["shell"] is False
    assert first.kwargs["env"] == {"FIRST": "corp"}
    assert first.kwargs["preserve_env"] is True
    assert second.args == ("true", NEW_IMG if second_from == "first" else BASE_IMG)
    expected_env = {"SECOND": "corp"}
    if second_from == "first":
        expected_env["FIRST"] = "corp"
    assert second.kwargs["env"] == expected_env
    assert second.kwargs["preserve_env"] is True


@pytest.mark.parametrize("directives", ["ENV MODE=corp", "ENV MODE=corp\nRUN true"])
def test_cached_env_needs_no_extra_operation(tmp_path, directives):
    write_dockerfile(tmp_path, f"FROM tag:base\n{directives}\n")
    args = BuildArgs(context=str(tmp_path))
    db_path = tmp_path / "sessions.db"
    success = make_op_success if "RUN" in directives else make_keepalive_success
    assert (
        run_build(
            ContreeTestClient(),
            args,
            [make_tag_lookup(BASE_IMG), make_spawn(), success(NEW_IMG)],
            db_path,
        )
        is None
    )
    cached_client = ContreeTestClient()

    assert run_build(cached_client, args, [make_tag_lookup(BASE_IMG)], db_path) is None
    assert cached_client.calls_for("spawn_instance") == []
    with SessionStore(db_path, make_session_key(tmp_path)) as store:
        assert store.current_image == NEW_IMG


def test_legacy_run_cache_does_not_skip_env_persistence(tmp_path: Path):
    write_dockerfile(tmp_path, "FROM tag:base\nENV MODE=corp\nRUN true\n")
    db_path = tmp_path / "sessions.db"
    with SessionStore(db_path, make_session_key(tmp_path)) as store:
        ctx = BuildContext(
            client=ContreeTestClient(),
            store=store,
            local=LocalContext.from_dir(tmp_path),
            env={"MODE": "corp"},
            parent_hash=hashlib.sha256(f"FROM:{BASE_IMG}".encode()).hexdigest(),
        )
        legacy_chain = ctx.chain('RUN shell=True parts=["true"]')
        ctx.commit_layer(
            f"layer:{ctx.short_hash(legacy_chain)}",
            NEW_IMG,
            kind="run",
            title="RUN true",
        )
    client = ContreeTestClient()

    assert (
        run_build(
            client,
            BuildArgs(context=str(tmp_path)),
            [make_tag_lookup(BASE_IMG), make_spawn(), make_op_success(NEW_IMG_2)],
            db_path,
        )
        is None
    )
    assert len(client.calls_for("spawn_instance")) == 1
    assert client.calls_for("spawn_instance")[0].kwargs["preserve_env"] is True
