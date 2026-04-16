"""Tests for GwsCliSyncAdapter — fixture-based parse + integration tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
from nexus.bricks.auth.profile import AuthProfileFailureReason

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "external_cli_output"
_STATUS_V1 = _FIXTURE_DIR / "gws_status_v1.json"
_STATUS_V2 = _FIXTURE_DIR / "gws_status_v2.json"
_STATUS_REAL = _FIXTURE_DIR / "gws_status_real.json"


@pytest.fixture()
def adapter() -> GwsCliSyncAdapter:
    return GwsCliSyncAdapter()


class TestGwsParseOutput:
    """Test parse_output against gws status JSON fixtures."""

    def test_parse_v1_single_account(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "user@example.com"
        assert profiles[0].backend_key == "gws-cli/user@example.com"
        assert profiles[0].provider == "google"
        assert profiles[0].source == "gws-cli"

    def test_parse_v2_multiple_accounts(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V2.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 2
        emails = {p.account_identifier for p in profiles}
        assert "user@example.com" in emails
        assert "admin@corp.com" in emails

    def test_parse_empty_returns_empty(self, adapter: GwsCliSyncAdapter) -> None:
        profiles = adapter.parse_output("{}", "")
        assert profiles == []

    def test_parse_real_single_account_with_keyring_preamble(
        self, adapter: GwsCliSyncAdapter
    ) -> None:
        """Real ``gws auth status --format=json`` prints a ``Using keyring backend:``
        preamble before the JSON blob and emits a single-account object with a
        ``user`` field. Both must be handled.
        """
        content = _STATUS_REAL.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "real.user@example.com"
        assert profiles[0].backend_key == "gws-cli/real.user@example.com"

    def test_parse_malformed_raises(self, adapter: GwsCliSyncAdapter) -> None:
        import json as _json

        with pytest.raises(_json.JSONDecodeError):
            adapter.parse_output("not json {{{", "")

    def test_backend_key_format(self, adapter: GwsCliSyncAdapter) -> None:
        content = _STATUS_V1.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")
        for p in profiles:
            assert p.backend_key.startswith("gws-cli/")


class TestGwsSync:
    async def test_sync_binary_not_found(self) -> None:
        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            result = await adapter.sync()
        assert result.error is not None
        assert "not found" in result.error

    async def test_detect_true_with_binary(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/gws"):
            adapter = GwsCliSyncAdapter()
            assert await adapter.detect() is True

    async def test_detect_false_without_binary(self) -> None:
        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            assert await adapter.detect() is False


class TestGwsFixHints:
    def test_fix_hints_defined(self) -> None:
        adapter = GwsCliSyncAdapter()
        hints = adapter.FIX_HINTS
        assert AuthProfileFailureReason.UPSTREAM_CLI_MISSING in hints
        assert AuthProfileFailureReason.AUTH_PERMANENT in hints
        assert AuthProfileFailureReason.SCOPE_INSUFFICIENT in hints

    def test_missing_binary_hint(self) -> None:
        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.UPSTREAM_CLI_MISSING]
        assert "gws" in hint.lower() or "install" in hint.lower()
