"""Tests for PathCLIBackend AUTH_SOURCE integration + connector migration."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.bricks.auth.credential_backend import ResolvedCredential
from nexus.bricks.auth.credential_pool import CredentialPoolRegistry
from nexus.bricks.auth.external_sync.external_cli_backend import ExternalCliBackend
from nexus.bricks.auth.profile import AuthProfile, InMemoryAuthProfileStore, ProfileUsageStats


class TestPathCLIBackendAuthSource:
    """Test two-phase token resolution via AUTH_SOURCE."""

    def test_external_cli_takes_priority_over_token_manager(self) -> None:
        """When AUTH_SOURCE is set and external credential exists, use it."""
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/user@example.com",
                provider="google",
                account_identifier="user@example.com",
                backend="external-cli",
                backend_key="gws-cli/user@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        pool_registry = CredentialPoolRegistry(store=store)

        mock_backend = MagicMock(spec=ExternalCliBackend)
        mock_backend.resolve_sync.return_value = ResolvedCredential(
            kind="bearer_token", access_token="external-token-123"
        )

        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        token = connector._get_user_token(context=None)
        assert token == "external-token-123"
        mock_backend.resolve_sync.assert_called_once_with("gws-cli/user@example.com")

    def test_falls_back_to_token_manager_when_no_external_profiles(self) -> None:
        """When no external profiles, fall back to TokenManager (here: no TM → None)."""
        store = InMemoryAuthProfileStore()  # empty
        pool_registry = CredentialPoolRegistry(store=store)
        mock_backend = MagicMock(spec=ExternalCliBackend)

        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            AUTH_SOURCE = "gws-cli"

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        # No TokenManager either → returns None
        token = connector._get_user_token(context=None)
        assert token is None

    def test_no_auth_source_skips_external_cli(self) -> None:
        """When AUTH_SOURCE is None, don't try external CLI."""
        store = InMemoryAuthProfileStore()
        store.upsert(
            AuthProfile(
                id="google/user@example.com",
                provider="google",
                account_identifier="user@example.com",
                backend="external-cli",
                backend_key="gws-cli/user@example.com",
                usage_stats=ProfileUsageStats(),
            )
        )
        pool_registry = CredentialPoolRegistry(store=store)
        mock_backend = MagicMock(spec=ExternalCliBackend)

        from nexus.backends.connectors.cli.base import PathCLIBackend

        class _TestConnector(PathCLIBackend):
            CLI_NAME = "gws"
            CLI_SERVICE = "gmail"
            # AUTH_SOURCE not set — default None

        connector = _TestConnector(
            credential_pool_registry=pool_registry,
            external_cli_backend=mock_backend,
        )

        token = connector._get_user_token(context=None)
        assert token is None  # No TokenManager, AUTH_SOURCE path skipped
        mock_backend.resolve_sync.assert_not_called()


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
