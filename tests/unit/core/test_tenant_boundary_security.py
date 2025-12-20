"""Unit tests for tenant boundary security (Issue #819).

Tests that admins with admin:read:* cannot access files from other tenants
unless they have MANAGE_TENANTS capability (system admin only).
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext
from nexus.core.permissions_enhanced import AdminCapability


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with permissions enforced."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


class TestTenantBoundarySecurity:
    """Test tenant boundary security for admin bypass."""

    def test_tenant_admin_cannot_access_other_tenant_files(self, nx: NexusFS) -> None:
        """Test that tenant admin cannot access files from other tenants."""
        # Setup: Create file in tenant1 as system admin
        system_admin = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            admin_capabilities={AdminCapability.READ_ALL, AdminCapability.WRITE_ALL},
        )

        test_file = "/tenant:acme/doc.txt"
        nx.write(test_file, b"secret acme data", context=system_admin)

        # Tenant admin from tenant2 (techcorp) tries to access tenant1 (acme) file
        tenant_admin_techcorp = OperationContext(
            user="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id="techcorp",  # Alice is admin of techcorp, not acme
            admin_capabilities={
                AdminCapability.READ_ALL,  # Has wildcard read, but NOT MANAGE_TENANTS
                AdminCapability.WRITE_ALL,
            },
        )

        # Should be denied - cross-tenant access without MANAGE_TENANTS
        with pytest.raises(PermissionError, match="Permission denied"):
            nx.read(test_file, context=tenant_admin_techcorp)

    def test_system_admin_can_access_any_tenant(self, nx: NexusFS) -> None:
        """Test that system admin with MANAGE_TENANTS can access any tenant."""
        # Setup: Create file in tenant1
        system_admin_no_tenant = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
                AdminCapability.MANAGE_TENANTS,  # System admin capability
            },
        )

        test_file = "/tenant:acme/doc.txt"
        nx.write(test_file, b"secret acme data", context=system_admin_no_tenant)

        # System admin from tenant2 should be able to access tenant1 file
        system_admin_tenant2 = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id="techcorp",  # Different tenant
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.MANAGE_TENANTS,  # System admin capability
            },
        )

        # Should succeed - system admin with MANAGE_TENANTS
        content = nx.read(test_file, context=system_admin_tenant2)
        assert content == b"secret acme data"

    def test_tenant_admin_can_access_own_tenant(self, nx: NexusFS) -> None:
        """Test that tenant admin can access files in their own tenant."""
        # Setup: Create file in tenant1
        system_admin = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            admin_capabilities={AdminCapability.READ_ALL, AdminCapability.WRITE_ALL},
        )

        test_file = "/tenant:acme/doc.txt"
        nx.write(test_file, b"acme data", context=system_admin)

        # Tenant admin from same tenant should be able to access
        tenant_admin_acme = OperationContext(
            user="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id="acme",  # Same tenant
            admin_capabilities={
                AdminCapability.READ_ALL,
                AdminCapability.WRITE_ALL,
            },
        )

        # Should succeed - same tenant
        content = nx.read(test_file, context=tenant_admin_acme)
        assert content == b"acme data"

    def test_cross_tenant_write_denied(self, nx: NexusFS) -> None:
        """Test that tenant admin cannot write to other tenant's files."""
        # Setup: Create file in tenant1
        system_admin = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            admin_capabilities={AdminCapability.READ_ALL, AdminCapability.WRITE_ALL},
        )

        test_file = "/tenant:acme/doc.txt"
        nx.write(test_file, b"original", context=system_admin)

        # Tenant admin from tenant2 tries to write to tenant1 file
        tenant_admin_techcorp = OperationContext(
            user="alice",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id="techcorp",  # Different tenant
            admin_capabilities={
                AdminCapability.WRITE_ALL,  # Has wildcard write, but NOT MANAGE_TENANTS
            },
        )

        # Should be denied - cross-tenant write without MANAGE_TENANTS
        with pytest.raises(PermissionError, match="Permission denied"):
            nx.write(test_file, b"hacked!", context=tenant_admin_techcorp)

    def test_system_admin_without_manage_tenants_denied(self, nx: NexusFS) -> None:
        """Test that admin without MANAGE_TENANTS cannot access other tenants."""
        # Setup: Create file in tenant1
        system_admin = OperationContext(
            user="system_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            admin_capabilities={AdminCapability.READ_ALL, AdminCapability.WRITE_ALL},
        )

        test_file = "/tenant:acme/doc.txt"
        nx.write(test_file, b"secret", context=system_admin)

        # Admin without MANAGE_TENANTS tries to access different tenant
        limited_admin = OperationContext(
            user="limited_admin",
            groups=[],
            is_admin=True,
            is_system=False,
            tenant_id="techcorp",  # Different tenant
            admin_capabilities={
                AdminCapability.READ_ALL,  # Has wildcard read
                # But NOT MANAGE_TENANTS
            },
        )

        # Should be denied
        with pytest.raises(PermissionError, match="Permission denied"):
            nx.read(test_file, context=limited_admin)
