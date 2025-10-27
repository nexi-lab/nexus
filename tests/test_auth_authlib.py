"""Tests for Authlib integration (LocalAuth and OIDCAuth)."""

from datetime import timedelta

import bcrypt
import pytest
from freezegun import freeze_time

from nexus.server.auth import LocalAuth, MultiOIDCAuth, OIDCAuth, create_auth_provider


class TestLocalAuth:
    """Test LocalAuth provider with JWT tokens."""

    def test_create_user(self):
        """Test creating a new user."""
        auth = LocalAuth()

        user_info = auth.create_user(
            email="alice@example.com",
            password="secure-password",
            subject_type="user",
            subject_id="alice",
            tenant_id="org_acme",
            is_admin=True,
            name="Alice",
        )

        assert user_info["subject_type"] == "user"
        assert user_info["subject_id"] == "alice"
        assert user_info["tenant_id"] == "org_acme"
        assert user_info["is_admin"] is True
        assert user_info["name"] == "Alice"
        assert "password_hash" not in user_info  # Should not be returned

        # Verify user is stored
        assert "alice@example.com" in auth.users

    def test_create_user_defaults(self):
        """Test creating user with defaults."""
        auth = LocalAuth()

        user_info = auth.create_user(email="bob@example.com", password="password123")

        assert user_info["subject_type"] == "user"
        assert user_info["subject_id"] == "bob"  # Defaults to email prefix
        assert user_info["name"] == "bob"  # Defaults to email prefix
        assert user_info["is_admin"] is False

    def test_create_duplicate_user(self):
        """Test that creating duplicate user raises error."""
        auth = LocalAuth()
        auth.create_user(email="alice@example.com", password="password123")

        with pytest.raises(ValueError, match="already exists"):
            auth.create_user(email="alice@example.com", password="another-password")

    def test_verify_password_success(self):
        """Test successful password verification."""
        auth = LocalAuth()
        auth.create_user(email="alice@example.com", password="correct-password")

        user_info = auth.verify_password("alice@example.com", "correct-password")

        assert user_info is not None
        assert user_info["subject_id"] == "alice"

    def test_verify_password_wrong_password(self):
        """Test password verification with wrong password."""
        auth = LocalAuth()
        auth.create_user(email="alice@example.com", password="correct-password")

        user_info = auth.verify_password("alice@example.com", "wrong-password")

        assert user_info is None

    def test_verify_password_wrong_email(self):
        """Test password verification with wrong email."""
        auth = LocalAuth()
        auth.create_user(email="alice@example.com", password="password123")

        user_info = auth.verify_password("bob@example.com", "password123")

        assert user_info is None

    def test_create_token(self):
        """Test JWT token creation."""
        auth = LocalAuth(jwt_secret="test-secret")
        user_info = {
            "subject_type": "user",
            "subject_id": "alice",
            "tenant_id": "org_acme",
            "is_admin": True,
            "name": "Alice",
        }

        token = auth.create_token("alice@example.com", user_info)

        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0

    def test_verify_token_success(self):
        """Test successful token verification."""
        auth = LocalAuth(jwt_secret="test-secret")
        user_info = {
            "subject_type": "user",
            "subject_id": "alice",
            "tenant_id": "org_acme",
            "is_admin": True,
            "name": "Alice",
        }

        token = auth.create_token("alice@example.com", user_info)
        claims = auth.verify_token(token)

        assert claims["sub"] == "alice"
        assert claims["subject_type"] == "user"
        assert claims["subject_id"] == "alice"
        assert claims["tenant_id"] == "org_acme"
        assert claims["is_admin"] is True
        assert claims["email"] == "alice@example.com"

    def test_verify_token_expired(self):
        """Test token verification with expired token."""
        with freeze_time("2025-01-01 12:00:00") as frozen_time:
            auth = LocalAuth(jwt_secret="test-secret", token_expiry=1)  # 1 second expiry
            user_info = {
                "subject_type": "user",
                "subject_id": "alice",
                "tenant_id": None,
                "is_admin": False,
                "name": "Alice",
            }

            token = auth.create_token("alice@example.com", user_info)

            # Advance time to expire the token
            frozen_time.tick(delta=timedelta(seconds=2))

            with pytest.raises(ValueError, match="Invalid token"):
                auth.verify_token(token)

    def test_verify_token_wrong_secret(self):
        """Test token verification with wrong secret."""
        auth1 = LocalAuth(jwt_secret="secret1")
        auth2 = LocalAuth(jwt_secret="secret2")

        user_info = {
            "subject_type": "user",
            "subject_id": "alice",
            "tenant_id": None,
            "is_admin": False,
            "name": "Alice",
        }

        token = auth1.create_token("alice@example.com", user_info)

        with pytest.raises(ValueError, match="Invalid token"):
            auth2.verify_token(token)

    def test_verify_password_and_create_token(self):
        """Test combined password verification and token creation."""
        auth = LocalAuth(jwt_secret="test-secret")
        auth.create_user(email="alice@example.com", password="password123")

        token = auth.verify_password_and_create_token("alice@example.com", "password123")

        assert token is not None
        assert isinstance(token, str)

        # Verify token is valid
        claims = auth.verify_token(token)
        assert claims["subject_id"] == "alice"

    def test_verify_password_and_create_token_wrong_password(self):
        """Test combined verification with wrong password."""
        auth = LocalAuth()
        auth.create_user(email="alice@example.com", password="password123")

        token = auth.verify_password_and_create_token("alice@example.com", "wrong-password")

        assert token is None

    @pytest.mark.asyncio
    async def test_authenticate_success(self):
        """Test successful authentication with JWT token."""
        auth = LocalAuth(jwt_secret="test-secret")
        auth.create_user(
            email="alice@example.com",
            password="password123",
            subject_type="user",
            subject_id="alice",
            tenant_id="org_acme",
            is_admin=True,
        )

        token = auth.verify_password_and_create_token("alice@example.com", "password123")
        result = await auth.authenticate(token)

        assert result.authenticated is True
        assert result.subject_type == "user"
        assert result.subject_id == "alice"
        assert result.tenant_id == "org_acme"
        assert result.is_admin is True
        assert result.metadata["email"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self):
        """Test authentication with invalid token."""
        auth = LocalAuth(jwt_secret="test-secret")

        result = await auth.authenticate("invalid-token")

        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_validate_token_success(self):
        """Test token validation."""
        auth = LocalAuth(jwt_secret="test-secret")
        auth.create_user(email="alice@example.com", password="password123")

        token = auth.verify_password_and_create_token("alice@example.com", "password123")
        is_valid = await auth.validate_token(token)

        assert is_valid is True

    @pytest.mark.asyncio
    async def test_validate_token_invalid(self):
        """Test validation with invalid token."""
        auth = LocalAuth(jwt_secret="test-secret")

        is_valid = await auth.validate_token("invalid-token")

        assert is_valid is False

    def test_from_config(self):
        """Test creating LocalAuth from config."""
        password_bytes = b"password123"
        password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")

        config = {
            "jwt_secret": "test-secret",
            "token_expiry": 7200,
            "users": {
                "alice@example.com": {
                    "password_hash": password_hash,
                    "subject_type": "user",
                    "subject_id": "alice",
                    "tenant_id": "org_acme",
                    "is_admin": True,
                }
            },
        }

        auth = LocalAuth.from_config(config)

        assert auth.jwt_secret == "test-secret"
        assert auth.token_expiry == 7200
        assert "alice@example.com" in auth.users

    def test_auto_generated_secret(self):
        """Test that JWT secret is auto-generated if not provided."""
        auth = LocalAuth()

        assert auth.jwt_secret is not None
        assert len(auth.jwt_secret) > 0


class TestOIDCAuth:
    """Test OIDCAuth provider for OAuth/OIDC."""

    def test_extract_provider_prefix_google(self):
        """Test extracting provider prefix for Google."""
        auth = OIDCAuth(issuer="https://accounts.google.com", audience="test-client-id")

        prefix = auth._extract_provider_prefix("https://accounts.google.com")

        assert prefix == "google"

    def test_extract_provider_prefix_github(self):
        """Test extracting provider prefix for GitHub."""
        auth = OIDCAuth(issuer="https://github.com", audience="test-client-id")

        prefix = auth._extract_provider_prefix("https://github.com")

        assert prefix == "github"

    def test_extract_provider_prefix_microsoft(self):
        """Test extracting provider prefix for Microsoft."""
        auth = OIDCAuth(issuer="https://login.microsoftonline.com", audience="test-client-id")

        prefix = auth._extract_provider_prefix("https://login.microsoftonline.com")

        assert prefix == "microsoft"

    def test_extract_provider_prefix_custom(self):
        """Test extracting provider prefix for custom domain."""
        auth = OIDCAuth(issuer="https://auth.example.com", audience="test-client-id")

        prefix = auth._extract_provider_prefix("https://auth.example.com")

        assert prefix == "auth.example.com"

    def test_from_config(self):
        """Test creating OIDCAuth from config."""
        config = {
            "issuer": "https://accounts.google.com",
            "audience": "test-client-id",
            "subject_type": "user",
            "admin_emails": ["admin@example.com"],
        }

        auth = OIDCAuth.from_config(config)

        assert auth.issuer == "https://accounts.google.com"
        assert auth.audience == "test-client-id"
        assert auth.subject_type == "user"
        assert "admin@example.com" in auth.admin_emails


class TestMultiOIDCAuth:
    """Test MultiOIDCAuth for multiple providers."""

    def test_from_config(self):
        """Test creating MultiOIDCAuth from config."""
        config = {
            "providers": {
                "google": {
                    "issuer": "https://accounts.google.com",
                    "audience": "google-client-id",
                },
                "github": {
                    "issuer": "https://github.com",
                    "audience": "github-client-id",
                },
            }
        }

        auth = MultiOIDCAuth.from_config(config)

        assert "google" in auth.providers
        assert "github" in auth.providers
        assert isinstance(auth.providers["google"], OIDCAuth)
        assert isinstance(auth.providers["github"], OIDCAuth)

    @pytest.mark.asyncio
    async def test_validate_token_multiple_providers(self):
        """Test that validation tries all providers."""
        # This would require mocking JWKS validation
        # For now, just test that it returns False for invalid token
        auth = MultiOIDCAuth(
            providers={
                "google": OIDCAuth(
                    issuer="https://accounts.google.com",
                    audience="google-client-id",
                ),
                "github": OIDCAuth(issuer="https://github.com", audience="github-client-id"),
            }
        )

        is_valid = await auth.validate_token("invalid-token")

        assert is_valid is False


class TestAuthFactory:
    """Test create_auth_provider factory function."""

    def test_create_local_auth(self):
        """Test creating LocalAuth via factory."""
        config = {"jwt_secret": "test-secret"}

        provider = create_auth_provider("local", config)

        assert isinstance(provider, LocalAuth)
        assert provider.jwt_secret == "test-secret"

    def test_create_oidc_auth(self):
        """Test creating OIDCAuth via factory."""
        config = {
            "issuer": "https://accounts.google.com",
            "audience": "test-client-id",
        }

        provider = create_auth_provider("oidc", config)

        assert isinstance(provider, OIDCAuth)
        assert provider.issuer == "https://accounts.google.com"

    def test_create_multi_oidc_auth(self):
        """Test creating MultiOIDCAuth via factory."""
        config = {
            "providers": {
                "google": {
                    "issuer": "https://accounts.google.com",
                    "audience": "google-client-id",
                }
            }
        }

        provider = create_auth_provider("multi-oidc", config)

        assert isinstance(provider, MultiOIDCAuth)
        assert "google" in provider.providers

    def test_create_none_auth(self):
        """Test creating no authentication."""
        provider = create_auth_provider(None)

        assert provider is None

    def test_create_unknown_auth_type(self):
        """Test that unknown auth type raises error."""
        with pytest.raises(ValueError, match="Unknown auth_type"):
            create_auth_provider("unknown", {})

    def test_create_local_without_config(self):
        """Test that local auth requires config."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("local", None)

    def test_create_oidc_without_config(self):
        """Test that OIDC auth requires config."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("oidc", None)
