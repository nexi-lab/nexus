"""Tests for session management module (CacheStore-backed).

Migrated from SQLAlchemy ORM to CacheStoreABC per data-storage-matrix.md Part 6.
"""

from datetime import timedelta

import pytest

from nexus.contracts.auth_store_types import SessionDTO
from nexus.contracts.cache_store import InMemoryCacheStore
from nexus.storage.auth_stores.cache_session_store import CacheSessionStore
from nexus.system_services.lifecycle.sessions import (
    create_session,
    get_session,
    list_user_sessions,
    update_session_activity,
)


@pytest.fixture
def cache():
    """Create in-memory CacheStore."""
    return InMemoryCacheStore()


@pytest.fixture
def store(cache):
    """Create CacheSessionStore backed by InMemoryCacheStore."""
    return CacheSessionStore(cache)


class TestCreateSession:
    """Test create_session function."""

    @pytest.mark.asyncio
    async def test_create_temporary_session(self, store):
        dto = await create_session(
            store,
            user_id="alice",
            ttl=timedelta(hours=8),
            ip_address="127.0.0.1",
            user_agent="Mozilla/5.0",
        )

        assert isinstance(dto, SessionDTO)
        assert dto.session_id is not None
        assert dto.user_id == "alice"
        assert dto.expires_at is not None
        assert dto.ip_address == "127.0.0.1"
        assert dto.user_agent == "Mozilla/5.0"
        assert dto.agent_id is None

    @pytest.mark.asyncio
    async def test_create_persistent_session(self, store):
        dto = await create_session(store, user_id="alice", ttl=None)
        assert dto.expires_at is None

    @pytest.mark.asyncio
    async def test_create_agent_session(self, store):
        dto = await create_session(
            store,
            user_id="alice",
            agent_id="agent1",
            ttl=timedelta(hours=1),
        )
        assert dto.agent_id == "agent1"
        assert dto.user_id == "alice"

    @pytest.mark.asyncio
    async def test_create_session_with_zone(self, store):
        dto = await create_session(
            store,
            user_id="alice",
            zone_id="acme",
            ttl=timedelta(hours=8),
        )
        assert dto.zone_id == "acme"

    @pytest.mark.asyncio
    async def test_session_auto_generates_id(self, store):
        s1 = await create_session(store, user_id="alice")
        s2 = await create_session(store, user_id="bob")
        assert s1.session_id != s2.session_id


class TestUpdateSessionActivity:
    """Test update_session_activity function."""

    @pytest.mark.asyncio
    async def test_update_activity_success(self, store):
        dto = await create_session(store, user_id="alice")
        original_activity = dto.last_activity

        import time

        time.sleep(0.01)

        success = await update_session_activity(store, dto.session_id)
        assert success is True

        updated = await get_session(store, dto.session_id)
        assert updated is not None
        assert updated.last_activity > original_activity

    @pytest.mark.asyncio
    async def test_update_activity_nonexistent(self, store):
        success = await update_session_activity(store, "nonexistent-id")
        assert success is False


class TestGetSession:
    """Test get_session function."""

    @pytest.mark.asyncio
    async def test_get_existing_session(self, store):
        created = await create_session(store, user_id="alice")
        fetched = await get_session(store, created.session_id)
        assert fetched is not None
        assert fetched.session_id == created.session_id
        assert fetched.user_id == "alice"

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, store):
        assert await get_session(store, "nonexistent") is None


class TestListUserSessions:
    """Test list_user_sessions function."""

    @pytest.mark.asyncio
    async def test_list_user_sessions(self, store):
        s1 = await create_session(store, user_id="alice", ttl=timedelta(hours=8))
        s2 = await create_session(store, user_id="alice", ttl=None)
        await create_session(store, user_id="bob", ttl=timedelta(hours=8))

        alice_sessions = await list_user_sessions(store, user_id="alice")
        assert len(alice_sessions) == 2
        session_ids = {s.session_id for s in alice_sessions}
        assert s1.session_id in session_ids
        assert s2.session_id in session_ids

    @pytest.mark.asyncio
    async def test_list_includes_persistent(self, store):
        await create_session(store, user_id="alice", ttl=None)
        await create_session(store, user_id="alice", ttl=timedelta(hours=8))

        sessions = await list_user_sessions(store, user_id="alice")
        assert len(sessions) == 2


class TestCacheSessionStore:
    """Direct CacheSessionStore tests."""

    @pytest.mark.asyncio
    async def test_delete_session(self, store):
        dto = await store.create(user_id="alice")
        assert await store.delete(dto.session_id) is True
        assert await store.get(dto.session_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store):
        assert await store.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_session_isolation(self, store):
        await store.create(user_id="alice")
        await store.create(user_id="bob")

        alice = await store.list_for_user("alice")
        bob = await store.list_for_user("bob")

        assert len(alice) == 1
        assert len(bob) == 1
        assert alice[0].session_id != bob[0].session_id

    @pytest.mark.asyncio
    async def test_serialization_roundtrip(self, store):
        dto = await store.create(
            user_id="alice",
            agent_id="a1",
            zone_id="zone1",
            ttl_seconds=3600,
            ip_address="10.0.0.1",
            user_agent="TestAgent",
        )

        fetched = await store.get(dto.session_id)
        assert fetched is not None
        assert fetched.user_id == "alice"
        assert fetched.agent_id == "a1"
        assert fetched.zone_id == "zone1"
        assert fetched.ip_address == "10.0.0.1"
        assert fetched.user_agent == "TestAgent"
        assert fetched.expires_at is not None


class TestSessionDTO:
    """Test SessionDTO.is_expired()."""

    def test_persistent_not_expired(self):
        dto = SessionDTO(session_id="s1", user_id="u1", expires_at=None)
        assert dto.is_expired() is False

    def test_unexpired_session(self):
        from datetime import UTC, datetime

        dto = SessionDTO(
            session_id="s1",
            user_id="u1",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        assert dto.is_expired() is False

    def test_expired_session(self):
        from datetime import UTC, datetime

        dto = SessionDTO(
            session_id="s1",
            user_id="u1",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        assert dto.is_expired() is True
