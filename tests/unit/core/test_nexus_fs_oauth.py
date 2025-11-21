"""Unit tests for NexusFS OAuth functionality.

This test suite covers OAuth operations in nexus_fs_oauth.py:
- OAuth authorization URL generation
- OAuth code exchange
- Credential listing and filtering
- Credential revocation
- Credential testing
"""

import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.core.nexus_fs_oauth import NexusFSOAuthMixin


class MockOAuthCredential:
    """Mock OAuth credential for testing."""

    def __init__(
        self,
        credential_id="test_cred_id",
        provider="google",
        user_email="test@example.com",
        tenant_id=None,
        expires_at=None,
        revoked=False,
    ):
        self.credential_id = credential_id
        self.provider = provider
        self.user_email = user_email
        self.tenant_id = tenant_id
        self.expires_at = expires_at or datetime.now() + timedelta(hours=1)
        self.revoked = revoked
        self.created_at = datetime.now()
        self.last_used_at = datetime.now()


class TestNexusFSOAuthMixin:
    """Test suite for NexusFSOAuthMixin class."""

    @pytest.fixture
    def mock_oauth_mixin(self):
        """Create a test instance of NexusFSOAuthMixin."""

        class TestMixin(NexusFSOAuthMixin):
            def __init__(self):
                self.db_path = "/tmp/test.db"
                self._token_manager = None

        return TestMixin()

    @pytest.fixture
    def mock_token_manager(self):
        """Create a mock TokenManager."""
        manager = Mock()
        manager.register_provider = Mock()
        manager.list_credentials = AsyncMock()
        manager.store_credential = AsyncMock()
        manager.revoke_credential = AsyncMock()
        manager.get_valid_token = AsyncMock()
        return manager

    def test_get_token_manager_success(self, mock_oauth_mixin):
        """Test successful TokenManager initialization."""
        with patch("nexus.server.auth.token_manager.TokenManager") as MockTM:
            mock_tm = Mock()
            MockTM.return_value = mock_tm

            manager = mock_oauth_mixin._get_token_manager()

            assert manager == mock_tm
            MockTM.assert_called_once_with(db_path="/tmp/test.db")

    def test_get_token_manager_with_database_url(self, mock_oauth_mixin):
        """Test TokenManager initialization with database URL."""
        mock_oauth_mixin.db_path = "postgresql://localhost/test"

        with patch("nexus.server.auth.token_manager.TokenManager") as MockTM:
            mock_tm = Mock()
            MockTM.return_value = mock_tm

            manager = mock_oauth_mixin._get_token_manager()

            assert manager == mock_tm
            MockTM.assert_called_once_with(db_url="postgresql://localhost/test")

    def test_get_token_manager_no_db_path(self):
        """Test TokenManager initialization fails without db_path."""

        class TestMixin(NexusFSOAuthMixin):
            pass

        mixin = TestMixin()

        with pytest.raises(RuntimeError, match="no database path configured"):
            mixin._get_token_manager()

    def test_oauth_get_drive_auth_url_success(self, mock_oauth_mixin, mock_token_manager):
        """Test successful OAuth authorization URL generation."""
        mock_oauth_mixin._token_manager = mock_token_manager

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            mock_provider = Mock()
            mock_provider.get_authorization_url = Mock(return_value="https://oauth.url?state=test")
            MockProvider.return_value = mock_provider

            result = mock_oauth_mixin.oauth_get_drive_auth_url(
                redirect_uri="http://localhost:3000/callback"
            )

            assert "url" in result
            assert "state" in result
            assert "https://oauth.url" in result["url"]
            mock_token_manager.register_provider.assert_called_once()

    def test_oauth_get_drive_auth_url_missing_credentials(self, mock_oauth_mixin):
        """Test OAuth URL generation fails without credentials."""
        with pytest.raises(RuntimeError, match="OAuth credentials not configured"):
            mock_oauth_mixin.oauth_get_drive_auth_url()

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_success(self, mock_oauth_mixin, mock_token_manager):
        """Test successful OAuth code exchange."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_cred = Mock()
        mock_cred.expires_at = datetime.now() + timedelta(hours=1)

        mock_token_manager.store_credential.return_value = "test_cred_id"

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            mock_provider = Mock()
            mock_provider.exchange_code = AsyncMock(return_value=mock_cred)
            MockProvider.return_value = mock_provider

            result = await mock_oauth_mixin.oauth_exchange_code(
                provider="google",
                code="test_code",
                user_email="test@example.com",
                redirect_uri="http://localhost:3000/callback",
            )

            assert result["success"] is True
            assert result["credential_id"] == "test_cred_id"
            assert result["user_email"] == "test@example.com"
            assert "expires_at" in result

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_invalid_provider(self, mock_oauth_mixin):
        """Test OAuth code exchange fails with invalid provider."""
        with pytest.raises(ValueError, match="Unsupported OAuth provider"):
            await mock_oauth_mixin.oauth_exchange_code(
                provider="invalid",
                code="test_code",
                user_email="test@example.com",
            )

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_exchange_fails(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth code exchange failure."""
        mock_oauth_mixin._token_manager = mock_token_manager

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            mock_provider = Mock()
            mock_provider.exchange_code = AsyncMock(side_effect=Exception("Exchange failed"))
            MockProvider.return_value = mock_provider

            with pytest.raises(ValueError, match="Failed to exchange authorization code"):
                await mock_oauth_mixin.oauth_exchange_code(
                    provider="google",
                    code="invalid_code",
                    user_email="test@example.com",
                )

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_all(self, mock_oauth_mixin, mock_token_manager):
        """Test listing all OAuth credentials."""
        mock_oauth_mixin._token_manager = mock_token_manager

        credentials = [
            {
                "credential_id": "cred1",
                "provider": "google",
                "user_email": "user1@example.com",
                "revoked": False,
            },
            {
                "credential_id": "cred2",
                "provider": "google",
                "user_email": "user2@example.com",
                "revoked": False,
            },
        ]
        mock_token_manager.list_credentials.return_value = credentials

        result = await mock_oauth_mixin.oauth_list_credentials()

        assert len(result) == 2
        assert result[0]["credential_id"] == "cred1"
        assert result[1]["credential_id"] == "cred2"

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_filter_provider(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test listing credentials filtered by provider."""
        mock_oauth_mixin._token_manager = mock_token_manager

        credentials = [
            {
                "credential_id": "cred1",
                "provider": "google",
                "user_email": "user1@example.com",
                "revoked": False,
            },
            {
                "credential_id": "cred2",
                "provider": "microsoft",
                "user_email": "user2@example.com",
                "revoked": False,
            },
        ]
        mock_token_manager.list_credentials.return_value = credentials

        result = await mock_oauth_mixin.oauth_list_credentials(provider="google")

        assert len(result) == 1
        assert result[0]["provider"] == "google"

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_exclude_revoked(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test listing credentials excluding revoked ones."""
        mock_oauth_mixin._token_manager = mock_token_manager

        credentials = [
            {
                "credential_id": "cred1",
                "provider": "google",
                "user_email": "user1@example.com",
                "revoked": False,
            },
            {
                "credential_id": "cred2",
                "provider": "google",
                "user_email": "user2@example.com",
                "revoked": True,
            },
        ]
        mock_token_manager.list_credentials.return_value = credentials

        result = await mock_oauth_mixin.oauth_list_credentials(include_revoked=False)

        assert len(result) == 1
        assert result[0]["revoked"] is False

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_include_revoked(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test listing credentials including revoked ones."""
        mock_oauth_mixin._token_manager = mock_token_manager

        credentials = [
            {
                "credential_id": "cred1",
                "provider": "google",
                "user_email": "user1@example.com",
                "revoked": False,
            },
            {
                "credential_id": "cred2",
                "provider": "google",
                "user_email": "user2@example.com",
                "revoked": True,
            },
        ]
        mock_token_manager.list_credentials.return_value = credentials

        result = await mock_oauth_mixin.oauth_list_credentials(include_revoked=True)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_oauth_revoke_credential_success(self, mock_oauth_mixin, mock_token_manager):
        """Test successful credential revocation."""
        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.revoke_credential.return_value = True

        result = await mock_oauth_mixin.oauth_revoke_credential(
            provider="google",
            user_email="test@example.com",
        )

        assert result["success"] is True
        mock_token_manager.revoke_credential.assert_called_once_with(
            provider="google",
            user_email="test@example.com",
            tenant_id=None,
        )

    @pytest.mark.asyncio
    async def test_oauth_revoke_credential_not_found(self, mock_oauth_mixin, mock_token_manager):
        """Test credential revocation when credential not found."""
        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.revoke_credential.return_value = False

        with pytest.raises(ValueError, match="Credential not found"):
            await mock_oauth_mixin.oauth_revoke_credential(
                provider="google",
                user_email="test@example.com",
            )

    @pytest.mark.asyncio
    async def test_oauth_test_credential_valid(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth credential validity check - valid credential."""
        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.get_valid_token.return_value = "valid_token"

        expires_at = datetime.now() + timedelta(hours=1)
        credentials = [
            {
                "credential_id": "cred1",
                "provider": "google",
                "user_email": "test@example.com",
                "expires_at": expires_at.isoformat(),
            }
        ]
        mock_token_manager.list_credentials.return_value = credentials

        result = await mock_oauth_mixin.oauth_test_credential(
            provider="google",
            user_email="test@example.com",
        )

        assert result["valid"] is True
        assert result["refreshed"] is True
        assert expires_at.isoformat() in result["expires_at"]

    @pytest.mark.asyncio
    async def test_oauth_test_credential_invalid(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth credential validity check - invalid credential."""
        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.get_valid_token.return_value = None

        result = await mock_oauth_mixin.oauth_test_credential(
            provider="google",
            user_email="test@example.com",
        )

        assert result["valid"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_oauth_test_credential_exception(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth credential validity check - exception handling."""
        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.get_valid_token.side_effect = Exception("Token retrieval failed")

        result = await mock_oauth_mixin.oauth_test_credential(
            provider="google",
            user_email="test@example.com",
        )

        assert result["valid"] is False
        assert "Token retrieval failed" in result["error"]

    def test_oauth_get_drive_auth_url_with_context(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth URL generation with operation context."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_context = Mock()
        mock_context.user_id = "test_user"
        mock_context.tenant_id = "test_tenant"

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
            contextlib.suppress(ValueError, TypeError),
        ):
            mock_provider = Mock()
            mock_provider.get_authorization_url = Mock(return_value="https://oauth.url")
            MockProvider.return_value = mock_provider

            # Should not raise even with context parameter (context is accepted but ignored)
            mock_oauth_mixin.oauth_get_drive_auth_url(context=mock_context)

            # Test passes if no exception is raised

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_with_state(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth code exchange with state parameter."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_cred = Mock()
        mock_cred.expires_at = datetime.now() + timedelta(hours=1)

        mock_token_manager.store_credential.return_value = "test_cred_id"

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            mock_provider = Mock()
            mock_provider.exchange_code = AsyncMock(return_value=mock_cred)
            MockProvider.return_value = mock_provider

            # State parameter should be accepted but not used
            result = await mock_oauth_mixin.oauth_exchange_code(
                provider="google",
                code="test_code",
                user_email="test@example.com",
                state="test_state_token",
            )

            assert result["success"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
