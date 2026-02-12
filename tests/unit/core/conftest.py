"""Shared fixtures for tests/unit/core/ subsystem tests.

Issue #1287: Extract NexusFS Domain Services from God Object.

Provides standard fixtures used across all subsystem test modules:
- operation_context: Pre-built OperationContext for test assertions
- mock_metadata_store: Mock FileMetadataProtocol
- mock_session_factory: Mock SQLAlchemy session factory
- mock_permission_enforcer: Mock PermissionEnforcer
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.core.permissions import OperationContext


@pytest.fixture
def operation_context() -> OperationContext:
    """Standard OperationContext for subsystem tests."""
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_admin=False,
    )


@pytest.fixture
def admin_context() -> OperationContext:
    """Admin OperationContext for subsystem tests."""
    return OperationContext(
        user="admin_user",
        groups=["admins"],
        zone_id="test_zone",
        is_admin=True,
    )


@pytest.fixture
def mock_metadata_store() -> MagicMock:
    """Mock FileMetadataProtocol for subsystem tests.

    Provides a MagicMock with commonly accessed attributes pre-configured.
    """
    store = MagicMock()
    store.engine = MagicMock()
    store.engine.url = "sqlite:///test.db"
    return store


@pytest.fixture
def mock_session_factory() -> MagicMock:
    """Mock SQLAlchemy session factory for subsystem tests.

    Returns a factory (callable) that produces mock sessions with
    commit/rollback/close and context-manager support.
    """
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    factory = MagicMock(return_value=session)
    return factory


@pytest.fixture
def mock_permission_enforcer() -> MagicMock:
    """Mock PermissionEnforcer for subsystem tests.

    Pre-configures check_permission() to return True (allow all) by default.
    Tests can override: ``mock_permission_enforcer.check_permission.return_value = False``
    """
    enforcer = MagicMock()
    enforcer.check_permission.return_value = True
    return enforcer
