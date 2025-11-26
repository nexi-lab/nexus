"""Tests for Microsoft OAuth provider."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from nexus.server.auth.microsoft_oauth import MicrosoftOAuthProvider
from nexus.server.auth.oauth_provider import OAuthCredential, OAuthError


class TestMicrosoftOAuthProvider:
    """Tests for MicrosoftOAuthProvider."""

    def test_init_with_default_tenant(self):
        """Test provider initialization with default tenant."""
        provider = MicrosoftOAuthProvider(
            client_id="test-client-id",
            client_secret="test-secret",
            redirect_uri="http://localhost:8080/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        assert provider.client_id == "test-client-id"
        assert provider.client_secret == "test-secret"
        assert provider.tenant_id == "common"
        assert "common" in provider.authorization_endpoint
        assert "common" in provider.token_endpoint

    def test_init_uses_common_tenant(self):
        """Test provider initialization always uses common tenant."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        assert provider.tenant_id == "common"
        assert "common" in provider.authorization_endpoint
        assert "common" in provider.token_endpoint

    def test_get_authorization_url_adds_offline_access(self):
        """Test that offline_access scope is automatically added."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        url = provider.get_authorization_url()

        assert "offline_access" in url
        assert "Files.ReadWrite.All" in url

    def test_get_authorization_url_with_state(self):
        """Test authorization URL with state parameter."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        url = provider.get_authorization_url(state="csrf-token")

        assert "state=csrf-token" in url
        assert "login.microsoftonline.com" in url
        assert "response_type=code" in url

    def test_get_authorization_url_without_duplicate_offline_access(self):
        """Test that offline_access is not duplicated if already in scopes."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All", "offline_access"],
            provider_name="microsoft-onedrive",
        )

        url = provider.get_authorization_url()

        # Count occurrences of offline_access in URL
        count = url.count("offline_access")
        assert count == 1

    @pytest.mark.asyncio
    async def test_exchange_code_success(self):
        """Test successful code exchange."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "EwAoA8l6BAAU...",
            "refresh_token": "M.R3_BAY.-CfvKc...",
            "expires_in": 3600,
            "scope": "Files.ReadWrite.All offline_access",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            credential = await provider.exchange_code("test-code")

            assert credential.access_token == "EwAoA8l6BAAU..."
            assert credential.refresh_token == "M.R3_BAY.-CfvKc..."
            assert credential.token_type == "Bearer"
            assert credential.provider == "microsoft-onedrive"
            assert "Files.ReadWrite.All" in credential.scopes
            assert credential.expires_at is not None

    @pytest.mark.asyncio
    async def test_exchange_code_http_error(self):
        """Test code exchange with HTTP error."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        mock_response = Mock()
        mock_response.text = "Invalid authorization code"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400 Bad Request", request=Mock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            with pytest.raises(OAuthError, match="Failed to exchange code"):
                await provider.exchange_code("invalid-code")

    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        """Test successful token refresh."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        old_credential = OAuthCredential(
            access_token="old-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            provider="microsoft",
            user_email="test@example.com",
            scopes=["Files.ReadWrite.All"],
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            new_credential = await provider.refresh_token(old_credential)

            assert new_credential.access_token == "new-token"
            assert new_credential.refresh_token == "new-refresh-token"
            assert new_credential.user_email == "test@example.com"
            assert new_credential.provider == "microsoft-onedrive"

    @pytest.mark.asyncio
    async def test_refresh_token_no_refresh_token(self):
        """Test refresh fails when no refresh_token available."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        credential = OAuthCredential(
            access_token="token",
            refresh_token=None,
            token_type="Bearer",
        )

        with pytest.raises(OAuthError, match="No refresh_token available"):
            await provider.refresh_token(credential)

    @pytest.mark.asyncio
    async def test_refresh_token_preserves_old_refresh_token(self):
        """Test that old refresh_token is preserved if new one not returned."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        old_credential = OAuthCredential(
            access_token="old-token",
            refresh_token="old-refresh",
            token_type="Bearer",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
            # Note: no refresh_token in response
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            new_credential = await provider.refresh_token(old_credential)

            assert new_credential.refresh_token == "old-refresh"

    @pytest.mark.asyncio
    async def test_revoke_token_success(self):
        """Test successful token revocation."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        credential = OAuthCredential(
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
        )

        # Microsoft revocation doesn't have a standard endpoint,
        # so this is a placeholder test
        result = await provider.revoke_token(credential)

        # Current implementation always returns True for Microsoft
        assert result is True

    @pytest.mark.asyncio
    async def test_validate_token_success(self):
        """Test successful token validation via Microsoft Graph."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "id": "user-id",
            "userPrincipalName": "test@example.com",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )

            result = await provider.validate_token("valid-token")

            assert result is True

    @pytest.mark.asyncio
    async def test_validate_token_invalid(self):
        """Test token validation with invalid token."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.HTTPStatusError("401", request=Mock(), response=Mock())
            )

            result = await provider.validate_token("invalid-token")

            assert result is False

    def test_parse_token_response_full(self):
        """Test parsing complete token response."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        token_data = {
            "access_token": "EwAoA8l6BAAU...",
            "refresh_token": "M.R3_BAY.-CfvKc...",
            "expires_in": 3600,
            "scope": "Files.ReadWrite.All offline_access",
            "token_type": "Bearer",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.access_token == "EwAoA8l6BAAU..."
        assert credential.refresh_token == "M.R3_BAY.-CfvKc..."
        assert credential.token_type == "Bearer"
        assert credential.provider == "microsoft-onedrive"
        assert "Files.ReadWrite.All" in credential.scopes
        assert "offline_access" in credential.scopes

    def test_parse_token_response_minimal(self):
        """Test parsing minimal token response."""
        provider = MicrosoftOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All"],
            provider_name="microsoft-onedrive",
        )

        token_data = {
            "access_token": "EwAoA8l6BAAU...",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.access_token == "EwAoA8l6BAAU..."
        assert credential.refresh_token is None
        assert credential.expires_at is None
        assert credential.scopes is None
