"""Tests for the CLI-side glue in ``contree_cli.client``.

Everything client-shaped (transport, headers, retries, typed methods,
models) is covered by the contree-client test suite; here the client
appears only as the ``contree_client.testing`` double. This file
covers what the CLI adds on top: profile-to-client construction and version reporting.
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from unittest.mock import MagicMock, patch

import pytest
from contree_client import testing
from contree_client.profiles import AUTH_TYPE_IAM, Profile
from contree_client.runtime import RetryPolicy
from contree_client.spec_info import DEFAULT_BASE_URL

from contree_cli.client import cli_version, client_from_profile

# ---------------------------------------------------------------------------
# client_from_profile
# ---------------------------------------------------------------------------


@pytest.fixture()
def test_client_double(
    monkeypatch: pytest.MonkeyPatch,
) -> type[testing.ContreeClient]:
    """Build clients on the official testing double: it records its
    constructor kwargs in ``constructed_with`` for exactly this kind
    of factory test."""
    monkeypatch.setattr("contree_cli.client.CliClient", testing.ContreeClient)
    return testing.ContreeClient


class TestClientFromProfile:
    def test_jwt_profile(self, test_client_double):
        profile = Profile(name="p", url="https://contree.dev", token="tok")
        client = client_from_profile(profile)
        assert isinstance(client, testing.ContreeClient)
        assert client.base_url == "https://contree.dev"
        assert client.token == "tok"
        assert client.project is None

    def test_attaches_unbounded_retry_policy(self, test_client_double):
        profile = Profile(name="p", url="https://contree.dev", token="tok")
        client = client_from_profile(profile)
        retry = client.constructed_with["retry"]
        assert isinstance(retry, RetryPolicy)
        assert retry.max_attempts is None

    def test_jwt_profile_requires_url(self):
        profile = Profile(name="p", url="", token="tok")
        with pytest.raises(ValueError, match="No URL configured"):
            client_from_profile(profile)

    def test_missing_token_raises(self):
        profile = Profile(name="p", url="https://contree.dev", token=None)
        with pytest.raises(ValueError, match="No token configured"):
            client_from_profile(profile)

    def test_iam_profile_defaults_url(self, test_client_double):
        profile = Profile(
            name="p",
            url="",
            token="tok",
            auth_type=AUTH_TYPE_IAM,
            project="aiproject-x",
        )
        client = client_from_profile(profile)
        assert client.base_url == DEFAULT_BASE_URL.rstrip("/")
        assert client.project == "aiproject-x"

    def test_iam_profile_requires_project(self):
        profile = Profile(
            name="p",
            url="",
            token="tok",
            auth_type=AUTH_TYPE_IAM,
            project=None,
        )
        with pytest.raises(ValueError, match="No project configured"):
            client_from_profile(profile)


# ---------------------------------------------------------------------------
# cli_version
# ---------------------------------------------------------------------------


class TestCliVersion:
    """``cli_version()`` must return ``"editable"`` whenever the install is
    not a regular wheel: either the package is missing from metadata, or
    PEP 610 ``direct_url.json`` marks the install as editable. The update
    checker keys off this sentinel to skip PyPI pings during local dev."""

    def make_dist(self, *, version: str, direct_url: str | None) -> MagicMock:
        dist = MagicMock()
        dist.version = version
        dist.read_text.return_value = direct_url
        return dist

    def test_returns_editable_when_package_not_installed(self):
        with patch(
            "contree_cli.client.distribution",
            side_effect=PackageNotFoundError("contree-cli"),
        ):
            assert cli_version() == "editable"

    def test_returns_editable_for_pep610_editable_install(self):
        dist = self.make_dist(
            version="0.5.0",
            direct_url=json.dumps(
                {
                    "url": "file:///path/to/contree-cli",
                    "dir_info": {"editable": True},
                },
            ),
        )
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "editable"

    def test_returns_version_for_regular_install(self):
        dist = self.make_dist(version="0.5.0", direct_url=None)
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"

    def test_returns_version_when_direct_url_lacks_editable_flag(self):
        dist = self.make_dist(
            version="0.5.0",
            direct_url=json.dumps(
                {
                    "url": "https://files.pythonhosted.org/.../contree_cli.whl",
                    "archive_info": {},
                },
            ),
        )
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"

    def test_returns_version_when_direct_url_is_malformed(self):
        dist = self.make_dist(version="0.5.0", direct_url="not json {")
        with patch("contree_cli.client.distribution", return_value=dist):
            assert cli_version() == "0.5.0"
