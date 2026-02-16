"""Tests for OAuthService.

Covers the security-critical OAuth credential management service:
- Provider discovery (listing available OAuth providers)
- OAuth flow (authorization URL generation, code exchange)
- Credential lifecycle (list, revoke, test validity)
- Token storage and retrieval
- Permission enforcement (user isolation, admin access)
- Input validation and error handling
- PKCE support for providers that require it
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.permissions import OperationContext
from nexus.services.oauth_service import OAuthService

# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def mock_oauth_factory():
    """Create a mock OAuthProviderFactory."""
    factory = MagicMock()

    # Provider config mock
    provider_config = MagicMock()
    provider_config.name = "google-drive"
    provider_config.display_name = "Google Drive"
    provider_config.scopes = ["https://www.googleapis.com/auth/drive"]
    provider_config.requires_pkce = False
    provider_config.icon_url = "https://example.com/google.png"
    provider_config.metadata = {}

    # OAuth config mock
    oauth_config = MagicMock()
    oauth_config.providers = [provider_config]
    factory._oauth_config = oauth_config

    # create_provider returns a provider instance mock
    provider_instance = MagicMock()
    provider_instance.provider_name = "google-drive"
    provider_instance.get_authorization_url.return_value = (
        "https://accounts.google.com/o/oauth2/auth?state=test"
    )
    factory.create_provider.return_value = provider_instance

    # get_provider_config returns the provider config
    factory.get_provider_config.return_value = provider_config

    return factory


@pytest.fixture
def mock_token_manager():
    """Create a mock TokenManager."""
    manager = AsyncMock()
    manager.store_credential = AsyncMock(return_value="cred-123")
    manager.list_credentials = AsyncMock(return_value=[])
    manager.get_credential = AsyncMock(return_value=None)
    manager.revoke_credential = AsyncMock(return_value=True)
    manager.get_valid_token = AsyncMock(return_value="valid-token-abc")
    manager.register_provider = MagicMock()
    return manager


@pytest.fixture
def service(mock_oauth_factory, mock_token_manager):
    """Create OAuthService with mocked dependencies."""
    return OAuthService(
        oauth_factory=mock_oauth_factory,
        token_manager=mock_token_manager,
    )


@pytest.fixture
def service_no_deps():
    """Create OAuthService without dependencies (for lazy init tests)."""
    return OAuthService(
        oauth_factory=None,
        token_manager=None,
    )


@pytest.fixture
def mock_credential():
    """Create a mock OAuth credential."""
    cred = MagicMock()
    cred.access_token = "access-token-123"
    cred.refresh_token = "refresh-token-456"
    cred.expires_at = datetime.now(UTC) + timedelta(hours=1)
    cred.user_email = "alice@example.com"
    cred.metadata = {"user_id": "alice"}
    return cred


@pytest.fixture
def user_context():
    """Create a standard user OperationContext."""
    return OperationContext(
        user="alice",
        groups=["users"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


@pytest.fixture
def admin_context():
    """Create an admin OperationContext."""
    return OperationContext(
        user="admin_user",
        groups=["admin"],
        zone_id="test_zone",
        is_system=False,
        is_admin=True,
    )


# =========================================================================
# Initialization Tests
# =========================================================================


class TestOAuthServiceInit:
    """Test OAuthService initialization."""

    def test_init_with_dependencies(self, mock_oauth_factory, mock_token_manager):
        """Test initialization with all dependencies provided."""
        svc = OAuthService(
            oauth_factory=mock_oauth_factory,
            token_manager=mock_token_manager,
            nexus_fs=MagicMock(),
        )
        assert svc._oauth_factory is mock_oauth_factory
        assert svc._token_manager is mock_token_manager
        assert svc.nexus_fs is not None

    def test_init_without_dependencies(self):
        """Test initialization without dependencies (lazy init pattern)."""
        svc = OAuthService()
        assert svc._oauth_factory is None
        assert svc._token_manager is None
        assert svc.nexus_fs is None

    def test_init_partial_dependencies(self, mock_oauth_factory):
        """Test initialization with only factory (token_manager lazy)."""
        svc = OAuthService(oauth_factory=mock_oauth_factory)
        assert svc._oauth_factory is mock_oauth_factory
        assert svc._token_manager is None


# =========================================================================
# oauth_list_providers Tests
# =========================================================================


class TestOAuthListProviders:
    """Test listing available OAuth providers."""

    @pytest.mark.asyncio
    async def test_list_providers_returns_configured(self, service, mock_oauth_factory):
        """Test listing returns all configured providers."""
        result = await service.oauth_list_providers()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "google-drive"
        assert result[0]["display_name"] == "Google Drive"
        assert result[0]["requires_pkce"] is False

    @pytest.mark.asyncio
    async def test_list_providers_includes_icon_url(self, service):
        """Test that icon_url is included when available."""
        result = await service.oauth_list_providers()
        assert "icon_url" in result[0]
        assert result[0]["icon_url"] == "https://example.com/google.png"

    @pytest.mark.asyncio
    async def test_list_providers_omits_icon_when_none(self, service, mock_oauth_factory):
        """Test that icon_url is omitted when not configured."""
        mock_oauth_factory._oauth_config.providers[0].icon_url = None
        result = await service.oauth_list_providers()
        assert "icon_url" not in result[0]

    @pytest.mark.asyncio
    async def test_list_providers_multiple(self, service, mock_oauth_factory):
        """Test listing multiple providers."""
        # Add a second provider
        second_provider = MagicMock()
        second_provider.name = "x"
        second_provider.display_name = "X (Twitter)"
        second_provider.scopes = ["tweet.read"]
        second_provider.requires_pkce = True
        second_provider.icon_url = None
        second_provider.metadata = {"version": "2"}
        mock_oauth_factory._oauth_config.providers.append(second_provider)

        result = await service.oauth_list_providers()
        assert len(result) == 2
        assert result[1]["name"] == "x"
        assert result[1]["requires_pkce"] is True

    @pytest.mark.asyncio
    async def test_list_providers_empty(self, service, mock_oauth_factory):
        """Test listing when no providers are configured."""
        mock_oauth_factory._oauth_config.providers = []
        result = await service.oauth_list_providers()
        assert result == []


# =========================================================================
# oauth_get_auth_url Tests
# =========================================================================


class TestOAuthGetAuthUrl:
    """Test OAuth authorization URL generation."""

    @pytest.mark.asyncio
    async def test_get_auth_url_returns_url_and_state(self, service, mock_oauth_factory):
        """Test that auth URL and state token are returned."""
        # Set up provider config to not require PKCE
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        result = await service.oauth_get_auth_url(
            provider="google",
            redirect_uri="http://localhost:3000/oauth/callback",
        )

        assert "url" in result
        assert "state" in result
        assert isinstance(result["state"], str)
        assert len(result["state"]) > 0

    @pytest.mark.asyncio
    async def test_get_auth_url_with_pkce(self, service, mock_oauth_factory):
        """Test auth URL generation for PKCE-required provider."""
        # Configure provider to require PKCE
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = True
        provider_instance = mock_oauth_factory.create_provider.return_value
        provider_instance.get_authorization_url_with_pkce.return_value = (
            "https://x.com/oauth/authorize?state=test",
            {
                "code_verifier": "verifier123",
                "code_challenge": "challenge456",
                "code_challenge_method": "S256",
            },
        )

        # Instance-level PKCE store (Issue #1597 — no more module-level _pkce_cache)
        result = await service.oauth_get_auth_url(
            provider="x",
            redirect_uri="http://localhost:3000/oauth/callback",
        )

        assert "url" in result
        assert "pkce_data" in result
        assert result["pkce_data"]["code_verifier"] == "verifier123"
        # Verify data was stored in instance PKCE store
        assert service._pkce_store.size > 0

    @pytest.mark.asyncio
    async def test_get_auth_url_maps_provider_name(self, service, mock_oauth_factory):
        """Test that user-friendly provider names are mapped to config names."""
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        await service.oauth_get_auth_url(
            provider="google",
            redirect_uri="http://localhost:3000/oauth/callback",
        )

        # Factory should receive the mapped name "google-drive"
        mock_oauth_factory.create_provider.assert_called_once_with(
            name="google-drive",
            redirect_uri="http://localhost:3000/oauth/callback",
            scopes=None,
        )


# =========================================================================
# oauth_exchange_code Tests
# =========================================================================


class TestOAuthExchangeCode:
    """Test OAuth authorization code exchange."""

    @pytest.mark.asyncio
    async def test_exchange_code_stores_credential(
        self, service, mock_oauth_factory, mock_token_manager
    ):
        """Test that code exchange stores the resulting credential."""
        provider_instance = mock_oauth_factory.create_provider.return_value
        mock_cred = MagicMock()
        mock_cred.expires_at = datetime.now(UTC) + timedelta(hours=1)
        mock_cred.user_email = None
        provider_instance.exchange_code = AsyncMock(return_value=mock_cred)

        # Provider does not require PKCE
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        result = await service.oauth_exchange_code(
            provider="google",
            code="auth-code-123",
            user_email="alice@example.com",
        )

        assert result["success"] is True
        assert result["credential_id"] == "cred-123"
        assert result["user_email"] == "alice@example.com"
        mock_token_manager.store_credential.assert_called_once()

    @pytest.mark.asyncio
    async def test_exchange_code_requires_email_or_provider_email(
        self, service, mock_oauth_factory, mock_token_manager
    ):
        """Test that exchange fails if email cannot be determined."""
        provider_instance = mock_oauth_factory.create_provider.return_value
        mock_cred = MagicMock()
        mock_cred.expires_at = None
        mock_cred.user_email = None
        mock_cred.access_token = "token"
        provider_instance.exchange_code = AsyncMock(return_value=mock_cred)
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        # Patch _get_user_email_from_provider to return None
        with (
            patch.object(service, "_get_user_email_from_provider", return_value=None),
            pytest.raises(ValueError, match="user_email is required"),
        ):
            await service.oauth_exchange_code(
                provider="google",
                code="auth-code-123",
                # no user_email provided
            )

    @pytest.mark.asyncio
    async def test_exchange_code_handles_exchange_failure(self, service, mock_oauth_factory):
        """Test that exchange failure raises ValueError."""
        provider_instance = mock_oauth_factory.create_provider.return_value
        provider_instance.exchange_code = AsyncMock(side_effect=Exception("Token exchange failed"))
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        with pytest.raises(ValueError, match="Failed to exchange authorization code"):
            await service.oauth_exchange_code(
                provider="google",
                code="bad-code",
                user_email="alice@example.com",
            )

    @pytest.mark.asyncio
    async def test_exchange_code_handles_store_failure(
        self, service, mock_oauth_factory, mock_token_manager
    ):
        """Test that credential store failure raises ValueError."""
        provider_instance = mock_oauth_factory.create_provider.return_value
        mock_cred = MagicMock()
        mock_cred.expires_at = None
        mock_cred.user_email = None
        provider_instance.exchange_code = AsyncMock(return_value=mock_cred)
        mock_oauth_factory.get_provider_config.return_value.requires_pkce = False

        mock_token_manager.store_credential = AsyncMock(side_effect=Exception("DB write failed"))

        with pytest.raises(ValueError, match="Failed to store credential"):
            await service.oauth_exchange_code(
                provider="google",
                code="auth-code-123",
                user_email="alice@example.com",
            )


# =========================================================================
# oauth_list_credentials Tests
# =========================================================================


class TestOAuthListCredentials:
    """Test credential listing with permission enforcement."""

    @pytest.mark.asyncio
    async def test_list_credentials_returns_user_creds(
        self, service, mock_token_manager, user_context
    ):
        """Test that non-admin users see only their own credentials."""
        mock_token_manager.list_credentials.return_value = [
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

        result = await service.oauth_list_credentials(context=user_context)

        # Alice should only see her own credential
        assert len(result) == 1
        assert result[0]["user_id"] == "alice"

    @pytest.mark.asyncio
    async def test_list_credentials_admin_sees_all(
        self, service, mock_token_manager, admin_context
    ):
        """Test that admin users see all credentials in the zone."""
        mock_token_manager.list_credentials.return_value = [
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

        result = await service.oauth_list_credentials(context=admin_context)

        # Admin sees all credentials
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_credentials_filters_by_provider(
        self, service, mock_token_manager, admin_context
    ):
        """Test filtering credentials by provider."""
        mock_token_manager.list_credentials.return_value = [
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

        result = await service.oauth_list_credentials(
            provider="google-drive",
            context=admin_context,
        )

        assert len(result) == 1
        assert result[0]["provider"] == "google-drive"

    @pytest.mark.asyncio
    async def test_list_credentials_excludes_revoked_by_default(
        self, service, mock_token_manager, admin_context
    ):
        """Test that revoked credentials are excluded by default."""
        mock_token_manager.list_credentials.return_value = [
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

        result = await service.oauth_list_credentials(context=admin_context)
        assert len(result) == 1
        assert result[0]["revoked"] is False

    @pytest.mark.asyncio
    async def test_list_credentials_includes_revoked_when_requested(
        self, service, mock_token_manager, admin_context
    ):
        """Test that revoked credentials are included when requested."""
        mock_token_manager.list_credentials.return_value = [
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

        result = await service.oauth_list_credentials(
            include_revoked=True,
            context=admin_context,
        )
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_list_credentials_empty(self, service, mock_token_manager, user_context):
        """Test listing when no credentials exist."""
        mock_token_manager.list_credentials.return_value = []
        result = await service.oauth_list_credentials(context=user_context)
        assert result == []


# =========================================================================
# oauth_revoke_credential Tests
# =========================================================================


class TestOAuthRevokeCredential:
    """Test credential revocation with permission enforcement."""

    @pytest.mark.asyncio
    async def test_revoke_own_credential(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that users can revoke their own credentials."""
        mock_token_manager.get_credential.return_value = mock_credential
        mock_credential.metadata = {"user_id": "alice"}
        mock_credential.user_email = "alice@example.com"

        result = await service.oauth_revoke_credential(
            provider="google-drive",
            user_email="alice@example.com",
            context=user_context,
        )

        assert result["success"] is True
        mock_token_manager.revoke_credential.assert_called_once()

    @pytest.mark.asyncio
    async def test_revoke_denied_for_other_user(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that users cannot revoke other users' credentials."""
        mock_credential.metadata = {"user_id": "bob"}
        mock_credential.user_email = "bob@example.com"
        mock_token_manager.get_credential.return_value = mock_credential

        with pytest.raises(ValueError, match="Permission denied"):
            await service.oauth_revoke_credential(
                provider="google-drive",
                user_email="bob@example.com",
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_revoke_admin_can_revoke_any(
        self, service, mock_token_manager, admin_context, mock_credential
    ):
        """Test that admins can revoke any credential."""
        mock_credential.metadata = {"user_id": "bob"}
        mock_token_manager.get_credential.return_value = mock_credential

        result = await service.oauth_revoke_credential(
            provider="google-drive",
            user_email="bob@example.com",
            context=admin_context,
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_revoke_nonexistent_credential(self, service, mock_token_manager, admin_context):
        """Test revoking a non-existent credential raises error."""
        mock_token_manager.revoke_credential.return_value = False

        with pytest.raises(ValueError, match="Credential not found"):
            await service.oauth_revoke_credential(
                provider="nonexistent",
                user_email="nobody@example.com",
                context=admin_context,
            )

    @pytest.mark.asyncio
    async def test_revoke_handles_token_manager_error(
        self, service, mock_token_manager, admin_context
    ):
        """Test that token manager errors are wrapped in ValueError."""
        mock_token_manager.revoke_credential.side_effect = Exception("DB error")

        with pytest.raises(ValueError, match="Failed to revoke credential"):
            await service.oauth_revoke_credential(
                provider="google-drive",
                user_email="alice@example.com",
                context=admin_context,
            )


# =========================================================================
# oauth_test_credential Tests
# =========================================================================


class TestOAuthTestCredential:
    """Test credential validity testing."""

    @pytest.mark.asyncio
    async def test_test_valid_credential(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that valid credentials return valid=True."""
        mock_token_manager.get_credential.return_value = mock_credential
        mock_credential.metadata = {"user_id": "alice"}
        mock_credential.user_email = "alice@example.com"

        mock_token_manager.get_valid_token.return_value = "valid-token"
        mock_token_manager.list_credentials.return_value = [
            {
                "user_email": "alice@example.com",
                "expires_at": "2026-12-31T23:59:59",
            }
        ]

        result = await service.oauth_test_credential(
            provider="google-drive",
            user_email="alice@example.com",
            context=user_context,
        )

        assert result["valid"] is True
        assert result["refreshed"] is True
        assert result["expires_at"] is not None

    @pytest.mark.asyncio
    async def test_test_invalid_credential(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that invalid/expired credentials return valid=False."""
        mock_token_manager.get_credential.return_value = mock_credential
        mock_credential.metadata = {"user_id": "alice"}
        mock_credential.user_email = "alice@example.com"

        mock_token_manager.get_valid_token.return_value = None

        result = await service.oauth_test_credential(
            provider="google-drive",
            user_email="alice@example.com",
            context=user_context,
        )

        assert result["valid"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_test_credential_handles_exception(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that exceptions during testing return valid=False with error."""
        mock_token_manager.get_credential.return_value = mock_credential
        mock_credential.metadata = {"user_id": "alice"}
        mock_credential.user_email = "alice@example.com"

        mock_token_manager.get_valid_token.side_effect = Exception("Token refresh failed")

        result = await service.oauth_test_credential(
            provider="google-drive",
            user_email="alice@example.com",
            context=user_context,
        )

        assert result["valid"] is False
        assert "Token refresh failed" in result["error"]

    @pytest.mark.asyncio
    async def test_test_credential_denied_for_other_user(
        self, service, mock_token_manager, user_context, mock_credential
    ):
        """Test that users cannot test other users' credentials."""
        mock_credential.metadata = {"user_id": "bob"}
        mock_credential.user_email = "bob@example.com"
        mock_token_manager.get_credential.return_value = mock_credential

        with pytest.raises(ValueError, match="Permission denied"):
            await service.oauth_test_credential(
                provider="google-drive",
                user_email="bob@example.com",
                context=user_context,
            )

    @pytest.mark.asyncio
    async def test_test_credential_admin_can_test_any(
        self, service, mock_token_manager, admin_context, mock_credential
    ):
        """Test that admins can test any credential."""
        mock_credential.metadata = {"user_id": "bob"}
        mock_token_manager.get_credential.return_value = mock_credential
        mock_token_manager.get_valid_token.return_value = "valid-token"
        mock_token_manager.list_credentials.return_value = [
            {"user_email": "bob@example.com", "expires_at": "2026-12-31T23:59:59"}
        ]

        result = await service.oauth_test_credential(
            provider="google-drive",
            user_email="bob@example.com",
            context=admin_context,
        )

        assert result["valid"] is True


# =========================================================================
# Helper Method Tests
# =========================================================================


class TestOAuthHelperMethods:
    """Test private helper methods."""

    def test_map_provider_name_google(self, service):
        """Test that 'google' maps to 'google-drive'."""
        assert service._map_provider_name("google") == "google-drive"

    def test_map_provider_name_twitter(self, service):
        """Test that 'twitter' maps to 'x'."""
        assert service._map_provider_name("twitter") == "x"

    def test_map_provider_name_x(self, service):
        """Test that 'x' stays as 'x'."""
        assert service._map_provider_name("x") == "x"

    def test_map_provider_name_microsoft(self, service):
        """Test that 'microsoft' maps to 'microsoft-onedrive'."""
        assert service._map_provider_name("microsoft") == "microsoft-onedrive"

    def test_map_provider_name_unknown_passthrough(self, service):
        """Test that unknown provider names pass through unchanged."""
        assert service._map_provider_name("github") == "github"
        assert service._map_provider_name("slack") == "slack"

    def test_get_oauth_factory_returns_injected(self, service, mock_oauth_factory):
        """Test that injected factory is returned directly."""
        result = service._get_oauth_factory()
        assert result is mock_oauth_factory

    def test_get_token_manager_returns_injected(self, service, mock_token_manager):
        """Test that injected token manager is returned directly."""
        result = service._get_token_manager()
        assert result is mock_token_manager

    def test_get_token_manager_raises_without_config(self, service_no_deps):
        """Test that missing token manager raises RuntimeError when no DB path."""
        with pytest.raises(RuntimeError):
            service_no_deps._get_token_manager()

    def test_create_provider_uses_factory(self, service, mock_oauth_factory):
        """Test that _create_provider delegates to factory with mapped name."""
        result = service._create_provider(
            provider="google",
            redirect_uri="http://localhost:3000/callback",
            scopes=["drive.readonly"],
        )

        mock_oauth_factory.create_provider.assert_called_with(
            name="google-drive",
            redirect_uri="http://localhost:3000/callback",
            scopes=["drive.readonly"],
        )
        assert result is mock_oauth_factory.create_provider.return_value

    def test_register_provider_delegates_to_token_manager(self, service, mock_token_manager):
        """Test that provider registration goes through token manager."""
        provider_instance = MagicMock()
        provider_instance.provider_name = "google-drive"

        service._register_provider(provider_instance)

        mock_token_manager.register_provider.assert_called_once_with(
            "google-drive", provider_instance
        )


# =========================================================================
# PKCE Helper Tests
# =========================================================================


class TestPKCEHelper:
    """Test PKCE verifier retrieval logic (async, instance-level store)."""

    @pytest.mark.asyncio
    async def test_get_pkce_verifier_from_parameter(self, service):
        """Test that directly provided verifier is used."""
        result = await service._get_pkce_verifier(
            provider="x",
            code_verifier="direct-verifier",
            state=None,
        )
        assert result == "direct-verifier"

    @pytest.mark.asyncio
    async def test_get_pkce_verifier_from_cache(self, service):
        """Test that verifier is retrieved from instance PKCE store via state."""
        await service._pkce_store.save("state-123", {"code_verifier": "cached-verifier"})
        result = await service._get_pkce_verifier(
            provider="x",
            code_verifier=None,
            state="state-123",
        )
        assert result == "cached-verifier"

    @pytest.mark.asyncio
    async def test_get_pkce_verifier_raises_when_missing(self, service):
        """Test that missing PKCE verifier raises ValueError."""
        with pytest.raises(ValueError, match="requires PKCE"):
            await service._get_pkce_verifier(
                provider="x",
                code_verifier=None,
                state=None,
            )

    @pytest.mark.asyncio
    async def test_get_pkce_verifier_cleans_cache(self, service):
        """Test that store entry is consumed (single-use) after retrieval."""
        await service._pkce_store.save("state-123", {"code_verifier": "verifier"})
        await service._get_pkce_verifier(
            provider="x",
            code_verifier=None,
            state="state-123",
        )
        # Entry should be consumed (popped)
        assert service._pkce_store.size == 0


# =========================================================================
# User Isolation Tests (Security-Critical)
# =========================================================================


class TestUserIsolation:
    """Test per-user credential isolation - a core security requirement."""

    @pytest.mark.asyncio
    async def test_user_cannot_see_other_users_credentials(self, service, mock_token_manager):
        """Test that credential listing enforces user isolation."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="z1", is_system=False, is_admin=False
        )

        mock_token_manager.list_credentials.return_value = [
            {
                "credential_id": "c1",
                "provider": "g",
                "user_id": "alice",
                "user_email": "alice@x.com",
                "revoked": False,
            },
            {
                "credential_id": "c2",
                "provider": "g",
                "user_id": "bob",
                "user_email": "bob@x.com",
                "revoked": False,
            },
            {
                "credential_id": "c3",
                "provider": "g",
                "user_id": "charlie",
                "user_email": "charlie@x.com",
                "revoked": False,
            },
        ]

        result = await service.oauth_list_credentials(context=ctx)

        # Only alice's credential should be visible
        assert len(result) == 1
        assert all(c["user_id"] == "alice" for c in result)

    @pytest.mark.asyncio
    async def test_user_cannot_revoke_other_users_credentials(
        self, service, mock_token_manager, mock_credential
    ):
        """Test that users are blocked from revoking others' credentials."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="z1", is_system=False, is_admin=False
        )

        mock_credential.metadata = {"user_id": "bob"}
        mock_credential.user_email = "bob@example.com"
        mock_token_manager.get_credential.return_value = mock_credential

        with pytest.raises(ValueError, match="Permission denied"):
            await service.oauth_revoke_credential(
                provider="google-drive",
                user_email="bob@example.com",
                context=ctx,
            )

        # Ensure revoke was never called
        mock_token_manager.revoke_credential.assert_not_called()

    @pytest.mark.asyncio
    async def test_user_cannot_test_other_users_credentials(
        self, service, mock_token_manager, mock_credential
    ):
        """Test that users are blocked from testing others' credentials."""
        ctx = OperationContext(
            user="alice", groups=[], zone_id="z1", is_system=False, is_admin=False
        )

        mock_credential.metadata = {"user_id": "bob"}
        mock_credential.user_email = "bob@example.com"
        mock_token_manager.get_credential.return_value = mock_credential

        with pytest.raises(ValueError, match="Permission denied"):
            await service.oauth_test_credential(
                provider="google-drive",
                user_email="bob@example.com",
                context=ctx,
            )

    @pytest.mark.asyncio
    async def test_fallback_user_email_matching(self, service, mock_token_manager):
        """Test user isolation fallback when user_id not set (email matching)."""
        ctx = OperationContext(
            user="alice@example.com",
            groups=[],
            zone_id="z1",
            is_system=False,
            is_admin=False,
        )

        mock_token_manager.list_credentials.return_value = [
            {
                "credential_id": "c1",
                "provider": "g",
                "user_email": "alice@example.com",
                "revoked": False,
                # no user_id - fallback to email matching
            },
            {
                "credential_id": "c2",
                "provider": "g",
                "user_email": "bob@example.com",
                "revoked": False,
            },
        ]

        result = await service.oauth_list_credentials(context=ctx)

        assert len(result) == 1
        assert result[0]["user_email"] == "alice@example.com"


# =========================================================================
# PKCEStateStore Tests (Issue #1597)
# =========================================================================


class TestPKCEStateStore:
    """Test the instance-level PKCE state store with async lock, TTL, and maxsize."""

    @pytest.mark.asyncio
    async def test_concurrent_save_and_pop(self):
        """Test that concurrent tasks can safely save/pop without data corruption."""
        import asyncio

        from nexus.services.oauth_service import PKCEStateStore

        store = PKCEStateStore(ttl=60.0, max_size=1000)
        results: list[str | None] = []

        async def flow(state: str, verifier: str) -> None:
            await store.save(state, {"code_verifier": verifier})
            # Simulate delay between save and pop
            await asyncio.sleep(0.001)
            data = await store.pop(state)
            results.append(data.get("code_verifier") if data else None)

        # Run 50 concurrent PKCE flows
        tasks = [flow(f"state-{i}", f"verifier-{i}") for i in range(50)]
        await asyncio.gather(*tasks)

        assert len(results) == 50
        # Every flow should retrieve its own verifier (no cross-contamination)
        for i, result in enumerate(results):
            assert result == f"verifier-{i}", f"Flow {i} got wrong verifier: {result}"

    @pytest.mark.asyncio
    async def test_double_pop_returns_none(self):
        """Test that pop is single-use — second pop returns None."""
        from nexus.services.oauth_service import PKCEStateStore

        store = PKCEStateStore()
        await store.save("state-1", {"code_verifier": "v1"})
        first = await store.pop("state-1")
        second = await store.pop("state-1")
        assert first is not None
        assert first["code_verifier"] == "v1"
        assert second is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        """Test that expired entries are not returned."""
        import time
        from unittest.mock import patch as mock_patch

        from nexus.services.oauth_service import PKCEStateStore

        store = PKCEStateStore(ttl=0.1)  # 100ms TTL
        await store.save("state-1", {"code_verifier": "v1"})

        # Advance monotonic time past TTL using mock
        original_monotonic = time.monotonic
        with mock_patch(
            "nexus.services.oauth_service.time.monotonic",
            side_effect=lambda: original_monotonic() + 1.0,
        ):
            result = await store.pop("state-1")

        assert result is None  # Expired

    @pytest.mark.asyncio
    async def test_eviction_removes_expired_entries(self):
        """Test that expired entries are evicted during save()."""
        import time
        from unittest.mock import patch as mock_patch

        from nexus.services.oauth_service import PKCEStateStore

        store = PKCEStateStore(ttl=0.1)  # 100ms TTL
        await store.save("state-1", {"code_verifier": "v1"})
        assert store.size == 1

        # Advance time past TTL, then save a new entry to trigger eviction
        original_monotonic = time.monotonic
        with mock_patch(
            "nexus.services.oauth_service.time.monotonic",
            side_effect=lambda: original_monotonic() + 1.0,
        ):
            await store.save("state-2", {"code_verifier": "v2"})

        # state-1 was expired and evicted, only state-2 remains
        assert store.size == 1

    @pytest.mark.asyncio
    async def test_max_size_raises_on_overflow(self):
        """Test that store raises RuntimeError when max_size is exceeded."""
        from nexus.services.oauth_service import PKCEStateStore

        store = PKCEStateStore(ttl=60.0, max_size=3)
        await store.save("state-1", {"code_verifier": "v1"})
        await store.save("state-2", {"code_verifier": "v2"})
        await store.save("state-3", {"code_verifier": "v3"})

        with pytest.raises(RuntimeError, match="capacity exceeded"):
            await store.save("state-4", {"code_verifier": "v4"})
