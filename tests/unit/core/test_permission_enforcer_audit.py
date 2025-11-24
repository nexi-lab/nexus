"""Tests for PermissionEnforcer audit logging (P0-4).

P0-4 adds comprehensive audit logging for all admin and system bypass operations.
Audit entries include:
- timestamp: When the bypass occurred
- request_id: For correlation across services
- user: Who performed the operation
- tenant_id: Multi-tenant isolation
- path: What was accessed
- permission: What operation (read/write/execute)
- bypass_type: "admin" or "system"
- allowed: True if bypass succeeded, False if denied
- capabilities: Admin capabilities at time of operation
- denial_reason: Why bypass was denied (if applicable)
"""

from datetime import datetime

import pytest

from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer


class MockReBACManager:
    """Mock ReBAC manager."""

    def rebac_check(self, subject, permission, object, tenant_id):
        return False  # Always deny


class MockAuditStore:
    """Mock audit store that captures all audit entries."""

    def __init__(self):
        self.entries = []

    def log_bypass(self, entry):
        """Log a bypass attempt."""
        self.entries.append(entry)


class TestAdminBypassAuditLogging:
    """Test audit logging for admin bypass operations."""

    def test_admin_bypass_success_creates_audit_entry(self):
        """Successful admin bypass should create audit log entry."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
            request_id="req-123",
            tenant_id="org_acme",
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have one audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.user == "admin"
        assert entry.tenant_id == "org_acme"
        assert entry.path == "/file.txt"
        assert entry.permission == "read"
        assert entry.bypass_type == "admin"
        assert entry.allowed is True
        assert entry.request_id == "req-123"
        assert "admin:read:*" in entry.capabilities

    def test_admin_bypass_denied_kill_switch_logged(self):
        """Admin bypass denied due to kill-switch should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=False,  # Kill-switch OFF
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Will fall through to ReBAC (which denies)
        enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit entry for denied bypass
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "kill_switch_disabled"
        assert entry.bypass_type == "admin"

    def test_admin_bypass_denied_missing_capability_logged(self):
        """Admin bypass denied due to missing capability should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities=set(),  # No capabilities
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert "missing_capability_admin:read:*" in entry.denial_reason
        assert len(entry.capabilities) == 0  # Empty capabilities

    def test_admin_bypass_denied_path_not_in_allowlist_logged(self):
        """Admin bypass denied due to path allowlist should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            admin_bypass_paths=["/admin/*"],  # Only /admin/* allowed
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Try to access path outside allowlist
        enforcer.check("/workspace/file.txt", Permission.READ, ctx)

        # Should have audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "path_not_in_allowlist"
        assert entry.path == "/workspace/file.txt"

    def test_admin_bypass_audit_includes_all_capabilities(self):
        """Audit entry should include all admin capabilities."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*", "admin:delete:*"},
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        entry = audit_store.entries[0]
        # All capabilities should be in audit log
        assert "admin:read:*" in entry.capabilities
        assert "admin:write:*" in entry.capabilities
        assert "admin:delete:*" in entry.capabilities


class TestSystemBypassAuditLogging:
    """Test audit logging for system bypass operations."""

    def test_system_bypass_success_creates_audit_entry(self):
        """Successful system bypass should create audit log entry."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), audit_store=audit_store)

        ctx = OperationContext(
            user="system",
            groups=[],
            is_system=True,
            request_id="sys-req-456",
            tenant_id="default",
        )

        enforcer.check("/workspace/file.txt", Permission.READ, ctx)

        # Should have audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.user == "system"
        assert entry.bypass_type == "system"
        assert entry.allowed is True
        assert entry.request_id == "sys-req-456"
        assert entry.permission == "read"

    def test_system_bypass_denied_kill_switch_logged(self):
        """System bypass denied due to kill-switch should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_system_bypass=False,  # Kill-switch OFF
            audit_store=audit_store,
        )

        ctx = OperationContext(user="system", groups=[], is_system=True)

        with pytest.raises(PermissionError):
            enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "kill_switch_disabled"
        assert entry.bypass_type == "system"

    def test_system_bypass_denied_scope_limit_logged(self):
        """System bypass denied due to scope limit should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), audit_store=audit_store)

        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Try to WRITE to non-system path (scope violation)
        with pytest.raises(PermissionError):
            enforcer.check("/workspace/file.txt", Permission.WRITE, ctx)

        # Should have audit entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "scope_limit"
        assert entry.permission == "write"
        assert entry.path == "/workspace/file.txt"


class TestAuditLoggingWithoutAuditStore:
    """Test that operations work when audit store is None."""

    def test_admin_bypass_without_audit_store_works(self):
        """Admin bypass should work even without audit store."""
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=None,  # No audit store
        )

        ctx = OperationContext(
            user="admin", groups=[], is_admin=True, admin_capabilities={"admin:read:*"}
        )

        # Should not raise error
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True

    def test_system_bypass_without_audit_store_works(self):
        """System bypass should work without audit store."""
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            audit_store=None,  # No audit
        )

        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Should not raise error
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True

    def test_denied_bypass_without_audit_store_works(self):
        """Denied bypass should work without audit store."""
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=False,
            audit_store=None,
        )

        ctx = OperationContext(
            user="admin", groups=[], is_admin=True, admin_capabilities={"admin:read:*"}
        )

        # Should fall through to ReBAC without error
        result = enforcer.check("/file.txt", Permission.READ, ctx)
        assert result is False  # ReBAC denies


class TestAuditEntryStructure:
    """Test the structure and content of audit log entries."""

    def test_audit_entry_has_timestamp(self):
        """Audit entries should include timestamp."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin", groups=[], is_admin=True, admin_capabilities={"admin:read:*"}
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        entry = audit_store.entries[0]
        # Timestamp should be present and valid ISO format
        assert hasattr(entry, "timestamp")
        assert entry.timestamp is not None

        # Should be valid ISO timestamp
        try:
            datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00"))
        except ValueError:
            pytest.fail(f"Invalid timestamp format: {entry.timestamp}")

    def test_audit_entry_correlates_with_request_id(self):
        """Audit entries should preserve request_id for correlation."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        request_id = "correlation-id-abc-123"
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
            request_id=request_id,
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        entry = audit_store.entries[0]
        assert entry.request_id == request_id

    def test_audit_entry_tenant_isolation(self):
        """Audit entries should include tenant_id for multi-tenant isolation."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
            tenant_id="tenant_alpha",
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        entry = audit_store.entries[0]
        assert entry.tenant_id == "tenant_alpha"


class TestMultipleBypassAttempts:
    """Test audit logging for multiple bypass attempts."""

    def test_multiple_bypass_attempts_all_logged(self):
        """All bypass attempts should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin", groups=[], is_admin=True, admin_capabilities={"admin:read:*"}
        )

        # Multiple operations
        enforcer.check("/file1.txt", Permission.READ, ctx)
        enforcer.check("/file2.txt", Permission.READ, ctx)
        enforcer.check("/file3.txt", Permission.READ, ctx)

        # All should be logged
        assert len(audit_store.entries) == 3

        # Each should have unique path
        paths = [entry.path for entry in audit_store.entries]
        assert "/file1.txt" in paths
        assert "/file2.txt" in paths
        assert "/file3.txt" in paths

    def test_mixed_success_and_failure_both_logged(self):
        """Both successful and failed bypasses should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            audit_store=audit_store,
        )

        # Context with read capability only
        ctx = OperationContext(
            user="admin", groups=[], is_admin=True, admin_capabilities={"admin:read:*"}
        )

        # Success: READ with read capability
        enforcer.check("/file.txt", Permission.READ, ctx)

        # Failure: WRITE without write capability
        enforcer.check("/file.txt", Permission.WRITE, ctx)

        # Both should be logged
        assert len(audit_store.entries) == 2

        # First should succeed
        assert audit_store.entries[0].allowed is True
        assert audit_store.entries[0].permission == "read"

        # Second should fail
        assert audit_store.entries[1].allowed is False
        assert audit_store.entries[1].permission == "write"
