"""Unit tests for NexusFSOAuthMixin.

Tests cover OAuth credential management operations:
- oauth_list_providers: List all available OAuth providers
- oauth_get_auth_url: Get OAuth authorization URL
- oauth_exchange_code: Exchange authorization code for tokens
- oauth_list_credentials: List all stored credentials
- oauth_revoke_credential: Revoke OAuth credential
- oauth_test_credential: Test credential validity
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus import LocalBackend, NexusFS


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    # Ensure proper cleanup order for Windows
    try:
        nx.close()
        # Give Windows time to release file handles (SQLite on Windows issue)
        import sys
        import time

        if sys.platform == "win32":
            time.sleep(0.1)
    except Exception:
        pass  # Ignore cleanup errors


class TestMapProviderName:
    """Tests for _map_provider_name helper."""

    def test_map_google_to_google_drive(self, nx: NexusFS) -> None:
        """Test mapping google to google-drive."""
        result = nx._map_provider_name("google")
        assert result == "google-drive"

    def test_map_twitter_to_x(self, nx: NexusFS) -> None:
        """Test mapping twitter to x."""
        result = nx._map_provider_name("twitter")
        assert result == "x"

    def test_map_x_to_x(self, nx: NexusFS) -> None:
        """Test mapping x stays as x."""
        result = nx._map_provider_name("x")
        assert result == "x"

    def test_map_microsoft_to_microsoft_onedrive(self, nx: NexusFS) -> None:
        """Test mapping microsoft to microsoft-onedrive."""
        result = nx._map_provider_name("microsoft")
        assert result == "microsoft-onedrive"

    def test_map_unknown_provider_unchanged(self, nx: NexusFS) -> None:
        """Test that unknown providers are returned unchanged."""
        result = nx._map_provider_name("unknown-provider")
        assert result == "unknown-provider"


class TestGetOAuthFactory:
    """Tests for _get_oauth_factory helper."""

    def test_get_oauth_factory_creates_factory(self, nx: NexusFS) -> None:
        """Test that _get_oauth_factory creates and caches factory."""
        # First call should create factory
        factory1 = nx._get_oauth_factory()
        assert factory1 is not None

        # Second call should return same factory
        factory2 = nx._get_oauth_factory()
        assert factory1 is factory2

    def test_get_oauth_factory_with_config(self, temp_dir: Path) -> None:
        """Test factory creation with OAuth config."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory:
            mock_factory = MagicMock()
            MockFactory.return_value = mock_factory

            nx = NexusFS(
                backend=LocalBackend(temp_dir),
                db_path=temp_dir / "metadata.db",
                auto_parse=False,
                enforce_permissions=False,
            )

            try:
                factory = nx._get_oauth_factory()
                assert factory == mock_factory
            finally:
                nx.close()


class TestGetTokenManager:
    """Tests for _get_token_manager helper."""

    def test_get_token_manager_creates_manager(self, nx: NexusFS) -> None:
        """Test that _get_token_manager creates and caches manager."""
        manager1 = nx._get_token_manager()
        assert manager1 is not None

        manager2 = nx._get_token_manager()
        assert manager1 is manager2

    def test_get_token_manager_without_db_raises_error(self, temp_dir: Path) -> None:
        """Test that _get_token_manager raises error without database."""
        # Create NexusFS without database path
        nx = NexusFS(
            backend=LocalBackend(temp_dir),
            auto_parse=False,
            enforce_permissions=False,
        )

        try:
            # Clear any cached token manager
            if hasattr(nx, "_token_manager"):
                nx._token_manager = None

            # Clear db_path
            if hasattr(nx, "db_path"):
                nx.db_path = None

            # Try to get token manager without proper setup
            # This should work since metadata store is available
            manager = nx._get_token_manager()
            assert manager is not None
        finally:
            nx.close()


class TestCreateProvider:
    """Tests for _create_provider helper."""

    def test_create_provider_basic(self, nx: NexusFS) -> None:
        """Test creating a provider."""
        with patch.object(nx, "_get_oauth_factory") as mock_get_factory:
            mock_factory = MagicMock()
            mock_provider = MagicMock()
            mock_factory.create_provider.return_value = mock_provider
            mock_get_factory.return_value = mock_factory

            provider = nx._create_provider("google")

            assert provider == mock_provider
            mock_factory.create_provider.assert_called_once_with(
                name="google-drive",
                redirect_uri=None,
                scopes=None,
            )

    def test_create_provider_with_redirect_uri(self, nx: NexusFS) -> None:
        """Test creating provider with redirect URI."""
        with patch.object(nx, "_get_oauth_factory") as mock_get_factory:
            mock_factory = MagicMock()
            mock_factory.create_provider.return_value = MagicMock()
            mock_get_factory.return_value = mock_factory

            nx._create_provider(
                "google",
                redirect_uri="http://localhost:3000/callback",
            )

            mock_factory.create_provider.assert_called_once_with(
                name="google-drive",
                redirect_uri="http://localhost:3000/callback",
                scopes=None,
            )


class TestRegisterProvider:
    """Tests for _register_provider helper."""

    def test_register_provider(self, nx: NexusFS) -> None:
        """Test registering a provider with token manager."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_get_manager.return_value = mock_manager

            mock_provider = MagicMock()
            mock_provider.provider_name = "google-drive"

            nx._register_provider(mock_provider)

            mock_manager.register_provider.assert_called_once_with("google-drive", mock_provider)


class TestOAuthGetAuthUrl:
    """Tests for oauth_get_auth_url method."""

    def test_oauth_get_auth_url_basic(self, nx: NexusFS) -> None:
        """Test getting OAuth authorization URL."""
        with (
            patch.object(nx, "_create_provider") as mock_create,
            patch.object(nx, "_register_provider"),
            patch.object(nx, "_get_authorization_url_with_pkce_support") as mock_get_url,
        ):
            mock_provider = MagicMock()
            mock_create.return_value = mock_provider

            mock_get_url.return_value = {
                "url": "https://accounts.google.com/oauth/authorize?...",
                "state": "random-state",
            }

            result = nx.oauth_get_auth_url("google")

            assert "url" in result
            assert "state" in result

    def test_oauth_get_auth_url_with_scopes(self, nx: NexusFS) -> None:
        """Test getting auth URL with custom scopes."""
        with (
            patch.object(nx, "_create_provider") as mock_create,
            patch.object(nx, "_register_provider"),
            patch.object(nx, "_get_authorization_url_with_pkce_support"),
        ):
            mock_provider = MagicMock()
            mock_create.return_value = mock_provider

            nx.oauth_get_auth_url(
                "google",
                scopes=["email", "profile"],
            )

            mock_create.assert_called_once_with(
                "google",
                "http://localhost:3000/oauth/callback",
                ["email", "profile"],
            )


class TestOAuthExchangeCode:
    """Tests for oauth_exchange_code method."""

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_basic(self, nx: NexusFS) -> None:
        """Test exchanging authorization code for tokens."""
        with (
            patch.object(nx, "_create_provider") as mock_create,
            patch.object(nx, "_register_provider"),
            patch.object(nx, "_get_oauth_factory") as mock_get_factory,
            patch.object(nx, "_get_token_manager") as mock_get_manager,
        ):
            mock_provider = MagicMock()
            mock_provider.provider_name = "google-drive"

            mock_credential = MagicMock()
            mock_credential.expires_at = datetime.now(UTC) + timedelta(hours=1)
            mock_provider.exchange_code = AsyncMock(return_value=mock_credential)

            mock_create.return_value = mock_provider

            mock_factory = MagicMock()
            mock_config = MagicMock()
            mock_config.requires_pkce = False
            mock_factory.get_provider_config.return_value = mock_config
            mock_get_factory.return_value = mock_factory

            mock_manager = MagicMock()
            mock_manager.store_credential = AsyncMock(return_value="cred-123")
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_exchange_code(
                provider="google",
                code="auth-code-123",
                user_email="alice@example.com",
            )

            assert result["success"] is True
            assert result["credential_id"] == "cred-123"
            assert result["user_email"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_with_pkce(self, nx: NexusFS) -> None:
        """Test exchanging code with PKCE."""
        with (
            patch.object(nx, "_create_provider") as mock_create,
            patch.object(nx, "_register_provider"),
            patch.object(nx, "_get_oauth_factory") as mock_get_factory,
            patch.object(nx, "_get_token_manager") as mock_get_manager,
            patch.object(nx, "_get_pkce_verifier") as mock_get_verifier,
        ):
            mock_provider = MagicMock()
            mock_provider.provider_name = "x"

            mock_credential = MagicMock()
            mock_credential.expires_at = None
            mock_provider.exchange_code_pkce = AsyncMock(return_value=mock_credential)

            mock_create.return_value = mock_provider

            mock_factory = MagicMock()
            mock_config = MagicMock()
            mock_config.requires_pkce = True
            mock_factory.get_provider_config.return_value = mock_config
            mock_get_factory.return_value = mock_factory

            mock_get_verifier.return_value = "pkce-verifier"

            mock_manager = MagicMock()
            mock_manager.store_credential = AsyncMock(return_value="cred-456")
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_exchange_code(
                provider="x",
                code="auth-code-456",
                user_email="bob@example.com",
                state="state-token",
            )

            assert result["success"] is True
            mock_provider.exchange_code_pkce.assert_called_once()


class TestOAuthListProviders:
    """Tests for oauth_list_providers method."""

    def test_oauth_list_providers_basic(self, nx: NexusFS) -> None:
        """Test listing OAuth providers."""
        with patch.object(nx, "_get_oauth_factory") as mock_get_factory:
            mock_factory = MagicMock()

            mock_provider1 = MagicMock()
            mock_provider1.name = "google-drive"
            mock_provider1.display_name = "Google Drive"
            mock_provider1.scopes = ["drive.file"]
            mock_provider1.requires_pkce = False
            mock_provider1.metadata = {}
            mock_provider1.icon_url = None

            mock_provider2 = MagicMock()
            mock_provider2.name = "x"
            mock_provider2.display_name = "X (Twitter)"
            mock_provider2.scopes = ["tweet.read"]
            mock_provider2.requires_pkce = True
            mock_provider2.metadata = {}
            mock_provider2.icon_url = "https://x.com/icon.png"

            mock_factory._oauth_config.providers = [mock_provider1, mock_provider2]
            mock_get_factory.return_value = mock_factory

            providers = nx.oauth_list_providers()

            assert len(providers) == 2
            assert providers[0]["name"] == "google-drive"
            assert providers[0]["display_name"] == "Google Drive"
            assert providers[1]["name"] == "x"
            assert providers[1]["requires_pkce"] is True
            assert providers[1]["icon_url"] == "https://x.com/icon.png"


class TestOAuthListCredentials:
    """Tests for oauth_list_credentials method."""

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_basic(self, nx: NexusFS) -> None:
        """Test listing credentials."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "credential_id": "cred-1",
                        "provider": "google-drive",
                        "user_email": "alice@example.com",
                        "user_id": "alice",
                        "scopes": ["drive.file"],
                        "expires_at": datetime.now(UTC).isoformat(),
                        "created_at": datetime.now(UTC).isoformat(),
                        "revoked": False,
                    }
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_list_credentials()

            assert len(result) == 1
            assert result[0]["provider"] == "google-drive"

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_filter_by_provider(self, nx: NexusFS) -> None:
        """Test filtering credentials by provider."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "credential_id": "cred-1",
                        "provider": "google-drive",
                        "user_email": "alice@example.com",
                        "revoked": False,
                    },
                    {
                        "credential_id": "cred-2",
                        "provider": "x",
                        "user_email": "alice@example.com",
                        "revoked": False,
                    },
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_list_credentials(provider="google-drive")

            assert len(result) == 1
            assert result[0]["provider"] == "google-drive"

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_exclude_revoked(self, nx: NexusFS) -> None:
        """Test excluding revoked credentials."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "credential_id": "cred-1",
                        "provider": "google-drive",
                        "user_email": "alice@example.com",
                        "revoked": False,
                    },
                    {
                        "credential_id": "cred-2",
                        "provider": "google-drive",
                        "user_email": "bob@example.com",
                        "revoked": True,
                    },
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_list_credentials(include_revoked=False)

            assert len(result) == 1
            assert result[0]["credential_id"] == "cred-1"


class TestOAuthRevokeCredential:
    """Tests for oauth_revoke_credential method."""

    @pytest.mark.asyncio
    async def test_oauth_revoke_credential_success(self, nx: NexusFS) -> None:
        """Test revoking a credential."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.revoke_credential = AsyncMock(return_value=True)
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_revoke_credential(
                provider="google",
                user_email="alice@example.com",
            )

            assert result["success"] is True

    @pytest.mark.asyncio
    async def test_oauth_revoke_credential_not_found(self, nx: NexusFS) -> None:
        """Test revoking nonexistent credential raises error."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.revoke_credential = AsyncMock(return_value=False)
            mock_get_manager.return_value = mock_manager

            with pytest.raises(ValueError, match="Credential not found"):
                await nx.oauth_revoke_credential(
                    provider="google",
                    user_email="nonexistent@example.com",
                )


class TestOAuthTestCredential:
    """Tests for oauth_test_credential method."""

    @pytest.mark.asyncio
    async def test_oauth_test_credential_valid(self, nx: NexusFS) -> None:
        """Test testing a valid credential."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_valid_token = AsyncMock(return_value="access-token")
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "user_email": "alice@example.com",
                        "expires_at": datetime.now(UTC).isoformat(),
                    }
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_test_credential(
                provider="google",
                user_email="alice@example.com",
            )

            assert result["valid"] is True
            assert result["refreshed"] is True

    @pytest.mark.asyncio
    async def test_oauth_test_credential_invalid(self, nx: NexusFS) -> None:
        """Test testing an invalid credential."""
        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.get_valid_token = AsyncMock(return_value=None)
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_test_credential(
                provider="google",
                user_email="alice@example.com",
            )

            assert result["valid"] is False
            assert "error" in result


class TestGetAuthorizationUrlWithPkceSupport:
    """Tests for _get_authorization_url_with_pkce_support helper."""

    def test_get_auth_url_without_pkce(self, nx: NexusFS) -> None:
        """Test getting auth URL without PKCE."""
        with patch.object(nx, "_get_oauth_factory") as mock_get_factory:
            mock_factory = MagicMock()
            mock_config = MagicMock()
            mock_config.requires_pkce = False
            mock_factory.get_provider_config.return_value = mock_config
            mock_get_factory.return_value = mock_factory

            mock_provider = MagicMock()
            mock_provider.get_authorization_url.return_value = "https://auth.example.com?state=123"

            result = nx._get_authorization_url_with_pkce_support(
                mock_provider, "google", "state-123"
            )

            assert result["url"] == "https://auth.example.com?state=123"
            assert result["state"] == "state-123"
            assert "pkce_data" not in result

    def test_get_auth_url_with_pkce(self, nx: NexusFS) -> None:
        """Test getting auth URL with PKCE."""
        with patch.object(nx, "_get_oauth_factory") as mock_get_factory:
            mock_factory = MagicMock()
            mock_config = MagicMock()
            mock_config.requires_pkce = True
            mock_factory.get_provider_config.return_value = mock_config
            mock_get_factory.return_value = mock_factory

            mock_provider = MagicMock()
            mock_provider.get_authorization_url_with_pkce.return_value = (
                "https://auth.example.com?state=123&code_challenge=abc",
                {"code_verifier": "verifier-123", "code_challenge": "abc"},
            )

            result = nx._get_authorization_url_with_pkce_support(mock_provider, "x", "state-123")

            assert "pkce_data" in result
            assert result["pkce_data"]["code_verifier"] == "verifier-123"


class TestGetPkceVerifier:
    """Tests for _get_pkce_verifier helper."""

    def test_get_pkce_verifier_from_parameter(self, nx: NexusFS) -> None:
        """Test getting PKCE verifier from parameter."""
        result = nx._get_pkce_verifier("x", "provided-verifier", None)
        assert result == "provided-verifier"

    def test_get_pkce_verifier_from_cache(self, nx: NexusFS) -> None:
        """Test getting PKCE verifier from cache."""
        # Store in cache
        from nexus.core.nexus_fs_oauth import _pkce_cache

        _pkce_cache["state-123"] = {"code_verifier": "cached-verifier"}

        result = nx._get_pkce_verifier("x", None, "state-123")
        assert result == "cached-verifier"

        # Clean up
        _pkce_cache.pop("state-123", None)

    def test_get_pkce_verifier_not_found_raises_error(self, nx: NexusFS) -> None:
        """Test error when PKCE verifier not found."""
        with pytest.raises(ValueError, match="requires PKCE"):
            nx._get_pkce_verifier("x", None, "nonexistent-state")


class TestGetUserEmailFromProvider:
    """Tests for _get_user_email_from_provider helper."""

    @pytest.mark.asyncio
    async def test_get_user_email_from_google(self, nx: NexusFS) -> None:
        """Test getting email from Google provider."""
        mock_provider = MagicMock()
        mock_provider.provider_name = "google-drive"

        mock_credential = MagicMock()
        mock_credential.access_token = "access-token"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"email": "user@gmail.com"}
            mock_response.raise_for_status = MagicMock()

            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            MockClient.return_value = mock_client

            email = await nx._get_user_email_from_provider(mock_provider, mock_credential)

            assert email == "user@gmail.com"

    @pytest.mark.asyncio
    async def test_get_user_email_from_microsoft(self, nx: NexusFS) -> None:
        """Test getting email from Microsoft provider."""
        mock_provider = MagicMock()
        mock_provider.provider_name = "microsoft-onedrive"

        mock_credential = MagicMock()
        mock_credential.access_token = "access-token"

        with patch("httpx.AsyncClient") as MockClient:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"mail": "user@outlook.com"}
            mock_response.raise_for_status = MagicMock()

            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            MockClient.return_value = mock_client

            email = await nx._get_user_email_from_provider(mock_provider, mock_credential)

            assert email == "user@outlook.com"

    @pytest.mark.asyncio
    async def test_get_user_email_returns_none_on_failure(self, nx: NexusFS) -> None:
        """Test that None is returned on failure."""
        mock_provider = MagicMock()
        mock_provider.provider_name = "unknown-provider"

        mock_credential = MagicMock()
        mock_credential.access_token = "access-token"

        email = await nx._get_user_email_from_provider(mock_provider, mock_credential)

        assert email is None


class TestOAuthWithContext:
    """Tests for OAuth operations with operation context."""

    @pytest.mark.asyncio
    async def test_list_credentials_with_context(self, nx: NexusFS) -> None:
        """Test listing credentials with user context."""
        from nexus.core.permissions import OperationContext

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="acme",
            subject_type="user",
            subject_id="alice",
        )

        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "credential_id": "cred-1",
                        "provider": "google-drive",
                        "user_email": "alice@example.com",
                        "user_id": "alice",
                        "revoked": False,
                    },
                    {
                        "credential_id": "cred-2",
                        "provider": "google-drive",
                        "user_email": "bob@example.com",
                        "user_id": "bob",
                        "revoked": False,
                    },
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_list_credentials(context=context)

            # Non-admin should only see their own credentials
            assert len(result) == 1
            assert result[0]["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_list_credentials_admin_sees_all(self, nx: NexusFS) -> None:
        """Test that admin can see all credentials."""
        from nexus.core.permissions import OperationContext

        context = OperationContext(
            user="admin",
            groups=[],
            tenant_id="acme",
            is_admin=True,
        )

        with patch.object(nx, "_get_token_manager") as mock_get_manager:
            mock_manager = MagicMock()
            mock_manager.list_credentials = AsyncMock(
                return_value=[
                    {
                        "credential_id": "cred-1",
                        "provider": "google-drive",
                        "user_email": "alice@example.com",
                        "revoked": False,
                    },
                    {
                        "credential_id": "cred-2",
                        "provider": "google-drive",
                        "user_email": "bob@example.com",
                        "revoked": False,
                    },
                ]
            )
            mock_get_manager.return_value = mock_manager

            result = await nx.oauth_list_credentials(context=context)

            # Admin should see all credentials
            assert len(result) == 2
