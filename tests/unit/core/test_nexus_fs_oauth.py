"""Unit tests for NexusFS OAuth functionality.

This test suite covers OAuth operations in nexus_fs_oauth.py:
- OAuth authorization URL generation
- OAuth code exchange
- Credential listing and filtering
- Credential revocation
- Credential testing
"""

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

        with pytest.raises(RuntimeError, match="No database path configured"):
            mixin._get_token_manager()

    def test_oauth_list_providers(self, mock_oauth_mixin):
        """Test listing all available OAuth providers."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        # Create mock OAuth config with providers
        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
                icon_url="https://example.com/google-drive.png",
            ),
            OAuthProviderConfig(
                name="x",
                display_name="X (Twitter)",
                provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                scopes=["tweet.read"],
                client_id_env="NEXUS_OAUTH_X_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_X_CLIENT_SECRET",
                requires_pkce=True,
            ),
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with patch(
            "nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory"
        ) as mock_factory:
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory.return_value = mock_factory_instance

            result = mock_oauth_mixin.oauth_list_providers()

            assert len(result) == 2
            assert result[0]["name"] == "google-drive"
            assert result[0]["display_name"] == "Google Drive"
            assert result[0]["requires_pkce"] is False
            assert result[1]["name"] == "x"
            assert result[1]["display_name"] == "X (Twitter)"
            assert result[1]["requires_pkce"] is True
            assert "scopes" in result[0]
            assert "metadata" in result[0]
            assert result[0]["icon_url"] == "https://example.com/google-drive.png"
            # X provider doesn't have icon_url in this test
            assert "icon_url" not in result[1] or result[1].get("icon_url") is None

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
        with pytest.raises(ValueError, match="not found in configuration"):
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
            tenant_id="default",
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

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_store_credential_fails(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test OAuth code exchange when storing credential fails."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_cred = Mock()
        mock_cred.expires_at = datetime.now() + timedelta(hours=1)

        mock_token_manager.store_credential.side_effect = Exception("Database error")

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
            mock_provider.provider_name = "google-drive"
            mock_provider.exchange_code = AsyncMock(return_value=mock_cred)
            MockProvider.return_value = mock_provider

            with pytest.raises(ValueError, match="Failed to store credential"):
                await mock_oauth_mixin.oauth_exchange_code(
                    provider="google",
                    code="test_code",
                    user_email="test@example.com",
                )

    def test_map_provider_name(self, mock_oauth_mixin):
        """Test provider name mapping."""
        assert mock_oauth_mixin._map_provider_name("google") == "google-drive"
        assert mock_oauth_mixin._map_provider_name("twitter") == "x"
        assert mock_oauth_mixin._map_provider_name("x") == "x"
        assert mock_oauth_mixin._map_provider_name("microsoft") == "microsoft-onedrive"
        assert mock_oauth_mixin._map_provider_name("microsoft-onedrive") == "microsoft-onedrive"
        assert mock_oauth_mixin._map_provider_name("unknown") == "unknown"

    def test_oauth_get_auth_url_success(self, mock_oauth_mixin):
        """Test successful OAuth authorization URL generation."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "google-drive"
        mock_provider.get_authorization_url = Mock(
            return_value="https://accounts.google.com/o/oauth2/v2/auth?state=test"
        )

        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_token_manager") as mock_tm,
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.get_provider_config = Mock(return_value=mock_providers[0])
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            mock_tm_instance = Mock()
            mock_tm_instance.register_provider = Mock()
            mock_tm.return_value = mock_tm_instance

            result = mock_oauth_mixin.oauth_get_auth_url(
                provider="google",
                redirect_uri="http://localhost:3000/callback",
            )

            assert "url" in result
            assert "state" in result
            assert len(result["state"]) > 0
            assert "pkce_data" not in result  # No PKCE for Google

    def test_oauth_get_auth_url_with_pkce(self, mock_oauth_mixin):
        """Test OAuth authorization URL generation with PKCE."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "x"
        mock_provider.get_authorization_url_with_pkce = Mock(
            return_value=(
                "https://twitter.com/i/oauth2/authorize?state=test",
                {"code_verifier": "test_verifier", "code_challenge": "test_challenge"},
            )
        )

        mock_providers = [
            OAuthProviderConfig(
                name="x",
                display_name="X (Twitter)",
                provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                scopes=["tweet.read"],
                client_id_env="NEXUS_OAUTH_X_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_X_CLIENT_SECRET",
                requires_pkce=True,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_token_manager") as mock_tm,
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.get_provider_config = Mock(return_value=mock_providers[0])
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            mock_tm_instance = Mock()
            mock_tm_instance.register_provider = Mock()
            mock_tm.return_value = mock_tm_instance

            result = mock_oauth_mixin.oauth_get_auth_url(provider="x")

            assert "url" in result
            assert "state" in result
            assert "pkce_data" in result
            assert "code_verifier" in result["pkce_data"]

    def test_create_provider_with_redirect_uri_parameter(self, mock_oauth_mixin):
        """Test _create_provider uses redirect_uri parameter when provided."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "google-drive"

        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            mock_oauth_mixin._create_provider(
                provider="google",
                redirect_uri="http://custom.com/callback",
            )

            # Verify factory was called with the provided redirect_uri
            mock_factory_instance.create_provider.assert_called_once()
            call_kwargs = mock_factory_instance.create_provider.call_args[1]
            assert call_kwargs["redirect_uri"] == "http://custom.com/callback"

    def test_create_provider_with_provider_config_redirect_uri(self, mock_oauth_mixin):
        """Test _create_provider uses provider config redirect_uri when parameter is None."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "google-drive"

        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
                redirect_uri="http://provider-config.com/callback",
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            mock_oauth_mixin._create_provider(provider="google", redirect_uri=None)

            # Verify factory was called with None redirect_uri (will use config)
            mock_factory_instance.create_provider.assert_called_once()
            call_kwargs = mock_factory_instance.create_provider.call_args[1]
            assert call_kwargs["redirect_uri"] is None

    def test_create_provider_with_global_config_redirect_uri(self, mock_oauth_mixin):
        """Test _create_provider uses global config redirect_uri when provider doesn't have one."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "google-drive"

        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
                redirect_uri=None,  # Provider doesn't have redirect_uri
            )
        ]
        mock_config = OAuthConfig(
            redirect_uri="http://global-config.com/callback",  # Global redirect_uri
            providers=mock_providers,
        )

        with (
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            mock_oauth_mixin._create_provider(provider="google", redirect_uri=None)

            # Verify factory was called with None redirect_uri (will use global config)
            mock_factory_instance.create_provider.assert_called_once()
            call_kwargs = mock_factory_instance.create_provider.call_args[1]
            assert call_kwargs["redirect_uri"] is None

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_with_pkce(self, mock_oauth_mixin, mock_token_manager):
        """Test OAuth code exchange with PKCE."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_cred = Mock()
        mock_cred.expires_at = datetime.now() + timedelta(hours=1)

        mock_token_manager.store_credential.return_value = "test_cred_id"

        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "x"
        mock_provider.exchange_code_pkce = AsyncMock(return_value=mock_cred)

        mock_providers = [
            OAuthProviderConfig(
                name="x",
                display_name="X (Twitter)",
                provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                scopes=["tweet.read"],
                client_id_env="NEXUS_OAUTH_X_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_X_CLIENT_SECRET",
                requires_pkce=True,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_X_CLIENT_ID": "test_client_id",
                },
            ),
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.get_provider_config = Mock(return_value=mock_providers[0])
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            result = await mock_oauth_mixin.oauth_exchange_code(
                provider="x",
                code="test_code",
                user_email="test@example.com",
                code_verifier="test_verifier",
            )

            assert result["success"] is True
            assert result["credential_id"] == "test_cred_id"
            mock_provider.exchange_code_pkce.assert_called_once_with("test_code", "test_verifier")

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_with_pkce_from_cache(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test OAuth code exchange with PKCE verifier from cache."""
        mock_oauth_mixin._token_manager = mock_token_manager

        mock_cred = Mock()
        mock_cred.expires_at = datetime.now() + timedelta(hours=1)

        mock_token_manager.store_credential.return_value = "test_cred_id"

        from nexus.core.nexus_fs_oauth import _pkce_cache
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        # Set up PKCE cache
        test_state = "test_state_token"
        _pkce_cache[test_state] = {"code_verifier": "cached_verifier"}

        mock_provider = Mock()
        mock_provider.provider_name = "x"
        mock_provider.exchange_code_pkce = AsyncMock(return_value=mock_cred)

        mock_providers = [
            OAuthProviderConfig(
                name="x",
                display_name="X (Twitter)",
                provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                scopes=["tweet.read"],
                client_id_env="NEXUS_OAUTH_X_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_X_CLIENT_SECRET",
                requires_pkce=True,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_X_CLIENT_ID": "test_client_id",
                },
            ),
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.get_provider_config = Mock(return_value=mock_providers[0])
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            result = await mock_oauth_mixin.oauth_exchange_code(
                provider="x",
                code="test_code",
                user_email="test@example.com",
                state=test_state,
            )

            assert result["success"] is True
            # Verify PKCE verifier was retrieved from cache
            mock_provider.exchange_code_pkce.assert_called_once_with("test_code", "cached_verifier")
            # Verify cache was cleaned up
            assert test_state not in _pkce_cache

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_pkce_missing_verifier(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test OAuth code exchange with PKCE fails when verifier is missing."""
        mock_oauth_mixin._token_manager = mock_token_manager

        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_provider = Mock()
        mock_provider.provider_name = "x"

        mock_providers = [
            OAuthProviderConfig(
                name="x",
                display_name="X (Twitter)",
                provider_class="nexus.server.auth.x_oauth.XOAuthProvider",
                scopes=["tweet.read"],
                client_id_env="NEXUS_OAUTH_X_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_X_CLIENT_SECRET",
                requires_pkce=True,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        with (
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_X_CLIENT_ID": "test_client_id",
                },
            ),
            patch("nexus.core.nexus_fs_oauth.NexusFSOAuthMixin._get_oauth_factory") as mock_factory,
        ):
            mock_factory_instance = Mock()
            mock_factory_instance._oauth_config = mock_config
            mock_factory_instance.get_provider_config = Mock(return_value=mock_providers[0])
            mock_factory_instance.create_provider = Mock(return_value=mock_provider)
            mock_factory.return_value = mock_factory_instance

            with pytest.raises(ValueError, match="requires PKCE"):
                await mock_oauth_mixin.oauth_exchange_code(
                    provider="x",
                    code="test_code",
                    user_email="test@example.com",
                    # No code_verifier and no state
                )

    def test_get_oauth_factory_with_config(self, mock_oauth_mixin):
        """Test _get_oauth_factory uses config when available."""
        from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig

        mock_providers = [
            OAuthProviderConfig(
                name="google-drive",
                display_name="Google Drive",
                provider_class="nexus.server.auth.google_oauth.GoogleOAuthProvider",
                scopes=["https://www.googleapis.com/auth/drive"],
                client_id_env="NEXUS_OAUTH_GOOGLE_CLIENT_ID",
                client_secret_env="NEXUS_OAUTH_GOOGLE_CLIENT_SECRET",
                requires_pkce=False,
            )
        ]
        mock_config = OAuthConfig(providers=mock_providers)

        # Create a mock config object
        mock_nexus_config = Mock()
        mock_nexus_config.oauth = mock_config

        mock_oauth_mixin._config = mock_nexus_config

        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory:
            mock_factory_instance = Mock()
            MockFactory.return_value = mock_factory_instance

            factory = mock_oauth_mixin._get_oauth_factory()

            assert factory == mock_factory_instance
            # Verify factory was created with the config
            MockFactory.assert_called_once_with(config=mock_config)

    def test_get_oauth_factory_without_config(self, mock_oauth_mixin):
        """Test _get_oauth_factory uses default config when no config available."""
        with patch("nexus.server.auth.oauth_factory.OAuthProviderFactory") as MockFactory:
            mock_factory_instance = Mock()
            MockFactory.return_value = mock_factory_instance

            factory = mock_oauth_mixin._get_oauth_factory()

            assert factory == mock_factory_instance
            # Verify factory was created with None (will use default)
            MockFactory.assert_called_once_with(config=None)

    def test_get_token_manager_uses_context_utils(self):
        """Test that _get_token_manager uses context_utils.get_database_url."""

        class TestMixin(NexusFSOAuthMixin):
            def __init__(self):
                self.db_path = "/tmp/test.db"
                self._token_manager = None

        mixin = TestMixin()

        with (
            patch("nexus.core.nexus_fs_oauth.get_database_url") as mock_get_db_url,
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
        ):
            mock_get_db_url.return_value = "/tmp/test.db"
            mock_tm = Mock()
            MockTM.return_value = mock_tm

            manager = mixin._get_token_manager()

            # Verify get_database_url was called
            mock_get_db_url.assert_called_once_with(mixin)
            assert manager == mock_tm

    @pytest.mark.asyncio
    async def test_oauth_exchange_code_uses_context_utils_tenant_id(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test that oauth_exchange_code uses context_utils.get_tenant_id."""
        from nexus.core.permissions import OperationContext

        mock_oauth_mixin._token_manager = mock_token_manager

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="acme_corp",
            subject_type="user",
            subject_id="alice",
        )

        with (
            patch("nexus.core.nexus_fs_oauth.get_tenant_id") as mock_get_tenant,
            patch.dict(
                "os.environ",
                {
                    "NEXUS_OAUTH_GOOGLE_CLIENT_ID": "test_client_id",
                    "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET": "test_secret",
                },
            ),
            patch("nexus.server.auth.google_oauth.GoogleOAuthProvider") as MockProvider,
        ):
            mock_get_tenant.return_value = "acme_corp"
            mock_provider = Mock()

            # Create a simple object that can have attributes set
            class CredentialObj:
                def __init__(self):
                    self.access_token = "token"
                    self.refresh_token = "refresh"
                    self.expires_at = None  # Add expires_at attribute

            mock_credential = CredentialObj()
            mock_provider.exchange_code = AsyncMock(return_value=mock_credential)
            MockProvider.return_value = mock_provider

            await mock_oauth_mixin.oauth_exchange_code(
                provider="google",
                code="test_code",
                user_email="test@example.com",
                context=context,
            )

            # Verify get_tenant_id was called with context
            mock_get_tenant.assert_called_with(context)

    @pytest.mark.asyncio
    async def test_oauth_list_credentials_uses_context_utils_tenant_id(
        self, mock_oauth_mixin, mock_token_manager
    ):
        """Test that oauth_list_credentials uses context_utils.get_tenant_id."""
        from nexus.core.permissions import OperationContext

        mock_oauth_mixin._token_manager = mock_token_manager
        mock_token_manager.list_credentials.return_value = []

        context = OperationContext(
            user="alice",
            groups=[],
            tenant_id="acme_corp",
            subject_type="user",
            subject_id="alice",
        )

        with patch("nexus.core.nexus_fs_oauth.get_tenant_id") as mock_get_tenant:
            mock_get_tenant.return_value = "acme_corp"

            await mock_oauth_mixin.oauth_list_credentials(context=context)

            # Verify get_tenant_id was called with context
            mock_get_tenant.assert_called_with(context)

    def test_get_token_manager_priority_order(self):
        """Test that get_database_url priority order is respected."""
        import os

        class TestMixin(NexusFSOAuthMixin):
            def __init__(self):
                self._config = Mock()
                self._config.db_path = "sqlite:///config.db"
                self.db_path = "sqlite:///obj.db"
                self.metadata = Mock()
                self.metadata.database_url = "postgresql://localhost/metadata"
                self._token_manager = None

        mixin = TestMixin()

        # Test env var priority
        with (
            patch.dict(os.environ, {"TOKEN_MANAGER_DB": "postgresql://localhost/env"}),
            patch("nexus.server.auth.token_manager.TokenManager") as MockTM,
        ):
            mock_tm = Mock()
            MockTM.return_value = mock_tm

            mixin._get_token_manager()

            # Should use env var
            MockTM.assert_called_once_with(db_url="postgresql://localhost/env")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
