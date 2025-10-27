"""Unit tests for database API key authentication provider."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel, Base


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture
def auth_provider(session_factory):
    """Create DatabaseAPIKeyAuth instance."""
    return DatabaseAPIKeyAuth(session_factory, require_expiry=False)


@pytest.fixture
def auth_provider_require_expiry(session_factory):
    """Create DatabaseAPIKeyAuth instance that requires expiry."""
    return DatabaseAPIKeyAuth(session_factory, require_expiry=True)


@pytest.mark.asyncio
async def test_create_key_basic(session_factory):
    """Test creating a basic API key."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Alice's laptop",
            is_admin=False,
        )
        session.commit()

    assert key_id is not None
    assert raw_key.startswith("sk-")
    assert len(raw_key) >= 32


@pytest.mark.asyncio
async def test_create_key_with_tenant(session_factory):
    """Test creating an API key with tenant."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
            tenant_id="org_acme",
            is_admin=True,
        )
        session.commit()

    assert "org_acme" in raw_key or raw_key.startswith("sk-org_acme")


@pytest.mark.asyncio
async def test_create_key_with_expiry(session_factory):
    """Test creating an API key with expiry."""
    expires_at = datetime.now(UTC) + timedelta(days=90)

    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
            expires_at=expires_at,
        )
        session.commit()

    # Verify key was created
    assert raw_key.startswith("sk-")


@pytest.mark.asyncio
async def test_authenticate_valid_key(auth_provider, session_factory):
    """Test authentication with valid API key."""
    # Create a key
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
            tenant_id="org_acme",
            is_admin=True,
        )
        session.commit()

    # Authenticate
    result = await auth_provider.authenticate(raw_key)

    assert result.authenticated is True
    assert result.subject_id == "alice"
    assert result.tenant_id == "org_acme"
    assert result.is_admin is True
    assert result.metadata["key_id"] == key_id
    assert result.metadata["key_name"] == "Test Key"


@pytest.mark.asyncio
async def test_authenticate_invalid_key(auth_provider):
    """Test authentication with invalid API key."""
    result = await auth_provider.authenticate("sk-invalid-key-that-does-not-exist")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_empty_token(auth_provider):
    """Test authentication with empty token."""
    result = await auth_provider.authenticate("")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_invalid_format(auth_provider):
    """Test authentication with key missing sk- prefix."""
    result = await auth_provider.authenticate("invalid-format-key")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_short_key(auth_provider):
    """Test authentication with key that's too short."""
    result = await auth_provider.authenticate("sk-short")

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_expired_key(auth_provider, session_factory):
    """Test authentication with expired API key."""
    # Create an expired key
    expired_time = datetime.now(UTC) - timedelta(days=1)

    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Expired Key",
            expires_at=expired_time,
        )
        session.commit()

    # Try to authenticate
    result = await auth_provider.authenticate(raw_key)

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_not_yet_expired(auth_provider, session_factory):
    """Test authentication with not-yet-expired API key."""
    # Create a future expiry key
    future_time = datetime.now(UTC) + timedelta(days=30)

    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Future Key",
            expires_at=future_time,
        )
        session.commit()

    # Authenticate
    result = await auth_provider.authenticate(raw_key)

    assert result.authenticated is True
    assert result.subject_id == "alice"


@pytest.mark.asyncio
async def test_authenticate_revoked_key(auth_provider, session_factory):
    """Test authentication with revoked API key."""
    # Create and then revoke a key
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
        )
        session.commit()

    # Revoke the key
    with session_factory() as session:
        DatabaseAPIKeyAuth.revoke_key(session, key_id)
        session.commit()

    # Try to authenticate
    result = await auth_provider.authenticate(raw_key)

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_no_expiry_with_require_expiry(
    auth_provider_require_expiry, session_factory
):
    """Test that keys without expiry are rejected when require_expiry=True."""
    # Create a key without expiry
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="No Expiry Key",
            expires_at=None,
        )
        session.commit()

    # Try to authenticate (should fail)
    result = await auth_provider_require_expiry.authenticate(raw_key)

    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_with_expiry_and_require_expiry(
    auth_provider_require_expiry, session_factory
):
    """Test that keys with expiry work when require_expiry=True."""
    # Create a key with expiry
    expires_at = datetime.now(UTC) + timedelta(days=90)

    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Expiry Key",
            expires_at=expires_at,
        )
        session.commit()

    # Authenticate (should succeed)
    result = await auth_provider_require_expiry.authenticate(raw_key)

    assert result.authenticated is True
    assert result.subject_id == "alice"


@pytest.mark.asyncio
async def test_validate_token_valid(auth_provider, session_factory):
    """Test token validation with valid key."""
    # Create a key
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
        )
        session.commit()

    # Validate
    is_valid = await auth_provider.validate_token(raw_key)
    assert is_valid is True


@pytest.mark.asyncio
async def test_validate_token_invalid(auth_provider):
    """Test token validation with invalid key."""
    is_valid = await auth_provider.validate_token("sk-invalid-key")
    assert is_valid is False


def test_revoke_key_success(session_factory):
    """Test revoking an existing key."""
    # Create a key
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
        )
        session.commit()

    # Revoke it
    with session_factory() as session:
        result = DatabaseAPIKeyAuth.revoke_key(session, key_id)
        session.commit()

    assert result is True


def test_revoke_key_not_found(session_factory):
    """Test revoking a non-existent key."""
    with session_factory() as session:
        result = DatabaseAPIKeyAuth.revoke_key(session, "nonexistent-key-id")
        session.commit()

    assert result is False


def test_hash_key_consistency():
    """Test that key hashing is consistent."""
    key = "sk-test-key-for-hashing"

    hash1 = DatabaseAPIKeyAuth._hash_key(key)
    hash2 = DatabaseAPIKeyAuth._hash_key(key)

    assert hash1 == hash2


def test_hash_key_different_keys():
    """Test that different keys produce different hashes."""
    key1 = "sk-test-key-one"
    key2 = "sk-test-key-two"

    hash1 = DatabaseAPIKeyAuth._hash_key(key1)
    hash2 = DatabaseAPIKeyAuth._hash_key(key2)

    assert hash1 != hash2


def test_validate_key_format_valid():
    """Test key format validation with valid keys."""
    assert DatabaseAPIKeyAuth._validate_key_format("sk-" + "a" * 30) is True


def test_validate_key_format_missing_prefix():
    """Test key format validation with missing prefix."""
    assert DatabaseAPIKeyAuth._validate_key_format("test-key-without-prefix") is False


def test_validate_key_format_too_short():
    """Test key format validation with too short key."""
    assert DatabaseAPIKeyAuth._validate_key_format("sk-short") is False


def test_close(auth_provider):
    """Test close method."""
    # Should not raise
    auth_provider.close()


@pytest.mark.asyncio
async def test_last_used_at_updated(auth_provider, session_factory):
    """Test that last_used_at is updated on authentication."""
    # Create a key
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
        )
        session.commit()

    # Get initial last_used_at (should be None)
    with session_factory() as session:
        from sqlalchemy import select

        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        initial_last_used = api_key.last_used_at

    # Authenticate
    await auth_provider.authenticate(raw_key)

    # Check that last_used_at was updated
    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        updated_last_used = api_key.last_used_at

    assert updated_last_used is not None
    if initial_last_used is not None:
        assert updated_last_used > initial_last_used
