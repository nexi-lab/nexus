"""Pre-extraction unit tests for MemoryViewRouter (#2035).

These tests validate the MemoryViewRouter directly — path detection,
resolution, query, create, and delete operations — before the Memory
service gets moved to the brick structure.
"""

import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.memory.router import MemoryViewRouter
from nexus.bricks.memory.service import Memory
from nexus.storage.models import Base

_er_mod = importlib.import_module("nexus.bricks.rebac.entity_registry")
EntityRegistry = _er_mod.EntityRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session(engine):
    """Create database session."""
    session_cls = sessionmaker(bind=engine)
    session = session_cls()
    yield session
    session.close()


@pytest.fixture
def backend(tmp_path):
    """Create local backend for content storage."""
    return CASLocalBackend(root_path=tmp_path)


@pytest.fixture
def entity_registry(session):
    """Create and populate entity registry."""
    registry = EntityRegistry(
        SimpleNamespace(session_factory=lambda: session)  # type: ignore[arg-type]
    )
    registry.register_entity("zone", "acme")
    registry.register_entity("user", "alice", parent_type="zone", parent_id="acme")
    registry.register_entity("agent", "agent1", parent_type="user", parent_id="alice")
    return registry


@pytest.fixture
def router(session, entity_registry):
    """Create MemoryViewRouter instance."""
    return MemoryViewRouter(session, entity_registry)


@pytest.fixture
def memory_api(session, backend, entity_registry):
    """Create Memory API instance for populating test data."""
    return Memory(
        session=session,
        backend=backend,
        zone_id="acme",
        user_id="alice",
        agent_id="agent1",
        entity_registry=entity_registry,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write_content(backend, data: bytes) -> str:
    """Write bytes to the backend and return the content hash."""
    return backend.write_content(data).content_hash


# ---------------------------------------------------------------------------
# TestIsMemoryPath
# ---------------------------------------------------------------------------


class TestIsMemoryPath:
    """Test the static is_memory_path() classifier."""

    def test_objs_memory_path(self):
        """Paths matching /objs/memory/{id} should be recognised."""
        assert MemoryViewRouter.is_memory_path("/objs/memory/mem-123") is True

    def test_memory_by_user_path(self):
        """Paths matching /memory/by-user/{id}/... should be recognised."""
        assert MemoryViewRouter.is_memory_path("/memory/by-user/alice/facts") is True

    def test_memory_by_agent_path(self):
        """Paths matching /memory/by-agent/{id}/... should be recognised."""
        assert MemoryViewRouter.is_memory_path("/memory/by-agent/agent1") is True

    def test_workspace_memory_path(self):
        """Paths matching /workspace/{...}/memory/... should be recognised."""
        assert MemoryViewRouter.is_memory_path("/workspace/acme/alice/memory/prefs") is True

    def test_non_memory_path_returns_false(self):
        """Non-memory paths should not be recognised."""
        assert MemoryViewRouter.is_memory_path("/files/documents/report.pdf") is False
        assert MemoryViewRouter.is_memory_path("/api/users/alice") is False

    def test_empty_path_returns_false(self):
        """Empty or root paths should not be recognised."""
        assert MemoryViewRouter.is_memory_path("") is False
        assert MemoryViewRouter.is_memory_path("/") is False


# ---------------------------------------------------------------------------
# TestResolve
# ---------------------------------------------------------------------------


class TestResolve:
    """Test router.resolve() for canonical ID paths."""

    def test_resolve_by_canonical_id(self, router, session, backend, memory_api):
        """Resolve /objs/memory/{id} returns the correct MemoryModel."""
        memory_id = memory_api.store(content="resolve test", scope="user")
        result = router.resolve(f"/objs/memory/{memory_id}")
        assert result is not None
        assert result.memory_id == memory_id

    def test_resolve_nonexistent_returns_none(self, router):
        """Resolve returns None for a non-existent canonical ID."""
        result = router.resolve("/objs/memory/nonexistent-id")
        assert result is None


# ---------------------------------------------------------------------------
# TestGetMemoryById
# ---------------------------------------------------------------------------


class TestGetMemoryById:
    """Test router.get_memory_by_id()."""

    def test_get_existing(self, router, session, backend, memory_api):
        """get_memory_by_id() returns a MemoryModel for an existing ID."""
        memory_id = memory_api.store(content="get by id", scope="user")
        result = router.get_memory_by_id(memory_id)
        assert result is not None
        assert result.memory_id == memory_id
        assert result.content_hash is not None

    def test_get_deleted_returns_none(self, router, session, backend, memory_api):
        """get_memory_by_id() returns None for soft-deleted memories."""
        memory_id = memory_api.store(content="to be deleted", scope="user")
        router.delete_memory(memory_id)
        result = router.get_memory_by_id(memory_id)
        assert result is None

    def test_get_nonexistent_returns_none(self, router):
        """get_memory_by_id() returns None for a non-existent ID."""
        result = router.get_memory_by_id("totally-fake-id-999")
        assert result is None


# ---------------------------------------------------------------------------
# TestQueryMemories
# ---------------------------------------------------------------------------


class TestQueryMemories:
    """Test router.query_memories()."""

    def test_query_by_zone(self, router, session, backend, memory_api):
        """query_memories(zone_id=...) returns memories in that zone."""
        memory_api.store(content="zone query", scope="user")
        results = router.query_memories(zone_id="acme")
        assert len(results) >= 1
        for m in results:
            assert m.zone_id == "acme"

    def test_query_by_user(self, router, session, backend, memory_api):
        """query_memories(user_id=...) returns memories for that user."""
        memory_api.store(content="user query", scope="user")
        results = router.query_memories(user_id="alice")
        assert len(results) >= 1
        for m in results:
            assert m.user_id == "alice"

    def test_query_filters_superseded(self, router, session, backend, memory_api):
        """By default, query_memories filters out superseded memories."""
        memory_api.store(
            content="v1",
            scope="user",
            namespace="ns",
            path_key="qf",
        )
        memory_api.store(
            content="v2",
            scope="user",
            namespace="ns",
            path_key="qf",
        )
        results = router.query_memories(user_id="alice")
        # Only the current (non-superseded) version should appear
        path_keys = [m.path_key for m in results if m.path_key == "qf"]
        assert len(path_keys) == 1

    def test_query_with_limit(self, router, session, backend, memory_api):
        """query_memories with limit caps the result count."""
        for i in range(5):
            memory_api.store(content=f"limited {i}", scope="user")
        results = router.query_memories(user_id="alice", limit=3)
        assert len(results) <= 3

    def test_query_temporal_filter(self, router, session, backend, memory_api):
        """query_memories with after/before filters by created_at."""
        from datetime import UTC, datetime, timedelta

        memory_api.store(content="temporal test", scope="user")
        future = datetime.now(UTC) + timedelta(hours=1)
        results = router.query_memories(user_id="alice", after=future)
        # No memories should be created in the future
        assert len(results) == 0


# ---------------------------------------------------------------------------
# TestCreateMemory
# ---------------------------------------------------------------------------


class TestCreateMemory:
    """Test router.create_memory()."""

    def test_create_basic(self, router, backend):
        """create_memory() returns a MemoryModel with the correct hash."""
        content_hash = _write_content(backend, b"basic create")
        memory = router.create_memory(
            content_hash=content_hash,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
        )
        assert memory.memory_id is not None
        assert memory.content_hash == content_hash
        assert memory.current_version == 1

    def test_create_with_version_history(self, router, backend, session):
        """create_memory() creates a version history entry."""
        from sqlalchemy import select

        from nexus.storage.models import VersionHistoryModel

        content_hash = _write_content(backend, b"versioned create")
        memory = router.create_memory(
            content_hash=content_hash,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            size_bytes=len(b"versioned create"),
        )
        stmt = select(VersionHistoryModel).where(
            VersionHistoryModel.resource_id == memory.memory_id,
            VersionHistoryModel.resource_type == "memory",
        )
        versions = list(session.execute(stmt).scalars().all())
        assert len(versions) == 1
        assert versions[0].version_number == 1
        assert versions[0].content_hash == content_hash

    def test_upsert_creates_supersedes_chain(self, router, backend):
        """create_memory() with same path_key creates a supersedes chain."""
        hash1 = _write_content(backend, b"v1 content")
        mem1 = router.create_memory(
            content_hash=hash1,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            namespace="ns",
            path_key="upsert_key",
        )

        hash2 = _write_content(backend, b"v2 content")
        mem2 = router.create_memory(
            content_hash=hash2,
            zone_id="acme",
            user_id="alice",
            agent_id="agent1",
            scope="user",
            namespace="ns",
            path_key="upsert_key",
        )

        assert mem2.supersedes_id == mem1.memory_id
        assert mem2.memory_id != mem1.memory_id
        assert mem2.current_version == 2

        # Old memory should be marked as superseded
        old = router._get_memory_by_id_raw(mem1.memory_id)
        assert old.superseded_by_id == mem2.memory_id
        assert old.invalid_at is not None


# ---------------------------------------------------------------------------
# TestDeleteMemory
# ---------------------------------------------------------------------------


class TestDeleteMemory:
    """Test router.delete_memory() soft-delete."""

    def test_soft_delete(self, router, session, backend, memory_api):
        """delete_memory() sets state='deleted' and invalid_at."""
        memory_id = memory_api.store(content="to soft-delete", scope="user")
        assert router.delete_memory(memory_id) is True

        # Should be gone from get_memory_by_id (excludes deleted)
        assert router.get_memory_by_id(memory_id) is None

        # But should still exist in raw lookup
        raw = router._get_memory_by_id_raw(memory_id)
        assert raw is not None
        assert raw.state == "deleted"
        assert raw.invalid_at is not None
