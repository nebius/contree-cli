from __future__ import annotations

import json
from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient, FakeResponse

from contree_cli import CLIENT, FORMATTER, PROFILE, SESSION_STORE
from contree_cli.cli.build import (
    BuildArgs,
    cmd_build,
    make_session_key,
)
from contree_cli.config import ConfigProfile
from contree_cli.output import JSONFormatter
from contree_cli.session import SessionStore

BASE_IMG = "11111111-1111-1111-1111-111111111111"
NEW_IMG = "22222222-2222-2222-2222-222222222222"
NEW_IMG_2 = "33333333-3333-3333-3333-333333333333"


def make_op_success(image: str, op_uuid: str = "op-1") -> FakeResponse:
    return FakeResponse.json(
        {
            "uuid": op_uuid,
            "kind": "instance",
            "status": "SUCCESS",
            "duration": 1.0,
            "metadata": {
                "result": {
                    "state": {"exit_code": 0},
                    "stdout": None,
                    "stderr": None,
                }
            },
            "result": {"image": image, "tag": ""},
        }
    )


def make_spawn(op_uuid: str = "op-1") -> FakeResponse:
    return FakeResponse.json({"uuid": op_uuid, "status": "PENDING"}, status=201)


def make_tag_lookup(image_uuid: str) -> FakeResponse:
    return FakeResponse.json({"images": [{"uuid": image_uuid, "tag": "ubuntu:latest"}]})


def run_build(
    tc: ContreeTestClient,
    args: BuildArgs,
    responses: list[FakeResponse],
    db_path: Path,
):
    tc.fake.responses.extend(responses)
    profile = ConfigProfile(name="test", url="http://x", token="t")
    PROFILE.set(profile)
    monkey_profile_path(profile, db_path)
    FORMATTER.set(JSONFormatter())
    CLIENT.set(tc)
    SESSION_STORE.set(SessionStore(db_path, "placeholder"))
    ctx = copy_context()
    with (
        patch("contree_cli.cli.run.time.sleep"),
        patch("contree_cli.docker.kw_from.time.sleep"),
    ):
        return ctx.run(cmd_build, args)


def monkey_profile_path(profile: ConfigProfile, db_path: Path):
    object.__setattr__(profile, "_session_db_override", db_path)
    from contree_cli.config import ConfigProfile as RealProfile

    if not hasattr(RealProfile, "_original_session_db_path"):
        RealProfile._original_session_db_path = RealProfile.session_db_path  # type: ignore[attr-defined]

        def patched(self):
            override = getattr(self, "_session_db_override", None)
            if override is not None:
                return override
            return RealProfile._original_session_db_path.fget(self)  # type: ignore[attr-defined]

        RealProfile.session_db_path = property(patched)  # type: ignore[assignment,misc]


@pytest.fixture
def context_dir(tmp_path: Path) -> Path:
    d = tmp_path / "ctx"
    d.mkdir()
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "session.db"


def write_dockerfile(d: Path, text: str) -> Path:
    p = d / "Dockerfile"
    p.write_text(text)
    return p


class TestArgparseWiring:
    def test_build_arg_namespace_decodes_to_build_args(self):
        """--build-arg KEY=VAL must reach BuildArgs.build_args after parsing."""
        import contree_cli.arguments

        ns = contree_cli.arguments.parser.parse_args(
            ["build", ".", "--build-arg", "VERSION=1.0", "--no-cache"]
        )
        loader = ns.load_args
        args = loader.from_args(ns)
        assert args.build_args == ("VERSION=1.0",)
        assert args.no_cache is True
        assert args.context == "."


class TestSimpleBuild:
    def test_from_run_creates_expected_api_calls(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN echo hi\n",
        )
        tc = ContreeTestClient()
        args = BuildArgs(context=str(context_dir))
        responses = [
            make_tag_lookup(BASE_IMG),
            make_spawn(),
            make_op_success(NEW_IMG),
        ]
        rc = run_build(tc, args, responses, db_path)
        assert rc is None
        # 4 wire calls: FROM's tag lookup, POST instances, SSE events
        # follow, then the streamer's terminal GET.
        assert tc.request_count == 4
        assert tc.get_request(0).method == "GET"
        assert "/v1/images" in tc.get_request(0).path
        assert tc.get_request(1).method == "POST"
        assert "/v1/instances" in tc.get_request(1).path
        assert tc.get_request(2).method == "GET"
        assert "/v1/operations/op-1/events?follow=1" in tc.get_request(2).path
        assert tc.get_request(3).method == "GET"
        assert (
            tc.get_request(3).path[-len("/v1/operations/op-1") :]
            == "/v1/operations/op-1"
        )

    def test_run_payload_carries_command(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN apt-get update\n",
        )
        tc = ContreeTestClient()
        args = BuildArgs(context=str(context_dir))
        run_build(
            tc,
            args,
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )
        spawn = tc.get_request(1)
        body = json.loads(spawn.body.decode())
        assert body["image"] == BASE_IMG
        assert body["command"] == "apt-get update"
        assert body["shell"] is True

    def test_run_streams_stdout_live(self, context_dir, db_path, capsys):
        """Docker-compat mode: RUN output must reach the user's terminal
        as the SSE events arrive, not just after the op completes.

        Uses a stubbed streamer that writes a chunk to `sys.stdout.buffer`
        and returns a completion-populated summary so the build finishes
        with the streamed image — mirrors what a live SSE `stdout` frame
        followed by a `completion` frame would produce."""
        import sys

        from contree_cli.cli.run import TerminalSummary

        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN echo hi\n",
        )
        tc = ContreeTestClient()

        def fake_stream(_client, op_uuid, _formatter):
            sys.stdout.buffer.write(b"hi from RUN\n")
            sys.stdout.buffer.flush()
            summary = TerminalSummary()
            summary.completion = {
                "type": "completion",
                "data": {"status": "SUCCESS", "result_image_uuid": NEW_IMG},
            }
            summary.exit_event = {"type": "exit", "spid": 1, "data": {"code": 0}}
            summary.stdout.extend(b"hi from RUN\n")
            return summary

        with patch(
            "contree_cli.docker.kw_run._stream_events_until_close",
            side_effect=fake_stream,
        ):
            rc = run_build(
                tc,
                BuildArgs(context=str(context_dir)),
                [make_tag_lookup(BASE_IMG), make_spawn()],
                db_path,
            )
        assert rc is None
        # The live-streamed chunk lands on stdout before the final
        # formatter record — verify both are present.
        out = capsys.readouterr().out
        assert "hi from RUN" in out


class TestCache:
    def test_second_build_is_full_cache_hit(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN echo hi\n",
        )
        args = BuildArgs(context=str(context_dir))

        first = ContreeTestClient()
        run_build(
            first,
            args,
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )

        second = ContreeTestClient()
        run_build(
            second,
            args,
            [make_tag_lookup(BASE_IMG)],
            db_path,
        )
        assert second.request_count == 1
        assert "/v1/images" in second.get_request(0).path

    def test_no_cache_reruns(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN echo hi\n",
        )

        first = ContreeTestClient()
        run_build(
            first,
            BuildArgs(context=str(context_dir)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )

        second = ContreeTestClient()
        rc = run_build(
            second,
            BuildArgs(context=str(context_dir), no_cache=True),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn("op-2"),
                make_op_success(NEW_IMG_2, "op-2"),
            ],
            db_path,
        )
        assert rc is None
        # 4 wire calls per build: FROM tag lookup, POST instances, SSE
        # follow, and the streamer's terminal GET.
        assert second.request_count == 4

    def test_no_cache_when_from_layer_is_active_branch(self, context_dir, db_path):
        """Regression: --no-cache must not blow up when the target layer
        branch is currently the active one.

        Reproduces the original failure where ``commit_layer`` tried to
        delete an active branch and then re-create it, producing
        ``Branch '...' already exists``. With ``set_image_on_branch`` we
        update the pointer in place without touching the active flag.
        """
        write_dockerfile(context_dir, "FROM tag:ubuntu:latest\nRUN echo hi\n")

        first = ContreeTestClient()
        run_build(
            first,
            BuildArgs(context=str(context_dir)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )

        # Force the active branch back to the FROM layer (simulates a user
        # doing `session checkout layer:<from-hash>` between builds, or a
        # prior build that ended on FROM only).
        import hashlib

        from contree_cli.cli.build import make_session_key

        session_key = make_session_key(context_dir.resolve())
        from_hash = hashlib.sha256(f"FROM:{BASE_IMG}".encode()).hexdigest()
        from_branch = f"layer:{from_hash[:16]}"
        store = SessionStore(db_path, session_key)
        try:
            store.switch_branch(from_branch)
        finally:
            store.close()

        # Rebuild with --no-cache: this previously failed with
        # "Branch 'layer:...' already exists".
        second = ContreeTestClient()
        rc = run_build(
            second,
            BuildArgs(context=str(context_dir), no_cache=True),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn("op-2"),
                make_op_success(NEW_IMG_2, "op-2"),
            ],
            db_path,
        )
        assert rc is None


class TestCopy:
    def test_copy_pending_attaches_to_next_run(self, context_dir, db_path):
        (context_dir / "app.py").write_text("print('hi')")
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nCOPY app.py /app.py\nRUN python /app.py\n",
        )
        tc = ContreeTestClient()
        responses = [
            make_tag_lookup(BASE_IMG),
            FakeResponse.json({}, status=404),
            FakeResponse.json({"uuid": "file-1", "sha256": "abc"}),
            make_spawn(),
            make_op_success(NEW_IMG),
        ]
        rc = run_build(
            tc,
            BuildArgs(context=str(context_dir)),
            responses,
            db_path,
        )
        assert rc is None
        spawn = tc.get_request(3)
        body = json.loads(spawn.body.decode())
        assert "files" in body
        assert "/app.py" in body["files"]
        assert body["files"]["/app.py"]["uuid"] == "file-1"


class TestUnsupportedDirective:
    def test_label_skipped_with_warning(self, context_dir, db_path, caplog):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nLABEL maintainer=me\nRUN echo hi\n",
        )
        tc = ContreeTestClient()
        rc = run_build(
            tc,
            BuildArgs(context=str(context_dir)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )
        assert rc is None
        assert any("not supported" in r.message for r in caplog.records)


class TestBuildArgs:
    def test_build_arg_substitutes_in_run(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nARG VERSION=1.0\nRUN echo $VERSION\n",
        )
        tc = ContreeTestClient()
        run_build(
            tc,
            BuildArgs(context=str(context_dir), build_args=("VERSION=2.5",)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )
        spawn_body = json.loads(tc.get_request(1).body.decode())
        assert spawn_body["command"] == "echo 2.5"

    def test_arg_default_flows_into_env(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\n"
            "ARG APP_HOME=/opt/streamforge\n"
            "ENV APP_HOME=${APP_HOME}\n"
            "RUN echo $APP_HOME\n",
        )
        tc = ContreeTestClient()
        run_build(
            tc,
            BuildArgs(context=str(context_dir)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )
        spawn_body = json.loads(tc.get_request(1).body.decode())
        assert spawn_body["command"] == "echo /opt/streamforge"
        assert spawn_body["env"] == {"APP_HOME": "/opt/streamforge"}

    def test_arg_default_referencing_earlier_arg(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\n"
            "ARG ROOT=/opt\n"
            "ARG APP_HOME=${ROOT}/streamforge\n"
            "ENV APP_HOME=${APP_HOME}\n"
            "RUN echo $APP_HOME\n",
        )
        tc = ContreeTestClient()
        run_build(
            tc,
            BuildArgs(context=str(context_dir)),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
            ],
            db_path,
        )
        spawn_body = json.loads(tc.get_request(1).body.decode())
        assert spawn_body["command"] == "echo /opt/streamforge"
        assert spawn_body["env"] == {"APP_HOME": "/opt/streamforge"}


class TestSessionKey:
    def test_deterministic(self, tmp_path):
        a = make_session_key(tmp_path / "p")
        b = make_session_key(tmp_path / "p")
        assert a == b
        assert a.startswith("build:")

    def test_differs_by_path(self, tmp_path):
        a = make_session_key(tmp_path / "a")
        b = make_session_key(tmp_path / "b")
        assert a != b


class TestTag:
    def test_final_image_tagged(self, context_dir, db_path):
        write_dockerfile(context_dir, "FROM tag:ubuntu:latest\nRUN echo hi\n")
        tc = ContreeTestClient()
        rc = run_build(
            tc,
            BuildArgs(context=str(context_dir), tag="mybuild:test"),
            [
                make_tag_lookup(BASE_IMG),
                make_spawn(),
                make_op_success(NEW_IMG),
                FakeResponse.json({}),
            ],
            db_path,
        )
        assert rc is None
        # PATCH lands after: tag lookup (0), spawn (1), SSE events (2),
        # streamer's terminal GET (3).
        tag_req = tc.get_request(4)
        assert tag_req.method == "PATCH"
        assert NEW_IMG in tag_req.path
        body = json.loads(tag_req.body.decode())
        assert body == {"tag": "mybuild:test"}
