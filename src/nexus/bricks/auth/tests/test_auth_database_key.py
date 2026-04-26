"""Unit tests for database API key authentication provider."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@pytest.fixture
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture
def engine(record_store):
    return record_store.engine


@pytest.fixture
def session_factory(record_store):
    """Session factory pre-seeded with the org_acme zone (#3871 round 3:
    DatabaseAPIKeyAuth.create_key now validates the ZoneModel exists before
    inserting the api_key_zones junction row)."""
    from nexus.storage.models.auth import ZoneModel

    sf = record_store.session_factory
    with sf() as s:
        s.add(ZoneModel(zone_id="org_acme", name="org_acme", phase="Active"))
        s.commit()
    return sf


@pytest.fixture
def auth_provider(record_store):
    return DatabaseAPIKeyAuth(record_store, require_expiry=False)


@pytest.fixture
def auth_provider_require_expiry(record_store):
    return DatabaseAPIKeyAuth(record_store, require_expiry=True)


def test_create_key_basic(session_factory) -> None:
    """Create a basic API key and verify it exists in the database."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Test Key",
            zone_id="org_acme",
        )
        session.commit()

    assert key_id is not None
    assert raw_key.startswith("sk-")
    assert len(raw_key) >= 32

    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.user_id == "alice"
        assert api_key.name == "Test Key"


def test_create_key_with_zone(session_factory) -> None:
    """Create a key with zone_id; the zone is stored in the api_key_zones
    junction (not the deprecated APIKeyModel.zone_id column, #3871 Phase 2)."""
    from nexus.storage.api_key_ops import get_zones_for_key

    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Zone Key",
            zone_id="org_acme",
        )
        session.commit()

    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.zone_id is None  # column deprecated, NULL by design
        assert get_zones_for_key(session, key_id) == ["org_acme"]


def test_create_key_with_subject(session_factory) -> None:
    """Create a key with subject_type and subject_id."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Agent Key",
            subject_type="agent",
            subject_id="agent_claude_001",
            zone_id="org_acme",
        )
        session.commit()

    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.subject_type == "agent"
        assert api_key.subject_id == "agent_claude_001"


def test_create_key_invalid_subject_type(session_factory) -> None:
    """Invalid subject_type raises ValueError."""
    with (
        session_factory() as session,
        pytest.raises(ValueError, match="subject_type must be one of"),
    ):
        DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Bad Key",
            subject_type="invalid_type",
        )


@pytest.mark.asyncio
async def test_authenticate_valid_key(auth_provider, session_factory) -> None:
    """Authenticate with a valid API key returns success."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Auth Test Key",
            zone_id="org_acme",
            is_admin=True,
        )
        session.commit()

    result = await auth_provider.authenticate(raw_key)
    assert result.authenticated is True
    assert result.subject_id == "alice"
    assert result.zone_id == "org_acme"
    assert result.is_admin is True
    assert result.metadata is not None
    assert result.metadata["key_id"] == key_id
    assert result.metadata["key_name"] == "Auth Test Key"


@pytest.mark.asyncio
async def test_authenticate_invalid_key(auth_provider) -> None:
    """Authenticate with an invalid API key returns failure."""
    result = await auth_provider.authenticate("sk-this-is-a-fake-key-that-does-not-exist-in-db")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_empty_token(auth_provider) -> None:
    """Authenticate with empty string returns failure."""
    result = await auth_provider.authenticate("")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_bad_format(auth_provider) -> None:
    """Authenticate with a key not starting with sk- returns failure."""
    result = await auth_provider.authenticate("bad-format-key-1234567890abcdef")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_short_key(auth_provider) -> None:
    """Authenticate with a key shorter than minimum length returns failure."""
    result = await auth_provider.authenticate("sk-short")
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_expired_key(auth_provider, session_factory) -> None:
    """Authenticate with an expired key returns failure."""
    expired_time = datetime.now(UTC) - timedelta(days=1)
    with session_factory() as session:
        _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Expired Key",
            zone_id="org_acme",
            expires_at=expired_time,
        )
        session.commit()

    result = await auth_provider.authenticate(raw_key)
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_revoked_key(auth_provider, session_factory) -> None:
    """Authenticate with a revoked key returns failure."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Revoked Key",
            zone_id="org_acme",
        )
        session.commit()

    # Revoke the key
    with session_factory() as session:
        DatabaseAPIKeyAuth.revoke_key(session, key_id)
        session.commit()

    result = await auth_provider.authenticate(raw_key)
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_require_expiry(auth_provider_require_expiry, session_factory) -> None:
    """When require_expiry=True, keys without expiry are rejected."""
    with session_factory() as session:
        _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="No Expiry Key",
            zone_id="org_acme",
        )
        session.commit()

    result = await auth_provider_require_expiry.authenticate(raw_key)
    assert result.authenticated is False


@pytest.mark.asyncio
async def test_authenticate_require_expiry_with_valid_expiry(
    auth_provider_require_expiry, session_factory
) -> None:
    """When require_expiry=True, keys with a future expiry are accepted."""
    future_time = datetime.now(UTC) + timedelta(days=30)
    with session_factory() as session:
        _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Expiry Key",
            zone_id="org_acme",
            expires_at=future_time,
        )
        session.commit()

    result = await auth_provider_require_expiry.authenticate(raw_key)
    assert result.authenticated is True


@pytest.mark.asyncio
async def test_validate_token(auth_provider, session_factory) -> None:
    """validate_token returns True for valid keys, False for invalid."""
    with session_factory() as session:
        _key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Validate Test Key",
            zone_id="org_acme",
        )
        session.commit()

    assert await auth_provider.validate_token(raw_key) is True
    assert (
        await auth_provider.validate_token("sk-invalid-key-that-does-not-exist-in-database")
        is False
    )


def test_revoke_key(session_factory) -> None:
    """Revoking a key marks it as revoked in the database."""
    with session_factory() as session:
        key_id, _raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Revoke Test Key",
            zone_id="org_acme",
        )
        session.commit()

    with session_factory() as session:
        result = DatabaseAPIKeyAuth.revoke_key(session, key_id)
        session.commit()
    assert result is True

    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.revoked == 1
        assert api_key.revoked_at is not None


def test_revoke_key_not_found(session_factory) -> None:
    """Revoking a non-existent key returns False."""
    with session_factory() as session:
        result = DatabaseAPIKeyAuth.revoke_key(session, "non_existent_key_id")
    assert result is False


def test_hash_consistency() -> None:
    """Hashing the same key twice produces the same result."""
    key = "sk-test-key-for-hash-consistency-check-12345"
    hash1 = DatabaseAPIKeyAuth._hash_key(key)
    hash2 = DatabaseAPIKeyAuth._hash_key(key)
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex digest


def test_key_format_validation() -> None:
    """_validate_key_format checks prefix and minimum length."""
    assert DatabaseAPIKeyAuth._validate_key_format("sk-" + "x" * 29) is True
    assert DatabaseAPIKeyAuth._validate_key_format("sk-short") is False
    assert DatabaseAPIKeyAuth._validate_key_format("bad-prefix-key-12345678901234567890") is False
    assert DatabaseAPIKeyAuth._validate_key_format("") is False


def test_close(auth_provider) -> None:
    """close() doesn't raise."""
    auth_provider.close()


@pytest.mark.asyncio
async def test_last_used_at_updated(auth_provider, session_factory) -> None:
    """Authenticating a key updates last_used_at."""
    with session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id="alice",
            name="Last Used Key",
            zone_id="org_acme",
        )
        session.commit()

    # Verify last_used_at is initially None
    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.last_used_at is None

    # Authenticate to trigger last_used_at update
    result = await auth_provider.authenticate(raw_key)
    assert result.authenticated is True

    # Verify last_used_at was set
    with session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == key_id)
        api_key = session.scalar(stmt)
        assert api_key is not None
        assert api_key.last_used_at is not None
