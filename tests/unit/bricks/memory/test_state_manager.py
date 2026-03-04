"""Pre-extraction unit tests for MemoryStateManager (#2035).

These tests validate the MemoryStateManager class — delete, approve,
deactivate, invalidate, revalidate, and batch operations — before the
Memory service gets moved to the brick structure.
"""

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from nexus.backends.storage.local import LocalBackend
from nexus.bricks.memory.router import MemoryViewRouter
from nexus.bricks.memory.service import Memory
from nexus.bricks.memory.state import MemoryStateManager
from nexus.contracts.types import OperationContext
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
    return LocalBackend(root_path=tmp_path)


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
def state_manager(session, entity_registry, mock_permission_enforcer, context):
    """Create MemoryStateManager with mock permission enforcer."""
    router = MemoryViewRouter(session, entity_registry)
    return MemoryStateManager(router, mock_permission_enforcer, context)


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
# TestDelete
# ---------------------------------------------------------------------------


class TestDelete:
    """Test MemoryStateManager.delete()."""

    def test_delete_existing(self, state_manager, memory_api):
        """delete() returns True and soft-deletes an existing memory."""
        memory_id = memory_api.store(content="delete me", scope="user")
        assert state_manager.delete(memory_id) is True
        # After deletion, get() should return None
        assert memory_api.get(memory_id) is None

    def test_delete_nonexistent(self, state_manager):
        """delete() returns False for a non-existent memory ID."""
        assert state_manager.delete("fake-id-000") is False

    def test_delete_no_permission(self, state_manager, memory_api, mock_permission_enforcer):
        """delete() returns False when permission is denied."""
        memory_id = memory_api.store(content="protected", scope="user")
        mock_permission_enforcer.check_memory.return_value = False
        assert state_manager.delete(memory_id) is False
        # Reset for other tests
        mock_permission_enforcer.check_memory.return_value = True


# ---------------------------------------------------------------------------
# TestApprove
# ---------------------------------------------------------------------------


class TestApprove:
    """Test MemoryStateManager.approve()."""

    def test_approve_activates_memory(self, state_manager, memory_api):
        """approve() changes state from 'inactive' to 'active'."""
        memory_id = memory_api.store(content="inactive memory", scope="user", state="inactive")
        result_before = memory_api.get(memory_id)
        assert result_before is not None
        assert result_before["state"] == "inactive"

        assert state_manager.approve(memory_id) is True

        result_after = memory_api.get(memory_id)
        assert result_after is not None
        assert result_after["state"] == "active"

    def test_approve_nonexistent(self, state_manager):
        """approve() returns False for a non-existent memory."""
        assert state_manager.approve("missing-id-123") is False


# ---------------------------------------------------------------------------
# TestDeactivate
# ---------------------------------------------------------------------------


class TestDeactivate:
    """Test MemoryStateManager.deactivate()."""

    def test_deactivate_memory(self, state_manager, memory_api):
        """deactivate() changes state from 'active' to 'inactive'."""
        memory_id = memory_api.store(content="active memory", scope="user")
        assert state_manager.deactivate(memory_id) is True

        result = memory_api.get(memory_id)
        assert result is not None
        assert result["state"] == "inactive"


# ---------------------------------------------------------------------------
# TestInvalidate
# ---------------------------------------------------------------------------


class TestInvalidate:
    """Test MemoryStateManager.invalidate()."""

    def test_invalidate_with_datetime(self, state_manager, memory_api):
        """invalidate() with a datetime sets invalid_at."""
        memory_id = memory_api.store(content="fact", scope="user")
        ts = datetime(2026, 1, 15, tzinfo=UTC)
        assert state_manager.invalidate(memory_id, invalid_at=ts) is True

        # The memory should now have invalid_at set
        model = state_manager._memory_router._get_memory_by_id_raw(memory_id)
        assert model is not None
        assert model.invalid_at is not None

    def test_invalidate_with_string(self, state_manager, memory_api):
        """invalidate() with an ISO-8601 string parses and sets invalid_at."""
        memory_id = memory_api.store(content="string invalidation", scope="user")
        assert state_manager.invalidate(memory_id, invalid_at="2026-02-01T00:00:00Z") is True

        model = state_manager._memory_router._get_memory_by_id_raw(memory_id)
        assert model is not None
        assert model.invalid_at is not None

    def test_invalidate_defaults_to_now(self, state_manager, memory_api):
        """invalidate() without invalid_at defaults to now()."""
        memory_id = memory_api.store(content="default invalidation", scope="user")
        before = datetime.now(UTC)
        assert state_manager.invalidate(memory_id) is True

        model = state_manager._memory_router._get_memory_by_id_raw(memory_id)
        assert model is not None
        assert model.invalid_at is not None
        # Should be approximately now (within a few seconds)
        # Use naive comparison since SQLite stores without timezone
        invalid_at = model.invalid_at
        if invalid_at.tzinfo is None:
            invalid_at = invalid_at.replace(tzinfo=UTC)
        delta = abs((invalid_at - before).total_seconds())
        assert delta < 5


# ---------------------------------------------------------------------------
# TestRevalidate
# ---------------------------------------------------------------------------


class TestRevalidate:
    """Test MemoryStateManager.revalidate()."""

    def test_revalidate_clears_invalid_at(self, state_manager, memory_api):
        """revalidate() clears the invalid_at timestamp."""
        memory_id = memory_api.store(content="revalidate me", scope="user")
        state_manager.invalidate(memory_id)

        # Verify it's invalidated
        model = state_manager._memory_router._get_memory_by_id_raw(memory_id)
        assert model.invalid_at is not None

        assert state_manager.revalidate(memory_id) is True

        model = state_manager._memory_router._get_memory_by_id_raw(memory_id)
        assert model.invalid_at is None


# ---------------------------------------------------------------------------
# TestBatchOperations
# ---------------------------------------------------------------------------


class TestBatchOperations:
    """Test batch operations on MemoryStateManager."""

    def test_approve_batch(self, state_manager, memory_api):
        """approve_batch() activates multiple memories."""
        ids = [
            memory_api.store(content=f"batch approve {i}", scope="user", state="inactive")
            for i in range(3)
        ]
        result = state_manager.approve_batch(ids)
        assert result["approved"] == 3
        assert result["failed"] == 0
        assert len(result["approved_ids"]) == 3
        assert len(result["failed_ids"]) == 0

        # Verify all are now active
        for mid in ids:
            mem = memory_api.get(mid)
            assert mem is not None
            assert mem["state"] == "active"

    def test_delete_batch_with_failures(self, state_manager, memory_api):
        """delete_batch() tracks both successes and failures."""
        real_id = memory_api.store(content="real memory", scope="user")
        ids = [real_id, "fake-id-1", "fake-id-2"]
        result = state_manager.delete_batch(ids)
        assert result["deleted"] == 1
        assert result["failed"] == 2
        assert real_id in result["deleted_ids"]
        assert "fake-id-1" in result["failed_ids"]
        assert "fake-id-2" in result["failed_ids"]

    def test_invalidate_batch(self, state_manager, memory_api):
        """invalidate_batch() invalidates multiple memories at once."""
        ids = [memory_api.store(content=f"batch invalidate {i}", scope="user") for i in range(3)]
        result = state_manager.invalidate_batch(ids)
        assert result["invalidated"] == 3
        assert result["failed"] == 0

        # Verify all are invalidated
        for mid in ids:
            model = state_manager._memory_router._get_memory_by_id_raw(mid)
            assert model is not None
            assert model.invalid_at is not None
