"""Tests for AsyncPermissionEnforcer.

These tests verify async permission enforcement functionality.
"""

import pytest

from nexus.core.async_permissions import AsyncPermissionEnforcer
from nexus.core.permissions import OperationContext, Permission


class TestAsyncPermissionEnforcer:
    """Test AsyncPermissionEnforcer functionality."""

    def test_init_without_rebac(self) -> None:
        """Test enforcer initialization without ReBAC manager."""
        enforcer = AsyncPermissionEnforcer()
        assert enforcer.rebac_manager is None
        assert enforcer.backends == {}

    def test_init_with_backends(self) -> None:
        """Test enforcer initialization with backends."""
        backends = {"mnt": object()}
        enforcer = AsyncPermissionEnforcer(backends=backends)
        assert enforcer.backends == backends

    @pytest.mark.asyncio
    async def test_check_permission_system_bypass(self) -> None:
        """Test that system context bypasses all checks."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="system", groups=[], is_system=True)

        result = await enforcer.check_permission("/test.txt", Permission.READ, context)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_permission_admin_bypass(self) -> None:
        """Test that admin context bypasses all checks."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="admin", groups=[], is_admin=True)

        result = await enforcer.check_permission("/test.txt", Permission.WRITE, context)
        assert result is True

    @pytest.mark.asyncio
    async def test_check_permission_no_rebac_permissive(self) -> None:
        """Test that without ReBAC manager, permissions are permissive."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="alice", groups=[])

        result = await enforcer.check_permission("/test.txt", Permission.READ, context)
        assert result is True

    @pytest.mark.asyncio
    async def test_filter_paths_empty(self) -> None:
        """Test filtering empty path list."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="alice", groups=[])

        result = await enforcer.filter_paths_by_permission([], context)
        assert result == []

    @pytest.mark.asyncio
    async def test_filter_paths_system_bypass(self) -> None:
        """Test that system context gets all paths."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="system", groups=[], is_system=True)
        paths = ["/a.txt", "/b.txt", "/c.txt"]

        result = await enforcer.filter_paths_by_permission(paths, context)
        assert result == paths

    @pytest.mark.asyncio
    async def test_filter_paths_admin_bypass(self) -> None:
        """Test that admin context gets all paths."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="admin", groups=[], is_admin=True)
        paths = ["/a.txt", "/b.txt", "/c.txt"]

        result = await enforcer.filter_paths_by_permission(paths, context)
        assert result == paths

    @pytest.mark.asyncio
    async def test_filter_paths_no_rebac_permissive(self) -> None:
        """Test that without ReBAC manager, all paths are returned."""
        enforcer = AsyncPermissionEnforcer()
        context = OperationContext(user="alice", groups=[])
        paths = ["/a.txt", "/b.txt", "/c.txt"]

        result = await enforcer.filter_paths_by_permission(paths, context)
        assert result == paths

    def test_get_object_type_default(self) -> None:
        """Test default object type."""
        enforcer = AsyncPermissionEnforcer()
        assert enforcer._get_object_type("/any/path.txt") == "file"

    def test_permission_to_name_read(self) -> None:
        """Test permission name conversion for READ."""
        enforcer = AsyncPermissionEnforcer()
        assert enforcer._permission_to_name(Permission.READ) == "read"

    def test_permission_to_name_write(self) -> None:
        """Test permission name conversion for WRITE."""
        enforcer = AsyncPermissionEnforcer()
        assert enforcer._permission_to_name(Permission.WRITE) == "write"

    def test_permission_to_name_execute(self) -> None:
        """Test permission name conversion for EXECUTE."""
        enforcer = AsyncPermissionEnforcer()
        assert enforcer._permission_to_name(Permission.EXECUTE) == "execute"
