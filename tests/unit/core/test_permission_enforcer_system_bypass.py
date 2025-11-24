"""Tests for PermissionEnforcer system bypass scoping (P0-4).

System bypass is limited in scope for security:
- READ: Allowed on any path (for auto-parse, indexing, etc.)
- WRITE/DELETE: Only allowed on /system/* paths
- EXECUTE: Only allowed on /system/* paths

This prevents system operations from accidentally modifying user data.
"""

import pytest

from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer


class MockReBACManager:
    """Mock ReBAC manager."""

    def __init__(self):
        self.checks = []

    def rebac_check(self, subject, permission, object, tenant_id):
        self.checks.append({"subject": subject, "permission": permission, "object": object})
        return False  # Always deny (system should bypass)


class MockAuditStore:
    """Mock audit store."""

    def __init__(self):
        self.entries = []

    def log_bypass(self, entry):
        self.entries.append(entry)


class TestSystemBypassReadOperations:
    """Test system bypass for READ operations."""

    def test_system_can_read_any_path(self):
        """System context can READ any path (for auto-parse, indexing, etc.)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Should allow READ on any path
        assert enforcer.check("/workspace/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/mnt/gcs/data.csv", Permission.READ, ctx) is True
        assert enforcer.check("/user/alice/private.txt", Permission.READ, ctx) is True
        assert enforcer.check("/system/config.yaml", Permission.READ, ctx) is True

    def test_system_read_bypasses_rebac(self):
        """System READ operations should bypass ReBAC checks."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="system", groups=[], is_system=True)

        assert enforcer.check("/any/file.txt", Permission.READ, ctx) is True

        # ReBAC should not be called (bypassed)
        assert len(rebac.checks) == 0


class TestSystemBypassWriteOperations:
    """Test system bypass for WRITE operations (scoped to /system/*)."""

    def test_system_can_write_to_system_paths(self):
        """System can WRITE to /system/* paths."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Should allow WRITE to /system/* paths
        assert enforcer.check("/system/config.yaml", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/cache/data.bin", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/logs/app.log", Permission.WRITE, ctx) is True

    def test_system_cannot_write_to_non_system_paths(self):
        """System CANNOT WRITE to non-/system/* paths (security)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Should deny WRITE to non-system paths
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/workspace/file.txt", Permission.WRITE, ctx)

        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/user/alice/data.csv", Permission.WRITE, ctx)

        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/mnt/gcs/file.txt", Permission.WRITE, ctx)

    def test_system_write_scope_is_strict(self):
        """System WRITE scope check is strict (/system prefix required)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Edge cases
        with pytest.raises(PermissionError):
            # Doesn't start with /system
            enforcer.check("/systemdata/file.txt", Permission.WRITE, ctx)

        with pytest.raises(PermissionError):
            # Root level, not under /system
            enforcer.check("/file.txt", Permission.WRITE, ctx)


class TestSystemBypassExecuteOperations:
    """Test system bypass for EXECUTE operations (scoped to /system/*)."""

    def test_system_can_execute_system_scripts(self):
        """System can EXECUTE scripts in /system/* paths."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        assert enforcer.check("/system/scripts/backup.sh", Permission.EXECUTE, ctx) is True
        assert enforcer.check("/system/bin/indexer", Permission.EXECUTE, ctx) is True

    def test_system_cannot_execute_non_system_scripts(self):
        """System CANNOT EXECUTE scripts outside /system/* (security)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/workspace/script.sh", Permission.EXECUTE, ctx)

        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/user/alice/malicious.py", Permission.EXECUTE, ctx)


class TestSystemBypassKillSwitch:
    """Test the allow_system_bypass kill-switch."""

    def test_system_bypass_enabled_by_default(self):
        """System bypass should be enabled by default (for internal operations)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())

        # Default should be True (unlike admin bypass)
        assert enforcer.allow_system_bypass is True

    def test_system_bypass_kill_switch_disabled(self):
        """When kill-switch is OFF, all system operations should fail."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), allow_system_bypass=False)
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # All operations should fail
        with pytest.raises(PermissionError, match="System bypass disabled"):
            enforcer.check("/any/file.txt", Permission.READ, ctx)

        with pytest.raises(PermissionError, match="System bypass disabled"):
            enforcer.check("/system/config.yaml", Permission.WRITE, ctx)

    def test_system_bypass_kill_switch_enabled(self):
        """When kill-switch is ON, system operations work normally."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), allow_system_bypass=True)
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # READ on any path should work
        assert enforcer.check("/any/file.txt", Permission.READ, ctx) is True

        # WRITE on /system/* should work
        assert enforcer.check("/system/config.yaml", Permission.WRITE, ctx) is True


class TestSystemBypassAuditLogging:
    """Test audit logging for system bypass operations."""

    def test_system_bypass_success_logged(self):
        """Successful system bypass should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), audit_store=audit_store)

        ctx = OperationContext(user="system", groups=[], is_system=True, request_id="sys-req-123")

        enforcer.check("/workspace/file.txt", Permission.READ, ctx)

        # Should have audit log entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.user == "system"
        assert entry.bypass_type == "system"
        assert entry.allowed is True
        assert entry.request_id == "sys-req-123"

    def test_system_bypass_denied_logged(self):
        """Denied system bypass should be logged with reason."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_system_bypass=False,
            audit_store=audit_store,
        )

        ctx = OperationContext(user="system", groups=[], is_system=True)

        with pytest.raises(PermissionError):
            enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit log entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "kill_switch_disabled"

    def test_system_bypass_scope_limit_logged(self):
        """System bypass denied due to scope limit should be logged."""
        audit_store = MockAuditStore()
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), audit_store=audit_store)

        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Try to write to non-system path
        with pytest.raises(PermissionError, match="System bypass not allowed"):
            enforcer.check("/workspace/file.txt", Permission.WRITE, ctx)

        # Should have audit log entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "scope_limit"


class TestSystemVsAdminBypass:
    """Test interaction between system and admin bypass."""

    def test_system_bypass_takes_priority_over_admin(self):
        """System bypass should be checked before admin bypass."""
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            allow_admin_bypass=True,
            allow_system_bypass=True,
        )

        # User is both system and admin
        ctx = OperationContext(
            user="system_admin",
            groups=["admins"],
            is_system=True,  # System flag set
            is_admin=True,  # Admin flag also set
            admin_capabilities=set(),  # No admin capabilities
        )

        # Should use system bypass (not admin)
        # System can READ any path, even without admin capabilities
        assert enforcer.check("/any/file.txt", Permission.READ, ctx) is True

    def test_admin_without_system_flag_uses_admin_bypass(self):
        """Admin without is_system flag should use admin bypass rules."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager(), allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            is_system=False,  # Not system
            admin_capabilities={"admin:read:*"},
        )

        # Should use admin bypass (requires capabilities)
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True


class TestSystemBypassEdgeCases:
    """Test edge cases for system bypass."""

    def test_system_bypass_with_root_path(self):
        """Test system bypass behavior with root path."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # READ on root should work
        assert enforcer.check("/", Permission.READ, ctx) is True

        # WRITE/EXECUTE on root should fail (not /system/*)
        with pytest.raises(PermissionError):
            enforcer.check("/", Permission.WRITE, ctx)

    def test_system_bypass_with_system_path_variations(self):
        """Test various /system/* path variations."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # All should work
        assert enforcer.check("/system/file.txt", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/sub/file.txt", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/deep/nested/path/file.txt", Permission.WRITE, ctx) is True

    def test_system_bypass_without_audit_store_works(self):
        """System bypass should work without audit store."""
        enforcer = PermissionEnforcer(
            rebac_manager=MockReBACManager(),
            audit_store=None,  # No audit
        )
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Should not raise error
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/system/config.yaml", Permission.WRITE, ctx) is True
