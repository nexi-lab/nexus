"""Tests for auth list command — Phase 4 profile store is authoritative (#3741).

The dual-read path (_try_profile_store_list fallback) was removed in Phase 4.
These tests verify that `nexus auth list` reads exclusively from
UnifiedAuthService.list_summaries(), with no profile-store overlay.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from nexus.contracts.unified_auth import AuthStatus, AuthSummary, CredentialKind


def _make_summaries() -> list[AuthSummary]:
    """Return test AuthSummary objects covering common provider types."""
    return [
        AuthSummary(
            service="s3",
            status=AuthStatus.AUTHED,
            source="native",
            kind=CredentialKind.NATIVE,
            message="ok",
            details={},
        ),
        AuthSummary(
            service="openai",
            status=AuthStatus.AUTHED,
            source="env",
            kind=CredentialKind.SECRET,
            message="OPENAI_API_KEY set",
            details={},
        ),
    ]


class TestAuthListNewTable:
    """Tests for the auth list table display (Phase 4 — no profile-store overlay)."""

    def test_list_shows_provider_column(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=_make_summaries())
        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with patch("nexus.bricks.auth.cli_commands._build_auth_service", return_value=mock_svc):
            result = runner.invoke(auth, ["list"])

        assert result.exit_code == 0, result.output
        assert "s3" in result.output
        assert "openai" in result.output

    def test_list_shows_source_column(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=_make_summaries())
        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with patch("nexus.bricks.auth.cli_commands._build_auth_service", return_value=mock_svc):
            result = runner.invoke(auth, ["list"])

        assert result.exit_code == 0, result.output
        assert "Source" in result.output
        assert "native" in result.output


class TestAuthListFallback:
    """Tests for graceful degradation when list_summaries() raises."""

    def test_degraded_output_on_auth_service_error(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_service = MagicMock()
        mock_service.list_summaries = AsyncMock(side_effect=RuntimeError("db unavailable"))

        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with patch("nexus.bricks.auth.cli_commands._build_auth_service", return_value=mock_service):
            result = runner.invoke(auth, ["list"])

        # Should not crash — degraded row is shown instead.
        assert result.exit_code == 0, result.output
        assert "degraded" in result.output or "error" in result.output

    def test_no_profile_store_function_in_module(self) -> None:
        """_try_profile_store_list must not exist after Phase 4 removal (#3741)."""
        from nexus.bricks.auth import cli_commands

        assert not hasattr(cli_commands, "_try_profile_store_list"), (
            "_try_profile_store_list was Phase 1 dual-read helper — must be deleted in Phase 4 (#3741)"
        )


class TestAuthListJsonOutput:
    """Tests for JSON output from the auth list command."""

    def test_json_output_format(self) -> None:
        from nexus.fs._auth_cli import auth

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=_make_summaries())
        with patch("nexus.bricks.auth.cli_commands._build_auth_service", return_value=mock_svc):
            result = runner.invoke(auth, ["list", "--json"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        items = parsed["data"]
        assert len(items) == 2
        # Verify expected fields are present
        assert items[0]["provider"] == "s3"
        assert items[0]["source"] == "native"
        assert items[1]["provider"] == "openai"
        assert items[1]["source"] == "env"
