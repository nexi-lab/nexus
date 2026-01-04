"""Shared fixtures for service layer tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.permissions import OperationContext


@pytest.fixture
def operation_context():
    """Create a standard operation context for tests.

    Returns:
        OperationContext with test user, group, and tenant
    """
    return OperationContext(
        user="test_user",
        groups=["test_group"],
        tenant_id="test_tenant",
        is_system=False,
        is_admin=False,
    )


@pytest.fixture
def system_context():
    """Create a system operation context (bypasses all permissions).

    Returns:
        OperationContext with system privileges
    """
    return OperationContext(
        user="system",
        groups=["system"],
        tenant_id="system",
        is_system=True,
        is_admin=True,
    )


@pytest.fixture
def admin_context():
    """Create an admin operation context.

    Returns:
        OperationContext with admin privileges
    """
    return OperationContext(
        user="admin_user",
        groups=["admin"],
        tenant_id="test_tenant",
        is_system=False,
        is_admin=True,
    )


@pytest.fixture
def mock_metadata_store():
    """Create a mock metadata store for testing.

    Returns:
        MagicMock configured for metadata store operations
    """
    mock = MagicMock()
    mock.get_file_metadata.return_value = {
        "path": "/test.txt",
        "size": 1024,
        "version": 1,
        "etag": "abc123",
        "created_at": "2026-01-01T00:00:00",
    }
    mock.list_versions.return_value = [
        {
            "version": 1,
            "etag": "abc123",
            "size": 1024,
            "created_at": "2026-01-01T00:00:00",
            "created_by": "test_user",
        }
    ]
    return mock


@pytest.fixture
def mock_cas_store():
    """Create a mock CAS store for testing.

    Returns:
        MagicMock configured for CAS operations
    """
    mock = MagicMock()
    mock.get.return_value = b"test content"
    mock.put.return_value = "abc123"  # etag/hash
    mock.exists.return_value = True
    return mock


@pytest.fixture
def mock_permission_enforcer():
    """Create a mock permission enforcer (permissive by default).

    Returns:
        AsyncMock that allows all operations by default
    """
    mock = AsyncMock()
    mock.check_permission.return_value = True
    mock.filter_paths_by_permission.side_effect = lambda paths, ctx: paths
    return mock


@pytest.fixture
def mock_router():
    """Create a mock path router for testing.

    Returns:
        MagicMock configured for path routing operations
    """
    mock = MagicMock()
    mock.resolve_backend.return_value = MagicMock()
    mock.get_mount_point.return_value = "/mnt/test"
    return mock


@pytest.fixture
def mock_rebac_manager():
    """Create a mock ReBAC manager for testing.

    Returns:
        AsyncMock configured for ReBAC operations
    """
    mock = AsyncMock()
    mock.rebac_check.return_value = True
    mock.rebac_create.return_value = None
    mock.rebac_delete.return_value = None
    return mock
