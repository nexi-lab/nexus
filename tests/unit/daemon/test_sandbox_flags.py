"""Unit tests for sandbox CLI flags on ``nexusd`` (Issue #3786).

Tests cover:
1. ``--workspace /tmp/ws --profile sandbox`` → parses workspace correctly
2. ``--workspace /tmp/ws`` (no ``--profile sandbox``) → error exit(1)
3. ``--hub-url grpc://hub --profile sandbox`` (no token) → error exit(1)
4. ``NEXUS_HUB_TOKEN`` env var picked up correctly
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from nexus.cli.exit_codes import ExitCode
from nexus.daemon.main import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server_mocks(monkeypatch):
    """Inject fake nexus + fastapi_server modules so CLI can reach run_server."""
    mock_nx = MagicMock()
    mock_connect = MagicMock(return_value=mock_nx)

    mock_app = MagicMock()
    mock_create_app = MagicMock(return_value=mock_app)
    mock_run_server = MagicMock()

    fake_mod = types.ModuleType("nexus.server.fastapi_server")
    fake_mod.create_app = mock_create_app
    fake_mod.run_server = mock_run_server
    monkeypatch.setitem(sys.modules, "nexus.server.fastapi_server", fake_mod)

    return mock_connect, mock_nx, mock_create_app, mock_run_server


# ---------------------------------------------------------------------------
# Test 1: --workspace + --profile sandbox → parses workspace correctly
# ---------------------------------------------------------------------------


class TestSandboxWorkspaceFlag:
    """--workspace is accepted with --profile sandbox."""

    def test_workspace_with_sandbox_profile_accepted(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        workspace = tmp_path / "ws"
        workspace.mkdir()

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)

        bootstrapper_mock = MagicMock()
        mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

        with (
            patch("nexus.connect", mock_connect),
            patch(
                "nexus.daemon.main.SandboxBootstrapper",
                mock_bootstrapper_cls,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--profile", "sandbox", "--workspace", str(workspace)],
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"
        # SandboxBootstrapper should have been instantiated with the workspace
        mock_bootstrapper_cls.assert_called_once()
        call_kwargs = mock_bootstrapper_cls.call_args
        assert call_kwargs.kwargs["workspace"] == workspace or (
            call_kwargs.args and call_kwargs.args[0] == workspace
        )
        # run() should have been called
        bootstrapper_mock.run.assert_called_once()

    def test_workspace_parsed_as_path_object(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        workspace = tmp_path / "my-workspace"
        workspace.mkdir()

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)

        captured: dict = {}

        def _capture_bootstrapper(**kwargs):
            captured.update(kwargs)
            inst = MagicMock()
            return inst

        with (
            patch("nexus.connect", mock_connect),
            patch(
                "nexus.daemon.main.SandboxBootstrapper",
                side_effect=_capture_bootstrapper,
            ),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--profile", "sandbox", "--workspace", str(workspace)],
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"
        assert "workspace" in captured
        assert captured["workspace"] == workspace


# ---------------------------------------------------------------------------
# Test 2: --workspace without --profile sandbox → error exit
# ---------------------------------------------------------------------------


class TestSandboxFlagsRequireProfile:
    """Sandbox flags without --profile sandbox must error with exit(1)."""

    def test_workspace_without_sandbox_profile_errors(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["--workspace", str(workspace)])

        assert result.exit_code == ExitCode.USAGE_ERROR.value
        combined = result.output
        assert "sandbox" in combined.lower()

    def test_hub_url_without_sandbox_profile_errors(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["--hub-url", "grpc://hub.example.com"])

        assert result.exit_code == ExitCode.USAGE_ERROR.value
        combined = result.output
        assert "sandbox" in combined.lower()

    def test_hub_token_without_sandbox_profile_errors(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["--hub-token", "tok-abc"])

        assert result.exit_code == ExitCode.USAGE_ERROR.value
        combined = result.output
        assert "sandbox" in combined.lower()


# ---------------------------------------------------------------------------
# Test 3: --hub-url without token → error exit
# ---------------------------------------------------------------------------


class TestSandboxHubUrlRequiresToken:
    """--hub-url without any token (flag or env) must error with exit(1)."""

    def test_hub_url_without_token_errors(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        # Ensure no env token leaks in
        monkeypatch.delenv("NEXUS_HUB_TOKEN", raising=False)

        workspace = tmp_path / "ws"
        workspace.mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--profile",
                "sandbox",
                "--workspace",
                str(workspace),
                "--hub-url",
                "grpc://hub.example.com",
            ],
        )

        assert result.exit_code == ExitCode.USAGE_ERROR.value
        combined = result.output
        # Error message should mention token or hub-token
        assert "token" in combined.lower()

    def test_hub_url_with_flag_token_ok(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("NEXUS_HUB_TOKEN", raising=False)

        workspace = tmp_path / "ws"
        workspace.mkdir()

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)
        bootstrapper_mock = MagicMock()
        mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

        with (
            patch("nexus.connect", mock_connect),
            patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    str(workspace),
                    "--hub-url",
                    "grpc://hub.example.com",
                    "--hub-token",
                    "tok-xyz",
                ],
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"
        mock_bootstrapper_cls.assert_called_once()
        kwargs = mock_bootstrapper_cls.call_args.kwargs
        assert kwargs.get("hub_url") == "grpc://hub.example.com"
        assert kwargs.get("hub_token") == "tok-xyz"


# ---------------------------------------------------------------------------
# Test 4: NEXUS_HUB_TOKEN env var picked up correctly
# ---------------------------------------------------------------------------


class TestSandboxHubTokenEnvVar:
    """NEXUS_HUB_TOKEN env var is used when --hub-token flag is omitted."""

    def test_hub_token_from_env_var(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("NEXUS_HUB_TOKEN", "env-token-abc")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)
        bootstrapper_mock = MagicMock()
        mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

        with (
            patch("nexus.connect", mock_connect),
            patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    str(workspace),
                    "--hub-url",
                    "grpc://hub.example.com",
                    # No --hub-token flag — env var should cover it
                ],
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"
        mock_bootstrapper_cls.assert_called_once()
        kwargs = mock_bootstrapper_cls.call_args.kwargs
        assert kwargs.get("hub_token") == "env-token-abc"

    def test_hub_token_env_var_satisfies_hub_url_requirement(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """With NEXUS_HUB_TOKEN set, --hub-url alone must NOT error."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setenv("NEXUS_HUB_TOKEN", "env-tok-xyz")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)
        bootstrapper_mock = MagicMock()
        mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

        with (
            patch("nexus.connect", mock_connect),
            patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "--profile",
                    "sandbox",
                    "--workspace",
                    str(workspace),
                    "--hub-url",
                    "grpc://hub.example.com",
                ],
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"

    def test_nexus_workspace_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """NEXUS_WORKSPACE env var should work just like --workspace flag."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("NEXUS_HUB_TOKEN", raising=False)

        workspace = tmp_path / "env-ws"
        workspace.mkdir()

        monkeypatch.setenv("NEXUS_WORKSPACE", str(workspace))

        mock_connect, mock_nx, mock_create_app, mock_run_server = _make_server_mocks(monkeypatch)
        bootstrapper_mock = MagicMock()
        mock_bootstrapper_cls = MagicMock(return_value=bootstrapper_mock)

        with (
            patch("nexus.connect", mock_connect),
            patch("nexus.daemon.main.SandboxBootstrapper", mock_bootstrapper_cls),
        ):
            runner = CliRunner()
            result = runner.invoke(
                main,
                ["--profile", "sandbox"],
                # env var provides workspace
            )

        assert result.exit_code == 0, f"Unexpected exit: {result.output}"
        mock_bootstrapper_cls.assert_called_once()
        kwargs = mock_bootstrapper_cls.call_args.kwargs
        assert kwargs.get("workspace") == workspace
