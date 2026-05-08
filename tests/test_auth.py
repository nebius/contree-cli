import argparse
import json
from contextlib import contextmanager
from contextvars import copy_context
from unittest.mock import patch

import pytest
from conftest import ContreeTestClient

from contree_cli import FORMATTER
from contree_cli.cli.auth import (
    AuthArgs,
    ProfilesArgs,
    RemoveArgs,
    SwitchArgs,
    cmd_auth,
    cmd_list,
    cmd_remove,
    cmd_switch,
)
from contree_cli.config import AuthType, Config


def _make_auth_args(**kwargs) -> AuthArgs:
    """Build an AuthArgs with defaults (JWT type for simplicity)."""
    defaults: dict[str, object] = dict(
        token="",
        url="https://test.dev",
        auth_type=AuthType.JWT,
        project=None,
        profile="default",
    )
    defaults.update(kwargs)
    return AuthArgs(**defaults)


def _make_iam_args(**kwargs) -> AuthArgs:
    """Build an AuthArgs with IAM defaults."""
    defaults: dict[str, object] = dict(
        token="",
        url="https://iam.test",
        auth_type=AuthType.IAM,
        project="aiproject-test",
        profile="default",
    )
    defaults.update(kwargs)
    return AuthArgs(**defaults)


def whoami_body(*, permissions: dict[str, bool] | None = None) -> bytes:
    body = {
        "token_uuid": "00000000-0000-0000-0000-000000000000",
        "token_expiration": None,
        "permissions": {"list": True} if permissions is None else permissions,
        "operations_stat": {},
    }
    return json.dumps(body).encode()


@contextmanager
def mock_whoami(status=200, *, body: bytes | None = None):
    """Patch client_from_profile to return a fresh ContreeTestClient per call."""
    last_client: list[ContreeTestClient] = []

    def factory(profile, timeout=None):  # type: ignore[no-untyped-def]
        tc = ContreeTestClient()
        tc.respond(status=status, body=body if body is not None else whoami_body())
        last_client.clear()
        last_client.append(tc)
        return tc

    with patch(
        "contree_cli.cli.auth.client_from_profile",
        side_effect=factory,
    ):
        yield last_client


def _make_ns(**kwargs) -> argparse.Namespace:
    """Build an argparse.Namespace for AuthArgs.from_args testing."""
    defaults = dict(
        auth_token=None,
        auth_url=None,
        auth_type=None,
        auth_project=None,
        profile=None,
        force=False,
    )
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Save with --token (JWT)
# ---------------------------------------------------------------------------


class TestAuthSave:
    def test_save_with_token(self, config_dir, caplog):
        args = _make_auth_args(
            token="my_token",
            url="https://my.dev",
            profile="default",
        )
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(args)
        p = Config().resolve()
        assert p.token == "my_token"
        assert p.url == "https://my.dev"
        assert "Setting token for profile 'default'" in caplog.text
        assert "auth accepted, profile 'default' saved to ->" in caplog.text

    def test_logs_updating_for_existing_profile(self, config_dir, caplog):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="old"))
        caplog.clear()
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(_make_auth_args(token="new", force=True))
        assert "Updating token for profile 'default'" in caplog.text

    def test_save_defaults_profile_and_url(self, config_dir):
        args = _make_auth_args(token="tok")
        with mock_whoami():
            cmd_auth(args)
        p = Config().resolve()
        assert p.token == "tok"
        assert p.url == "https://test.dev"

    def test_save_named_profile(self, config_dir, caplog):
        args = _make_auth_args(token="tok", profile="staging")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(args)
        cfg = Config()
        cfg.switch("staging")
        p = Config().resolve()
        assert p.token == "tok"
        assert p.name == "staging"
        assert "auth accepted, profile 'staging' saved to ->" in caplog.text

    def test_save_jwt_stores_type(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok"))
        p = Config().resolve()
        assert p.auth_type == AuthType.JWT

    def test_save_iam_stores_type_and_project(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_iam_args(token="tok"))
        p = Config().resolve()
        assert p.auth_type == AuthType.IAM
        assert p.project == "aiproject-test"
        assert p.url == "https://iam.test"


# ---------------------------------------------------------------------------
# Save with getpass prompt (via from_args)
# ---------------------------------------------------------------------------


class TestAuthPrompt:
    def test_no_prompt_in_from_args(self, config_dir):
        """from_args must NOT prompt for token --- that happens in cmd_auth."""
        ns = _make_ns()
        args = AuthArgs.from_args(ns)
        assert args.token is None

    def test_prompts_when_no_token_jwt(self, config_dir):
        ns = _make_ns(auth_type=AuthType.JWT, auth_url="https://test.dev")
        args = AuthArgs.from_args(ns)
        with (
            patch(
                "contree_cli.cli.auth.getpass.getpass",
                return_value="prompted_token",
            ),
            mock_whoami(),
        ):
            cmd_auth(args)
        p = Config().resolve()
        assert p.token == "prompted_token"

    def test_from_args_defaults_to_iam(self):
        ns = _make_ns()
        args = AuthArgs.from_args(ns)
        assert args.auth_type == AuthType.IAM


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


class TestAuthVerify:
    def test_bad_token_rejected(self, config_dir, caplog):
        args = _make_auth_args(token="bad")
        with caplog.at_level("ERROR"), mock_whoami(status=401):
            rc = cmd_auth(args)
        assert rc == 1
        assert "Profile not changed" in caplog.text

    def test_bad_token_does_not_save(self, config_dir):
        args = _make_auth_args(token="bad")
        with mock_whoami(status=401):
            cmd_auth(args)
        p = Config().resolve()
        assert p.token is None

    def test_no_list_permission_warns_but_saves(self, config_dir, caplog):
        args = _make_auth_args(token="tok")
        with (
            caplog.at_level("WARNING"),
            mock_whoami(body=whoami_body(permissions={"list": False})),
        ):
            rc = cmd_auth(args)
        assert rc is None
        assert "sandboxes are disabled" in caplog.text
        assert "Warning" in caplog.text
        assert Config().resolve().token == "tok"

    def test_no_list_permission_warning_includes_project(self, config_dir, caplog):
        args = _make_iam_args(token="tok", project="aiproject-restricted")
        with (
            caplog.at_level("WARNING"),
            mock_whoami(body=whoami_body(permissions={"list": False})),
        ):
            cmd_auth(args)
        assert "aiproject-restricted" in caplog.text

    def test_missing_permissions_field_warns(self, config_dir, caplog):
        args = _make_auth_args(token="tok")
        body = b'{"token_uuid":"x","token_expiration":null,"operations_stat":{}}'
        with caplog.at_level("WARNING"), mock_whoami(body=body):
            rc = cmd_auth(args)
        assert rc is None
        assert "sandboxes are disabled" in caplog.text
        assert Config().resolve().token == "tok"

    def test_unparseable_whoami_rejected(self, config_dir, caplog):
        args = _make_auth_args(token="tok")
        with caplog.at_level("ERROR"), mock_whoami(body=b"not-json"):
            rc = cmd_auth(args)
        assert rc == 1
        assert Config().resolve().token is None

    def test_success_logs_saved(self, config_dir, caplog):
        args = _make_auth_args(token="good")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(args)
        assert "auth accepted" in caplog.text

    def test_whoami_called(self, config_dir):
        args = _make_auth_args(token="tok")
        with mock_whoami() as clients:
            cmd_auth(args)
        tc = clients[0]
        assert tc.request_count == 1
        assert "/v1/whoami" in tc.request_paths[0]


# ---------------------------------------------------------------------------
# Nebius env var shortcuts
# ---------------------------------------------------------------------------


class TestNebius:
    def test_nebius_api_key_used_as_token(self, config_dir, caplog, monkeypatch):
        monkeypatch.setenv("NEBIUS_API_KEY", "nebius-tok")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(
                AuthArgs(
                    url="https://test.dev",
                    auth_type=AuthType.JWT,
                )
            )
        p = Config().resolve()
        assert p.token == "nebius-tok"
        assert "Using token from NEBIUS_API_KEY" in caplog.text

    def test_nebius_ai_project_used(self, config_dir, caplog, monkeypatch):
        monkeypatch.setenv("NEBIUS_AI_PROJECT", "aiproject-neb")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(
                AuthArgs(
                    token="tok",
                    auth_type=AuthType.IAM,
                    url="https://iam.test",
                )
            )
        p = Config().resolve()
        assert p.project == "aiproject-neb"
        assert "Using project from NEBIUS_AI_PROJECT" in caplog.text

    def test_both_nebius_vars_skip_all_prompts(self, config_dir, caplog, monkeypatch):
        monkeypatch.setenv("NEBIUS_API_KEY", "neb-tok")
        monkeypatch.setenv("NEBIUS_AI_PROJECT", "aiproject-auto")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(AuthArgs(auth_type=AuthType.IAM, url="https://iam.test"))
        p = Config().resolve()
        assert p.token == "neb-tok"
        assert p.project == "aiproject-auto"


class TestContreeEnvFallbacks:
    def test_contree_token_used_when_token_omitted(
        self, config_dir, caplog, monkeypatch
    ):
        monkeypatch.setenv("CONTREE_TOKEN", "ctok")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(AuthArgs(url="https://test.dev", auth_type=AuthType.JWT))
        p = Config().resolve()
        assert p.token == "ctok"
        assert "Using token from CONTREE_TOKEN" in caplog.text

    def test_contree_token_preferred_over_nebius_api_key(
        self, config_dir, caplog, monkeypatch
    ):
        monkeypatch.setenv("CONTREE_TOKEN", "ctok")
        monkeypatch.setenv("NEBIUS_API_KEY", "ntok")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(AuthArgs(url="https://test.dev", auth_type=AuthType.JWT))
        p = Config().resolve()
        assert p.token == "ctok"

    def test_contree_url_used_when_url_omitted(self, config_dir, caplog, monkeypatch):
        monkeypatch.setenv("CONTREE_URL", "https://env-url.dev")
        monkeypatch.setenv("NEBIUS_API_KEY", "tok")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(AuthArgs(auth_type=AuthType.JWT))
        p = Config().resolve()
        assert p.url == "https://env-url.dev"

    def test_contree_project_used_when_project_omitted(
        self, config_dir, caplog, monkeypatch
    ):
        monkeypatch.setenv("CONTREE_PROJECT", "aiproject-c")
        with caplog.at_level("INFO"), mock_whoami():
            cmd_auth(
                AuthArgs(
                    token="tok",
                    url="https://iam.test",
                    auth_type=AuthType.IAM,
                )
            )
        p = Config().resolve()
        assert p.project == "aiproject-c"
        assert "Using project from CONTREE_PROJECT" in caplog.text

    def test_explicit_token_flag_beats_env(self, config_dir, monkeypatch):
        monkeypatch.setenv("CONTREE_TOKEN", "from-env")
        with mock_whoami():
            cmd_auth(
                AuthArgs(
                    token="from-flag",
                    url="https://test.dev",
                    auth_type=AuthType.JWT,
                )
            )
        p = Config().resolve()
        assert p.token == "from-flag"


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


class TestAuthSwitch:
    def test_switch_profile(self, config_dir, caplog):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok1", profile="default"))
            cmd_auth(_make_auth_args(token="tok2", profile="staging"))

        args = SwitchArgs(profile_name="staging")
        with caplog.at_level("INFO"):
            cmd_switch(args)

        p = Config().resolve()
        assert p.name == "staging"
        assert "Switched to profile 'staging'" in caplog.text

    def test_switch_nonexistent_raises(self, config_dir):
        args = SwitchArgs(profile_name="nope")
        with pytest.raises(ValueError, match="does not exist"):
            cmd_switch(args)


class TestAuthProfiles:
    def test_profiles_show_status(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok-ok", profile="ok"))
            cmd_auth(_make_auth_args(token="tok-timeout", profile="timeout"))
            cmd_auth(_make_auth_args(token="tok-error", profile="error"))
        Config().switch("ok")

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        def fake_factory(profile, timeout=None):  # type: ignore[no-untyped-def]
            tc = ContreeTestClient(token=profile.token)
            if profile.token == "tok-ok":
                tc.respond(status=200, body=whoami_body())
            elif profile.token == "tok-timeout":

                def timeout_get(path, params=None):  # type: ignore[no-untyped-def]
                    raise TimeoutError("timeout")

                tc.get = timeout_get  # type: ignore[assignment]
            else:

                def error_get(path, params=None):  # type: ignore[no-untyped-def]
                    raise OSError("boom")

                tc.get = error_get  # type: ignore[assignment]
            return tc

        FORMATTER.set(CaptureFormatter())
        ctx = copy_context()
        with patch(
            "contree_cli.cli.auth.client_from_profile",
            side_effect=fake_factory,
        ):
            ctx.run(cmd_list, ProfilesArgs(offline=False))

        by_name = {str(row["name"]): row for row in rows}
        assert by_name["ok"]["status"] == "ok"
        assert by_name["timeout"]["status"] == "timeout"
        assert by_name["error"]["status"] == "error"

    def test_profiles_inactive_status(self, config_dir):
        """Profile whose token lacks `list` permission is reported as inactive."""
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="restricted"))

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        def fake_factory(profile, timeout=None):  # type: ignore[no-untyped-def]
            tc = ContreeTestClient(token=profile.token)
            tc.respond(
                status=200,
                body=whoami_body(permissions={"list": False, "spawn": True}),
            )
            return tc

        FORMATTER.set(CaptureFormatter())
        ctx = copy_context()
        with patch(
            "contree_cli.cli.auth.client_from_profile",
            side_effect=fake_factory,
        ):
            ctx.run(cmd_list, ProfilesArgs(offline=False))

        by_name = {str(row["name"]): row for row in rows}
        assert by_name["restricted"]["status"] == "inactive"

    def test_profiles_offline_skips_probe(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="offline-test"))
        Config().switch("offline-test")

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        FORMATTER.set(CaptureFormatter())
        ctx = copy_context()
        ctx.run(cmd_list, type("Args", (), {"offline": True})())

        assert rows[0]["name"] == "offline-test"
        assert rows[0]["status"] == "offline mode"

    def test_env_profile_marks_active(self, config_dir, monkeypatch):
        """CONTREE_PROFILE env var overrides active marker in listing."""
        with mock_whoami():
            cmd_auth(_make_auth_args(token="t1", profile="default"))
            cmd_auth(_make_auth_args(token="t2", profile="e2e"))

        monkeypatch.setenv("CONTREE_PROFILE", "e2e")

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        FORMATTER.set(CaptureFormatter())
        ctx = copy_context()
        ctx.run(cmd_list, ProfilesArgs(offline=True))

        by_name = {str(row["name"]): row for row in rows}
        assert by_name["e2e"]["active"] is True
        assert by_name["default"]["active"] is False

    def test_env_profile_nonexistent_warns(self, config_dir, monkeypatch, caplog):
        """CONTREE_PROFILE pointing to missing profile logs a warning."""
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="default"))

        monkeypatch.setenv("CONTREE_PROFILE", "ghost")

        rows: list[dict[str, object]] = []

        class CaptureFormatter:
            def __call__(self, **kwargs: object) -> None:
                rows.append(kwargs)

            def flush(self) -> None:
                return

        FORMATTER.set(CaptureFormatter())
        ctx = copy_context()
        with caplog.at_level("WARNING"):
            ctx.run(cmd_list, ProfilesArgs(offline=True))

        assert "does not exist" in caplog.text
        assert "ghost" in caplog.text


# ---------------------------------------------------------------------------
# Overwrite confirmation
# ---------------------------------------------------------------------------


class TestAuthOverwrite:
    def test_overwrite_aborted(self, config_dir, capsys):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="old"))
        with patch("builtins.input", return_value="n"):
            rc = cmd_auth(_make_auth_args(token="new"))
        assert rc == 1
        assert "Aborted" in capsys.readouterr().out
        p = Config().resolve()
        assert p.token == "old"

    def test_overwrite_confirmed(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="old"))
            with patch("builtins.input", return_value="y"):
                rc = cmd_auth(_make_auth_args(token="new"))
        assert rc is None
        p = Config().resolve()
        assert p.token == "new"

    def test_overwrite_force(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="old"))
            rc = cmd_auth(_make_auth_args(token="new", force=True))
        assert rc is None
        p = Config().resolve()
        assert p.token == "new"

    def test_overwrite_empty_input_aborts(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="old"))
            with patch("builtins.input", return_value=""):
                rc = cmd_auth(_make_auth_args(token="new"))
        assert rc == 1


class TestAuthRemove:
    def test_remove_deletes_profile(self, config_dir, caplog):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="staging"))
        rc = cmd_remove(RemoveArgs(profile_name="staging", force=True))
        assert rc is None
        assert "staging" not in Config()

    def test_remove_nonexistent_fails(self, config_dir, caplog):
        with caplog.at_level("ERROR"):
            rc = cmd_remove(RemoveArgs(profile_name="nope", force=True))
        assert rc == 1
        assert "does not exist" in caplog.text

    def test_remove_active_switches_to_remaining(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="t1", profile="first"))
            cmd_auth(_make_auth_args(token="t2", profile="second"))
        cfg = Config()
        cfg.switch("first")
        cmd_remove(RemoveArgs(profile_name="first", force=True))
        p = Config().resolve()
        assert p.name != "first"

    def test_remove_aborted(self, config_dir, capsys):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="keep"))
        with patch("builtins.input", return_value="n"):
            rc = cmd_remove(RemoveArgs(profile_name="keep"))
        assert rc == 1
        assert "keep" in Config()

    def test_remove_confirmed(self, config_dir):
        with mock_whoami():
            cmd_auth(_make_auth_args(token="tok", profile="gone"))
        with patch("builtins.input", return_value="y"):
            rc = cmd_remove(RemoveArgs(profile_name="gone"))
        assert rc is None
        assert "gone" not in Config()


# ---------------------------------------------------------------------------
# AuthArgs.from_args
# ---------------------------------------------------------------------------


class TestAuthFromArgs:
    def test_from_args_all_fields(self):
        ns = _make_ns(
            auth_token="tok",
            auth_url="https://url.dev",
            auth_type=AuthType.JWT,
            auth_project="aiproject-x",
            profile="prod",
            force=True,
        )
        args = AuthArgs.from_args(ns)
        assert args.token == "tok"
        assert args.url == "https://url.dev"
        assert args.auth_type == AuthType.JWT
        assert args.project == "aiproject-x"
        assert args.profile == "prod"
        assert args.force is True

    def test_from_args_defaults(self):
        ns = _make_ns()
        args = AuthArgs.from_args(ns)
        assert args.token is None
        assert args.url is None
        assert args.auth_type == AuthType.IAM
        assert args.project is None
        assert args.profile == "default"
        assert args.force is False
