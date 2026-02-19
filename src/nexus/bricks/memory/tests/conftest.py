"""Centralized test fixtures for Memory brick tests.

Eliminates ~1,575 LOC of duplicated fixtures across 55 test files by providing
shared, reusable fixtures with consistent test data and mock objects.

Usage:
    # In any test file under bricks/memory/tests/
    from conftest import memory_api_mock, sample_memories

    def test_example(memory_api_mock, sample_memories) -> None:
        # Use fixtures directly
        pass

Related: Issue #2128 (Memory brick extraction)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Iterator
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

# ============================================================================
# Database Fixtures
# ============================================================================


@pytest.fixture
def db_engine() -> Any:
    """In-memory SQLite engine for testing."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine) -> Any:
    """SQLAlchemy session factory for testing."""
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory) -> Any:
    """Database session for a single test."""
    sess = session_factory()
    yield sess
    sess.rollback()
    sess.close()


@pytest.fixture
async def async_db_engine() -> Any:
    """Async in-memory SQLite engine for async tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    yield engine
    await engine.dispose()


@pytest.fixture
async def async_session_factory(async_db_engine) -> Any:
    """Async SQLAlchemy session factory."""
    return async_sessionmaker(bind=async_db_engine, expire_on_commit=False)


@pytest.fixture
async def async_session(async_session_factory) -> Any:
    """Async database session with auto-rollback."""
    async with async_session_factory() as sess, sess.begin():
        yield sess
        await sess.rollback()


@pytest.fixture
def isolated_session(session_factory) -> Any:
    """Session with auto-rollback for integration tests.

    Prevents test pollution by rolling back all changes after the test.
    """
    sess = session_factory()
    sess.begin_nested()  # Create savepoint
    yield sess
    sess.rollback()  # Rollback to savepoint
    sess.close()


# ============================================================================
# Mock Fixtures
# ============================================================================


@pytest.fixture
def memory_api_mock() -> Any:
    """Standard MemoryAPI mock implementing MemoryProtocol.

    Provides mocked versions of all MemoryProtocol methods.
    """
    mock = Mock()
    mock.store = Mock(return_value="mem_test_123")
    mock.get = Mock(return_value={
        "memory_id": "mem_test_123",
        "content": "Test content",
        "state": "active",
        "importance": 1.0,
    })
    mock.retrieve = Mock(return_value=None)
    mock.delete = Mock(return_value=True)
    mock.list = Mock(return_value=[])
    mock.query = Mock(return_value=[])
    mock.search = Mock(return_value=[])
    mock.approve = Mock(return_value=True)
    mock.deactivate = Mock(return_value=True)
    mock.invalidate = Mock(return_value=True)
    mock.revalidate = Mock(return_value=True)
    return mock


@pytest.fixture
def async_memory_api_mock() -> Any:
    """Async MemoryAPI mock for async tests."""
    mock = AsyncMock()
    mock.store = AsyncMock(return_value="mem_test_123")
    mock.get = AsyncMock(return_value={
        "memory_id": "mem_test_123",
        "content": "Test content",
        "state": "active",
        "importance": 1.0,
    })
    mock.retrieve = AsyncMock(return_value=None)
    mock.delete = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def permission_enforcer_allow_all() -> Any:
    """Permission enforcer stub that allows all operations."""
    mock = Mock()
    mock.check_memory = Mock(return_value=True)
    mock.check = Mock(return_value=True)
    mock.filter_list = Mock(side_effect=lambda items, *_args, **_kwargs: items)
    return mock


@pytest.fixture
def permission_enforcer_deny_all() -> Any:
    """Permission enforcer stub that denies all operations."""
    mock = Mock()
    mock.check_memory = Mock(return_value=False)
    mock.check = Mock(return_value=False)
    mock.filter_list = Mock(return_value=[])
    return mock


@pytest.fixture
def memory_router_mock() -> Any:
    """Mock MemoryViewRouter for testing."""
    mock = Mock()
    mock.get_memory_by_id = Mock(return_value=None)
    mock.create_memory = Mock(return_value=Mock(memory_id="mem_test_123"))
    mock.approve_memory = Mock(return_value=True)
    mock.deactivate_memory = Mock(return_value=True)
    mock.delete_memory = Mock(return_value=True)
    return mock


@pytest.fixture
def backend_mock() -> Any:
    """Mock Backend for CAS operations."""
    mock = Mock()
    mock.write_content = Mock(return_value=Mock(unwrap=lambda: "hash_abc123"))
    mock.read_content = Mock(return_value=Mock(unwrap=lambda: b"Test content"))
    return mock


@pytest.fixture
def graph_store_mock() -> Any:
    """Mock GraphStore for entity/relationship operations."""
    mock = AsyncMock()
    mock.add_entity = AsyncMock(return_value="entity_123")
    mock.add_relationship = AsyncMock(return_value="rel_123")
    mock.get_entities_batch = AsyncMock(return_value=[])
    mock.get_relationships_batch = AsyncMock(return_value={})
    return mock


# ============================================================================
# Test Data Fixtures
# ============================================================================


@pytest.fixture
def sample_memories() -> Any:
    """Standard test memory dataset (5 memories with different states)."""
    return [
        {
            "memory_id": "mem_1",
            "content": "User prefers Python for data analysis",
            "content_hash": "hash_1",
            "scope": "user",
            "memory_type": "preference",
            "importance": 1.0,
            "state": "active",
            "namespace": "test",
            "path_key": "pref/lang",
            "created_at": datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        },
        {
            "memory_id": "mem_2",
            "content": "Project deadline is March 15th",
            "content_hash": "hash_2",
            "scope": "agent",
            "memory_type": "fact",
            "importance": 0.8,
            "state": "active",
            "namespace": "test",
            "created_at": datetime(2025, 1, 2, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 2, 12, 0, 0, tzinfo=UTC),
        },
        {
            "memory_id": "mem_3",
            "content": "Deprecated API endpoint",
            "content_hash": "hash_3",
            "scope": "zone",
            "memory_type": "fact",
            "importance": 0.3,
            "state": "inactive",
            "namespace": "test",
            "created_at": datetime(2025, 1, 3, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 3, 12, 0, 0, tzinfo=UTC),
        },
        {
            "memory_id": "mem_4",
            "content": "Temporary cache key",
            "content_hash": "hash_4",
            "scope": "session",
            "memory_type": "ephemeral",
            "importance": 0.1,
            "state": "active",
            "namespace": "test",
            "created_at": datetime(2025, 1, 4, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 4, 12, 0, 0, tzinfo=UTC),
        },
        {
            "memory_id": "mem_5",
            "content": "Obsolete fact",
            "content_hash": "hash_5",
            "scope": "user",
            "memory_type": "fact",
            "importance": 0.5,
            "state": "deleted",
            "namespace": "test",
            "invalid_at": datetime(2025, 1, 5, 12, 0, 0, tzinfo=UTC),
            "created_at": datetime(2025, 1, 5, 12, 0, 0, tzinfo=UTC),
            "updated_at": datetime(2025, 1, 5, 12, 0, 0, tzinfo=UTC),
        },
    ]


@pytest.fixture
def enrichment_flags() -> Any:
    """Standard enrichment flags for testing."""
    from nexus.bricks.memory.enrichment import EnrichmentFlags

    return EnrichmentFlags(
        generate_embedding=True,
        extract_entities=True,
        extract_temporal=True,
        classify_stability=True,
        # Expensive operations disabled by default
        extract_relationships=False,
        detect_evolution=False,
        resolve_coreferences=False,
        resolve_temporal=False,
    )


@pytest.fixture
def operation_context() -> Any:
    """Standard OperationContext for permission tests."""
    from nexus.core.permissions import OperationContext

    return OperationContext(
        user_id="test_user",
        agent_id="test_agent",
        groups=["test_group"],
        is_admin=False,
    )


@pytest.fixture
def admin_context() -> Any:
    """Admin OperationContext for privileged tests."""
    from nexus.core.permissions import OperationContext

    return OperationContext(
        user_id="admin",
        agent_id="admin_agent",
        groups=["admin"],
        is_admin=True,
    )


# ============================================================================
# Event Loop Fixtures (for async tests)
# ============================================================================


@pytest.fixture(scope="session")
def event_loop() -> Any:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# ============================================================================
# Cleanup Utilities
# ============================================================================


@pytest.fixture(autouse=True)
def cleanup_test_data() -> Any:
    """Auto-cleanup fixture that runs after each test.

    Add cleanup logic here that should run after every test.
    """
    yield
    # Cleanup logic goes here
    pass
