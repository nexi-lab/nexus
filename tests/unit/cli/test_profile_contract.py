"""Tests for `nexus profile contract` subcommand (Issue #4132 Gap 1)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from nexus.cli.commands.profile import profile_group
from nexus.cli.config import ResolvedConnection
from tests.unit.cli.conftest import make_config

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FULL_FEATURES_PAYLOAD = {
    "profile": "full",
    "mode": "standalone",
    "enabled_bricks": ["llm", "search", "pay", "scheduler", "eventlog", "namespace", "permissions"],
    "disabled_bricks": ["federation"],
    "version": "1.2.3",
    "performance_tuning": None,
    "rate_limit_enabled": False,
}


def _make_resolved(
    url: str = "http://localhost:2026", api_key: str | None = None
) -> ResolvedConnection:
    return ResolvedConnection(url=url, api_key=api_key, source="test")


# ---------------------------------------------------------------------------
# Happy-path: full profile, with project config present
# ---------------------------------------------------------------------------


class TestContractFullProfile:
    def test_exit_code_zero(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        assert result.exit_code == 0

    def test_deployment_profile_field(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["deployment_profile"] == "full"

    def test_bricks_sorted(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["bricks"] == sorted(FULL_FEATURES_PAYLOAD["enabled_bricks"])

    def test_disabled_bricks_sorted(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["disabled_bricks"] == sorted(FULL_FEATURES_PAYLOAD["disabled_bricks"])

    def test_drivers_non_empty_for_full(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert len(data["drivers"]) > 0
        assert data["drivers"] == sorted(data["drivers"])

    def test_grpc_required_is_true(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["grpc_required"] is True

    def test_auth_mode_from_project_config(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["auth_mode"] == "database"

    def test_auth_mode_unknown_when_no_project_config(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["auth_mode"] == "unknown"

    def test_version_field_present(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["version"] == "1.2.3"

    def test_mode_field_present(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=FULL_FEATURES_PAYLOAD,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        data = json.loads(result.output)
        assert data["mode"] == "standalone"


# ---------------------------------------------------------------------------
# Unknown profile path — drivers must be [] and exit 0
# ---------------------------------------------------------------------------


class TestContractUnknownProfile:
    def test_unknown_profile_drivers_empty_exit_zero(self, cli_runner: CliRunner) -> None:
        weird_payload = {
            "profile": "weird",
            "mode": "standalone",
            "enabled_bricks": ["llm"],
            "disabled_bricks": [],
            "version": None,
            "performance_tuning": None,
            "rate_limit_enabled": False,
        }
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=weird_payload,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["drivers"] == []
        assert data["deployment_profile"] == "weird"

    def test_unknown_profile_still_emits_bricks(self, cli_runner: CliRunner) -> None:
        weird_payload = {
            "profile": "weird",
            "mode": "standalone",
            "enabled_bricks": ["some_brick"],
            "disabled_bricks": ["other_brick"],
            "version": None,
            "performance_tuning": None,
            "rate_limit_enabled": False,
        }
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                return_value=weird_payload,
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["bricks"] == ["some_brick"]
        assert data["disabled_bricks"] == ["other_brick"]


# ---------------------------------------------------------------------------
# Connection failure path — non-zero exit, actionable error, no traceback
# ---------------------------------------------------------------------------


class TestContractConnectionFailure:
    def test_http_error_nonzero_exit(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(url="http://localhost:2026"),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        assert result.exit_code != 0

    def test_http_error_actionable_message(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(url="http://localhost:2026"),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        # Should mention the URL and suggest nexus doctor
        assert "api/v2/features" in result.output or "localhost" in result.output
        assert "nexus doctor" in result.output

    def test_http_error_no_traceback(self, cli_runner: CliRunner) -> None:
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(url="http://localhost:2026"),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                side_effect=httpx.ConnectError("Connection refused"),
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        # No Python traceback in output
        assert "Traceback" not in result.output
        assert "raise" not in result.output

    def test_http_status_error_nonzero_exit(self, cli_runner: CliRunner) -> None:
        """Non-200 responses also produce non-zero exit."""
        config = make_config()
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.request = MagicMock()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(url="http://localhost:2026"),
            ),
            patch(
                "nexus.cli.commands.profile.NexusApiClient.get",
                side_effect=httpx.HTTPStatusError(
                    "503 Service Unavailable",
                    request=mock_response.request,
                    response=mock_response,
                ),
            ),
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            result = cli_runner.invoke(profile_group, ["contract"])
        assert result.exit_code != 0
