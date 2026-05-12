from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from unittest.mock import MagicMock, patch

from conftest import ContreeTestClient

from contree_cli.session import ImageCache, Session
from contree_cli.shell.completer import ShellCompleter
from contree_cli.shell.parser import build_shell_parser, get_command_names


def _make_completer(**kwargs) -> ShellCompleter:
    _, commands = build_shell_parser()
    return ShellCompleter(commands, **kwargs)


def _make_file(
    path: str,
    *,
    is_dir: bool = False,
    is_symlink: bool = False,
) -> dict:
    return {
        "path": path,
        "size": 128,
        "mode": 0o644,
        "owner": "root",
        "group": "root",
        "mtime": 1700000000,
        "is_dir": is_dir,
        "is_symlink": is_symlink,
    }


def _path_completer(
    files: list[dict],
    cache: ImageCache,
    cwd: str = "",
) -> tuple[ShellCompleter, ContreeTestClient]:
    """Build a completer with ContreeTestClient that returns given files."""
    client = ContreeTestClient()
    client.respond_json({"path": "/etc", "files": files})

    store = MagicMock()
    store.session = Session(
        session_key="test",
        active_branch="main",
        current_image="a1b2c3d4-5678-9abc-def0-111111111111",
        last_kind="run",
        last_title="test",
        updated_at="2025-01-01",
    )
    store.get_cwd.return_value = cwd

    def _resolve(path: str) -> str:
        if not path:
            return cwd or "/"
        if not PurePosixPath(path).is_absolute():
            path = (cwd or "/").rstrip("/") + "/" + path
        return posixpath.normpath(path)

    store.resolve_path.side_effect = _resolve
    store.cache = cache

    completer = _make_completer(client=client, store=store)
    return completer, client


def _complete_line(
    completer: ShellCompleter,
    text: str,
    line: str,
    begidx: int | None = None,
) -> list[str]:
    """Simulate readline completing `text` within `line`."""
    if begidx is None:
        begidx = line.rindex(text) if text else len(line)
    return completer.compute_completions(text, line, begidx)


class TestFirstTokenCompletion:
    """Empty input / first-token completion returns shell commands."""

    def test_empty_input_returns_shell_commands(self):
        completer = _make_completer()
        results = _complete_line(completer, "", "")
        for name in (
            "contree",
            "exit",
            "quit",
            "help",
            "history",
            "cd",
            "pwd",
            "ls",
            "cat",
            "vim",
            "vi",
            "nano",
        ):
            assert name + " " in results

    def test_partial_contree_completes(self):
        completer = _make_completer()
        results = _complete_line(completer, "con", "con")
        assert "contree " in results

    def test_partial_exit_completes(self):
        completer = _make_completer()
        results = _complete_line(completer, "ex", "ex")
        assert "exit " in results

    def test_partial_ls_completes(self):
        completer = _make_completer()
        results = _complete_line(completer, "ls", "ls")
        assert "ls " in results

    def test_partial_cd_completes(self):
        completer = _make_completer()
        results = _complete_line(completer, "cd", "cd")
        assert "cd " in results

    def test_partial_vim_completes(self):
        completer = _make_completer()
        results = _complete_line(completer, "vi", "vi")
        assert "vim " in results
        assert "vi " in results

    def test_unknown_prefix_empty(self):
        completer = _make_completer()
        results = _complete_line(completer, "zzz", "zzz")
        assert results == []


class TestContreeCommandCompletion:
    """``contree <TAB>`` completes management command names."""

    def test_contree_prefix_returns_all_commands(self):
        completer = _make_completer()
        results = _complete_line(completer, "", "contree ")
        command_names = get_command_names()
        assert len(results) == len(command_names)
        for name in command_names:
            assert name + " " in results

    def test_contree_partial_command_filters(self):
        completer = _make_completer()
        results = _complete_line(completer, "ru", "contree ru", begidx=8)
        assert "run " in results
        assert "ls " not in results

    def test_contree_alias_completion(self):
        completer = _make_completer()
        results = _complete_line(completer, "ci", "contree ci", begidx=8)
        assert "ci " in results

    def test_contree_unknown_subcommand_empty(self):
        completer = _make_completer()
        results = _complete_line(completer, "zzz", "contree zzz", begidx=8)
        assert results == []


class TestContreeFlagCompletion:
    """``contree run --<TAB>`` completes flags."""

    def test_run_flags(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "--",
            "contree run --",
            begidx=12,
        )
        flag_names = [r.rstrip(" ") for r in results]
        assert "--timeout" in flag_names
        assert "--shell" in flag_names
        assert "--disposable" in flag_names

    def test_single_dash(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "-",
            "contree run -",
            begidx=12,
        )
        # Should include short flags like -t, -s, -D etc.
        assert len(results) > 0

    def test_partial_flag(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "--tim",
            "contree run --tim",
            begidx=12,
        )
        assert "--timeout " in results


class TestContreeSubcommandCompletion:
    """``contree file <TAB>`` completes subcommands."""

    def test_file_subcommands(self):
        completer = _make_completer()
        results = _complete_line(completer, "", "contree file ")
        names = [r.rstrip(" ") for r in results]
        assert "edit" in names
        assert "cp" in names

    def test_file_partial_subcommand(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "e",
            "contree file e",
            begidx=13,
        )
        names = [r.rstrip(" ") for r in results]
        assert "edit" in names or "e" in names

    def test_session_subcommands(self):
        completer = _make_completer()
        results = _complete_line(completer, "", "contree session ")
        names = [r.rstrip(" ") for r in results]
        assert "branch" in names
        assert "show" in names


class TestHelpCompletion:
    """``help <TAB>`` completes all known command names."""

    def test_help_completes_command_names(self):
        completer = _make_completer()
        results = _complete_line(completer, "", "help ")
        # Should include contree subcommands, aliases, editors, cd
        names = [r.rstrip(" ") for r in results]
        assert "ls" in names
        assert "cat" in names
        assert "vim" in names
        assert "cd" in names
        assert "run" in names

    def test_help_partial_filters(self):
        completer = _make_completer()
        results = _complete_line(completer, "ru", "help ru", begidx=5)
        assert "run " in results


class TestContreeAliasCompletion:
    """Bare ``ls`` and ``cat`` complete as contree commands."""

    def test_bare_ls_completes_paths(self, image_cache):
        files = [
            _make_file("/etc/hosts"),
            _make_file("/etc/hostname"),
        ]
        completer = _path_completer(files, image_cache)[0]

        results = _complete_line(
            completer,
            "/etc/ho",
            "ls /etc/ho",
            begidx=3,
        )

        assert "/etc/hosts " in results
        assert "/etc/hostname " in results

    def test_bare_cat_completes_paths(self, image_cache):
        files = [_make_file("/etc/hosts")]
        completer = _path_completer(files, image_cache)[0]

        results = _complete_line(
            completer,
            "/etc/ho",
            "cat /etc/ho",
            begidx=4,
        )

        assert "/etc/hosts " in results


class TestEditorCompletion:
    """``vim``, ``nano`` complete sandbox paths."""

    def test_vim_completes_paths(self, image_cache):
        files = [_make_file("/app/main.py")]
        completer = _path_completer(files, image_cache)[0]

        results = _complete_line(
            completer,
            "/app/main",
            "vim /app/main",
            begidx=4,
        )

        assert "/app/main.py " in results

    def test_nano_completes_paths(self, image_cache):
        files = [_make_file("/etc/config.ini")]
        completer = _path_completer(files, image_cache)[0]

        results = _complete_line(
            completer,
            "/etc/con",
            "nano /etc/con",
            begidx=5,
        )

        assert "/etc/config.ini " in results


class TestCdCompletion:
    """``cd`` completes only directories."""

    def test_cd_completes_directories_only(self, image_cache):
        files = [
            _make_file("/etc/conf.d", is_dir=True),
            _make_file("/etc/hosts"),
        ]
        completer = _path_completer(files, image_cache)[0]

        results = _complete_line(
            completer,
            "/etc/",
            "cd /etc/",
            begidx=3,
        )

        assert "/etc/conf.d/" in results
        assert "/etc/hosts " not in results

    def test_cd_no_client_returns_empty(self):
        completer = _make_completer(client=None, store=None)
        results = _complete_line(completer, "/etc", "cd /etc", begidx=3)
        assert results == []


class TestContainerPathCompletion:
    """Container path completion for ``contree ls`` and implicit run."""

    def test_no_client_returns_empty(self):
        completer = _make_completer(client=None, store=None)
        results = _complete_line(completer, "/etc", "grep /etc", begidx=5)
        assert results == []

    def test_no_session_returns_empty(self):
        store = MagicMock()
        store.session = None
        completer = _make_completer(client=MagicMock(), store=store)
        results = _complete_line(completer, "/etc", "grep /etc", begidx=5)
        assert results == []

    def test_contree_ls_completes_paths(self, image_cache):
        """contree ls /etc/<TAB> completes via inspect API."""
        files = [
            _make_file("/etc/hosts"),
            _make_file("/etc/hostname"),
            _make_file("/etc/passwd"),
        ]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/etc/ho",
            "contree ls /etc/ho",
            begidx=11,
        )

        assert "/etc/hosts " in results
        assert "/etc/hostname " in results
        assert "/etc/passwd " not in results

    def test_directories_get_trailing_slash(self, image_cache):
        files = [
            _make_file("/etc/conf.d", is_dir=True),
            _make_file("/etc/hosts"),
        ]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/etc/",
            "contree ls /etc/",
            begidx=11,
        )

        assert "/etc/conf.d/" in results
        assert "/etc/hosts " in results

    def test_empty_prefix_returns_all(self, image_cache):
        files = [
            _make_file("/etc/hosts"),
            _make_file("/etc/passwd"),
        ]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/etc/",
            "contree ls /etc/",
            begidx=11,
        )

        assert len(results) == 2

    def test_contree_cat_completes_paths(self, image_cache):
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/etc/ho",
            "contree cat /etc/ho",
            begidx=12,
        )

        assert "/etc/hosts " in results

    def test_contree_file_edit_completes_paths(self, image_cache):
        files = [_make_file("/app/main.py")]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/app/main",
            "contree file edit /app/main",
            begidx=18,
        )

        assert "/app/main.py " in results

    def test_api_error_returns_empty(self):
        """API failures during completion should not crash."""
        client = ContreeTestClient()
        store = MagicMock()
        store.session = Session(
            session_key="test",
            active_branch="main",
            current_image="a1b2c3d4-5678-9abc-def0-111111111111",
            last_kind="run",
            last_title="test",
            updated_at="2025-01-01",
        )
        # Queue a response that will cause an error when parsed
        client.fake.responses.append(
            type(
                "ErrorResp",
                (),
                {
                    "status": 500,
                    "reason": "Error",
                    "read": lambda self, amt=None: b"error",
                },
            )()
        )
        completer = _make_completer(client=client, store=store)

        results = _complete_line(
            completer,
            "",
            "contree ls /etc/",
            begidx=11,
        )

        assert results == []

    def test_bare_non_path_no_completion(self):
        """Bare command args that don't start with / get no completion."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "hello",
            "echo hello",
            begidx=5,
        )
        assert results == []


class TestContreeCdCompletion:
    """``contree cd /etc/<TAB>`` completes directories only."""

    def test_contree_cd_completes_dirs_only(self, image_cache):
        files = [
            _make_file("/etc/conf.d", is_dir=True),
            _make_file("/etc/hosts"),
        ]
        completer, _client = _path_completer(files, image_cache)

        results = _complete_line(
            completer,
            "/etc/",
            "contree cd /etc/",
            begidx=11,
        )

        assert "/etc/conf.d/" in results
        assert "/etc/hosts " not in results

    def test_contree_cd_in_command_names(self):
        completer = _make_completer()
        results = _complete_line(completer, "cd", "contree cd", begidx=8)
        assert "cd " in results


class TestCwdAwareCompletion:
    """Relative path completion resolves against session cwd."""

    def test_relative_text_uses_cwd(self, image_cache):
        """Typing 'src' after cd /app should query /app/ and return 'src/'."""
        files = [
            _make_file("/app/src", is_dir=True),
            _make_file("/app/main.py"),
        ]
        completer, _client = _path_completer(files, image_cache, cwd="/app")

        results = _complete_line(
            completer,
            "sr",
            "cd sr",
            begidx=3,
        )

        assert "src/" in results

    def test_relative_subdir_uses_cwd(self, image_cache):
        """Typing 'src/' after cd /app should query /app/src/."""
        files = [
            _make_file("/app/src/lib", is_dir=True),
            _make_file("/app/src/main.py"),
        ]
        completer, _client = _path_completer(files, image_cache, cwd="/app")

        results = _complete_line(
            completer,
            "src/",
            "cat src/",
            begidx=4,
        )

        assert "src/main.py " in results
        assert "src/lib/" in results

    def test_relative_vim_uses_cwd(self, image_cache):
        """Typing 'main' after cd /app in vim should query /app/."""
        files = [_make_file("/app/main.py")]
        completer, _client = _path_completer(files, image_cache, cwd="/app")

        results = _complete_line(
            completer,
            "main",
            "vim main",
            begidx=4,
        )

        assert "main.py " in results

    def test_absolute_path_ignores_cwd(self, image_cache):
        """Absolute paths should not be affected by cwd."""
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache, cwd="/app")

        results = _complete_line(
            completer,
            "/etc/ho",
            "cat /etc/ho",
            begidx=4,
        )

        assert "/etc/hosts " in results

    def test_empty_cwd_defaults_to_root(self, image_cache):
        """When cwd is empty, relative paths resolve against /."""
        files = [_make_file("/etc", is_dir=True)]
        completer, _client = _path_completer(files, image_cache, cwd="")

        results = _complete_line(
            completer,
            "et",
            "cd et",
            begidx=3,
        )

        assert "etc/" in results

    def test_root_slash_does_not_produce_double_slash(self, image_cache):
        """'ls /' must query '/' not '//' (server returns 500 on '//')."""
        files = [_make_file("/etc", is_dir=True)]
        completer, _client = _path_completer(files, image_cache, cwd="/")

        with patch.object(completer, "list_dir", wraps=completer.list_dir) as spy:
            _complete_line(completer, "/", "ls /", begidx=3)
        spy.assert_called_once()
        queried_path = spy.call_args[0][1]
        assert "//" not in queried_path

    def test_dotdot_normalized_in_completion(self, image_cache):
        """'cat ../etc/' after cd /tmp should query /etc/, not /tmp/../etc/."""
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache, cwd="/tmp")

        with patch.object(completer, "list_dir", wraps=completer.list_dir) as spy:
            results = _complete_line(
                completer,
                "../etc/",
                "cat ../etc/",
                begidx=4,
            )
        spy.assert_called_once()
        queried_path = spy.call_args[0][1]
        assert queried_path == "/etc/"
        assert "../etc/hosts " in results


class TestReadlineInterface:
    def test_complete_state_0(self):
        completer = _make_completer()
        # Mock readline to provide line buffer
        mock_rl = MagicMock()
        mock_rl.get_line_buffer.return_value = "con"
        mock_rl.get_begidx.return_value = 0

        with (
            patch.dict("sys.modules", {"readline": mock_rl}),
            patch("contree_cli.shell.completer.readline", mock_rl, create=True),
        ):
            # Direct test of state iteration
            completer._matches = ["contree "]
            assert completer.complete("con", 0) == "contree "
            assert completer.complete("con", 1) is None


def _make_image(
    uuid: str,
    tag: str | None = None,
) -> dict[str, object]:
    return {"uuid": uuid, "tag": tag, "created_at": "2025-01-01"}


def _image_completer(
    images: list[dict[str, object]],
    cache: ImageCache,
) -> tuple[ShellCompleter, ContreeTestClient]:
    """Build a completer with ContreeTestClient that returns given images."""
    client = ContreeTestClient()
    client.respond_json({"images": images})

    store = MagicMock()
    store.cache = cache
    completer = _make_completer(client=client, store=store)
    return completer, client


class TestImageCompletion:
    """``use`` and ``tag`` complete image tags and UUIDs."""

    def test_use_completes_tags(self, image_cache):
        images = [
            _make_image("aaaa-1111", tag="common/python-ml/python:3.11-slim"),
            _make_image("bbbb-2222", tag="myproj/dev-env/ubuntu:noble"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "tag:",
            "contree use tag:",
            begidx=12,
        )

        assert "tag:common/python-ml/python:3.11-slim " in results
        assert "tag:myproj/dev-env/ubuntu:noble " in results

    def test_use_completes_tags_with_prefix(self, image_cache):
        images = [
            _make_image("aaaa-1111", tag="common/python-ml/python:3.11-slim"),
            _make_image("bbbb-2222", tag="myproj/dev-env/ubuntu:noble"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "tag:common/",
            "contree use tag:common/",
            begidx=12,
        )

        assert "tag:common/python-ml/python:3.11-slim " in results
        assert "tag:myproj/dev-env/ubuntu:noble " not in results

    def test_use_completes_uuids(self, image_cache):
        images = [
            _make_image("a1b2c3d4-5678-9abc-def0-111111111111"),
            _make_image("ffffffff-0000-1111-2222-333333333333"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "a1b2",
            "contree use a1b2",
            begidx=12,
        )

        assert "a1b2c3d4-5678-9abc-def0-111111111111 " in results
        assert "ffffffff-0000-1111-2222-333333333333 " not in results

    def test_tag_completes_images(self, image_cache):
        images = [
            _make_image("aaaa-1111", tag="common/rust/ubuntu:noble"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "",
            "contree tag ",
            begidx=12,
        )

        assert "tag:common/rust/ubuntu:noble " in results
        assert "aaaa-1111 " in results

    def test_image_cache_persists(self, image_cache):
        """Second call returns cached data without hitting the API."""
        images = [_make_image("aaaa-1111", tag="old/tag")]
        completer, client = _image_completer(images, image_cache)

        results1 = _complete_line(
            completer,
            "tag:",
            "contree use tag:",
            begidx=12,
        )
        assert "tag:old/tag " in results1

        # Second call -- should NOT call the API again (cached)
        # Queue a different response that should NOT be used
        client.respond_json(
            {"images": [_make_image("bbbb-2222", tag="new/tag")]},
        )
        results2 = _complete_line(
            completer,
            "tag:",
            "contree use tag:",
            begidx=12,
        )

        # Still returns cached data
        assert "tag:old/tag " in results2
        assert "tag:new/tag " not in results2

    def test_no_client_returns_empty(self):
        completer = _make_completer(client=None)
        results = _complete_line(
            completer,
            "tag:",
            "contree use tag:",
            begidx=12,
        )
        assert results == []

    def test_api_error_returns_empty(self):
        client = ContreeTestClient()
        # Queue an error response
        client.respond(status=500, body=b"error")
        completer = _make_completer(client=client)

        results = _complete_line(
            completer,
            "tag:",
            "contree use tag:",
            begidx=12,
        )

        assert results == []

    def test_bare_text_completes_as_tag(self, image_cache):
        """Typing ``common<TAB>`` (no ``tag:`` prefix) offers ``tag:common/...``."""
        images = [
            _make_image("aaaa-1111", tag="common/python-ml/python:3.11-slim"),
            _make_image("bbbb-2222", tag="myproj/dev-env/ubuntu:noble"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "common",
            "contree use common",
            begidx=12,
        )

        assert "tag:common/python-ml/python:3.11-slim " in results
        assert "tag:myproj/dev-env/ubuntu:noble " not in results

    def test_bare_empty_offers_all_tags_and_uuids(self, image_cache):
        """Empty text offers both ``tag:NAME`` and UUID candidates."""
        images = [
            _make_image("aaaa-1111", tag="common/rust/ubuntu:noble"),
        ]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "",
            "contree use ",
            begidx=12,
        )

        assert "tag:common/rust/ubuntu:noble " in results
        assert "aaaa-1111 " in results

    def test_untagged_images_only_return_uuid(self, image_cache):
        images = [_make_image("aaaa-1111", tag=None)]
        completer, _client = _image_completer(images, image_cache)

        results = _complete_line(
            completer,
            "",
            "contree use ",
            begidx=12,
        )

        assert "aaaa-1111 " in results
        # No tag: prefix entries
        assert all(not r.startswith("tag:") for r in results)


def _make_operation(
    uuid: str,
    state: str = "SUCCESS",
) -> dict[str, object]:
    return {
        "uuid": uuid,
        "state": state,
        "kind": "instance",
        "created_at": "2025-01-01",
    }


def _op_completer(
    operations: list[dict[str, object]],
    cache: ImageCache,
) -> tuple[ShellCompleter, ContreeTestClient]:
    """Build a completer with ContreeTestClient that returns given operations."""
    client = ContreeTestClient()
    client.respond_json({"operations": operations})

    store = MagicMock()
    store.cache = cache
    completer = _make_completer(client=client, store=store)
    return completer, client


class TestOperationCompletion:
    """``show`` and ``kill`` complete operation UUIDs."""

    def test_show_completes_operation_uuid(self, image_cache):
        ops = [
            _make_operation("op-aaaa-1111-2222-3333"),
            _make_operation("op-bbbb-4444-5555-6666"),
        ]
        completer, _client = _op_completer(ops, image_cache)

        results = _complete_line(
            completer,
            "op-a",
            "contree show op-a",
            begidx=13,
        )

        assert "op-aaaa-1111-2222-3333 " in results
        assert "op-bbbb-4444-5555-6666 " not in results

    def test_kill_completes_operation_uuid(self, image_cache):
        ops = [
            _make_operation("op-aaaa-1111-2222-3333"),
            _make_operation("op-bbbb-4444-5555-6666"),
        ]
        completer, _client = _op_completer(ops, image_cache)

        results = _complete_line(
            completer,
            "",
            "contree kill ",
            begidx=13,
        )

        assert "op-aaaa-1111-2222-3333 " in results
        assert "op-bbbb-4444-5555-6666 " in results

    def test_operation_empty_on_api_failure(self):
        client = ContreeTestClient()
        client.respond(status=500, body=b"error")
        completer = _make_completer(client=client)

        results = _complete_line(
            completer,
            "op-",
            "contree show op-",
            begidx=13,
        )

        assert results == []

    def test_operation_prefix_filter(self, image_cache):
        ops = [
            _make_operation("aaaa-1111"),
            _make_operation("aaaa-2222"),
            _make_operation("bbbb-3333"),
        ]
        completer, _client = _op_completer(ops, image_cache)

        results = _complete_line(
            completer,
            "aaaa",
            "contree show aaaa",
            begidx=13,
        )

        assert "aaaa-1111 " in results
        assert "aaaa-2222 " in results
        assert "bbbb-3333 " not in results


def _session_completer(
    cache: ImageCache,
    sessions: list[Session] | None = None,
    branches: list[tuple[str, bool]] | None = None,
) -> ShellCompleter:
    """Build a completer with mocked store for session/branch completion."""
    store = MagicMock()
    store.list_sessions.return_value = sessions or []
    store.list_branches.return_value = branches or []
    store.cache = cache
    return _make_completer(client=MagicMock(), store=store)


class TestSessionCompletion:
    """``session use`` completes session names, ``session checkout`` branches."""

    def test_session_use_completes_session_names(self, image_cache):
        sessions = [
            Session(
                session_key="prof_abc123",
                active_branch="main",
                current_image="img-1",
                last_kind="run",
                last_title="test",
                updated_at="2025-01-01",
            ),
            Session(
                session_key="prof_def456",
                active_branch="main",
                current_image="img-2",
                last_kind="run",
                last_title="test",
                updated_at="2025-01-01",
            ),
        ]
        completer = _session_completer(image_cache, sessions=sessions)
        results = _complete_line(
            completer,
            "",
            "contree session use ",
            begidx=20,
        )

        assert "prof_abc123 " in results
        assert "prof_def456 " in results
        # Suffix matches too
        assert "abc123 " in results
        assert "def456 " in results

    def test_session_use_filters_by_prefix(self, image_cache):
        sessions = [
            Session(
                session_key="prof_abc123",
                active_branch="main",
                current_image="img-1",
                last_kind="run",
                last_title="test",
                updated_at="2025-01-01",
            ),
            Session(
                session_key="prof_def456",
                active_branch="main",
                current_image="img-2",
                last_kind="run",
                last_title="test",
                updated_at="2025-01-01",
            ),
        ]
        completer = _session_completer(image_cache, sessions=sessions)
        results = _complete_line(
            completer,
            "abc",
            "contree session use abc",
            begidx=20,
        )

        assert "abc123 " in results
        assert "def456 " not in results
        assert "prof_def456 " not in results

    def test_session_checkout_completes_branches(self, image_cache):
        branches = [("main", True), ("feature", False), ("bugfix", False)]
        completer = _session_completer(image_cache, branches=branches)
        results = _complete_line(
            completer,
            "",
            "contree session checkout ",
            begidx=24,
        )

        assert "main " in results
        assert "feature " in results
        assert "bugfix " in results

    def test_session_co_alias(self, image_cache):
        branches = [("main", True), ("feature", False)]
        completer = _session_completer(image_cache, branches=branches)
        results = _complete_line(
            completer,
            "f",
            "contree session co f",
            begidx=19,
        )

        assert "feature " in results
        assert "main " not in results

    def test_session_branch_completes_branches(self, image_cache):
        branches = [("main", True), ("dev", False)]
        completer = _session_completer(image_cache, branches=branches)
        results = _complete_line(
            completer,
            "",
            "contree session branch foo --from ",
            begidx=34,
        )

        assert "main " in results
        assert "dev " in results

    def test_session_br_alias(self, image_cache):
        branches = [("main", True), ("dev", False)]
        completer = _session_completer(image_cache, branches=branches)
        results = _complete_line(
            completer,
            "d",
            "contree session br foo --from d",
            begidx=31,
        )

        assert "dev " in results
        assert "main " not in results

    def test_no_store_returns_empty(self):
        completer = _make_completer(client=MagicMock(), store=None)
        results = _complete_line(
            completer,
            "",
            "contree session use ",
            begidx=20,
        )
        assert results == []

    def test_store_error_returns_empty(self, image_cache):
        store = MagicMock()
        store.list_sessions.side_effect = Exception("db error")
        store.cache = image_cache
        completer = _make_completer(client=MagicMock(), store=store)
        results = _complete_line(
            completer,
            "",
            "contree session use ",
            begidx=20,
        )
        assert results == []


class TestFormatCompletion:
    """``--format`` and ``-f`` complete format names."""

    def test_format_completes_json(self):
        """'--format j<TAB>' completes to json and json-pretty."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "j",
            "--format j",
            begidx=9,
        )
        names = [r.rstrip(" ") for r in results]
        assert "json" in names
        assert "json-pretty" in names

    def test_format_completes_all(self):
        """'--format <TAB>' returns all format names."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "--format ",
            begidx=9,
        )
        from contree_cli.output import FORMATTERS

        for name in FORMATTERS:
            assert name + " " in results

    def test_short_flag_completes_formats(self):
        """'-f <TAB>' returns format names."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "-f ",
            begidx=3,
        )
        from contree_cli.output import FORMATTERS

        for name in FORMATTERS:
            assert name + " " in results

    def test_contree_f_flag_completes_formats(self):
        """'contree -f <TAB>' completes format names."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "contree -f ",
            begidx=11,
        )
        from contree_cli.output import FORMATTERS

        for name in FORMATTERS:
            assert name + " " in results

    def test_contree_format_flag_completes(self):
        """'contree --format t<TAB>' completes table and tsv."""
        completer = _make_completer()
        results = _complete_line(
            completer,
            "t",
            "contree --format t",
            begidx=17,
        )
        names = [r.rstrip(" ") for r in results]
        assert "table" in names
        assert "tsv" in names
        assert "json" not in names


class TestArgparseDrivenCompletion:
    """New argparse-walker dispatch covers nested subcommand positionals."""

    def test_session_delete_completes_session_keys(self, image_cache):
        sessions = [
            Session(
                session_key="alpha_keep",
                active_branch="main",
                current_image="img-1",
                last_kind="run",
                last_title="t",
                updated_at="2025-01-01",
            ),
            Session(
                session_key="beta_drop",
                active_branch="main",
                current_image="img-2",
                last_kind="run",
                last_title="t",
                updated_at="2025-01-01",
            ),
        ]
        completer = _session_completer(image_cache, sessions=sessions)
        results = _complete_line(
            completer,
            "",
            "contree session delete ",
            begidx=23,
        )
        assert "alpha_keep " in results
        assert "beta_drop " in results

    def test_session_show_completes_session_keys(self, image_cache):
        sessions = [
            Session(
                session_key="alpha_one",
                active_branch="main",
                current_image="img-1",
                last_kind="run",
                last_title="t",
                updated_at="2025-01-01",
            ),
        ]
        completer = _session_completer(image_cache, sessions=sessions)
        results = _complete_line(
            completer,
            "",
            "contree session show ",
            begidx=21,
        )
        assert "alpha_one " in results

    def test_run_use_flag_value_completes_image(self, image_cache):
        images = [_make_image("img-1", tag="ubuntu:noble")]
        completer, _client = _image_completer(images, image_cache)
        results = _complete_line(
            completer,
            "",
            "contree run --use ",
            begidx=18,
        )
        assert "tag:ubuntu:noble " in results
        assert "img-1 " in results

    def test_run_equals_form_use_flag(self, image_cache):
        images = [_make_image("img-1", tag="ubuntu:noble")]
        completer, _client = _image_completer(images, image_cache)
        results = _complete_line(
            completer,
            "--use=tag:ub",
            "contree run --use=tag:ub",
            begidx=12,
        )
        assert "tag:ubuntu:noble " in results

    def test_run_remainder_swallows_flags(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "--",
            "contree run -- ./script --",
            begidx=24,
        )
        assert results == []

    def test_run_remainder_completes_sandbox_path(self, image_cache):
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache)
        results = _complete_line(
            completer,
            "/etc/",
            "contree run -- /etc/",
            begidx=15,
        )
        assert "/etc/hosts " in results

    def test_help_topic_completes_run(self):
        completer = _make_completer()
        results = _complete_line(completer, "ru", "help ru", begidx=5)
        assert "run " in results

    def test_session_branch_from_completes(self, image_cache):
        branches = [("main", True), ("feature", False)]
        completer = _session_completer(image_cache, branches=branches)
        results = _complete_line(
            completer,
            "f",
            "contree session branch new --from f",
            begidx=34,
        )
        assert "feature " in results

    def test_run_cwd_flag_dirs_only(self, image_cache):
        files = [
            _make_file("/etc/conf.d", is_dir=True),
            _make_file("/etc/hosts"),
        ]
        completer, _client = _path_completer(files, image_cache)
        results = _complete_line(
            completer,
            "/etc/",
            "contree run --cwd /etc/",
            begidx=18,
        )
        assert "/etc/conf.d/" in results
        assert "/etc/hosts " not in results

    def test_help_value_excludes_after_help_flag(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "contree run --help ",
            begidx=19,
        )
        # After --help, argparse stops. We return nothing.
        assert results == []

    def test_choices_auto_bind_via_format_flag(self):
        # Auto-bind for actions with `choices=` works through the argparse
        # walker (the trie path also handles --format; this exercises the
        # walker via the contree-prefixed form).
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "contree -f ",
            begidx=11,
        )
        names = [r.rstrip(" ") for r in results]
        assert "json" in names
        assert "table" in names


class TestMappedFileCompletion:
    """`--file ./host:/inst` whole-token replacement completion."""

    def test_initial_host_path(self, tmp_path, image_cache):
        f = tmp_path / "Makefile"
        f.write_text("")
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache)
        results = _complete_line(
            completer,
            f"{tmp_path}/Make",
            f"contree run --file {tmp_path}/Make",
            begidx=19,
        )
        assert any(r.endswith("/Makefile") for r in results)

    def test_after_first_colon_offers_tags(self, image_cache):
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache)
        results = _complete_line(
            completer,
            "./Makefile:",
            "contree run --file ./Makefile:",
            begidx=19,
        )
        # Empty tail after colon offers u/g/m + /
        assert "./Makefile:u" in results
        assert "./Makefile:g" in results
        assert "./Makefile:m" in results
        assert "./Makefile:/" in results

    def test_after_colon_with_slash_completes_sandbox_path(self, image_cache):
        files = [_make_file("/etc/hosts")]
        completer, _client = _path_completer(files, image_cache)
        results = _complete_line(
            completer,
            "./Makefile:/etc/",
            "contree run --file ./Makefile:/etc/",
            begidx=19,
        )
        assert "./Makefile:/etc/hosts" in results


class TestProfileNamespacing:
    """Cache keys are namespaced by active profile name."""

    def test_image_cache_per_profile(self, image_cache):
        from contree_cli.shell.cache import SourceCache

        cache_a = SourceCache(image_cache, "alice")
        cache_b = SourceCache(image_cache, "bob")
        cache_a.set(scope="", kind="images", value=[{"uuid": "x"}])
        assert cache_a.get(scope="", kind="images", ttl=60.0) == [{"uuid": "x"}]
        assert cache_b.get(scope="", kind="images", ttl=60.0) is None

    def test_invalidate_all_only_drops_active_profile(self, image_cache):
        from contree_cli.shell.cache import SourceCache

        cache_a = SourceCache(image_cache, "alice")
        cache_b = SourceCache(image_cache, "bob")
        cache_a.set(scope="", kind="images", value=[1])
        cache_b.set(scope="", kind="images", value=[2])
        cache_a.invalidate_all()
        assert cache_a.get(scope="", kind="images", ttl=60.0) is None
        assert cache_b.get(scope="", kind="images", ttl=60.0) == [2]


class TestEnvKeySource:
    """`env -d <TAB>` completes session env keys."""

    def test_env_d_completes_existing_keys(self, image_cache):
        store = MagicMock()
        store.cache = image_cache
        store.get_env.return_value = {"PATH": "/usr/bin", "DEBUG": "1"}
        completer = _make_completer(client=MagicMock(), store=store)
        results = _complete_line(
            completer,
            "",
            "contree env -d ",
            begidx=15,
        )
        assert "PATH " in results
        assert "DEBUG " in results

    def test_env_no_store_returns_empty(self):
        completer = _make_completer(client=MagicMock(), store=None)
        results = _complete_line(
            completer,
            "",
            "contree env -d ",
            begidx=15,
        )
        assert results == []


class TestSkillSpecSource:
    """`skill remove <TAB>` completes spec prefixes and host paths."""

    def test_skill_remove_offers_prefixes(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "",
            "contree skill remove ",
            begidx=21,
        )
        assert "claude:" in results
        assert "codex:~" in results
        assert "claude:~" in results

    def test_skill_remove_filters_by_prefix(self):
        completer = _make_completer()
        results = _complete_line(
            completer,
            "claude",
            "contree skill remove claude",
            begidx=21,
        )
        assert "claude:" in results
        assert "claude:~" in results
        assert "codex:~" not in results
