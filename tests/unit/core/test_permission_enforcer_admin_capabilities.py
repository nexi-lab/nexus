"""Tests for PermissionEnforcer admin capability-based bypass (P0-4).

This module tests the scoped admin bypass feature where admin users
must have specific capabilities to bypass permission checks.

P0-4 Security Features:
- Kill-switch: allow_admin_bypass=False by default (production security)
- Capability-based: Admin must have specific capabilities (e.g., admin:read:*)
- Path-scoped: Optional allowlist limits bypass to specific paths
- Audit logging: All bypass attempts logged (allowed and denied)

Admin bypass flow:
1. Check allow_admin_bypass flag (kill-switch)
2. Check path allowlist (if configured)
3. Check required capability for operation
4. Log bypass attempt (success or failure)
5. Grant or deny based on above checks
"""

from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer


class MockReBACManager:
    """Mock ReBAC manager for testing."""

    def __init__(self):
        self.checks = []
        self.granted_permissions = {}

    def grant(self, subject, permission, object_id):
        """Grant permission for testing."""
        key = (subject, permission, object_id)
        self.granted_permissions[key] = True

    def rebac_check(self, subject, permission, object, tenant_id):
        """Check if permission was explicitly granted."""
        object_type, object_id = object
        key = (subject, permission, object_id)
        self.checks.append(
            {
                "subject": subject,
                "permission": permission,
                "object": object,
                "tenant_id": tenant_id,
            }
        )
        return self.granted_permissions.get(key, False)


class MockAuditStore:
    """Mock audit store for testing bypass logging."""

    def __init__(self):
        self.entries = []

    def log_bypass(self, entry):
        """Log a bypass attempt."""
        self.entries.append(entry)


class TestAdminBypassRequiresCapabilities:
    """Test that admin bypass requires specific capabilities."""

    def test_admin_with_read_capability_can_bypass(self):
        """Admin with admin:read:* capability can bypass READ permission checks."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should bypass permission check
        assert enforcer.check("/any/file.txt", Permission.READ, ctx) is True

        # ReBAC should NOT be called (bypassed)
        assert len(rebac.checks) == 0

    def test_admin_with_write_capability_can_bypass(self):
        """Admin with admin:write:* capability can bypass WRITE permission checks."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:write:*"},
        )

        # Should bypass permission check
        assert enforcer.check("/any/file.txt", Permission.WRITE, ctx) is True

        # ReBAC should NOT be called
        assert len(rebac.checks) == 0

    def test_admin_without_capability_falls_through_to_rebac(self):
        """Admin without required capability should fall through to ReBAC check."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Admin with no capabilities
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities=set(),  # Empty capabilities
        )

        # Should fall through to ReBAC (which will deny)
        result = enforcer.check("/any/file.txt", Permission.READ, ctx)

        assert result is False, "Should fall through to ReBAC when capability missing"

        # ReBAC should be called
        assert len(rebac.checks) > 0

    def test_admin_with_wrong_capability_falls_through(self):
        """Admin with wrong capability (e.g., write when need read) falls through."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Admin with write capability trying to read
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:write:*"},  # Has write, needs read
        )

        # Should fall through to ReBAC
        result = enforcer.check("/any/file.txt", Permission.READ, ctx)

        assert result is False
        assert len(rebac.checks) > 0

    def test_admin_with_multiple_capabilities(self):
        """Admin can have multiple capabilities."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*", "admin:delete:*"},
        )

        # Should bypass all permission types
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is True

        # No ReBAC checks
        assert len(rebac.checks) == 0


class TestAdminBypassKillSwitch:
    """Test the allow_admin_bypass kill-switch (P0-4 security)."""

    def test_admin_bypass_disabled_by_default(self):
        """Kill-switch should be OFF by default (production security)."""
        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())

        # Default should be False
        assert enforcer.allow_admin_bypass is False

    def test_admin_bypass_kill_switch_disabled(self):
        """When kill-switch is OFF, admin falls through to ReBAC even with capabilities."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=False)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*"},  # Has all capabilities
        )

        # Should still fall through to ReBAC (kill-switch OFF)
        result = enforcer.check("/file.txt", Permission.READ, ctx)

        assert result is False, "Admin should not bypass when kill-switch is OFF"

        # ReBAC should be called
        assert len(rebac.checks) > 0

    def test_admin_bypass_kill_switch_enabled(self):
        """When kill-switch is ON, admin with capabilities can bypass."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should bypass
        result = enforcer.check("/file.txt", Permission.READ, ctx)

        assert result is True, "Admin should bypass when kill-switch is ON"
        assert len(rebac.checks) == 0


class TestAdminBypassPathAllowlist:
    """Test path-based allowlist for scoped admin bypass."""

    def test_admin_bypass_path_allowlist_match(self):
        """Admin bypass allowed for paths matching allowlist."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=True,
            admin_bypass_paths=["/admin/*", "/system/*"],
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Paths in allowlist should bypass
        assert enforcer.check("/admin/settings.json", Permission.READ, ctx) is True
        assert enforcer.check("/system/config.yaml", Permission.READ, ctx) is True

        # ReBAC not called (bypassed)
        assert len(rebac.checks) == 0

    def test_admin_bypass_path_allowlist_no_match(self):
        """Admin bypass denied for paths NOT in allowlist."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=True,
            admin_bypass_paths=["/admin/*"],  # Only /admin/* allowed
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Path outside allowlist should fall through to ReBAC
        result = enforcer.check("/workspace/file.txt", Permission.READ, ctx)

        assert result is False, "Should deny bypass for path outside allowlist"

        # ReBAC should be called
        assert len(rebac.checks) > 0

    def test_admin_bypass_empty_allowlist_allows_all(self):
        """Empty allowlist means no path restrictions (all paths allowed)."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=True,
            admin_bypass_paths=[],  # Empty = no restrictions
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Any path should be allowed
        assert enforcer.check("/any/path.txt", Permission.READ, ctx) is True
        assert enforcer.check("/another/path.txt", Permission.READ, ctx) is True

    def test_admin_bypass_allowlist_with_wildcards(self):
        """Test allowlist with various wildcard patterns."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=True,
            admin_bypass_paths=[
                "/admin/*",  # All under /admin
                "/workspace/shared/*",  # All under /workspace/shared
                "/config.json",  # Exact match
            ],
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should match patterns
        assert enforcer.check("/admin/users.db", Permission.READ, ctx) is True
        assert enforcer.check("/workspace/shared/data.csv", Permission.READ, ctx) is True
        assert enforcer.check("/config.json", Permission.READ, ctx) is True

        # Should not match
        rebac.checks.clear()
        result = enforcer.check("/workspace/private/secret.txt", Permission.READ, ctx)
        assert result is False
        assert len(rebac.checks) > 0  # Fell through to ReBAC


class TestAdminBypassWithReBAC:
    """Test interaction between admin bypass and ReBAC permissions."""

    def test_admin_without_capabilities_uses_rebac_permissions(self):
        """Admin without bypass capabilities should work like normal user with ReBAC."""
        rebac = MockReBACManager()

        # Grant ReBAC permission to admin as user
        rebac.grant(("user", "admin"), "read", "/workspace/file.txt")

        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Admin without capabilities
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities=set(),  # No capabilities
        )

        # Should use ReBAC permissions
        assert enforcer.check("/workspace/file.txt", Permission.READ, ctx) is True

        # Different file should be denied
        assert enforcer.check("/workspace/other.txt", Permission.READ, ctx) is False

    def test_admin_bypass_has_priority_over_rebac(self):
        """When admin has capability, bypass takes priority over ReBAC."""
        rebac = MockReBACManager()

        # Do NOT grant ReBAC permission
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should bypass (no ReBAC permission needed)
        assert enforcer.check("/any/file.txt", Permission.READ, ctx) is True

        # ReBAC not checked
        assert len(rebac.checks) == 0


class TestAdminBypassAuditLogging:
    """Test audit logging for admin bypass attempts."""

    def test_admin_bypass_success_logged(self):
        """Successful admin bypass should be logged."""
        rebac = MockReBACManager()
        audit_store = MockAuditStore()

        enforcer = PermissionEnforcer(
            rebac_manager=rebac, allow_admin_bypass=True, audit_store=audit_store
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
            request_id="test-request-123",
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit log entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.user == "admin"
        assert entry.path == "/file.txt"
        assert entry.permission == "read"
        assert entry.bypass_type == "admin"
        assert entry.allowed is True
        assert entry.request_id == "test-request-123"
        assert "admin:read:*" in entry.capabilities

    def test_admin_bypass_denied_logged_with_reason(self):
        """Failed admin bypass should be logged with denial reason."""
        rebac = MockReBACManager()
        audit_store = MockAuditStore()

        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=False,  # Kill-switch OFF
            audit_store=audit_store,
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        enforcer.check("/file.txt", Permission.READ, ctx)

        # Should have audit log entry
        assert len(audit_store.entries) == 1

        entry = audit_store.entries[0]
        assert entry.allowed is False
        assert entry.denial_reason == "kill_switch_disabled"

    def test_admin_bypass_without_audit_store_works(self):
        """Admin bypass should work even without audit store."""
        rebac = MockReBACManager()

        enforcer = PermissionEnforcer(
            rebac_manager=rebac,
            allow_admin_bypass=True,
            audit_store=None,  # No audit store
        )

        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should not raise error
        assert enforcer.check("/file.txt", Permission.READ, ctx) is True


class TestCapabilityScoping:
    """Test that different operations require different capabilities."""

    def test_read_requires_admin_read_capability(self):
        """READ operations require admin:read:* capability."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Only has write capability
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:write:*"},
        )

        # Should not bypass READ (wrong capability)
        result = enforcer.check("/file.txt", Permission.READ, ctx)
        assert result is False

    def test_write_requires_admin_write_capability(self):
        """WRITE operations require admin:write:* capability."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Only has read capability
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should not bypass WRITE (wrong capability)
        result = enforcer.check("/file.txt", Permission.WRITE, ctx)
        assert result is False

    def test_execute_requires_admin_execute_capability(self):
        """EXECUTE operations require admin:execute:* capability."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, allow_admin_bypass=True)

        # Only has read capability
        ctx = OperationContext(
            user="admin",
            groups=["admins"],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        # Should not bypass EXECUTE
        result = enforcer.check("/script.sh", Permission.EXECUTE, ctx)
        assert result is False
