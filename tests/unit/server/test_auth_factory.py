"""Tests for authentication factory."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from nexus.server.auth.base import AuthResult
from nexus.server.auth.factory import (
    DiscriminatingAuthProvider,
    create_auth_provider,
)


# Create concrete implementation for testing
class TestableDiscriminatingAuthProvider(DiscriminatingAuthProvider):
    """Testable version of DiscriminatingAuthProvider with close() implemented."""

    def close(self) -> None:
        """Implement abstract close method."""
        pass


class TestDiscriminatingAuthProvider:
    """Tests for DiscriminatingAuthProvider."""

    @pytest.mark.asyncio
    async def test_authenticate_with_api_key(self):
        """Test authentication with API key (sk- prefix)."""
        # Mock API key provider
        api_key_provider = Mock()
        api_key_provider.authenticate = AsyncMock(
            return_value=AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="alice",
                is_admin=True,
            )
        )

        provider = TestableDiscriminatingAuthProvider(api_key_provider=api_key_provider)

        result = await provider.authenticate("sk-test-key-123")

        assert result.authenticated is True
        assert result.subject_id == "alice"
        api_key_provider.authenticate.assert_called_once_with("sk-test-key-123")

    @pytest.mark.asyncio
    async def test_authenticate_with_jwt(self):
        """Test authentication with JWT token."""
        # Mock JWT provider
        jwt_provider = Mock()
        jwt_provider.authenticate = AsyncMock(
            return_value=AuthResult(
                authenticated=True,
                subject_type="user",
                subject_id="bob",
            )
        )

        provider = TestableDiscriminatingAuthProvider(jwt_provider=jwt_provider)

        # Mock JWT token (3 parts separated by dots)
        jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.signature"

        with patch.object(provider, "_looks_like_jwt", return_value=True):
            result = await provider.authenticate(jwt_token)

            assert result.authenticated is True
            assert result.subject_id == "bob"
            jwt_provider.authenticate.assert_called_once_with(jwt_token)

    @pytest.mark.asyncio
    async def test_authenticate_empty_token(self):
        """Test authentication with empty token."""
        provider = TestableDiscriminatingAuthProvider()

        result = await provider.authenticate("")

        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_authenticate_api_key_no_provider(self):
        """Test API key authentication when no API key provider configured."""
        provider = TestableDiscriminatingAuthProvider(jwt_provider=Mock())

        result = await provider.authenticate("sk-test-key")

        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_authenticate_jwt_no_provider(self):
        """Test JWT authentication when no JWT provider configured."""
        provider = TestableDiscriminatingAuthProvider(api_key_provider=Mock())

        jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib2IifQ.signature"

        with patch.object(provider, "_looks_like_jwt", return_value=True):
            result = await provider.authenticate(jwt_token)

            assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token_format(self):
        """Test authentication with invalid token format."""
        jwt_provider = Mock()
        provider = TestableDiscriminatingAuthProvider(jwt_provider=jwt_provider)

        # Not an API key, not a valid JWT
        result = await provider.authenticate("invalid-token-format")

        assert result.authenticated is False

    def test_looks_like_jwt_invalid_parts(self):
        """Test JWT format detection with wrong number of parts."""
        provider = TestableDiscriminatingAuthProvider()

        assert provider._looks_like_jwt("only-two.parts") is False
        assert provider._looks_like_jwt("one") is False
        assert provider._looks_like_jwt("too.many.parts.here") is False

    @pytest.mark.asyncio
    async def test_validate_token_success(self):
        """Test token validation (wrapper for authenticate)."""
        api_key_provider = Mock()
        api_key_provider.authenticate = AsyncMock(
            return_value=AuthResult(authenticated=True, subject_type="user", subject_id="alice")
        )

        provider = TestableDiscriminatingAuthProvider(api_key_provider=api_key_provider)

        result = await provider.validate_token("sk-test-key")

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_token_failure(self):
        """Test token validation with invalid token."""
        provider = TestableDiscriminatingAuthProvider()

        result = await provider.validate_token("sk-invalid-key")

        assert result is False


class TestCreateAuthProvider:
    """Tests for create_auth_provider factory function."""

    def test_create_none_auth(self):
        """Test creating no authentication provider."""
        provider = create_auth_provider(None)

        assert provider is None

    def test_create_static_auth(self):
        """Test creating static API key auth provider."""
        auth_config = {
            "api_keys": {
                "sk-alice-xxx": {"user_id": "alice", "is_admin": True},
            }
        }

        provider = create_auth_provider("static", auth_config)

        assert provider is not None
        assert hasattr(provider, "authenticate")

    def test_create_static_auth_no_config(self):
        """Test creating static auth without config raises error."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("static")

    def test_create_database_auth(self):
        """Test creating database API key auth provider."""
        session_factory = Mock()

        provider = create_auth_provider("database", session_factory=session_factory)

        assert provider is not None

    def test_create_database_auth_no_session_factory(self):
        """Test creating database auth without session factory raises error."""
        with pytest.raises(ValueError, match="session_factory is required"):
            create_auth_provider("database")

    def test_create_local_auth(self):
        """Test creating local auth provider."""
        auth_config = {
            "jwt_secret": "test-secret",
            "users": {
                "alice@example.com": {
                    "password_hash": "$2b$12$test",
                    "subject_id": "alice",
                }
            },
        }

        provider = create_auth_provider("local", auth_config)

        assert provider is not None

    def test_create_local_auth_no_config(self):
        """Test creating local auth without config raises error."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("local")

    def test_create_oidc_auth(self):
        """Test creating OIDC auth provider."""
        auth_config = {
            "issuer": "https://accounts.google.com",
            "audience": "test-client-id",
        }

        provider = create_auth_provider("oidc", auth_config)

        assert provider is not None

    def test_create_oidc_auth_no_config(self):
        """Test creating OIDC auth without config raises error."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("oidc")

    def test_create_multi_oidc_auth(self):
        """Test creating multi-OIDC auth provider."""
        auth_config = {
            "providers": {
                "google": {
                    "issuer": "https://accounts.google.com",
                    "audience": "google-client-id",
                },
                "github": {
                    "issuer": "https://token.actions.githubusercontent.com",
                    "audience": "github-client-id",
                },
            }
        }

        provider = create_auth_provider("multi-oidc", auth_config)

        assert provider is not None

    def test_create_multi_oidc_auth_no_config(self):
        """Test creating multi-OIDC auth without config raises error."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("multi-oidc")

    def test_create_unknown_auth_type(self):
        """Test creating auth with unknown type raises error."""
        with pytest.raises(ValueError, match="Unknown auth_type"):
            create_auth_provider("unknown-type")

    def test_create_static_auth_integration(self):
        """Test static auth provider can actually authenticate."""
        auth_config = {
            "api_keys": {
                "sk-alice-key": {"user_id": "alice", "is_admin": True},
            }
        }

        provider = create_auth_provider("static", auth_config)

        # Test authentication (sync version for simplicity)
        import asyncio

        result = asyncio.run(provider.authenticate("sk-alice-key"))

        assert result.authenticated is True
        assert result.subject_id == "alice"
        assert result.is_admin is True
