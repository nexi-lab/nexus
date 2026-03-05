"""Pre-extraction unit tests for MemoryVersioning (#2035).

These tests validate the MemoryVersioning class — list_versions,
get_version, rollback, diff, resolve_to_current, get_chain_memory_ids,
and gc_old_versions — before the Memory service gets moved to the
brick structure.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.storage.cas_local import CASLocalBackend
from nexus.bricks.memory.router import MemoryViewRouter
from nexus.bricks.memory.service import Memory
from nexus.bricks.memory.versioning import MemoryVersioning
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.contracts.types import OperationContext
from nexus.storage.models import Base

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
def mock_permission_enforcer():
    """Always-permit permission enforcer."""
    enforcer = MagicMock()
    enforcer.check_memory.return_value = True
    return enforcer


@pytest.fixture
def context():
    """Standard operation context for tests."""
    return OperationContext(user_id="alice", groups=[], is_admin=False)


@pytest.fixture
def versioning(session, entity_registry, backend, mock_permission_enforcer, context):
    """Create MemoryVersioning instance."""
    router = MemoryViewRouter(session, entity_registry)
    return MemoryVersioning(
        session_factory=lambda: session,
        memory_router=router,
        permission_enforcer=mock_permission_enforcer,
        backend=backend,
        context=context,
    )


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
# TestListVersions
# ---------------------------------------------------------------------------


class TestListVersions:
    """Test MemoryVersioning.list_versions()."""

    def test_list_versions_single(self, versioning, memory_api):
        """list_versions() returns one entry for a newly stored memory."""
        memory_id = memory_api.store(content="single version", scope="user")
        versions = versioning.list_versions(memory_id)
        assert len(versions) == 1
        assert versions[0]["version"] == 1
        assert versions[0]["source_type"] == "original"
        assert versions[0]["content_hash"] is not None

    def test_list_versions_after_update(self, versioning, memory_api):
        """list_versions() returns multiple entries after upsert updates."""
        memory_api.store(
            content="v1",
            scope="user",
            namespace="ver",
            path_key="list_ver",
        )
        id2 = memory_api.store(
            content="v2",
            scope="user",
            namespace="ver",
            path_key="list_ver",
        )
        # Use either ID; the chain should include both
        versions = versioning.list_versions(id2)
        assert len(versions) >= 2
        version_numbers = [v["version"] for v in versions]
        assert 1 in version_numbers
        assert 2 in version_numbers


# ---------------------------------------------------------------------------
# TestGetVersion
# ---------------------------------------------------------------------------


class TestGetVersion:
    """Test MemoryVersioning.get_version()."""

    def test_get_specific_version(self, versioning, memory_api):
        """get_version() returns content for a specific version number."""
        memory_id = memory_api.store(content="version content", scope="user")
        result = versioning.get_version(memory_id, version=1)
        assert result is not None
        assert result["version"] == 1
        assert result["content"] == "version content"
        assert result["memory_id"] == memory_id

    def test_get_nonexistent_version(self, versioning, memory_api):
        """get_version() returns None for a non-existent version number."""
        memory_id = memory_api.store(content="only v1", scope="user")
        result = versioning.get_version(memory_id, version=999)
        assert result is None


# ---------------------------------------------------------------------------
# TestRollback
# ---------------------------------------------------------------------------


class TestRollback:
    """Test MemoryVersioning.rollback()."""

    def test_rollback_to_previous(self, versioning, memory_api):
        """rollback() restores the content_hash to the target version."""
        memory_api.store(
            content="original",
            scope="user",
            namespace="rb",
            path_key="rollback_key",
        )
        id2 = memory_api.store(
            content="updated",
            scope="user",
            namespace="rb",
            path_key="rollback_key",
        )

        # Roll back to version 1
        versioning.rollback(id2, version=1)

        # The current memory should now have the original content hash
        v1_data = versioning.get_version(id2, version=1)
        assert v1_data is not None
        current_model = versioning._memory_router.get_memory_by_id(id2)
        assert current_model is not None
        assert current_model.content_hash == v1_data["content_hash"]

        # A new rollback version entry should exist
        versions = versioning.list_versions(id2)
        source_types = [v["source_type"] for v in versions]
        assert "rollback" in source_types

    def test_rollback_nonexistent_memory_raises(self, versioning):
        """rollback() raises ValueError for a non-existent memory."""
        with pytest.raises(ValueError, match="Memory not found"):
            versioning.rollback("nonexistent-id-42", version=1)


# ---------------------------------------------------------------------------
# TestDiffVersions
# ---------------------------------------------------------------------------


class TestDiffVersions:
    """Test MemoryVersioning.diff_versions()."""

    def test_metadata_diff(self, versioning, memory_api):
        """diff_versions(mode='metadata') returns hash and size comparison."""
        memory_api.store(
            content="diff v1",
            scope="user",
            namespace="diff_ns",
            path_key="diff_key",
        )
        id2 = memory_api.store(
            content="diff v2 with more content",
            scope="user",
            namespace="diff_ns",
            path_key="diff_key",
        )

        result = versioning.diff_versions(id2, v1=1, v2=2, mode="metadata")
        assert isinstance(result, dict)
        assert result["v1"] == 1
        assert result["v2"] == 2
        assert "content_hash_v1" in result
        assert "content_hash_v2" in result
        assert result["content_changed"] is True
        assert "size_delta" in result

    def test_content_diff(self, versioning, memory_api):
        """diff_versions(mode='content') returns a unified diff string."""
        memory_api.store(
            content="line one\nline two\n",
            scope="user",
            namespace="cdiff",
            path_key="cdiff_key",
        )
        id2 = memory_api.store(
            content="line one\nline three\n",
            scope="user",
            namespace="cdiff",
            path_key="cdiff_key",
        )

        result = versioning.diff_versions(id2, v1=1, v2=2, mode="content")
        assert isinstance(result, str)
        # Unified diff should contain diff markers
        assert "---" in result or "++" in result or "@@" in result


# ---------------------------------------------------------------------------
# TestResolveToCurrentAndChain
# ---------------------------------------------------------------------------


class TestResolveToCurrentAndChain:
    """Test resolve_to_current() and get_chain_memory_ids()."""

    def test_resolve_current_no_supersedes(self, versioning, memory_api):
        """resolve_to_current() returns the same memory if not superseded."""
        memory_id = memory_api.store(content="standalone", scope="user")
        current = versioning.resolve_to_current(memory_id)
        assert current is not None
        assert current.memory_id == memory_id

    def test_get_chain_ids(self, versioning, memory_api):
        """get_chain_memory_ids() returns all IDs in order (oldest to newest)."""
        id1 = memory_api.store(
            content="chain v1",
            scope="user",
            namespace="chain_ns",
            path_key="chain_key",
        )
        id2 = memory_api.store(
            content="chain v2",
            scope="user",
            namespace="chain_ns",
            path_key="chain_key",
        )
        chain = versioning.get_chain_memory_ids(id2)
        assert len(chain) == 2
        assert chain[0] == id1  # oldest first
        assert chain[1] == id2  # newest last

        # Should also work starting from the old ID
        chain_from_old = versioning.get_chain_memory_ids(id1)
        assert chain_from_old == chain


# ---------------------------------------------------------------------------
# TestGcOldVersions
# ---------------------------------------------------------------------------


class TestGcOldVersions:
    """Test MemoryVersioning.gc_old_versions()."""

    def test_gc_removes_old_superseded(self, versioning, memory_api, session):
        """gc_old_versions() removes superseded memories older than threshold."""

        id1 = memory_api.store(
            content="gc v1",
            scope="user",
            namespace="gc_ns",
            path_key="gc_key",
        )
        _id2 = memory_api.store(
            content="gc v2",
            scope="user",
            namespace="gc_ns",
            path_key="gc_key",
        )

        # Manually age the superseded memory's invalid_at beyond threshold
        old_model = versioning._memory_router._get_memory_by_id_raw(id1)
        assert old_model is not None
        old_model.invalid_at = datetime.now(UTC) - timedelta(days=400)
        session.commit()

        removed = versioning.gc_old_versions(older_than_days=365)
        assert removed >= 1

        # The old memory should be physically deleted
        assert versioning._memory_router._get_memory_by_id_raw(id1) is None

    def test_gc_preserves_current(self, versioning, memory_api, session):
        """gc_old_versions() never removes current (non-superseded) memories."""

        memory_id = memory_api.store(content="current memory", scope="user")
        versioning.gc_old_versions(older_than_days=0)

        # Current memory should NOT be removed even with threshold=0
        model = versioning._memory_router.get_memory_by_id(memory_id)
        assert model is not None
