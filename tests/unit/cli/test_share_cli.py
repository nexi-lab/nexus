"""Tests for nexus share CLI commands."""

from __future__ import annotations

import json
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.clients.share import ShareClient
from nexus.cli.commands.share import share

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1"}


@contextmanager
def _mock_client(**overrides: Any):
    """Patch ShareClient so service_command uses a mock instance.

    The @service_command decorator captures client_class in a closure at import
    time, so patching the module-level name has no effect.  Instead we patch the
    class methods directly via ``patch.object``.
    """
    with ExitStack() as stack:
        stack.enter_context(patch.object(ShareClient, "__init__", lambda self, **kw: None))
        stack.enter_context(patch.object(ShareClient, "__enter__", lambda self: self))
        stack.enter_context(patch.object(ShareClient, "__exit__", lambda self, *a: False))
        mocks: dict[str, MagicMock] = {}
        for name, retval in overrides.items():
            m = MagicMock(return_value=retval)
            stack.enter_context(patch.object(ShareClient, name, m))
            mocks[name] = m
        yield mocks


class TestShareCreate:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            create={
                "token": "abc123",
                "url": "https://nexus.io/s/abc123",
                "path": "/file.txt",
            }
        ):
            result = runner.invoke(share, ["create", "/file.txt", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "abc123" in result.output

    def test_default_permission_is_viewer(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(create={"token": "xyz"}) as mocks:
            runner.invoke(share, ["create", "/file.txt", "--remote-url", MOCK_URL])
        mocks["create"].assert_called_once_with(
            "/file.txt",
            permission_level="viewer",
            expires_in_hours=None,
            password=None,
        )

    def test_with_options(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(create={"token": "xyz"}) as mocks:
            result = runner.invoke(
                share,
                [
                    "create",
                    "/file.txt",
                    "--expires",
                    "24",
                    "--password",
                    "secret",
                    "--remote-url",
                    MOCK_URL,
                ],
            )
        assert result.exit_code == 0
        mocks["create"].assert_called_once_with(
            "/file.txt",
            permission_level="viewer",
            expires_in_hours=24,
            password="secret",
        )

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(create={"token": "abc123", "path": "/file.txt"}):
            result = runner.invoke(
                share, ["create", "/file.txt", "--remote-url", MOCK_URL, "--json"]
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["token"] == "abc123"

    def test_missing_url_fails(self) -> None:
        runner = CliRunner(env=_ENV)
        result = runner.invoke(share, ["create", "/file.txt"])
        assert result.exit_code != 0


class TestShareList:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            list={
                "links": [
                    {
                        "token": "abc",
                        "path": "/f.txt",
                        "permission_level": "viewer",
                        "expires_at": "2025-12-31T00:00:00",
                    }
                ]
            }
        ):
            result = runner.invoke(share, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0

    def test_empty(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(list={"links": []}):
            result = runner.invoke(share, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No active share links" in result.output

    def test_filter_by_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(list={"links": []}) as mocks:
            runner.invoke(share, ["list", "--path", "/data", "--remote-url", MOCK_URL])
        mocks["list"].assert_called_once_with(path="/data")

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(list={"links": [{"token": "abc", "path": "/f.txt"}]}):
            result = runner.invoke(share, ["list", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]["links"]) == 1


class TestShareShow:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(
            show={
                "path": "/file.txt",
                "permission_level": "viewer",
                "created_at": "2025-01-01T00:00:00",
                "expires_at": "2025-12-31T00:00:00",
                "access_count": 5,
            }
        ) as mocks:
            result = runner.invoke(share, ["show", "abc123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        mocks["show"].assert_called_once_with("abc123")

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(show={"path": "/file.txt", "access_count": 5}):
            result = runner.invoke(share, ["show", "abc123", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["access_count"] == 5


class TestShareRevoke:
    def test_happy_path(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(revoke={}) as mocks:
            result = runner.invoke(share, ["revoke", "abc123", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "revoked" in result.output.lower()
        mocks["revoke"].assert_called_once_with("abc123")

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        with _mock_client(revoke={}):
            result = runner.invoke(share, ["revoke", "abc123", "--remote-url", MOCK_URL, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"] is not None
