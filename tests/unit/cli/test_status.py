"""Tests for ``nexus status`` command."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
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
    def test_uses_public_health_without_api_key(self, mock_client_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "healthy", "service": "nexus-rpc"}
        mock_client.get.return_value = mock_resp
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _server_health("http://localhost:2026")

        assert result == {"status": "healthy", "service": "nexus-rpc"}
        mock_client.get.assert_called_once_with("http://localhost:2026/health")
        mock_client_cls.assert_called_once_with(timeout=5.0, headers={}, trust_env=False)

    @patch("httpx.Client")
    def test_returns_none_on_connection_error(self, mock_client_cls: MagicMock) -> None:
        mock_client_cls.side_effect = Exception("Connection refused")
        result = _server_health("http://localhost:2026")
        assert result is None

    @patch("httpx.Client")
    def test_returns_degraded_when_detailed_times_out(self, mock_client_cls: MagicMock) -> None:
        """Timeout on authenticated /health/detailed surfaces as degraded —
        never silently fall back to unauth /health, which would mask the real
        failure while authenticated callers still hang."""
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.TimeoutException("detailed health timed out")
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _server_health("http://localhost:2026", api_key="sk-test")

        assert result is not None
        assert result["status"] == "degraded"
        assert "timed out" in result["reason"]
        assert mock_client.get.call_count == 1

    @patch("httpx.Client")
    def test_falls_back_to_public_health_on_401_403(self, mock_client_cls: MagicMock) -> None:
        """Detailed endpoint may require admin auth — preserve the original
        a0252a5f9 fallback to the public /health probe so we still report a
        useful status when the supplied key lacks admin scope."""
        mock_client = MagicMock()
        detailed_resp = MagicMock(status_code=403)
        fallback_resp = MagicMock(status_code=200)
        fallback_resp.json.return_value = {"status": "healthy", "service": "nexus-rpc"}
        mock_client.get.side_effect = [detailed_resp, fallback_resp]
        mock_client.__enter__ = lambda s: mock_client
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _server_health("http://localhost:2026", api_key="sk-test")

        assert result == {"status": "healthy", "service": "nexus-rpc"}
        assert mock_client.get.call_count == 2


# ---------------------------------------------------------------------------
# _collect_status
# ---------------------------------------------------------------------------


class TestCollectStatus:
    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features", return_value=None)
    @patch("nexus.cli.commands.status._docker_services", return_value=[])
    @patch("nexus.cli.commands.status._server_health", return_value=None)
    def test_server_unreachable(
        self, mock_health: MagicMock, mock_docker: MagicMock, mock_features: MagicMock
    ) -> None:
        data = _collect_status("http://localhost:2026")
        assert data["server_reachable"] is False
        assert data["server_health"] is None
        assert data["docker_services"] == []

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features", return_value=None)
    @patch(
        "nexus.cli.commands.status._docker_services",
        return_value=[{"Name": "nexus-server", "State": "running"}],
    )
    @patch("nexus.cli.commands.status._server_health", return_value={"status": "healthy"})
    def test_server_reachable(
        self, mock_health: MagicMock, mock_docker: MagicMock, mock_features: MagicMock
    ) -> None:
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
        result = cli_runner.invoke(
            status, ["--url", "http://remote:3000"], env={"NEXUS_API_KEY": ""}
        )
        assert result.exit_code == 0
        mock_collect.assert_called_once_with("http://remote:3000", None, None)

    # -----------------------------------------------------------------------
    # deployment_profile + auth_mode enrichment (Gap 2 of #4132)
    # -----------------------------------------------------------------------

    @patch(
        "nexus.cli.commands.status._fetch_deployment_profile_from_features",
        return_value=None,
    )
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_deployment_profile_env_is_offline_fallback(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """NEXUS_PROFILE is used only as the OFFLINE fallback — when the live
        hub features endpoint is unreachable (mocked → None), the env value
        surfaces as deployment_profile."""
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {}

        result = cli_runner.invoke(status, ["--json"], env={"NEXUS_PROFILE": "full"})
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        data = parsed.get("data", parsed)
        assert data["deployment_profile"] == "full"

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features")
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_live_features_wins_over_env_profile(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """`nexus status` reports the *running hub*. The live
        /api/v2/features value MUST override a local NEXUS_PROFILE env var
        (otherwise `NEXUS_PROFILE=full nexus status --url <other-hub>`
        would mislabel a different hub)."""
        mock_collect.return_value = {
            "server_url": "http://otherhub:2026",
            "server_reachable": True,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {}
        mock_fetch.return_value = "lite"  # the live hub actually runs `lite`

        result = cli_runner.invoke(status, ["--json"], env={"NEXUS_PROFILE": "full"})
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        data = parsed.get("data", parsed)
        assert data["deployment_profile"] == "lite"  # live hub wins, not env

    @patch(
        "nexus.cli.commands.status._fetch_deployment_profile_from_features",
        return_value=None,  # remote features probe fails (unreachable/401)
    )
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_remote_url_probe_failure_does_not_borrow_local_env_profile(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """`NEXUS_PROFILE=full nexus status --json --url http://otherhub`
        with a failed features probe must report deployment_profile
        "unknown" — NOT the local NEXUS_PROFILE (no evidence about that
        remote hub)."""
        mock_collect.return_value = {
            "server_url": "http://otherhub:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {}  # no project config

        result = cli_runner.invoke(
            status,
            ["--json", "--url", "http://otherhub:2026"],
            env={"NEXUS_PROFILE": "full"},
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        data = data.get("data", data)
        assert data["deployment_profile"] == "unknown"  # NOT "full"

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features")
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_local_stack_key_not_sent_to_remote_url(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """Auth-boundary: `nexus status --url <remote>` run inside a local
        project must NOT send the local stack's bearer token to the
        remote features probe. It uses the explicitly-supplied remote key
        (or none) — never conn_env's local NEXUS_API_KEY."""
        mock_collect.return_value = {
            "server_url": "http://otherhub:2026",
            "server_reachable": True,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {"auth": "static", "data_dir": "./nx"}
        mock_fetch.return_value = "lite"

        with (
            patch("nexus.cli.state.load_runtime_state", return_value={}),
            patch(
                "nexus.cli.state.resolve_connection_env",
                return_value={
                    "NEXUS_URL": "http://localhost:2026",  # local stack
                    "NEXUS_API_KEY": "LOCAL-SECRET",  # must NOT leak to remote
                },
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref_from_config",
                return_value="nexus:latest",
            ),
        ):
            result = cli_runner.invoke(
                status,
                [
                    "--json",
                    "--url",
                    "http://otherhub:2026",
                    "--remote-api-key",
                    "REMOTE-K",
                ],
            )
        assert result.exit_code == 0
        # the probe key is the explicit remote key, never the local one
        sent_key = mock_fetch.call_args.args[1]
        assert sent_key == "REMOTE-K"
        assert sent_key != "LOCAL-SECRET"
        # AND the local stack key/connection_env must not appear anywhere
        # in the rendered output for a remote target (auth boundary).
        assert "LOCAL-SECRET" not in result.output
        data = json.loads(result.output)
        data = data.get("data", data)
        assert "connection_env" not in data
        assert "project_name" not in data

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features")
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_features_probe_uses_connection_api_key(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """The connection's API key must be threaded into the features
        probe so an authenticated hub's /api/v2/features isn't silently
        401'd into the env/unknown fallback."""
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": True,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {"auth": "static", "data_dir": "./nx"}
        mock_fetch.return_value = "full"

        with (
            patch("nexus.cli.state.load_runtime_state", return_value={}),
            patch(
                "nexus.cli.state.resolve_connection_env",
                return_value={
                    "NEXUS_URL": "http://localhost:2026",
                    "NEXUS_API_KEY": "sk-secret",
                },
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref_from_config",
                return_value="nexus:latest",
            ),
        ):
            result = cli_runner.invoke(status, ["--json"], env={"NEXUS_PROFILE": ""})
        assert result.exit_code == 0
        # api key from the resolved connection env is forwarded to the probe
        assert mock_fetch.call_args.args[1] == "sk-secret" or mock_fetch.call_args == (
            ("http://localhost:2026", "sk-secret"),
            {},
        )

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features")
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_explicit_remote_url_does_not_leak_local_auth_or_profile(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """Project config present + explicit --url to a DIFFERENT hub: the
        local nexus.yaml auth and the local stack URL must NOT be reported
        for that remote hub. auth_mode → 'unknown'; deployment_profile is
        resolved against the actual --url target."""
        mock_collect.return_value = {
            "server_url": "http://otherhub:2026",  # the --url status target
            "server_reachable": True,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {"auth": "database", "data_dir": "./nx"}
        mock_fetch.return_value = "lite"  # otherhub actually runs `lite`

        with (
            patch("nexus.cli.state.load_runtime_state", return_value={}),
            patch(
                "nexus.cli.state.resolve_connection_env",
                return_value={"NEXUS_URL": "http://localhost:2026"},  # LOCAL stack
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref_from_config",
                return_value="nexus:latest",
            ),
        ):
            result = cli_runner.invoke(
                status, ["--json", "--url", "http://otherhub:2026"], env={"NEXUS_PROFILE": ""}
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        data = data.get("data", data)
        # local nexus.yaml auth="database" must NOT leak to a remote target
        assert data["auth_mode"] == "unknown"
        # profile comes from the --url target's features, not the local stack
        assert data["deployment_profile"] == "lite"

    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_auth_mode_from_project_config(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """auth_mode reflects the project config's auth key."""
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {
            "auth": "database",
            "data_dir": "./nexus-data",
        }

        with (
            patch("nexus.cli.state.load_runtime_state", return_value={}),
            patch(
                "nexus.cli.state.resolve_connection_env",
                return_value={"NEXUS_URL": "http://localhost:2026"},
            ),
            patch(
                "nexus.cli.commands.stack._resolve_image_ref_from_config",
                return_value="nexus:latest",
            ),
        ):
            result = cli_runner.invoke(status, ["--json"], env={"NEXUS_PROFILE": ""})
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        data = parsed.get("data", parsed)
        assert data["auth_mode"] == "database"

    @patch("nexus.cli.commands.status._fetch_deployment_profile_from_features", return_value=None)
    @patch("nexus.cli.commands.status._load_project_config_optional")
    @patch("nexus.cli.commands.status._collect_status")
    def test_new_keys_present_offline_no_project_config(
        self,
        mock_collect: MagicMock,
        mock_project_cfg: MagicMock,
        mock_fetch_profile: MagicMock,
        cli_runner: CliRunner,
    ) -> None:
        """Both keys are present even when offline with no project config (no real network call)."""
        mock_collect.return_value = {
            "server_url": "http://localhost:2026",
            "server_reachable": False,
            "server_health": None,
            "docker_services": [],
        }
        mock_project_cfg.return_value = {}

        result = cli_runner.invoke(status, ["--json"], env={"NEXUS_PROFILE": "", "NEXUS_URL": ""})
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        data = parsed.get("data", parsed)
        assert data["deployment_profile"] == "unknown"
        # No project config ⇒ no locally-managed stack to attest auth;
        # report "unknown" rather than fabricating "none" for what could
        # be an authenticated remote hub.
        assert data["auth_mode"] == "unknown"
