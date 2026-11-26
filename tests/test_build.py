from __future__ import annotations

import io
import posixpath
import tarfile
from contextvars import copy_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

import pytest
from conftest import ContreeTestClient, make_file_item
from contree_client.exceptions import NotFoundError
from contree_client.models import (
    DirectoryList,
    FileResponse,
    Image,
    InstanceSpawnResponse,
    OperationResponse,
)
from contree_client.profiles import Profile
from test_run import completion_event, make_event

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


def mock_destination_tree(client, directories):
    client.mock("inspect_image_list", DirectoryList(path="/", files=[]))

    def listing(image, path):
        return DirectoryList.from_dict(
            {"path": path, "files": directories.get(path, [])}
        )

    return patch.object(client, "inspect_image_list", side_effect=listing)


def make_keepalive_success(image: str, op_uuid: str = "op-1") -> MockSpec:
    response = make_op_success(image, op_uuid)[1]
    return ("wait_operation", response)


def build_events(response: OperationResponse, *, keepalive: bool):
    if keepalive:
        return [
            make_event(
                "lifecycle",
                {
                    "keep_alive": True,
                    "pid": None,
                    "cwd": "/",
                    "uid": 0,
                    "gid": 0,
                    "truncate_at": 65536,
                },
                spid=1,
            )
        ]
    return [
        make_event("exit", {"code": 0, "timed_out": False}, spid=1),
        completion_event(
            result_image_uuid=response.to_dict()["result"]["image"],
            duration_ms=1000,
        ),
    ]


def make_metadata_success(spid: int = 2) -> list[MockSpec]:
    return [
        ("operation_subprocess_create", spid),
        (
            "iter_operation_events",
            [
                make_event(
                    "exit",
                    {"code": 0, "timed_out": False},
                    spid=spid,
                    event_id=spid,
                )
            ],
        ),
    ]


def run_build(
    tc: ContreeTestClient,
    args: BuildArgs,
    mocks: list[MockSpec],
    db_path: Path,
):
    if "inspect_image_list" not in tc.mocks and all(
        name != "inspect_image_list" for name, value in mocks
    ):
        tc.mock("inspect_image_list", DirectoryList(path="/", files=[]))
    for name, value in mocks:
        if isinstance(value, BaseException):
            tc.mock(name, error=value)
        else:
            tc.mock(name, value)
        if (
            name in {"get_operation_status", "wait_operation"}
            and isinstance(value, OperationResponse)
            and value.kind == "instance"
        ):
            tc.mock(
                "iter_operation_events",
                build_events(value, keepalive=name == "wait_operation"),
            )
            if name == "wait_operation":
                tc.mock("operation_subprocess_kill", None)
                tc.mock("apply_instance_file", None)
    tc.uploaded_contents = []
    ensure_file = tc.ensure_file

    def capture_upload(content):
        position = content.tell()
        tc.uploaded_contents.append(content.read())
        content.seek(position)
        return ensure_file(content)

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
        patch.object(tc, "ensure_file", side_effect=capture_upload),
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
        # Completion events supply the result without a terminal GET.
        assert [c.operation for c in tc.calls] == [
            "inspect_find_image_by_tag",
            "spawn_instance",
            "iter_operation_events",
        ]
        assert tc.calls_for("inspect_find_image_by_tag")[0].args == ("ubuntu:latest",)
        events_call = tc.calls_for("iter_operation_events")[0]
        assert events_call.args == ("op-1",)
        assert events_call.kwargs["follow"] is True
        assert tc.calls_for("get_operation_status") == []

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
        # A forced rebuild consumes a new completion event.
        assert [c.operation for c in second.calls] == [
            "inspect_find_image_by_tag",
            "spawn_instance",
            "iter_operation_events",
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
    def test_copy_materializes_before_next_run(self, context_dir, db_path):
        (context_dir / "app.py").write_text("print('hi')")
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest\nCOPY app.py /app.py\nRUN python /app.py\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_ensure_file(),
            make_spawn(),
            make_keepalive_success(NEW_IMG),
            make_spawn("op-2"),
            make_op_success(NEW_IMG_2, "op-2"),
        ]
        assert (
            run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path) is None
        )
        copy, run = tc.calls_for("spawn_instance")
        assert copy.args == ("", BASE_IMG)
        assert copy.kwargs["shell"] is False
        assert run.args == ("python /app.py", NEW_IMG)
        assert tc.calls_for("apply_instance_file") == []
        assert copy.kwargs["files"]["/app.py"].uuid == "tar-1"
        assert tc.uploaded_contents == [b"print('hi')"]


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
        # Tag the result image after the completion event.
        assert tc.calls[-1].operation == "update_image_tag"
        tag_call = tc.calls_for("update_image_tag")[0]
        assert tag_call.args == (NEW_IMG, "mybuild:test")


# ── Multistage builds ────────────────────────────────────────────────


STAGE_IMG = "44444444-4444-4444-4444-444444444444"
FINAL_IMG = "55555555-5555-5555-5555-555555555555"


def tar_bytes(
    entries: dict[str, bytes],
    dirs: tuple[str, ...] = (),
    owner: tuple[int, int] = (0, 0),
) -> bytes:
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
            info.uid, info.gid = owner
            tar.addfile(info)
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.uid, info.gid = owner
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
    """The COPY instance runs between the stage RUN and the final RUN."""
    return tc.calls_for("spawn_instance")[index]


class TestMultistage:
    def build_two_stage(
        self, tc, context_dir, db_path, copy_line, extra_mocks=(), metadata_mocks=()
    ):
        write_dockerfile(context_dir, two_stage_dockerfile(copy_line))
        mocks = [
            make_tag_lookup(BASE_IMG),  # FROM ... AS build
            make_spawn("op-1"),  # RUN make
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),  # FROM (final stage)
            *extra_mocks,
            make_spawn("op-2"),  # COPY instance
            make_keepalive_success(NEW_IMG, "op-2"),
            *metadata_mocks,
            make_spawn("op-3"),  # RUN app
            make_op_success(FINAL_IMG, "op-3"),
        ]
        return run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)

    def test_from_reuses_sealed_stage(self, context_dir, db_path):
        """FROM <alias> reuses the sealed stage image locally, without
        resolving or importing it as an external reference."""
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\nRUN make\nFROM build AS next\nRUN app\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),  # FROM tag:ubuntu:latest AS build
            make_spawn("op-1"),  # RUN make
            make_op_success(STAGE_IMG, "op-1"),
            # FROM build AS next resolves locally: no further API call.
            make_spawn("op-2"),  # RUN app
            make_op_success(NEW_IMG, "op-2"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        assert len(tc.calls_for("inspect_find_image_by_tag")) == 1
        assert tc.calls_for("resolve_image") == []
        assert tc.calls_for("import_image") == []
        second_run = tc.calls_for("spawn_instance")[1]
        assert second_run.args[1] == STAGE_IMG

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
        assert archive_call.kwargs["compressed"] is False

        spawn = extraction_spawn(tc)
        assert spawn.args == ("", BASE_IMG)
        assert spawn.kwargs["shell"] is False
        applied = tc.calls_for("apply_instance_file")[0]
        assert applied.args == ("op-2", "tar-1", "/usr/local/bin")
        assert applied.kwargs == {"unpack": True, "replace": True, "merge": True}
        with tarfile.open(fileobj=io.BytesIO(tc.uploaded_contents[0])) as archive:
            assert archive.getnames() == ["app"]
            assert archive.extractfile("app").read() == b"binary"

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

    def test_copy_from_unresolved_image_imports(self, context_dir, db_path):
        """COPY --from=<external image> auto-imports on a resolve miss,
        matching FROM's own resolve_or_import fallback.

        Both FROM directives resolve "ubuntu:latest" via the first two
        queued `inspect_find_image_by_tag` outcomes; the COPY --from
        reference is a bare non-UUID tag too (resolve_image() falls
        through to the same typed call for those), so the third queued
        outcome -- a 404 -- drives it into the import fallback.
        """
        tc = ContreeTestClient()
        tc.mock("import_image", "import-op-1")
        tc.mock(
            "wait_operation",
            OperationResponse.from_dict(
                {
                    "uuid": "import-op-1",
                    "kind": "image_import",
                    "status": "SUCCESS",
                    "duration": 1.0,
                    "result": {"image": NEW_IMG_2, "tag": "someimage:latest"},
                }
            ),
        )
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            "COPY --from=someimage:latest /bin/tool /bin/tool",
            extra_mocks=[
                ("inspect_find_image_by_tag", NotFoundError(404, "no such image")),
                make_archive({"tool": b"binary"}),
                make_ensure_file(),
            ],
        )
        assert rc is None
        assert tc.calls_for("import_image")[0].kwargs["tag"] == "someimage:latest"
        archive_call = tc.calls_for("inspect_image_archive")[0]
        assert archive_call.args[0] == NEW_IMG_2

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
        applied = tc.calls_for("apply_instance_file")[0]
        assert applied.args == ("op-2", "tar-1", "/srv/out")
        with tarfile.open(fileobj=io.BytesIO(tc.uploaded_contents[0])) as archive:
            assert set(archive.getnames()) == {".", "sub", "a.txt", "sub/b.txt"}

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
        applied = tc.calls_for("apply_instance_file")[0]
        assert applied.args == ("op-2", "tar-1", "/usr/local/bin")
        with tarfile.open(fileobj=io.BytesIO(tc.uploaded_contents[0])) as archive:
            assert archive.getnames() == ["app"]

    @pytest.mark.parametrize("destination", ["/app", "/opt/app"])
    def test_copy_from_chown_chmod(self, context_dir, db_path, destination):
        tc = ContreeTestClient()
        rc = self.build_two_stage(
            tc,
            context_dir,
            db_path,
            f"COPY --from=build --chown=10:20 --chmod=0755 /out/app {destination}",
            extra_mocks=[
                make_archive({"app": b"binary"}),
                make_ensure_file(),
            ],
            metadata_mocks=[] if destination == "/app" else make_metadata_success(),
        )
        assert rc is None
        if destination == "/app":
            assert tc.calls_for("operation_subprocess_create") == []
            assert tc.calls_for("apply_instance_file") == []
            file = extraction_spawn(tc).kwargs["files"]["/app"]
            assert (file.uuid, file.uid, file.gid, file.mode) == (
                "tar-1",
                10,
                20,
                "0755",
            )
            assert tc.uploaded_contents == [b"binary"]
        else:
            metadata_call = tc.calls_for("operation_subprocess_create")[0]
            assert metadata_call.args == ("op-2", "chown")
            assert metadata_call.kwargs["args"] == ["-h", "10:20", "/opt/app"]
            assert metadata_call.kwargs["shell"] is False
            with tarfile.open(fileobj=io.BytesIO(tc.uploaded_contents[0])) as archive:
                assert archive.getmember("app").mode == 0o755

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
            make_tag_lookup(BASE_IMG),
            make_archive({"app": b"binary"}),
            make_ensure_file(),
            make_spawn("op-2"),
            make_keepalive_success(NEW_IMG, "op-2"),
            make_spawn("op-3"),
            make_op_success(FINAL_IMG, "op-3"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        extraction = extraction_spawn(tc).args[0]
        assert "su -s" not in extraction
        # The user RUN after it is still wrapped.
        final = tc.calls_for("spawn_instance")[2].args[0]
        assert "su -s" in final

    def test_stage_state_reset_on_from(self, context_dir, db_path):
        """USER, ENV, and WORKDIR do not leak into the next stage."""
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\n"
            "USER app\n"
            "ENV FOO=bar\n"
            "WORKDIR /src\n"
            "RUN make\n"
            "FROM tag:ubuntu:latest\n"
            "RUN id\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_spawn("op-1"),
            make_op_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),
            make_spawn("op-2"),
            make_op_success(NEW_IMG, "op-2"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        first, second = tc.calls_for("spawn_instance")
        assert "su -s" in first.args[0]
        assert first.kwargs["cwd"] == "/src"
        assert first.kwargs["env"] == {"FOO": "bar"}
        assert "su -s" not in second.args[0]
        assert second.kwargs["cwd"] is ...
        assert second.kwargs["env"] is ...

    @pytest.mark.parametrize("user", ["", "USER 1000:1000\n"], ids=["root", "user"])
    def test_stage_ending_with_copy_is_sealed_as_root(self, context_dir, db_path, user):
        """Commit COPY before the next stage without inheriting USER."""
        (context_dir / "a.txt").write_text("a")
        write_dockerfile(
            context_dir,
            "FROM tag:ubuntu:latest AS build\n"
            f"{user}COPY a.txt /a.txt\n"
            "FROM tag:ubuntu:latest\n"
            "COPY --from=build /a.txt /b.txt\n",
        )
        tc = ContreeTestClient()
        mocks = [
            make_tag_lookup(BASE_IMG),
            make_ensure_file("file-1", "s"),
            make_spawn("op-1"),  # COPY instance sealing stage `build`
            make_keepalive_success(STAGE_IMG, "op-1"),
            make_tag_lookup(BASE_IMG),
            make_archive({"a.txt": b"a"}),
            make_ensure_file(),
            make_spawn("op-2"),  # COPY instance
            make_keepalive_success(NEW_IMG, "op-2"),
        ]
        rc = run_build(tc, BuildArgs(context=str(context_dir)), mocks, db_path)
        assert rc is None
        closer = tc.calls_for("spawn_instance")[0]
        assert closer.args[0] == ""
        assert "su -s" not in closer.args[0]
        assert closer.kwargs["shell"] is False
        assert tc.calls_for("apply_instance_file") == []
        assert closer.kwargs["files"]["/a.txt"].uuid == "file-1"
        second = tc.calls_for("spawn_instance")[1]
        assert second.kwargs["files"]["/b.txt"].uuid == "tar-1"
        assert tc.uploaded_contents == [b"a", b"a"]
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


@pytest.mark.parametrize("from_image", [False, True])
def test_copy_file_into_existing_directory_without_trailing_slash(
    context_dir, db_path, from_image
):
    (context_dir / "artifact").write_bytes(b"binary")
    copy = (
        "COPY --from=tag:source /out/artifact /usr/local/bin"
        if from_image
        else "COPY artifact /usr/local/bin"
    )
    write_dockerfile(context_dir, f"FROM tag:base\n{copy}\n")
    client = ContreeTestClient()
    directories = {
        "/": [make_file_item("usr", is_dir=True, mode=0o755)],
        "/usr": [make_file_item("local", is_dir=True, mode=0o755)],
        "/usr/local": [make_file_item("bin", is_dir=True, mode=0o755)],
    }
    mocks = [make_tag_lookup(BASE_IMG)]
    if from_image:
        mocks.extend(
            [make_tag_lookup(STAGE_IMG), make_archive({"artifact": b"binary"})]
        )
    mocks.extend([make_ensure_file(), make_spawn(), make_keepalive_success(NEW_IMG)])

    with mock_destination_tree(client, directories) as listing:
        assert (
            run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path)
            is None
        )
    assert [call.args for call in listing.call_args_list[:3]] == [
        (BASE_IMG, "/"),
        (BASE_IMG, "/usr"),
        (BASE_IMG, "/usr/local"),
    ]
    applied = client.calls_for("apply_instance_file")[0]
    assert applied.args == ("op-1", "tar-1", "/usr/local/bin")
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        assert archive.getnames() == ["artifact"]
        assert archive.extractfile("artifact").read() == b"binary"
    assert client.calls_for("spawn_instance")[0].kwargs["shell"] is False


@pytest.mark.parametrize(
    "existing", [False, True], ids=["new-destination", "existing-destination"]
)
@pytest.mark.parametrize(
    "chown", ["", "0:0", "300:400"], ids=["source-owner", "root-owner", "custom-owner"]
)
def test_copy_from_restores_directory_ownership_through_subprocess_api(
    context_dir, db_path, existing, chown
):
    flag = f" --chown={chown}" if chown else ""
    write_dockerfile(
        context_dir, f"FROM tag:base\nCOPY --from=tag:source{flag} /tree /dst\n"
    )
    client = ContreeTestClient()
    directories = (
        {
            "/": [make_file_item("dst", is_dir=True, uid=500, gid=600, mode=0o700)],
            "/dst": [make_file_item("sub", is_dir=True, uid=700, gid=800, mode=0o755)],
        }
        if existing
        else {}
    )
    commands = []
    if chown != "0:0":
        paths = ["/dst/sub/file", "/dst/sub"]
        if chown and not existing:
            paths.append("/dst")
        commands.append(("chown", ["-h", chown or "100:200", *paths]))
    elif existing:
        commands.append(("chown", ["-h", "0:0", "/dst/sub"]))
    if existing:
        commands.append(("chmod", ["700", "/dst"]))
    archive = tar_bytes(
        {"tree/sub/file": b"source contents"}, ("tree", "tree/sub"), (100, 200)
    )
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_tag_lookup(STAGE_IMG),
        ("inspect_image_archive", [archive]),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]
    for spid in range(2, 2 + len(commands)):
        mocks.extend(make_metadata_success(spid))

    with mock_destination_tree(client, directories):
        assert (
            run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path)
            is None
        )

    calls = client.calls_for("operation_subprocess_create")
    assert [(call.args[1], call.kwargs["args"]) for call in calls] == commands
    for call in calls:
        assert call.args[0] == "op-1"
        assert call.kwargs["shell"] is False
        assert (call.kwargs["uid"], call.kwargs["gid"], call.kwargs["cwd"]) == (
            0,
            0,
            "/",
        )
    assert client.calls_for("apply_instance_file")[0].args == ("op-1", "tar-1", "/dst")
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as uploaded:
        assert uploaded.getnames() == [".", "sub", "sub/file"]
        assert uploaded.extractfile("sub/file").read() == b"source contents"
    assert len(client.calls_for("operation_subprocess_kill")) == 1


@pytest.mark.parametrize(
    "reinclude", [False, True], ids=["ignored-sibling", "included-unreadable-child"]
)
def test_copy_ignored_unreadable_directory_respects_exceptions(
    context_dir, db_path, reinclude, monkeypatch, caplog
):
    tree = context_dir / "tree"
    denied = tree / "blocked"
    denied.mkdir(parents=True)
    (tree / "keep.txt").write_text("included")
    (denied / "secret.txt").write_text("unreadable")
    exceptions = "!tree/blocked/secret.txt\n" if reinclude else ""
    (context_dir / ".dockerignore").write_text("tree/\n!tree/keep.txt\n" + exceptions)
    write_dockerfile(context_dir, "FROM tag:base\nCOPY tree/ /out/\n")
    iterdir = Path.iterdir

    def guarded_iterdir(path):
        if path == denied:
            raise PermissionError("included directory is unreadable")
        return iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded_iterdir)
    client = ContreeTestClient()
    mocks = [make_tag_lookup(BASE_IMG)]
    if not reinclude:
        mocks.extend(
            [make_ensure_file(), make_spawn(), make_keepalive_success(NEW_IMG)]
        )

    result = run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path)

    if reinclude:
        assert result == 1
        assert "included directory is unreadable" in caplog.text
        assert client.calls_for("ensure_file") == []
        assert client.calls_for("spawn_instance") == []
    else:
        assert result is None
        with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as uploaded:
            assert uploaded.getnames() == [".", "keep.txt"]
            assert uploaded.extractfile("keep.txt").read() == b"included"


def test_copy_missing_local_source_fails_before_upload(context_dir, db_path, caplog):
    write_dockerfile(context_dir, "FROM tag:base\nCOPY missing /out/\n")
    client = ContreeTestClient()

    result = run_build(
        client,
        BuildArgs(context=str(context_dir)),
        [make_tag_lookup(BASE_IMG)],
        db_path,
    )

    assert result == 1
    assert "missing" in caplog.text
    assert client.calls_for("ensure_file") == []
    assert client.calls_for("spawn_instance") == []


def test_copy_new_file_when_destination_parent_does_not_exist(context_dir, db_path):
    (context_dir / "artifact").write_bytes(b"binary")
    write_dockerfile(context_dir, "FROM tag:base\nCOPY artifact /new/name\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        ("inspect_image_list", NotFoundError(404, "parent missing")),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]

    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )

    assert client.calls_for("apply_instance_file")[0].args == ("op-1", "tar-1", "/new")
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        assert archive.getnames() == ["name"]


@pytest.mark.parametrize("source", ["/tools/python", "/linked-tools/python"])
def test_copy_from_image_uses_requested_symlink_basename(context_dir, db_path, source):
    write_dockerfile(
        context_dir, f"FROM tag:base\nCOPY --from=tag:source {source} /out/\n"
    )
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_tag_lookup(STAGE_IMG),
        make_archive({"python3.12": b"python executable"}),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]

    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )

    assert client.calls_for("inspect_image_archive")[0].args == (STAGE_IMG, source)
    assert client.calls_for("apply_instance_file")[0].args == ("op-1", "tar-1", "/out")
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        assert archive.getnames() == ["python"]
        assert archive.getmember("python").isreg()
        assert archive.extractfile("python").read() == b"python executable"


@pytest.mark.parametrize("directory", [False, True])
def test_copy_explicit_local_symlink_uses_target_contents(
    context_dir, db_path, directory
):
    target = context_dir / "target"
    if directory:
        target.mkdir()
        (target / "artifact").write_bytes(b"target contents")
    else:
        target.write_bytes(b"target contents")
    (context_dir / "link").symlink_to("target", target_is_directory=directory)
    write_dockerfile(context_dir, "FROM tag:base\nCOPY link /out/\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]

    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )

    assert client.calls_for("apply_instance_file")[0].args == ("op-1", "tar-1", "/out")
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        name = "artifact" if directory else "link"
        assert archive.getmember(name).isreg()
        assert archive.extractfile(name).read() == b"target contents"
        assert all(not member.issym() for member in archive.getmembers())


@pytest.mark.parametrize("target", ["original", "/original", "sub/../original"])
def test_copy_file_follows_destination_symlink(context_dir, db_path, target):
    (context_dir / "artifact").write_bytes(b"binary")
    write_dockerfile(context_dir, "FROM tag:base\nCOPY artifact /alias\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        (
            "inspect_image_list",
            DirectoryList.from_dict(
                {
                    "path": "/",
                    "files": [
                        make_file_item("alias", is_symlink=True, symlink_to=target),
                        make_file_item("original"),
                    ],
                }
            ),
        ),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]
    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )
    files = client.calls_for("spawn_instance")[0].kwargs["files"]
    assert set(files) == {"/original"}
    assert client.uploaded_contents == [b"binary"]


def test_copy_destination_symlink_cycle_fails_before_upload(context_dir, db_path):
    (context_dir / "artifact").write_bytes(b"binary")
    write_dockerfile(context_dir, "FROM tag:base\nCOPY artifact /alias\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        (
            "inspect_image_list",
            DirectoryList.from_dict(
                {
                    "path": "/",
                    "files": [
                        make_file_item("alias", is_symlink=True, symlink_to="alias")
                    ],
                }
            ),
        ),
    ]
    assert run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) == 1
    assert not client.calls_for("spawn_instance")
    assert not client.uploaded_contents


@pytest.mark.parametrize("destination", ["/", "/usr/local/bin/", "/usr/local/bin/."])
def test_explicit_directory_destination_preserves_filename(
    context_dir, db_path, destination
):
    (context_dir / "artifact").write_bytes(b"binary")
    write_dockerfile(context_dir, f"FROM tag:base\nCOPY artifact {destination}\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]

    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )

    if destination == "/":
        assert client.calls_for("apply_instance_file") == []
        spawn = client.calls_for("spawn_instance")[0]
        assert spawn.kwargs["files"]["/artifact"].uuid == "tar-1"
        assert client.uploaded_contents == [b"binary"]
    else:
        applied = client.calls_for("apply_instance_file")[0]
        assert applied.args[2] == posixpath.normpath(destination)
        with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
            assert archive.getnames() == ["artifact"]


def test_copy_explicit_ignored_directory_exception_keeps_bytes(context_dir, db_path):
    assets = context_dir / "assets"
    assets.mkdir()
    (assets / "keep.txt").write_bytes(b"kept content")
    (context_dir / ".dockerignore").write_text("assets/\n!assets/keep.txt\n")
    write_dockerfile(context_dir, "FROM tag:base\nCOPY assets/keep.txt /kept\n")
    client = ContreeTestClient()
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]
    assert (
        run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path) is None
    )
    assert client.uploaded_contents == [b"kept content"]
    files = client.calls_for("spawn_instance")[0].kwargs["files"]
    assert set(files) == {"/kept"}


@pytest.mark.parametrize("destination", ["/alias/leaf", "/alias/../out"])
def test_copy_resolves_parent_symlinks_before_dotdot(context_dir, db_path, destination):
    (context_dir / "artifact").write_bytes(b"binary")
    write_dockerfile(context_dir, f"FROM tag:base\nCOPY artifact {destination}\n")
    client = ContreeTestClient()
    directories = {
        "/": [
            make_file_item("alias", is_symlink=True, symlink_to="/real/dir"),
            make_file_item("real", is_dir=True, mode=0o755),
        ],
        "/real": [
            make_file_item("dir", is_dir=True, mode=0o755),
            make_file_item("out"),
        ],
        "/real/dir": [make_file_item("leaf", is_symlink=True, symlink_to="../out")],
    }
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]
    with mock_destination_tree(client, directories) as listing:
        assert (
            run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path)
            is None
        )
    assert client.calls_for("apply_instance_file")[0].args[2] == "/real"
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        assert archive.getnames() == ["out"]
        assert archive.extractfile("out").read() == b"binary"
    assert all(call.args[1] != "/alias" for call in listing.call_args_list)


@pytest.mark.parametrize("directory_source", [False, True])
@pytest.mark.parametrize("destination", ["/alias", "/alias/"])
def test_copy_into_directory_symlink_uses_resolved_destination(
    context_dir, db_path, directory_source, destination
):
    (context_dir / "tree").mkdir()
    (context_dir / "tree").chmod(0o777)
    (context_dir / "tree" / "artifact").write_bytes(b"binary")
    source = "tree/" if directory_source else "tree/artifact"
    write_dockerfile(context_dir, f"FROM tag:base\nCOPY {source} {destination}\n")
    client = ContreeTestClient()
    directories = {
        "/": [
            make_file_item("alias", is_symlink=True, symlink_to="/real/dir"),
            make_file_item("real", is_dir=True, mode=0o755),
        ],
        "/real": [make_file_item("dir", is_dir=True, mode=0o755)],
    }
    mocks = [
        make_tag_lookup(BASE_IMG),
        make_ensure_file(),
        make_spawn(),
        make_keepalive_success(NEW_IMG),
    ]
    with (
        mock_destination_tree(client, directories),
        patch("contree_cli.docker.transfer.run_metadata_command") as metadata,
    ):
        assert (
            run_build(client, BuildArgs(context=str(context_dir)), mocks, db_path)
            is None
        )
    if directory_source:
        metadata.assert_called_once_with(ANY, "op-1", "chmod", ["755", "/real/dir"])
    else:
        metadata.assert_not_called()
    assert client.calls_for("apply_instance_file")[0].args[2] == "/real/dir"
    with tarfile.open(fileobj=io.BytesIO(client.uploaded_contents[0])) as archive:
        assert archive.extractfile("artifact").read() == b"binary"
        assert archive.getmember("artifact").isreg()


def test_destination_resolution_can_leave_missing_subtree():
    from contree_cli.docker.kw_copy import resolve_destination

    client = ContreeTestClient()
    directories = {
        "/": [
            make_file_item("alias", is_symlink=True, symlink_to="/real"),
            make_file_item("real", is_dir=True, mode=0o755),
        ]
    }
    context = SimpleNamespace(client=client, last_image=BASE_IMG)
    with mock_destination_tree(client, directories) as listing:
        assert resolve_destination(context, "/missing/child/../../alias") == (
            "/real",
            True,
        )
    assert [call.args[1] for call in listing.call_args_list] == ["/"]


@pytest.mark.parametrize("count", [40, 41])
def test_destination_resolution_limits_symlink_expansions(count):
    from contree_cli.docker.kw_copy import resolve_destination

    client = ContreeTestClient()
    directories = {
        "/": [
            make_file_item(
                f"link{index}", is_symlink=True, symlink_to=f"link{index + 1}"
            )
            for index in range(count)
        ]
    }
    context = SimpleNamespace(client=client, last_image=BASE_IMG)
    with mock_destination_tree(client, directories):
        if count == 40:
            assert resolve_destination(context, "/link0") == ("/link40", False)
        else:
            with pytest.raises(ValueError, match="too many symlinks"):
                resolve_destination(context, "/link0")
