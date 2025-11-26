"""Tests for Google OAuth provider."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from nexus.server.auth.google_oauth import GoogleOAuthProvider
from nexus.server.auth.oauth_provider import OAuthCredential, OAuthError


class TestGoogleOAuthProvider:
    """Tests for GoogleOAuthProvider."""

    def test_init(self):
        """Test provider initialization."""
        provider = GoogleOAuthProvider(
            client_id="test-client-id",
            client_secret="test-secret",
            redirect_uri="http://localhost:8080/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        assert provider.client_id == "test-client-id"
        assert provider.client_secret == "test-secret"
        assert provider.redirect_uri == "http://localhost:8080/callback"
        assert provider.scopes == ["https://www.googleapis.com/auth/drive"]
        assert provider.provider_name == "google-drive"

    def test_get_authorization_url_without_state(self):
        """Test authorization URL generation without state."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        url = provider.get_authorization_url()

        assert "https://accounts.google.com/o/oauth2/v2/auth" in url
        assert "client_id=test-id" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%2Fcallback" in url
        assert "response_type=code" in url
        assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "state=" not in url

    def test_get_authorization_url_with_state(self):
        """Test authorization URL generation with state."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        url = provider.get_authorization_url(state="csrf-token-123")

        assert "state=csrf-token-123" in url

    def test_get_authorization_url_multiple_scopes(self):
        """Test authorization URL with multiple scopes."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=[
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/calendar",
            ],
            provider_name="google-drive",
        )

        url = provider.get_authorization_url()

        assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive" in url
        assert "calendar" in url

    @pytest.mark.asyncio
    async def test_exchange_code_success(self):
        """Test successful code exchange."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "ya29.test-token",
            "refresh_token": "1//test-refresh",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/drive",
            "token_type": "Bearer",
        }
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            credential = await provider.exchange_code("test-code")

            assert credential.access_token == "ya29.test-token"
            assert credential.refresh_token == "1//test-refresh"
            assert credential.token_type == "Bearer"
            assert credential.provider == "google-drive"
            assert credential.scopes == ["https://www.googleapis.com/auth/drive"]
            assert credential.expires_at is not None

    @pytest.mark.asyncio
    async def test_exchange_code_http_error(self):
        """Test code exchange with HTTP error."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        mock_response = Mock()
        mock_response.text = "Invalid code"
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
    async def test_exchange_code_generic_error(self):
        """Test code exchange with generic error."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=Exception("Network error")
            )

            with pytest.raises(OAuthError, match="Failed to exchange code"):
                await provider.exchange_code("test-code")

    @pytest.mark.asyncio
    async def test_refresh_token_success(self):
        """Test successful token refresh."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        old_credential = OAuthCredential(
            access_token="old-token",
            refresh_token="refresh-token",
            token_type="Bearer",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
            provider="google",
            user_email="test@example.com",
            scopes=["https://www.googleapis.com/auth/drive"],
        )

        mock_response = Mock()
        mock_response.json.return_value = {
            "access_token": "new-token",
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
            assert new_credential.refresh_token == "refresh-token"  # Preserved
            assert new_credential.user_email == "test@example.com"  # Preserved
            assert new_credential.provider == "google-drive"
            assert new_credential.scopes == ["https://www.googleapis.com/auth/drive"]

    @pytest.mark.asyncio
    async def test_refresh_token_no_refresh_token(self):
        """Test refresh fails when no refresh_token available."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        credential = OAuthCredential(
            access_token="token",
            refresh_token=None,
            token_type="Bearer",
        )

        with pytest.raises(OAuthError, match="No refresh_token available"):
            await provider.refresh_token(credential)

    @pytest.mark.asyncio
    async def test_refresh_token_http_error(self):
        """Test token refresh with HTTP error."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        credential = OAuthCredential(
            access_token="token",
            refresh_token="refresh-token",
            token_type="Bearer",
        )

        mock_response = Mock()
        mock_response.text = "Invalid refresh token"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400 Bad Request", request=Mock(), response=mock_response
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            with pytest.raises(OAuthError, match="Failed to refresh token"):
                await provider.refresh_token(credential)

    @pytest.mark.asyncio
    async def test_revoke_token_success(self):
        """Test successful token revocation."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        credential = OAuthCredential(
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
        )

        mock_response = Mock()
        mock_response.raise_for_status = Mock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=mock_response
            )

            result = await provider.revoke_token(credential)

            assert result is True

    @pytest.mark.asyncio
    async def test_revoke_token_http_error(self):
        """Test token revocation with HTTP error."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        credential = OAuthCredential(
            access_token="access-token",
            refresh_token="refresh-token",
            token_type="Bearer",
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(
                side_effect=httpx.HTTPStatusError("400", request=Mock(), response=Mock())
            )

            result = await provider.revoke_token(credential)

            assert result is False

    @pytest.mark.asyncio
    async def test_revoke_token_no_token(self):
        """Test token revocation with no token."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        credential = OAuthCredential(
            access_token=None,
            refresh_token=None,
            token_type="Bearer",
        )

        result = await provider.revoke_token(credential)

        assert result is False

    @pytest.mark.asyncio
    async def test_validate_token_success(self):
        """Test successful token validation."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        mock_response = Mock()
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
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.HTTPStatusError("400", request=Mock(), response=Mock())
            )

            result = await provider.validate_token("invalid-token")

            assert result is False

    @pytest.mark.asyncio
    async def test_validate_token_network_error(self):
        """Test token validation with network error."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=Exception("Network error")
            )

            result = await provider.validate_token("token")

            assert result is False

    def test_parse_token_response_full(self):
        """Test parsing complete token response."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        token_data = {
            "access_token": "ya29.test",
            "refresh_token": "1//test",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/calendar",
            "token_type": "Bearer",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.access_token == "ya29.test"
        assert credential.refresh_token == "1//test"
        assert credential.token_type == "Bearer"
        assert credential.provider == "google-drive"
        assert credential.scopes == [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/calendar",
        ]
        assert credential.expires_at is not None

    def test_parse_token_response_minimal(self):
        """Test parsing minimal token response."""
        provider = GoogleOAuthProvider(
            client_id="test-id",
            client_secret="secret",
            redirect_uri="http://localhost/callback",
            scopes=["https://www.googleapis.com/auth/drive"],
            provider_name="google-drive",
        )

        token_data = {
            "access_token": "ya29.test",
        }

        credential = provider._parse_token_response(token_data)

        assert credential.access_token == "ya29.test"
        assert credential.refresh_token is None
        assert credential.token_type == "Bearer"
        assert credential.expires_at is None
        assert credential.scopes is None
