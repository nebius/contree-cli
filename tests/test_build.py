from __future__ import annotations

from contextvars import copy_context
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient
from contree_client.exceptions import NotFoundError
from contree_client.models import (
    FileResponse,
    Image,
    InstanceSpawnResponse,
    OperationResponse,
)
from contree_client.profiles import Profile

from contree_cli import CLIENT, FORMATTER, PROFILE, SESSION_STORE
from contree_cli.cli.build import (
    BuildArgs,
    cmd_build,
    make_session_key,
)
from contree_cli.output import JSONFormatter
from contree_cli.session import SessionStore

BASE_IMG = "11111111-1111-1111-1111-111111111111"
NEW_IMG = "22222222-2222-2222-2222-222222222222"
NEW_IMG_2 = "33333333-3333-3333-3333-333333333333"

# A prepared mock outcome: (operation name, result model or exception).
MockSpec = tuple[str, object]


def make_op_success(image: str, op_uuid: str = "op-1") -> MockSpec:
    # OperationInstanceMetadata requires `command` and `image` on the
    # wire; the API always echoes the spawn parameters back here.
    return (
        "get_operation_status",
        OperationResponse.from_dict(
            {
                "uuid": op_uuid,
                "kind": "instance",
                "status": "SUCCESS",
                "duration": 1.0,
                "metadata": {
                    "command": "echo hi",
                    "image": BASE_IMG,
                    "shell": True,
                    "result": {
                        "state": {"exit_code": 0},
                        "stdout": None,
                        "stderr": None,
                    },
                },
                "result": {"image": image, "tag": ""},
            }
        ),
    )


def make_spawn(op_uuid: str = "op-1") -> MockSpec:
    return ("spawn_instance", InstanceSpawnResponse.from_dict({"uuid": op_uuid}))


def make_tag_lookup(image_uuid: str) -> MockSpec:
    return ("inspect_find_image_by_tag", image_uuid)


def run_build(
    tc: ContreeTestClient,
    args: BuildArgs,
    mocks: list[MockSpec],
    db_path: Path,
):
    for name, value in mocks:
        if isinstance(value, BaseException):
            tc.mock(name, error=value)
        else:
            tc.mock(name, value)
    # The RUN streamer always opens the SSE event stream before falling
    # back to the terminal GET; serve it empty unless the test cares.
    if all(name != "iter_operation_events" for name, _ in mocks):
        tc.mock("iter_operation_events", [])
    profile = Profile(name="test", url="http://x", token="t")
    PROFILE.set(profile)
    FORMATTER.set(JSONFormatter())
    CLIENT.set(tc)
    ctx = copy_context()
    previous_store = ctx.get(SESSION_STORE)
    with (
        patch("contree_cli.cli.build.session_db_path", lambda name: db_path),
        patch("contree_cli.cli.run.time.sleep"),
        patch("contree_client.base.time.sleep"),
    ):
        result = ctx.run(cmd_build, args)
    assert ctx.get(SESSION_STORE) is previous_store
    return result


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
    def test_store_is_closed_when_build_fails(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN false\n",
        )
        closed: list[SessionStore] = []
        original_close = SessionStore.close

        def track_close(store: SessionStore) -> None:
            original_close(store)
            closed.append(store)

        with patch.object(SessionStore, "close", track_close):
            rc = run_build(
                ContreeTestClient(),
                BuildArgs(context=str(context_dir)),
                [
                    make_tag_lookup(BASE_IMG),
                    ("spawn_instance", RuntimeError("spawn failed")),
                ],
                db_path,
            )

        assert rc == 1
        assert len(closed) == 1

    def test_from_run_creates_expected_api_calls(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nRUN echo hi\n",
        )
        tc = ContreeTestClient()
        args = BuildArgs(context=str(context_dir))
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_spawn(),
            make_op_success(NEW_IMG),
        ]
        rc = run_build(tc, args, mocks, db_path)
        assert rc is None
        # FROM's tag lookup, spawn, SSE events follow, the library's
        # terminal probe and the streamer's fallback payload fetch.
        assert [c.operation for c in tc.calls] == [
            "inspect_find_image_by_tag",
            "spawn_instance",
            "iter_operation_events",
            "get_operation_status",
            "get_operation_status",
        ]
        assert tc.calls_for("inspect_find_image_by_tag")[0].args == ("ubuntu:latest",)
        events_call = tc.calls_for("iter_operation_events")[0]
        assert events_call.args == ("op-1",)
        assert events_call.kwargs["follow"] is True
        assert tc.calls_for("get_operation_status")[0].args == ("op-1",)

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
        spawn = tc.calls_for("spawn_instance")[0]
        assert spawn.args == ("apt-get update", BASE_IMG)
        assert spawn.kwargs["shell"] is True

    def test_run_streams_stdout_live(self, context_dir, db_path, capsys):
        """Docker-compat mode: RUN output must reach the user's terminal
        as the SSE events arrive, not just after the op completes.

        Uses a stubbed streamer that writes a chunk to `sys.stdout.buffer`
        and returns a completion-populated summary so the build finishes
        with the streamed image; mirrors what a live SSE `stdout` frame
        followed by a `completion` frame would produce."""
        import sys

        from contree_client.models import OperationEvent

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
            summary.completion = OperationEvent.from_dict(
                {
                    "id": 2,
                    "ts": "2026-01-01T00:00:00+00:00",
                    "type": "completion",
                    "data": {
                        "status": "SUCCESS",
                        "duration_ms": 1000,
                        "result_image_uuid": NEW_IMG,
                        "error": None,
                        "image_size_bytes": 4096,
                    },
                }
            )
            summary.exit_event = OperationEvent.from_dict(
                {
                    "id": 1,
                    "ts": "2026-01-01T00:00:00+00:00",
                    "type": "exit",
                    "spid": 1,
                    "data": {"code": 0, "timed_out": False},
                }
            )
            summary.stdout.extend(b"hi from RUN\n")
            return summary

        with patch(
            "contree_cli.docker.kw_run.stream_events_until_close",
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
        # formatter record; verify both are present.
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
        assert [c.operation for c in second.calls] == ["inspect_find_image_by_tag"]

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
        # FROM tag lookup, spawn, SSE follow, the library's terminal
        # probe and the streamer's fallback payload fetch.
        assert [c.operation for c in second.calls] == [
            "inspect_find_image_by_tag",
            "spawn_instance",
            "iter_operation_events",
            "get_operation_status",
            "get_operation_status",
        ]

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
        mocks = [
            make_tag_lookup(BASE_IMG),
            ("get_file", NotFoundError(404, "not found")),
            ("upload_file", FileResponse(uuid="file-1", sha256="abc", size=11)),
            make_spawn(),
            make_op_success(NEW_IMG),
        ]
        rc = run_build(
            tc,
            BuildArgs(context=str(context_dir)),
            mocks,
            db_path,
        )
        assert rc is None
        spawn = tc.calls_for("spawn_instance")[0]
        files = spawn.kwargs["files"]
        assert "/app.py" in files
        assert files["/app.py"].uuid == "file-1"


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
        spawn = tc.calls_for("spawn_instance")[0]
        assert spawn.args[0] == "echo 2.5"

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
        spawn = tc.calls_for("spawn_instance")[0]
        assert spawn.args[0] == "echo /opt/streamforge"
        assert spawn.kwargs["env"] == {"APP_HOME": "/opt/streamforge"}

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
        spawn = tc.calls_for("spawn_instance")[0]
        assert spawn.args[0] == "echo /opt/streamforge"
        assert spawn.kwargs["env"] == {"APP_HOME": "/opt/streamforge"}


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
                (
                    "update_image_tag",
                    Image.from_dict({"uuid": NEW_IMG, "tag": "mybuild:test"}),
                ),
            ],
            db_path,
        )
        assert rc is None
        # The tag update lands after: tag lookup, spawn, SSE events,
        # and the streamer's terminal GET.
        assert tc.calls[-1].operation == "update_image_tag"
        tag_call = tc.calls_for("update_image_tag")[0]
        assert tag_call.args == (NEW_IMG, "mybuild:test")


# ── Multistage builds ────────────────────────────────────────────────


STAGE_IMG = "44444444-4444-4444-4444-444444444444"
FINAL_IMG = "55555555-5555-5555-5555-555555555555"


def tar_bytes(entries: dict[str, bytes], dirs: tuple[str, ...] = ()) -> bytes:
    """Build an in-memory tar the way the archive endpoint serves it:
    members rooted at the archived basename, no leading "./"."""
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def make_archive(entries: dict[str, bytes], dirs: tuple[str, ...] = ()) -> MockSpec:
    return ("inspect_image_archive", [tar_bytes(entries, dirs)])


def make_ensure_file(uuid: str = "tar-1", sha: str = "sha-1") -> MockSpec:
    return ("ensure_file", FileResponse(uuid=uuid, sha256=sha, size=1024))


def two_stage_dockerfile(copy_line: str) -> str:
    return (
        "FROM tag:ubuntu:latest AS build\n"
        "RUN make\n"
        "FROM tag:ubuntu:latest\n"
        f"{copy_line}\n"
        "RUN app\n"
    )


def extraction_spawn(tc: ContreeTestClient, index: int = 1):
    """The extraction RUN sits between the stage RUN and the final RUN."""
    return tc.calls_for("spawn_instance")[index]


class TestMultistage:
    def build_two_stage(self, tc, context_dir, db_path, copy_line, extra_mocks=()):
        write_dockerfile(context_dir, two_stage_dockerfile(copy_line))
        mocks = [
            make_tag_lookup(BASE_IMG),  # FROM ... AS build
            make_spawn("op-1"),  # RUN make
            make_op_success(STAGE_IMG, "op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),  # FROM (final stage)
            *extra_mocks,
            make_spawn("op-2"),  # extraction RUN
            make_op_success(NEW_IMG, "op-2"),
            make_op_success(NEW_IMG, "op-2"),
            make_spawn("op-3"),  # RUN app
            make_op_success(FINAL_IMG, "op-3"),
            make_op_success(FINAL_IMG, "op-3"),
        ]
        return run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)

    def test_copy_from_alias_single_file(self, context_dir, db_path):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=build /out/app /usr/local/bin/app",
            extra_mocks=[
                make_archive({"app": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None

        # The stage alias resolves locally: no image lookup beyond the
        # two FROM directives.
        assert len(tc.calls_for("inspect_find_image_by_tag")) == 2
        archive_call = tc.calls_for("inspect_image_archive")[0]
        assert archive_call.args == (STAGE_IMG, "/out/app")

        spawn = extraction_spawn(tc)
        files = spawn.kwargs["files"]
        assert files["/.contree-build/copy-0.tar"].uuid == "tar-1"
        command = spawn.args[0]
        assert "tar -xf /.contree-build/copy-0.tar" in command
        assert "mv /.contree-build/extract-0/app /usr/local/bin/app" in command
        assert "rm -rf /.contree-build" in command

    def test_copy_from_numeric_index(self, context_dir, db_path):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=0 /out/app /app",
            extra_mocks=[
                make_archive({"app": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None
        assert tc.calls_for("inspect_image_archive")[0].args == (STAGE_IMG, "/out/app")

    def test_copy_from_external_image(self, context_dir, db_path):
        tc = ContreeTestClient()
        tc.mock("resolve_image", STAGE_IMG)
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=someimage:latest /bin/tool /bin/tool",
            extra_mocks=[
                make_archive({"tool": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None
        # resolve_image also serves the FROM lookups once mocked; the
        # external --from reference must be among the resolved refs.
        refs = [call.args[0] for call in tc.calls_for("resolve_image")]
        assert "someimage:latest" in refs

    def test_copy_from_directory_source(self, context_dir, db_path):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=build /out /srv/out",
            extra_mocks=[
                make_archive(
                    {"out/a.txt": b"a", "out/sub/b.txt": b"b"},
                    dirs=("out", "out/sub"),
                ),
                make_ensure_file(),
            ],
        )
        assert rc is None
        command = extraction_spawn(tc).args[0]
        # Directory sources copy their CONTENTS into dest.
        assert "mkdir -p /srv/out" in command
        assert "cp -a /.contree-build/extract-0/out/. /srv/out/" in command

    def test_copy_from_file_into_dir_dest(self, context_dir, db_path):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=build /out/app /usr/local/bin/",
            extra_mocks=[
                make_archive({"app": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None
        command = extraction_spawn(tc).args[0]
        assert "mv /.contree-build/extract-0/app /usr/local/bin/app" in command

    def test_copy_from_chown_chmod(self, context_dir, db_path):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=build --chown=10:20 --chmod=0755 /out/app /app",
            extra_mocks=[
                make_archive({"app": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None
        command = extraction_spawn(tc).args[0]
        assert "chown -R 10:20 /.contree-build/extract-0/app" in command
        assert "chmod 755 /.contree-build/extract-0/app" in command

    def test_extraction_not_wrapped_with_user(self, context_dir, db_path):
        """COPY --from extracts as root even under an active USER."""
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\n"
            "RUN make\n"
            "FROM tag:ubuntu:latest\n"
            "USER app\n"
            "COPY --from=build /out/app /app\n"
            "RUN app\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_spawn("op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),
            make_archive({"app": b"binary"}),
            make_ensure_file(),
            make_spawn("op-2"),
            make_op_success(NEW_IMG, "op-2"),
            make_op_success(NEW_IMG, "op-2"),
            make_spawn("op-3"),
            make_op_success(FINAL_IMG, "op-3"),
            make_op_success(FINAL_IMG, "op-3"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        extraction = extraction_spawn(tc).args[0]
        assert "su -s" not in extraction
        # The user RUN after it is still wrapped.
        final = tc.calls_for("spawn_instance")[2].args[0]
        assert "su -s" in final

    def test_stage_with_pending_files_sealed_via_closer(self, context_dir, db_path):
        """A stage ending with a local COPY is committed by the closer
        RUN before the next FROM starts."""
        (context_dir / "a.txt").write_text("a")
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\n"
            "COPY a.txt /a.txt\n"
            "FROM tag:ubuntu:latest\n"
            "COPY --from=build /a.txt /b.txt\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            ("get_file", NotFoundError(404, "missing")),
            ("upload_file", FileResponse(uuid="file-1", sha256="s", size=1)),
            make_spawn("op-1"),  # closer RUN sealing stage `build`
            make_op_success(STAGE_IMG, "op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),
            make_archive({"a.txt": b"a"}),
            make_ensure_file(),
            make_spawn("op-2"),  # extraction RUN
            make_op_success(NEW_IMG, "op-2"),
            make_op_success(NEW_IMG, "op-2"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        closer = tc.calls_for("spawn_instance")[0]
        assert closer.args[0] == ":"
        assert "/a.txt" in closer.kwargs["files"]
        # The archive is exported from the sealed stage image.
        assert tc.calls_for("inspect_image_archive")[0].args == (STAGE_IMG, "/a.txt")

    def test_unknown_stage_fails(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nCOPY --from=nosuch /x /x\n",
        )
        tc = ContreeTestClient()
        tc.mock("resolve_image", error=NotFoundError(404, "no such image"))
        mocks = [make_tag_lookup(BASE_IMG)]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc == 1

    def test_missing_path_in_stage_fails(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\n"
            "RUN make\n"
            "FROM tag:ubuntu:latest\n"
            "COPY --from=build /nope /x\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_spawn("op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),
            ("inspect_image_archive", NotFoundError(404, "path not found")),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc == 1

    def test_add_from_fails(self, context_dir, db_path):
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nADD --from=build /x /x\n",
        )
        tc = ContreeTestClient()
        mocks = [make_tag_lookup(BASE_IMG)]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc == 1
