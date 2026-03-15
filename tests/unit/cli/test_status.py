"""Tests for ``nexus status`` command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli.commands.status import (
    _build_table,
    _collect_status,
    _server_health,
    status,
)


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _server_health
# ---------------------------------------------------------------------------


class TestServerHealth:
    @patch("httpx.Client")
    def test_returns_json_on_200(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "healthy", "components": {}}
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _server_health("http://localhost:2026")
        assert result is not None
        assert result["status"] == "healthy"

    @patch("httpx.Client")
    def test_returns_none_on_connection_error(self, mock_client_cls: MagicMock) -> None:
        mock_client_cls.side_effect = Exception("Connection refused")
        result = _server_health("http://localhost:2026")
        assert result is None


# ---------------------------------------------------------------------------
# _collect_status
# ---------------------------------------------------------------------------


class TestCollectStatus:
    @patch("nexus.cli.commands.status._docker_services", return_value=[])
    @patch("nexus.cli.commands.status._server_health", return_value=None)
    def test_server_unreachable(self, mock_health: MagicMock, mock_docker: MagicMock) -> None:
        data = _collect_status("http://localhost:2026")
        assert data["server_reachable"] is False
        assert data["server_health"] is None
        assert data["docker_services"] == []

    @patch(
        "nexus.cli.commands.status._docker_services",
        return_value=[{"Name": "nexus-server", "State": "running"}],
    )
    @patch("nexus.cli.commands.status._server_health", return_value={"status": "healthy"})
    def test_server_reachable(self, mock_health: MagicMock, mock_docker: MagicMock) -> None:
        data = _collect_status("http://localhost:2026")
        assert data["server_reachable"] is True
        assert len(data["docker_services"]) == 1


# ---------------------------------------------------------------------------
# _build_table
# ---------------------------------------------------------------------------


class TestBuildTable:
    def test_unreachable_server(self) -> None:
        data = {
            "server_url": "http://localhost:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        table = _build_table(data)
        assert table.title == "Nexus Service Status"
        assert table.row_count >= 1

    def test_healthy_server(self) -> None:
        data = {
            "server_url": "http://localhost:2026",
            "server_reachable": True,
            "server_health": {"status": "healthy", "components": {}},
            "docker_services": [],
        }
        table = _build_table(data)
        assert table.row_count >= 1

    def test_with_docker_services(self) -> None:
        data = {
            "server_url": "http://localhost:2026",
            "server_reachable": True,
            "server_health": {"status": "healthy", "components": {}},
            "docker_services": [
                {
                    "Name": "nexus-dragonfly",
                    "State": "running",
                    "Health": "healthy",
                    "Ports": "6379",
                },
            ],
        }
        table = _build_table(data)
        assert table.row_count == 2  # server + dragonfly

    def test_degraded_components(self) -> None:
        data = {
            "server_url": "http://localhost:2026",
            "server_reachable": True,
            "server_health": {
                "status": "degraded",
                "components": {
                    "rebac": {"status": "degraded", "circuit_state": "half_open"},
                    "search": {"status": "healthy"},
                },
            },
            "docker_services": [],
        }
        table = _build_table(data)
        assert table.row_count >= 1


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestStatusCommand:
    @patch("nexus.cli.commands.status._collect_status")
    def test_json_output(self, mock_collect: MagicMock, cli_runner: CliRunner) -> None:
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        result = cli_runner.invoke(status, ["--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "server_reachable" in parsed.get("data", parsed)

    @patch("nexus.cli.commands.status._collect_status")
    def test_table_output(self, mock_collect: MagicMock, cli_runner: CliRunner) -> None:
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": True,
            "server_health": {"status": "healthy", "components": {}},
            "docker_services": [],
        }
        result = cli_runner.invoke(status, env={"NEXUS_NO_AUTO_JSON": "1"})
        assert result.exit_code == 0
        assert "Nexus Service Status" in result.output

    @patch("nexus.cli.commands.status._collect_status")
    def test_custom_url(self, mock_collect: MagicMock, cli_runner: CliRunner) -> None:
        mock_collect.return_value = {
            "server_url": "http://remote:3000",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        result = cli_runner.invoke(status, ["--url", "http://remote:3000"])
        assert result.exit_code == 0
        mock_collect.assert_called_once_with("http://remote:3000", None)
