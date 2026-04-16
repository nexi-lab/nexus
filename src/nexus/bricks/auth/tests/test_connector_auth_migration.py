"""Tests for PathCLIBackend AUTH_SOURCE integration + connector migration."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nexus.bricks.auth.credential_pool import CredentialPoolRegistry
from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore, ProfileUsageStats


class TestPathCLIBackendAuthSource:
    """Two-phase token resolution: external-CLI path via ``_external_sync_boot``.

    Each test patches ``resolve_token_for_provider`` at the import site inside
    ``PathCLIBackend._resolve_from_external_cli`` — that's the contract
    between the connector and the unified profile store.
    """

    def test_external_cli_takes_priority_over_token_manager(self) -> None:
        """AUTH_SOURCE set + helper returns a token → that token is used."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="external-token-123",
            ) as mock_resolve,
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=None)

        assert token == "external-token-123"
        mock_resolve.assert_called_once_with("google", account=None)

    def test_passes_user_email_as_account_for_multi_user(self) -> None:
        """When context.user_id is set, selection is scoped to that account."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()
        ctx = SimpleNamespace(user_id="alice@example.com", zone_id=None)

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value="alice-token",
            ) as mock_resolve,
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=ctx)

        assert token == "alice-token"
        mock_resolve.assert_called_once_with("google", account="alice@example.com")

    def test_falls_back_to_none_when_no_external_profile_and_no_tm(self) -> None:
        """No external profile and no TokenManager → returns None."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with (
            patch(
                "nexus.fs._external_sync_boot.resolve_token_for_provider",
                return_value=None,
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            token = connector._get_user_token(context=None)

        assert token is None

    def test_no_auth_source_skips_external_cli(self) -> None:
        """AUTH_SOURCE=None → helper is never called."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # AUTH_SOURCE not set — default None

        connector = _TestConnector()

        with patch(
            "nexus.fs._external_sync_boot.resolve_token_for_provider",
        ) as mock_resolve:
            token = connector._get_user_token(context=None)

        assert token is None
        mock_resolve.assert_not_called()

    def test_resolver_exception_falls_through_to_token_manager(self) -> None:
        """If the external helper raises, _get_user_token must swallow and fall through."""
        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector()

        with patch(
            "nexus.fs._external_sync_boot.resolve_token_for_provider",
            side_effect=RuntimeError("boom"),
        ):
            # No TokenManager configured → returns None rather than propagating
            token = connector._get_user_token(context=None)

        assert token is None


class TestUnifiedAuthServiceWithoutProfileStore:
    """Reproduces the production wiring: UnifiedAuthService with no profile_store.

    All 4 production constructor sites (auth_cli.py, doctor.py, _tui/__init__.py,
    _auth_cli.py) instantiate ``UnifiedAuthService(oauth_service=...)`` with no
    profile_store arg. _gws_native_from_profile_store must still work via the
    _external_sync_boot fallback.
    """

    def test_reads_profiles_via_boot_helper_when_no_store_injected(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()  # no profile_store, no oauth_service

        fake_profile = SimpleNamespace(
            provider="google",
            backend="external-cli",
            backend_key="gws-cli/bob@example.com",
            account_identifier="bob@example.com",
        )

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                return_value=[fake_profile],
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is not None
        assert native["source"] == "native:gws_cli"
        assert native["email"] == "bob@example.com"

    def test_returns_none_when_helper_has_no_matching_profiles(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()
        # list_profiles returns non-gws profiles only
        other_profile = SimpleNamespace(
            provider="s3",
            backend="external-cli",
            backend_key="aws-cli/default",
            account_identifier="default",
        )

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                return_value=[other_profile],
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is None

    def test_returns_none_on_helper_exception(self) -> None:
        from nexus.bricks.auth.unified_service import UnifiedAuthService

        service = UnifiedAuthService()

        with (
            patch(
                "nexus.fs._external_sync_boot.list_profiles",
                side_effect=RuntimeError("boom"),
            ),
            patch("nexus.fs._external_sync_boot.ensure_external_sync"),
        ):
            native = service._gws_native_from_profile_store()

        assert native is None


class TestConcurrentSelect:
    """Concurrency test: multiple coroutines × providers, no deadlock."""

    async def test_concurrent_select_no_deadlock(self) -> None:
        store = InMemoryAuthProfileStore()
        providers = ["google", "github", "s3", "codex", "gcs"]

        for provider in providers:
            for i in range(2):
                store.upsert(
                    AuthProfile(
                        id=f"{provider}/acct{i}@example.com",
                        provider=provider,
                        account_identifier=f"acct{i}@example.com",
                        backend="external-cli",
                        backend_key=f"test/{provider}/acct{i}",
                        usage_stats=ProfileUsageStats(),
                    )
                )

        registry = CredentialPoolRegistry(store=store)

        async def hammer(provider: str) -> None:
            pool = registry.get(provider)
            for _ in range(50):
                profile = await pool.select()
                assert profile.provider == provider

        await asyncio.wait_for(
            asyncio.gather(*[hammer(p) for p in providers for _ in range(2)]),
            timeout=5.0,
        )


class TestBug3713Regression:
    """Regression: #3713 failure reasons classified correctly with fix hints."""

    def test_missing_binary_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.UPSTREAM_CLI_MISSING]
        assert "install" in hint.lower() or "gws" in hint.lower()

    def test_revoked_token_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.AUTH_PERMANENT]
        assert "login" in hint.lower()

    def test_scope_insufficient_classified(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter
        from nexus.bricks.auth.profile import AuthProfileFailureReason

        adapter = GwsCliSyncAdapter()
        hint = adapter.FIX_HINTS[AuthProfileFailureReason.SCOPE_INSUFFICIENT]
        assert "scope" in hint.lower()


# ---------------------------------------------------------------------------
# Offline-safety tests — adapters must not attempt network I/O during sync
# ---------------------------------------------------------------------------
#
# A global socket monkeypatch breaks pytest-xdist's worker IPC, so we instead
# verify offline safety by asserting that sync() completes within a bounded
# wall-clock budget using only filesystem / subprocess I/O. FileAdapters are
# inherently offline (they only read files). SubprocessAdapters time-bound
# their subprocess calls to 5s each — proven by the gws "binary missing" path.


class TestOfflineSafety:
    """Adapters must return degraded results without network access."""

    async def test_gcloud_sync_completes_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "nope"))
        adapter = GcloudSyncAdapter()
        result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None or result.profiles == []

    async def test_codex_sync_completes_without_network(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.bricks.auth.external_sync.codex_sync import CodexSyncAdapter

        monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "nope"))
        adapter = CodexSyncAdapter()
        result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None or result.profiles == []

    async def test_gh_file_fallback_works_without_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil

        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        config_dir = tmp_path / "gh"
        config_dir.mkdir()
        fixture_dir = Path(__file__).parent / "fixtures" / "external_cli_output"
        shutil.copy(fixture_dir / "gh_hosts_v2.40.yml", config_dir / "hosts.yml")
        monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))

        with patch("nexus.bricks.auth.external_sync.gh_sync.shutil.which", return_value=None):
            adapter = GhCliSyncAdapter()
            result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is None
        assert len(result.profiles) == 1

    async def test_gws_no_binary_returns_fast(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        with patch("shutil.which", return_value=None):
            adapter = GwsCliSyncAdapter()
            result = await asyncio.wait_for(adapter.sync(), timeout=2.0)
        assert result.error is not None


# ---------------------------------------------------------------------------
# Opt-in real-binary e2e tests (nightly)
# ---------------------------------------------------------------------------
# Set TEST_WITH_REAL_<CLI>=1 to exercise actual binaries. Skipped by default
# so local / CI runs stay hermetic. Runs against the user's real config —
# validates that sync() produces parseable output on a working install.


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GCLOUD_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GCLOUD_CLI=1",
)
class TestRealGcloudBinary:
    async def test_gcloud_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gcloud_sync import GcloudSyncAdapter

        adapter = GcloudSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gcloud not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gcloud profile"


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GH_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GH_CLI=1",
)
class TestRealGhBinary:
    async def test_gh_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gh_sync import GhCliSyncAdapter

        adapter = GhCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gh not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gh profile"


@pytest.mark.skipif(
    not os.environ.get("TEST_WITH_REAL_GWS_CLI"),
    reason="opt-in: set TEST_WITH_REAL_GWS_CLI=1",
)
class TestRealGwsBinary:
    async def test_gws_real_sync(self) -> None:
        from nexus.bricks.auth.external_sync.gws_sync import GwsCliSyncAdapter

        adapter = GwsCliSyncAdapter()
        if not await adapter.detect():
            pytest.skip("gws not configured on this machine")
        result = await adapter.sync()
        assert result.profiles, "Expected at least one gws profile"
