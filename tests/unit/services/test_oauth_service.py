"""Unit tests for OAuthCredentialService (bricks/auth/oauth/credential_service.py).

Covers:
- PKCEStateStore save/pop/expired/no-cache
- list_providers
- exchange_code (normal + PKCE)
- list_credentials filtering
- revoke_credential with ownership
- test_credential
- _map_provider_name
- _get_pkce_verifier error paths
- _check_credential_ownership permission enforcement
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService, PKCEStateStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeCacheStore:
    """Minimal in-memory cache for PKCEStateStore tests.

    Note: Intentionally does not subclass CacheStoreABC to keep test deps minimal.
    PKCEStateStore only uses get/set/delete — duck typing is sufficient.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def set(self, key: str, value: bytes, *, ttl: int | None = None) -> None:  # noqa: ARG002
        self._store[key] = value

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


@dataclass
class FakeProviderConfig:
    name: str = "google-drive"
    display_name: str = "Google Drive"
    scopes: list[str] | None = None
    requires_pkce: bool = False
    icon_url: str | None = None
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.scopes is None:
            self.scopes = ["https://www.googleapis.com/auth/drive"]
        if self.metadata is None:
            self.metadata = {}


@dataclass
class FakeCredential:
    access_token: str = "ya29.test"
    refresh_token: str | None = "1//test"
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: list[str] | None = None
    provider: str = "google-drive"
    user_email: str | None = None
    client_id: str | None = "test_client"
    token_uri: str | None = "https://oauth2.googleapis.com/token"
    metadata: dict[str, Any] | None = None


class FakeOAuthProvider:
    """Minimal provider mock."""

    def __init__(self, name: str = "google-drive") -> None:
        self.provider_name = name

    def get_authorization_url(self, state: str | None = None) -> str:
        return f"https://accounts.google.com/o/oauth2/auth?state={state}"

    def get_authorization_url_with_pkce(
        self, state: str | None = None
    ) -> tuple[str, dict[str, str]]:
        return (
            f"https://auth.example.com?state={state}",
            {
                "code_verifier": "test_verifier",
                "code_challenge": "test_challenge",
                "code_challenge_method": "S256",
            },
        )

    async def exchange_code(self, code: str) -> FakeCredential:  # noqa: ARG002
        return FakeCredential(
            access_token="ya29.exchanged",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    async def exchange_code_pkce(self, code: str, code_verifier: str) -> FakeCredential:  # noqa: ARG002
        return FakeCredential(
            access_token="ya29.exchanged_pkce",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


# ===========================================================================
# PKCEStateStore
# ===========================================================================


class TestPKCEStateStore:
    """Tests for PKCEStateStore."""

    @pytest.mark.asyncio
    async def test_save_and_pop(self) -> None:
        cache = FakeCacheStore()
        store = PKCEStateStore(cache_store=cache, ttl=600)

        pkce_data = {"code_verifier": "abc123", "code_challenge": "xyz789"}
        await store.save("state_token", pkce_data)

        result = await store.pop("state_token")
        assert result == pkce_data

    @pytest.mark.asyncio
    async def test_pop_is_single_use(self) -> None:
        """Pop should delete the entry — second pop returns None."""
        cache = FakeCacheStore()
        store = PKCEStateStore(cache_store=cache, ttl=600)

        await store.save("state_token", {"code_verifier": "v"})

        first = await store.pop("state_token")
        assert first is not None

        second = await store.pop("state_token")
        assert second is None

    @pytest.mark.asyncio
    async def test_pop_missing_state(self) -> None:
        cache = FakeCacheStore()
        store = PKCEStateStore(cache_store=cache, ttl=600)

        result = await store.pop("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_cache_store_degrades_gracefully(self) -> None:
        store = PKCEStateStore(cache_store=None)

        # Should not raise
        await store.save("state", {"code_verifier": "v"})

        # Pop returns None (no storage)
        result = await store.pop("state")
        assert result is None


# ===========================================================================
# OAuthCredentialService — Provider Discovery
# ===========================================================================


class TestListProviders:
    """Tests for list_providers."""

    @pytest.fixture
    def mock_factory(self) -> MagicMock:
        factory = MagicMock()
        factory.list_providers.return_value = [
            FakeProviderConfig(name="google-drive", display_name="Google Drive"),
            FakeProviderConfig(
                name="x", display_name="X", requires_pkce=True, icon_url="https://x.com/icon.png"
            ),
        ]
        return factory

    @pytest.mark.asyncio
    async def test_list_providers(self, mock_factory: MagicMock) -> None:
        service = OAuthCredentialService(oauth_factory=mock_factory)
        providers = await service.list_providers()

        assert len(providers) == 2
        assert providers[0]["name"] == "google-drive"
        assert providers[0]["display_name"] == "Google Drive"
        assert providers[0]["requires_pkce"] is False
        assert "icon_url" not in providers[0]

        assert providers[1]["name"] == "x"
        assert providers[1]["requires_pkce"] is True
        assert providers[1]["icon_url"] == "https://x.com/icon.png"

    @pytest.mark.asyncio
    async def test_list_providers_empty(self) -> None:
        factory = MagicMock()
        factory.list_providers.return_value = []
        service = OAuthCredentialService(oauth_factory=factory)

        providers = await service.list_providers()
        assert providers == []


# ===========================================================================
# OAuthCredentialService — OAuth Flow (exchange_code)
# ===========================================================================


class TestExchangeCode:
    """Tests for exchange_code."""

    @pytest.fixture
    def mock_factory(self) -> MagicMock:
        factory = MagicMock()
        config = FakeProviderConfig(name="google-drive", requires_pkce=False)
        factory.get_provider_config.return_value = config
        factory.create_provider.return_value = FakeOAuthProvider("google-drive")
        return factory

    @pytest.fixture
    def mock_token_manager(self) -> MagicMock:
        tm = MagicMock()
        tm.store_credential = AsyncMock(return_value="cred-123")
        tm.register_provider = MagicMock()
        return tm

    @pytest.mark.asyncio
    async def test_exchange_code_normal(
        self, mock_factory: MagicMock, mock_token_manager: MagicMock
    ) -> None:
        service = OAuthCredentialService(
            oauth_factory=mock_factory,
            token_manager=mock_token_manager,
        )

        with patch("nexus.lib.context_utils.get_zone_id", return_value="test-zone"):
            result = await service.exchange_code(
                provider="google-drive",
                code="4/0Abc",
                user_email="alice@example.com",
            )

        assert result["success"] is True
        assert result["credential_id"] == "cred-123"
        assert result["user_email"] == "alice@example.com"
        mock_token_manager.store_credential.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exchange_code_pkce(self, mock_token_manager: MagicMock) -> None:
        factory = MagicMock()
        pkce_config = FakeProviderConfig(name="x", requires_pkce=True)
        factory.get_provider_config.return_value = pkce_config
        factory.create_provider.return_value = FakeOAuthProvider("x")

        pkce_store = PKCEStateStore(cache_store=FakeCacheStore())
        await pkce_store.save("state123", {"code_verifier": "test_verifier"})

        service = OAuthCredentialService(
            oauth_factory=factory,
            token_manager=mock_token_manager,
            pkce_store=pkce_store,
        )

        with patch("nexus.lib.context_utils.get_zone_id", return_value="test-zone"):
            result = await service.exchange_code(
                provider="x",
                code="auth_code",
                user_email="bob@example.com",
                state="state123",
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_exchange_code_missing_email_raises(self, mock_factory: MagicMock) -> None:
        """Without user_email and no provider email lookup, should raise."""
        provider = MagicMock()
        provider.provider_name = "google-drive"
        provider.exchange_code = AsyncMock(return_value=FakeCredential())
        mock_factory.create_provider.return_value = provider

        tm = MagicMock()
        tm.register_provider = MagicMock()
        service = OAuthCredentialService(oauth_factory=mock_factory, token_manager=tm)

        with (
            patch("nexus.lib.context_utils.get_zone_id", return_value="test-zone"),
            patch.object(
                service, "_get_user_email_from_provider", new_callable=AsyncMock, return_value=None
            ),
            pytest.raises(ValueError, match="user_email is required"),
        ):
            await service.exchange_code(
                provider="google-drive",
                code="code",
                user_email=None,
            )


# ===========================================================================
# OAuthCredentialService — Credential Management
# ===========================================================================


class TestListCredentials:
    """Tests for list_credentials with filtering."""

    @pytest.fixture
    def mock_token_manager(self) -> MagicMock:
        tm = MagicMock()
        tm.list_credentials = AsyncMock(
            return_value=[
                {
                    "credential_id": "1",
                    "provider": "google-drive",
                    "user_email": "alice@example.com",
                    "user_id": "user-alice",
                    "revoked": False,
                },
                {
                    "credential_id": "2",
                    "provider": "x",
                    "user_email": "bob@example.com",
                    "user_id": "user-bob",
                    "revoked": False,
                },
                {
                    "credential_id": "3",
                    "provider": "google-drive",
                    "user_email": "alice@example.com",
                    "user_id": "user-alice",
                    "revoked": True,
                },
            ]
        )
        return tm

    @pytest.mark.asyncio
    async def test_list_all_as_admin(self, mock_token_manager: MagicMock) -> None:
        service = OAuthCredentialService(token_manager=mock_token_manager)
        ctx = MagicMock(user_id="admin-user", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.list_credentials(context=ctx)

        # Excludes revoked by default
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_filters_by_provider(self, mock_token_manager: MagicMock) -> None:
        service = OAuthCredentialService(token_manager=mock_token_manager)
        ctx = MagicMock(user_id="admin-user", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.list_credentials(provider="x", context=ctx)

        assert len(result) == 1
        assert result[0]["provider"] == "x"

    @pytest.mark.asyncio
    async def test_list_includes_revoked(self, mock_token_manager: MagicMock) -> None:
        service = OAuthCredentialService(token_manager=mock_token_manager)
        ctx = MagicMock(user_id="admin-user", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.list_credentials(include_revoked=True, context=ctx)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_non_admin_sees_only_own(self, mock_token_manager: MagicMock) -> None:
        service = OAuthCredentialService(token_manager=mock_token_manager)
        ctx = MagicMock(user_id="user-alice", is_admin=False)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.list_credentials(context=ctx)

        # Alice should only see her own non-revoked credentials
        assert len(result) == 1
        assert result[0]["user_id"] == "user-alice"


# ===========================================================================
# OAuthCredentialService — Revoke
# ===========================================================================


class TestRevokeCredential:
    """Tests for revoke_credential."""

    @pytest.mark.asyncio
    async def test_revoke_success(self) -> None:
        tm = MagicMock()
        tm.revoke_credential = AsyncMock(return_value=True)
        tm.get_credential = AsyncMock(return_value=None)
        tm.register_provider = MagicMock()

        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.revoke_credential(
                provider="google-drive",
                user_email="alice@example.com",
                context=ctx,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_revoke_not_found(self) -> None:
        tm = MagicMock()
        tm.revoke_credential = AsyncMock(return_value=False)
        tm.get_credential = AsyncMock(return_value=None)

        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=True)

        with (
            patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"),
            pytest.raises(ValueError, match="Credential not found"),
        ):
            await service.revoke_credential(
                provider="google-drive",
                user_email="unknown@example.com",
                context=ctx,
            )


# ===========================================================================
# OAuthCredentialService — Test Credential
# ===========================================================================


class TestTestCredential:
    """Tests for test_credential."""

    @pytest.mark.asyncio
    async def test_valid_credential(self) -> None:
        tm = MagicMock()
        tm.get_valid_token = AsyncMock(return_value="ya29.valid")
        tm.list_credentials = AsyncMock(
            return_value=[
                {
                    "user_email": "alice@example.com",
                    "expires_at": "2026-12-31T00:00:00Z",
                }
            ]
        )
        tm.get_credential = AsyncMock(return_value=None)

        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.test_credential(
                provider="google-drive",
                user_email="alice@example.com",
                context=ctx,
            )

        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_invalid_credential(self) -> None:
        tm = MagicMock()
        tm.get_valid_token = AsyncMock(return_value=None)
        tm.get_credential = AsyncMock(return_value=None)

        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=True)

        with patch("nexus.lib.context_utils.get_zone_id", return_value="zone1"):
            result = await service.test_credential(
                provider="google-drive",
                user_email="alice@example.com",
                context=ctx,
            )

        assert result["valid"] is False
        assert "error" in result


# ===========================================================================
# OAuthCredentialService — Credential Ownership
# ===========================================================================


class TestCheckCredentialOwnership:
    """Tests for _check_credential_ownership permission enforcement."""

    @pytest.mark.asyncio
    async def test_admin_can_access_any(self) -> None:
        tm = MagicMock()
        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="admin", is_admin=True)

        # Should not raise
        await service._check_credential_ownership("google-drive", "other@example.com", "zone1", ctx)

    @pytest.mark.asyncio
    async def test_owner_can_access_own(self) -> None:
        cred = MagicMock()
        cred.metadata = {"user_id": "user-alice"}
        cred.user_email = "alice@example.com"

        tm = MagicMock()
        tm.get_credential = AsyncMock(return_value=cred)
        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=False)

        # Should not raise
        await service._check_credential_ownership("google-drive", "alice@example.com", "zone1", ctx)

    @pytest.mark.asyncio
    async def test_non_owner_cannot_access(self) -> None:
        cred = MagicMock()
        cred.metadata = {"user_id": "user-bob"}
        cred.user_email = "bob@example.com"

        tm = MagicMock()
        tm.get_credential = AsyncMock(return_value=cred)
        service = OAuthCredentialService(token_manager=tm)
        ctx = MagicMock(user_id="user-alice", is_admin=False)

        with pytest.raises(ValueError, match="Permission denied"):
            await service._check_credential_ownership(
                "google-drive", "bob@example.com", "zone1", ctx
            )


# ===========================================================================
# OAuthCredentialService — Helper: _map_provider_name
# ===========================================================================


class TestMapProviderName:
    """Tests for _map_provider_name."""

    def test_known_mappings(self) -> None:
        service = OAuthCredentialService()
        assert service._map_provider_name("google") == "google-drive"
        assert service._map_provider_name("twitter") == "x"
        assert service._map_provider_name("x") == "x"
        assert service._map_provider_name("microsoft") == "microsoft-onedrive"

    def test_unknown_passed_through(self) -> None:
        service = OAuthCredentialService()
        assert service._map_provider_name("custom-provider") == "custom-provider"


# ===========================================================================
# OAuthCredentialService — Helper: _get_pkce_verifier
# ===========================================================================


class TestGetPKCEVerifier:
    """Tests for _get_pkce_verifier."""

    @pytest.mark.asyncio
    async def test_returns_direct_verifier(self) -> None:
        service = OAuthCredentialService()
        result = await service._get_pkce_verifier("x", "direct_verifier", None)
        assert result == "direct_verifier"

    @pytest.mark.asyncio
    async def test_returns_from_cache(self) -> None:
        cache = FakeCacheStore()
        store = PKCEStateStore(cache_store=cache)
        await store.save("state123", {"code_verifier": "cached_verifier"})

        service = OAuthCredentialService(pkce_store=store)
        result = await service._get_pkce_verifier("x", None, "state123")
        assert result == "cached_verifier"

    @pytest.mark.asyncio
    async def test_raises_when_not_found(self) -> None:
        service = OAuthCredentialService(pkce_store=PKCEStateStore(cache_store=None))

        with pytest.raises(ValueError, match="PKCE"):
            await service._get_pkce_verifier("x", None, None)

    @pytest.mark.asyncio
    async def test_raises_when_state_expired(self) -> None:
        cache = FakeCacheStore()
        store = PKCEStateStore(cache_store=cache)
        # Don't save anything — simulates expiry

        service = OAuthCredentialService(pkce_store=store)

        with pytest.raises(ValueError, match="PKCE"):
            await service._get_pkce_verifier("x", None, "expired_state")


# ===========================================================================
# OAuthCredentialService — Lazy Factory/TokenManager Creation
# ===========================================================================


class TestLazyCreation:
    """Tests for lazy creation of factory and token manager."""

    def test_get_oauth_factory_creates_on_demand(self) -> None:
        service = OAuthCredentialService(oauth_factory=None, oauth_config=None)

        with patch("nexus.bricks.auth.oauth.factory.OAuthProviderFactory") as MockFactory:
            MockFactory.return_value = MagicMock()
            factory = service._get_oauth_factory()
            assert factory is not None
            MockFactory.assert_called_once()

    def test_get_token_manager_no_db_returns_none(self) -> None:
        service = OAuthCredentialService(token_manager=None, database_url=None)

        result = service._get_token_manager()
        assert result is None

    def test_get_token_manager_with_db_url(self) -> None:
        service = OAuthCredentialService(token_manager=None, database_url="sqlite:///test.db")

        with patch("nexus.bricks.auth.oauth.token_manager.TokenManager") as MockTM:
            MockTM.return_value = MagicMock()
            tm = service._get_token_manager()
            assert tm is not None
            MockTM.assert_called_once_with(db_url="sqlite:///test.db")
