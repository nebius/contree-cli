import configparser
import os
import stat
import sys

import pytest

from contree_cli.config import (
    AuthType,
    CliSettings,
    Config,
    ConfigProfile,
)

# ---------------------------------------------------------------------------
# save / load via Config
# ---------------------------------------------------------------------------


class TestSaveAndLoad:
    def test_save_creates_file(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok123",
            url="https://test.dev",
        )
        assert (config_dir / "auth.ini").exists()

    def test_load_reads_saved_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok123",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.token == "tok123"
        assert p.url == "https://test.dev"
        assert p.name == "default"

    def test_save_multiple_profiles(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = ConfigProfile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        p = Config().resolve()
        assert p.token == "tok1"  # default profile active

    def test_save_overwrites_existing(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="old",
            url="https://old.dev",
        )
        cfg["default"] = ConfigProfile(
            name="default",
            token="new",
            url="https://new.dev",
        )
        p = Config().resolve()
        assert p.token == "new"
        assert p.url == "https://new.dev"

    def test_load_defaults_when_no_file(self, config_dir):
        p = Config().resolve()
        assert p.name == "default"
        assert p.token is None
        assert p.url == ""
        assert p.auth_type == AuthType.JWT


# ---------------------------------------------------------------------------
# Config with explicit path
# ---------------------------------------------------------------------------


class TestLoadConfigPath:
    def test_load_from_explicit_path(self, tmp_path):
        cfg_file = tmp_path / "custom.ini"
        cfg = Config(path=cfg_file)
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok_custom",
            url="https://custom.dev",
        )
        p = Config(path=cfg_file).resolve()
        assert p.token == "tok_custom"
        assert p.url == "https://custom.dev"


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------


class TestProfileResolution:
    def test_defaults_to_default_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.name == "default"

    def test_uses_switched_profile(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = ConfigProfile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        cfg.switch("staging")
        p = Config().resolve()
        assert p.name == "staging"
        assert p.token == "tok2"
        assert p.url == "https://staging.dev"

    def test_env_profile_overrides_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = ConfigProfile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        monkeypatch.setenv("CONTREE_PROFILE", "staging")
        p = Config().resolve()
        assert p.name == "staging"
        assert p.token == "tok2"

    def test_env_token_overrides_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="cfg_token",
            url="https://test.dev",
        )
        monkeypatch.setenv("CONTREE_TOKEN", "env_token")
        p = Config().resolve()
        assert p.token == "env_token"

    def test_env_url_overrides_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://custom.dev",
        )
        monkeypatch.setenv("CONTREE_URL", "https://env.dev")
        p = Config().resolve()
        assert p.url == "https://env.dev"

    def test_url_falls_back_for_jwt_when_missing(self, config_dir):
        """JWT profile with url key removed falls back to empty string."""
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(Config.PROFILE_PREFIX + "default", "url")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.url == ""

    def test_url_falls_back_for_iam_when_missing(self, config_dir):
        """IAM profile with url key removed falls back to IAM default."""
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AuthType.IAM,
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(Config.PROFILE_PREFIX + "default", "url")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.url == Config.DEFAULT_IAM_URL

    def test_nonexistent_profile_returns_defaults(self, config_dir, monkeypatch):
        monkeypatch.setenv("CONTREE_PROFILE", "nonexistent")
        p = Config().resolve()
        assert p.name == "nonexistent"
        assert p.token is None
        assert p.url == ""
        assert p.auth_type == AuthType.JWT


# ---------------------------------------------------------------------------
# Auth type and project
# ---------------------------------------------------------------------------


class TestAuthType:
    def test_default_type_is_jwt(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.auth_type == AuthType.JWT

    def test_iam_type_stored_and_loaded(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AuthType.IAM,
            project="aiproject-x",
        )
        p = Config().resolve()
        assert p.auth_type == AuthType.IAM
        assert p.project == "aiproject-x"

    def test_legacy_profile_without_type_is_jwt(self, config_dir):
        """Profile saved without type key (legacy) defaults to jwt."""
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://old.dev",
        )
        cp = configparser.ConfigParser()
        cp.read(config_dir / "auth.ini")
        cp.remove_option(Config.PROFILE_PREFIX + "default", "type")
        with open(config_dir / "auth.ini", "w") as f:
            cp.write(f)
        p = Config().resolve()
        assert p.auth_type == AuthType.JWT

    def test_project_none_when_not_set(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        assert p.project is None

    def test_env_project_overrides_config(self, config_dir, monkeypatch):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AuthType.IAM,
            project="aiproject-cfg",
        )
        monkeypatch.setenv("CONTREE_PROJECT", "aiproject-env")
        p = Config().resolve()
        assert p.project == "aiproject-env"

    def test_save_clears_project_when_none(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://iam.test",
            auth_type=AuthType.IAM,
            project="aiproject-old",
        )
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
            auth_type=AuthType.JWT,
        )
        p = Config().resolve()
        assert p.project is None


# ---------------------------------------------------------------------------
# ConfigProfile dataclass
# ---------------------------------------------------------------------------


class TestConfigProfileDataclass:
    def test_frozen(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        with pytest.raises(AttributeError):
            p.token = "other"

    def test_repr_masks_token(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="secret_tok",
            url="https://test.dev",
        )
        p = Config().resolve()
        r = repr(p)
        assert "secret_tok" not in r
        assert "***" in r

    def test_repr_none_token(self, config_dir):
        p = Config().resolve()
        r = repr(p)
        assert "None" in r


# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------


class TestSwitchProfile:
    def test_switch_updates_default(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="tok1",
            url="https://test.dev",
        )
        cfg["staging"] = ConfigProfile(
            name="staging",
            token="tok2",
            url="https://staging.dev",
        )
        cfg.switch("staging")
        p = Config().resolve()
        assert p.name == "staging"

    def test_switch_nonexistent_raises(self, config_dir):
        cfg = Config()
        with pytest.raises(ValueError, match="does not exist"):
            cfg.switch("nonexistent")


# ---------------------------------------------------------------------------
# auth.ini permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
class TestAuthFilePermissions:
    def test_file_mode_is_0600(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="secret-token",
            url="https://test.dev",
        )
        path = config_dir / "auth.ini"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_rewrite_keeps_0600(self, config_dir):
        cfg = Config()
        cfg["default"] = ConfigProfile(
            name="default",
            token="t1",
            url="https://test.dev",
        )
        path = config_dir / "auth.ini"
        os.chmod(path, 0o644)
        cfg["default"] = ConfigProfile(
            name="default",
            token="t2",
            url="https://test.dev",
        )
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# cli.ini parsing
# ---------------------------------------------------------------------------


class TestCliSettings:
    def test_missing_file_yields_empty_defaults(self, tmp_path):
        cli = CliSettings.load(tmp_path / "cli.ini")
        assert cli.log_level is None
        assert cli.output_format is None
        assert cli.editor is None

    def test_reads_known_keys(self, tmp_path):
        path = tmp_path / "cli.ini"
        path.write_text("[cli]\nlog_level = debug\nformat = json\neditor = nvim\n")
        cli = CliSettings.load(path)
        assert cli.log_level == "debug"
        assert cli.output_format == "json"
        assert cli.editor == "nvim"

    def test_ignores_unknown_keys(self, tmp_path):
        path = tmp_path / "cli.ini"
        path.write_text("[cli]\nunknown = foo\nlog_level = info\n")
        cli = CliSettings.load(path)
        assert cli.log_level == "info"

    def test_no_section_yields_empty(self, tmp_path):
        path = tmp_path / "cli.ini"
        path.write_text("[other]\nfoo = bar\n")
        cli = CliSettings.load(path)
        assert cli.log_level is None
        assert cli.output_format is None
        assert cli.editor is None
