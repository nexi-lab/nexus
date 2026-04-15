"""Tests for nexus-fs auth list dual-read cutover."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from nexus.bricks.auth.profile import (
    AuthProfile,
    AuthProfileFailureReason,
    ProfileUsageStats,
)


def _make_store_profiles() -> list[AuthProfile]:
    """Return 3 test profiles for the new profile-store path."""
    now = datetime.now(UTC)
    return [
        AuthProfile(
            id="s3/default",
            provider="s3",
            account_identifier="default",
            backend="external-cli",
            backend_key="aws/default",
            last_synced_at=now - timedelta(hours=1),
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=14),
                success_count=42,
                failure_count=0,
            ),
        ),
        AuthProfile(
            id="s3/work-prod",
            provider="s3",
            account_identifier="work-prod",
            backend="external-cli",
            backend_key="aws/work-prod",
            last_synced_at=now - timedelta(hours=2),
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=60),
                success_count=10,
                failure_count=3,
                cooldown_until=now + timedelta(minutes=43),
                cooldown_reason=AuthProfileFailureReason.RATE_LIMIT,
            ),
        ),
        AuthProfile(
            id="openai/team",
            provider="openai",
            account_identifier="team",
            backend="nexus-token-manager",
            backend_key="openai/team",
            last_synced_at=now - timedelta(minutes=5),
            usage_stats=ProfileUsageStats(
                last_used_at=now - timedelta(minutes=2),
                success_count=100,
                failure_count=1,
            ),
        ),
    ]


class TestAuthListNewTable:
    """Tests for the new profile-store table display."""

    def test_new_table_shows_source_column(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=[])
        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with (
            patch(
                "nexus.fs._auth_cli._try_profile_store_list", return_value=_make_store_profiles()
            ),
            patch("nexus.fs._auth_cli._build_auth_service", return_value=mock_svc),
        ):
            result = runner.invoke(auth, ["list"])

        assert result.exit_code == 0, result.output
        assert "Source" in result.output
        assert "external" in result.output

    def test_new_table_shows_cooldown_status(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=[])
        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with (
            patch(
                "nexus.fs._auth_cli._try_profile_store_list", return_value=_make_store_profiles()
            ),
            patch("nexus.fs._auth_cli._build_auth_service", return_value=mock_svc),
        ):
            result = runner.invoke(auth, ["list"])

        assert result.exit_code == 0, result.output
        assert "cooldown" in result.output
        assert "rate_limit" in result.output


class TestAuthListFallback:
    """Tests for fallback to the old UnifiedAuthService path."""

    def test_falls_back_when_store_returns_none(self) -> None:
        from nexus.fs._auth_cli import auth

        mock_service = MagicMock()
        mock_service.list_summaries = AsyncMock(return_value=[])
        mock_service.secret_store_path = "/tmp/fake"

        runner = CliRunner(env={"NEXUS_NO_AUTO_JSON": "1"})
        with (
            patch("nexus.fs._auth_cli._try_profile_store_list", return_value=None),
            patch("nexus.fs._auth_cli._build_auth_service", return_value=mock_service),
        ):
            result = runner.invoke(auth, ["list"])

        assert result.exit_code == 0, result.output
        mock_service.list_summaries.assert_called_once()


class TestAuthListJsonOutput:
    """Tests for JSON output from the new profile-store path."""

    def test_json_output_new_format(self) -> None:
        from nexus.fs._auth_cli import auth

        runner = CliRunner()
        mock_svc = MagicMock()
        mock_svc.list_summaries = AsyncMock(return_value=[])
        with (
            patch(
                "nexus.fs._auth_cli._try_profile_store_list", return_value=_make_store_profiles()
            ),
            patch("nexus.fs._auth_cli._build_auth_service", return_value=mock_svc),
        ):
            result = runner.invoke(auth, ["list", "--json"])

        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        items = parsed["data"]
        assert len(items) == 3
        # Check first item fields
        assert items[0]["provider"] == "s3"
        assert items[0]["account"] == "default"
        assert items[0]["source"] == "external"
        assert items[0]["status"] == "ok"
        # Check cooldown item
        assert "cooldown" in items[1]["status"]
        # Check nexus-token-manager item
        assert items[2]["source"] == "nexus"
