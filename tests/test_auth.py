"""Tests for authentication system."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.auth import (
    DatabaseAPIKeyAuth,
    StaticAPIKeyAuth,
    create_auth_provider,
)
from nexus.storage.models import APIKeyModel, Base


class TestStaticAPIKeyAuth:
    """Test StaticAPIKeyAuth provider."""

    @pytest.mark.asyncio
    async def test_authenticate_valid_key(self):
        """Test authentication with valid API key."""
        config = {
            "api_keys": {
                "sk-alice-test123": {
                    "user_id": "alice",
                    "tenant_id": None,
                    "is_admin": True,
                },
                "sk-bob-test456": {
                    "user_id": "bob",
                    "tenant_id": "acme",
                    "is_admin": False,
                },
            }
        }

        auth = StaticAPIKeyAuth.from_config(config)
        result = await auth.authenticate("sk-alice-test123")

        assert result.authenticated is True
        assert result.user_id == "alice"
        assert result.tenant_id is None
        assert result.is_admin is True

    @pytest.mark.asyncio
    async def test_authenticate_invalid_key(self):
        """Test authentication with invalid API key."""
        config = {
            "api_keys": {
                "sk-alice-test123": {
                    "user_id": "alice",
                    "is_admin": True,
                }
            }
        }

        auth = StaticAPIKeyAuth.from_config(config)
        result = await auth.authenticate("sk-invalid-key")

        assert result.authenticated is False
        assert result.user_id is None

    @pytest.mark.asyncio
    async def test_validate_token(self):
        """Test token validation."""
        config = {"api_keys": {"sk-alice-test123": {"user_id": "alice", "is_admin": True}}}

        auth = StaticAPIKeyAuth.from_config(config)

        assert await auth.validate_token("sk-alice-test123") is True
        assert await auth.validate_token("sk-invalid-key") is False


class TestDatabaseAPIKeyAuth:
    """Test DatabaseAPIKeyAuth provider."""

    @pytest.fixture
    def db_session(self):
        """Create test database session."""
        # Use in-memory SQLite for tests
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine)
        return SessionFactory

    @pytest.mark.asyncio
    async def test_authenticate_valid_key(self, db_session):
        """Test authentication with valid database key."""
        auth = DatabaseAPIKeyAuth(db_session)

        # Create a test key
        with db_session() as session:
            test_key_id, test_raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Test Key",
                is_admin=True,
            )
            session.commit()

        # Authenticate with the key
        result = await auth.authenticate(test_raw_key)

        assert result.authenticated is True
        assert result.user_id == "alice"
        assert result.is_admin is True
        assert result.metadata["key_id"] == test_key_id
        assert result.metadata["key_name"] == "Test Key"

    @pytest.mark.asyncio
    async def test_authenticate_invalid_key(self, db_session):
        """Test authentication with invalid key."""
        auth = DatabaseAPIKeyAuth(db_session)

        result = await auth.authenticate("sk-invalid-key")

        assert result.authenticated is False
        assert result.user_id is None

    @pytest.mark.asyncio
    async def test_authenticate_expired_key(self, db_session):
        """Test authentication with expired key."""
        auth = DatabaseAPIKeyAuth(db_session)

        # Create an expired key
        with db_session() as session:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Expired Key",
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
            session.commit()

        # Try to authenticate
        result = await auth.authenticate(raw_key)

        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_authenticate_revoked_key(self, db_session):
        """Test authentication with revoked key."""
        auth = DatabaseAPIKeyAuth(db_session)

        # Create and revoke a key
        with db_session() as session:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Revoked Key",
            )
            session.commit()

        # Revoke the key
        with db_session() as session:
            DatabaseAPIKeyAuth.revoke_key(session, key_id)
            session.commit()

        # Try to authenticate
        result = await auth.authenticate(raw_key)

        assert result.authenticated is False

    @pytest.mark.asyncio
    async def test_last_used_at_update(self, db_session):
        """Test that last_used_at is updated on authentication."""
        auth = DatabaseAPIKeyAuth(db_session)

        # Create a key
        with db_session() as session:
            test_key_id, test_raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Test Key",
            )
            session.commit()

        # Get initial last_used_at
        initial_last_used = None
        with db_session() as session:
            from sqlalchemy import select

            stmt = select(APIKeyModel).where(APIKeyModel.key_id == test_key_id)
            api_key = session.scalar(stmt)
            if api_key:
                initial_last_used = api_key.last_used_at

        # Authenticate
        await auth.authenticate(test_raw_key)

        # Check last_used_at was updated
        with db_session() as session:
            stmt = select(APIKeyModel).where(APIKeyModel.key_id == test_key_id)
            api_key = session.scalar(stmt)
            assert api_key is not None
            assert api_key.last_used_at is not None
            if initial_last_used:
                assert api_key.last_used_at > initial_last_used

    def test_create_key(self, db_session):
        """Test creating API keys."""
        with db_session() as session:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Alice's Key",
                tenant_id="acme",
                is_admin=True,
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
            session.commit()

            # Verify key was created
            from sqlalchemy import select

            stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
            api_key = session.scalar(stmt)

            assert api_key is not None
            assert api_key.user_id == "alice"
            assert api_key.name == "Alice's Key"
            assert api_key.tenant_id == "acme"
            assert bool(api_key.is_admin) is True  # SQLite stores as Integer
            assert api_key.expires_at is not None
            assert bool(api_key.revoked) is False  # SQLite stores as Integer

            # Verify key hash matches
            expected_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            assert api_key.key_hash == expected_hash

    def test_revoke_key(self, db_session):
        """Test revoking API keys."""
        with db_session() as session:
            key_id, raw_key = DatabaseAPIKeyAuth.create_key(
                session,
                user_id="alice",
                name="Test Key",
            )
            session.commit()

        # Revoke the key
        with db_session() as session:
            result = DatabaseAPIKeyAuth.revoke_key(session, key_id)
            session.commit()
            assert result is True

        # Verify key is revoked
        with db_session() as session:
            from sqlalchemy import select

            stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
            api_key = session.scalar(stmt)
            assert bool(api_key.revoked) is True  # SQLite stores as Integer
            assert api_key.revoked_at is not None

    def test_revoke_nonexistent_key(self, db_session):
        """Test revoking a non-existent key."""
        with db_session() as session:
            result = DatabaseAPIKeyAuth.revoke_key(session, "nonexistent-key")
            assert result is False


class TestAuthProviderFactory:
    """Test authentication provider factory."""

    def test_create_static_provider(self):
        """Test creating static auth provider."""
        config = {"api_keys": {"sk-test-key": {"user_id": "test", "is_admin": False}}}

        provider = create_auth_provider("static", config)

        assert isinstance(provider, StaticAPIKeyAuth)

    def test_create_database_provider(self):
        """Test creating database auth provider."""
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)

        provider = create_auth_provider("database", session_factory=session_factory)

        assert isinstance(provider, DatabaseAPIKeyAuth)

    def test_create_none_provider(self):
        """Test creating no auth provider."""
        provider = create_auth_provider(None)
        assert provider is None

    def test_create_static_without_config(self):
        """Test creating static provider without config."""
        with pytest.raises(ValueError, match="auth_config is required"):
            create_auth_provider("static")

    def test_create_database_without_session(self):
        """Test creating database provider without session factory."""
        with pytest.raises(ValueError, match="session_factory is required"):
            create_auth_provider("database")

    def test_create_invalid_type(self):
        """Test creating provider with invalid type."""
        with pytest.raises(ValueError, match="Unknown auth_type"):
            create_auth_provider("invalid-type")
