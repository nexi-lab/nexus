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
        assert len(data["client_inferred_drivers"]) > 0
        assert data["client_inferred_drivers"] == sorted(data["client_inferred_drivers"])

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

    def test_auth_mode_unknown_when_no_auth_key(self, cli_runner: CliRunner) -> None:
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
        assert data["client_inferred_drivers"] == []
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


# ---------------------------------------------------------------------------
# Remote targeting — regression test for the real bug
# (contract_cmd must honour --url/--api-key and NEXUS_URL/NEXUS_API_KEY)
# ---------------------------------------------------------------------------


class TestContractRemoteTargeting:
    """Verify that --url/--api-key (and their env-var equivalents) are forwarded
    to NexusApiClient so the command can actually reach a non-localhost hub."""

    def test_explicit_flags_forwarded_to_api_client(self, cli_runner: CliRunner) -> None:
        """--url and --api-key must be used when constructing NexusApiClient."""
        config = make_config()
        remote_url = "http://hub:9999"
        remote_key = "K"
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(
                profile_group, ["contract", "--url", remote_url, "--api-key", remote_key]
            )

        assert result.exit_code == 0, result.output
        mock_client_cls.assert_called_once_with(url=remote_url, api_key=remote_key)

    def test_api_key_only_without_url_is_forwarded(self, cli_runner: CliRunner) -> None:
        """`--api-key K` with NO --url (default localhost static-auth hub):
        the explicit key must still reach NexusApiClient. resolve_connection
        drops remote_api_key when no remote_url is set, so the command must
        preserve `api_key or resolved.api_key`."""
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(profile_group, ["contract", "--api-key", "K"])

        assert result.exit_code == 0, result.output
        mock_client_cls.assert_called_once_with(url="http://localhost:2026", api_key="K")

    def test_envvar_nexus_url_forwarded_to_api_client(self, cli_runner: CliRunner) -> None:
        """NEXUS_URL env var must cause NexusApiClient to use the remote URL."""
        config = make_config()
        remote_url = "http://hub-env:8888"
        remote_key = "envkey"
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(
                profile_group,
                ["contract"],
                env={"NEXUS_URL": remote_url, "NEXUS_API_KEY": remote_key},
            )

        assert result.exit_code == 0, result.output
        mock_client_cls.assert_called_once_with(url=remote_url, api_key=remote_key)

    def test_no_remote_args_uses_default_localhost(self, cli_runner: CliRunner) -> None:
        """When no --url / env vars are set, behaviour is unchanged (localhost:2026)."""
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(
                profile_group,
                ["contract"],
                env={"NEXUS_URL": "", "NEXUS_API_KEY": ""},
            )

        assert result.exit_code == 0, result.output
        mock_client_cls.assert_called_once_with(url="http://localhost:2026", api_key=None)

    def test_bare_contract_uses_managed_stack_resolved_endpoint(
        self, cli_runner: CliRunner
    ) -> None:
        """Bare `nexus profile contract` (no --url) with a project config
        must hit the managed stack's *resolved* endpoint (derived/runtime
        ports + key), NOT a hard-coded localhost:2026 that could be an
        unrelated daemon."""
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"data_dir": "./nx", "auth": "static"},
            ),
            patch("nexus.cli.state.load_runtime_state", return_value={}),
            patch(
                "nexus.cli.state.resolve_connection_env",
                return_value={
                    "NEXUS_URL": "http://localhost:34567",  # runtime-resolved port
                    "NEXUS_API_KEY": "sk-managed",
                },
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(
                profile_group, ["contract"], env={"NEXUS_URL": "", "NEXUS_API_KEY": ""}
            )

        assert result.exit_code == 0, result.output
        mock_client_cls.assert_called_once_with(url="http://localhost:34567", api_key="sk-managed")

    def test_global_profile_forwarded_to_resolve_connection(self, cli_runner: CliRunner) -> None:
        """`nexus --profile staging profile contract` must pass the global
        profile through to resolve_connection (regression: it used to be
        silently dropped, so a requested profile got localhost instead)."""
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={},
            ),
            patch(
                "nexus.cli.commands.profile.resolve_connection",
                return_value=_make_resolved(url="http://staging:1234", api_key="sk"),
            ) as mock_resolve,
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(profile_group, ["contract"], obj={"profile": "staging"})

        assert result.exit_code == 0, result.output
        assert mock_resolve.call_args.kwargs.get("profile_name") == "staging"

    def test_remote_target_auth_mode_unknown_with_sources(self, cli_runner: CliRunner) -> None:
        """For an explicit remote target, the local nexus.yaml auth does
        NOT describe that hub: auth_mode must be 'unknown', and a
        `_sources` provenance map must mark client-inferred fields."""
        config = make_config()
        with (
            patch("nexus.cli.commands.profile.load_cli_config", return_value=config),
            patch("nexus.cli.commands.profile.NexusApiClient") as mock_client_cls,
            patch(
                "nexus.cli.commands.profile.load_project_config_optional",
                return_value={"auth": "database"},  # local config — must be ignored for remote
            ),
        ):
            mock_instance = MagicMock()
            mock_instance.get.return_value = FULL_FEATURES_PAYLOAD
            mock_client_cls.return_value = mock_instance

            result = cli_runner.invoke(profile_group, ["contract", "--url", "http://hub:9999"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["auth_mode"] == "unknown"  # NOT "database" (that's local-only)
        assert "_sources" in data
        assert "client-inferred" in data["_sources"]["client_inferred_drivers"]
        assert "remote target" in data["_sources"]["auth_mode"]
        # Hub-authoritative fields still come straight from /api/v2/features
        assert data["deployment_profile"] == "full"
