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

    def test_parse_v2_multi_account_emits_only_active(self, adapter: GwsCliSyncAdapter) -> None:
        """Only the active account is emitted — gws auth token always returns
        the active account's credential, so non-active profiles would be
        unresolvable and risk cross-user bleed."""
        content = _STATUS_V2.read_text(encoding="utf-8")
        profiles = adapter.parse_output(content, "")

        assert len(profiles) == 1
        assert profiles[0].account_identifier == "user@example.com"
        # admin@corp.com has active=false in the v2 fixture — should NOT appear.
        emails = {p.account_identifier for p in profiles}
        assert "admin@corp.com" not in emails

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


class TestGwsResolveAccountMismatch:
    """Regression: bare `gws auth token` returns the active account's token
    regardless of which profile the caller asks for. Resolve must verify the
    active account matches before returning, or fail closed."""

    def test_resolve_fails_closed_when_active_account_mismatches(
        self, adapter: GwsCliSyncAdapter
    ) -> None:
        from nexus.bricks.auth.credential_backend import CredentialResolutionError

        # Active account per `gws auth status` is bob; caller asks for alice.
        status_output = '{"user": "bob@example.com", "token_valid": true}'
        token_output = '{"access_token": "bob-s-token-not-alice"}'

        call_count = {"n": 0}

        def _fake_run(_args, **_kwargs):  # noqa: ANN001, ANN003
            call_count["n"] += 1
            # First call is `auth status`, second would be `auth token`
            stdout = status_output if call_count["n"] == 1 else token_output
            return type("P", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        with (
            patch(
                "nexus.bricks.auth.external_sync.gws_sync.shutil.which",
                return_value="/usr/bin/gws",
            ),
            patch(
                "nexus.bricks.auth.external_sync.gws_sync.subprocess.run",
                side_effect=_fake_run,
            ),
            pytest.raises(CredentialResolutionError) as exc_info,
        ):
            adapter.resolve_credential_sync("gws-cli/alice@example.com")

        assert "alice@example.com" in str(exc_info.value)
        assert "bob@example.com" in str(exc_info.value)
        # Must fail BEFORE making the token call — verified by checking only
        # one subprocess call was made (the status check), not two.
        assert call_count["n"] == 1, "resolve should abort before calling auth token"

    def test_resolve_succeeds_when_active_account_matches(self, adapter: GwsCliSyncAdapter) -> None:
        status_output = '{"user": "alice@example.com", "token_valid": true}'
        token_output = '{"access_token": "alice-actual-token"}'

        call_count = {"n": 0}

        def _fake_run(_args, **_kwargs):  # noqa: ANN001, ANN003
            call_count["n"] += 1
            stdout = status_output if call_count["n"] == 1 else token_output
            return type("P", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        with (
            patch(
                "nexus.bricks.auth.external_sync.gws_sync.shutil.which",
                return_value="/usr/bin/gws",
            ),
            patch(
                "nexus.bricks.auth.external_sync.gws_sync.subprocess.run",
                side_effect=_fake_run,
            ),
        ):
            cred = adapter.resolve_credential_sync("gws-cli/alice@example.com")

        assert cred.access_token == "alice-actual-token"
        assert call_count["n"] == 2  # status + token


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
