"""Tests for `nexus path-context` CLI commands (Issue #3773)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.commands.path_context import path_context

MOCK_URL = "http://localhost:2026"
_ENV = {"NEXUS_NO_AUTO_JSON": "1", "NEXUS_URL": MOCK_URL}


def _patched_client(**method_returns):
    client = MagicMock()
    for name, value in method_returns.items():
        setattr(client, name, MagicMock(return_value=value))
    # The internal fields accessed by the delete handler.
    client._base_url = MOCK_URL
    client._headers = MagicMock(return_value={"Authorization": "Bearer k"})
    client._timeout = 30.0
    return client


class TestPathContextSet:
    def test_put_sends_expected_payload(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(put={"zone_id": "root", "path_prefix": "src", "description": "x"})
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(
                path_context,
                ["set", "src", "x", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0, result.output
        fake.put.assert_called_once_with(
            "/api/v2/path-contexts/",
            {"zone_id": "root", "path_prefix": "src", "description": "x"},
        )
        assert "root:src" in result.output

    def test_custom_zone_id(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(put={"zone_id": "a", "path_prefix": "src", "description": "x"})
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(
                path_context,
                ["set", "src", "x", "--zone-id", "a", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0
        call = fake.put.call_args
        assert call.args[1]["zone_id"] == "a"


class TestPathContextList:
    def test_empty_list(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(get={"contexts": []})
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(path_context, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "No path contexts" in result.output

    def test_pretty_listing(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(
            get={
                "contexts": [
                    {"zone_id": "root", "path_prefix": "src", "description": "source root"},
                    {"zone_id": "root", "path_prefix": "docs", "description": "docs"},
                ]
            }
        )
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(path_context, ["list", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        assert "root:src" in result.output
        assert "source root" in result.output

    def test_json_output(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(
            get={
                "contexts": [
                    {"zone_id": "root", "path_prefix": "src", "description": "x"},
                ]
            }
        )
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(path_context, ["list", "--json", "--remote-url", MOCK_URL])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["path_prefix"] == "src"

    def test_zone_id_filter_passed_through(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(get={"contexts": []})
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            runner.invoke(path_context, ["list", "--zone-id", "other", "--remote-url", MOCK_URL])
        fake.get.assert_called_once_with("/api/v2/path-contexts/", params={"zone_id": "other"})


class TestPathContextDelete:
    def test_delete_success(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client()
        resp = MagicMock(status_code=200)
        resp.raise_for_status = MagicMock()
        with (
            patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake),
            patch("httpx.delete", return_value=resp) as delete_mock,
        ):
            result = runner.invoke(path_context, ["delete", "src", "--remote-url", MOCK_URL])
        assert result.exit_code == 0, result.output
        assert "deleted" in result.output
        assert delete_mock.call_args.kwargs["params"] == {
            "zone_id": "root",
            "path_prefix": "src",
        }

    def test_delete_404_exits_nonzero(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client()
        resp = MagicMock(status_code=404)
        with (
            patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake),
            patch("httpx.delete", return_value=resp),
        ):
            result = runner.invoke(path_context, ["delete", "missing", "--remote-url", MOCK_URL])
        assert result.exit_code == 1
        assert "No path context found" in result.output
