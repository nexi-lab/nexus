"""Tests for PermissionEnforcer and OperationContext classes."""

import pytest

from nexus.core.permissions import (
    OperationContext,
    Permission,
    PermissionEnforcer,
)


class TestOperationContext:
    """Tests for OperationContext dataclass."""

    def test_create_regular_user_context(self):
        """Test creating a regular user context."""
        ctx = OperationContext(user="alice", groups=["developers"])
        assert ctx.user == "alice"
        assert ctx.groups == ["developers"]
        assert ctx.is_admin is False
        assert ctx.is_system is False
        assert ctx.subject_type == "user"
        assert ctx.subject_id == "alice"

    def test_create_admin_context(self):
        """Test creating an admin context."""
        ctx = OperationContext(user="admin", groups=["admins"], is_admin=True)
        assert ctx.user == "admin"
        assert ctx.groups == ["admins"]
        assert ctx.is_admin is True
        assert ctx.is_system is False

    def test_create_system_context(self):
        """Test creating a system context."""
        ctx = OperationContext(user="system", groups=[], is_system=True)
        assert ctx.user == "system"
        assert ctx.groups == []
        assert ctx.is_admin is False
        assert ctx.is_system is True

    def test_create_agent_context(self):
        """Test creating an AI agent context."""
        ctx = OperationContext(
            user="claude", groups=["ai_agents"], subject_type="agent", subject_id="claude_001"
        )
        assert ctx.subject_type == "agent"
        assert ctx.subject_id == "claude_001"
        assert ctx.get_subject() == ("agent", "claude_001")

    def test_create_service_context(self):
        """Test creating a service context."""
        ctx = OperationContext(
            user="backup", groups=["services"], subject_type="service", subject_id="backup_service"
        )
        assert ctx.subject_type == "service"
        assert ctx.subject_id == "backup_service"
        assert ctx.get_subject() == ("service", "backup_service")

    def test_tenant_id_in_context(self):
        """Test tenant ID in context for multi-tenant isolation."""
        ctx = OperationContext(user="alice", groups=["developers"], tenant_id="org_acme")
        assert ctx.tenant_id == "org_acme"

    def test_requires_user(self):
        """Test that user is required."""
        with pytest.raises(ValueError, match="user is required"):
            OperationContext(user="", groups=[])

    def test_requires_groups_list(self):
        """Test that groups must be a list."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user="alice", groups="developers")  # type: ignore

    def test_empty_groups_allowed(self):
        """Test that empty groups list is allowed."""
        ctx = OperationContext(user="alice", groups=[])
        assert ctx.groups == []

    def test_get_subject_defaults_to_user(self):
        """Test that get_subject() defaults to user when subject_id is None."""
        ctx = OperationContext(user="alice", groups=["developers"])
        assert ctx.get_subject() == ("user", "alice")


class TestPermissionEnforcer:
    """Tests for PermissionEnforcer class with ReBAC-only model."""

    def test_admin_bypass(self):
        """Test that admin users bypass all checks."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*", "admin:write:*", "admin:execute:*"},
        )

        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is True
        assert enforcer.check("/any/path", Permission.EXECUTE, ctx) is True

    def test_system_bypass(self):
        """Test that system operations bypass all checks (scoped to /system/* for write/execute)."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)

        # Read operations allowed on any path
        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        # Write/execute operations only allowed on /system/* paths
        assert enforcer.check("/system/any/path", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/any/path", Permission.EXECUTE, ctx) is True

    def test_no_rebac_manager_denies_all(self):
        """Test that without ReBAC manager, access is denied (secure by default)."""
        enforcer = PermissionEnforcer(metadata_store=None, rebac_manager=None)
        ctx = OperationContext(user="alice", groups=["developers"])

        assert enforcer.check("/any/path", Permission.READ, ctx) is False
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is False
        assert enforcer.check("/any/path", Permission.EXECUTE, ctx) is False

    def test_rebac_check_with_mock_manager(self):
        """Test ReBAC permission checking with mock manager."""

        class MockReBACManager:
            def __init__(self):
                self.checks = []

            def rebac_check(self, subject, permission, object, tenant_id):
                self.checks.append(
                    {
                        "subject": subject,
                        "permission": permission,
                        "object": object,
                        "tenant_id": tenant_id,
                    }
                )
                return subject == ("user", "alice") and permission == "read"

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=["developers"])

        assert enforcer.check("/file.txt", Permission.READ, ctx) is True
        assert enforcer.check("/file.txt", Permission.WRITE, ctx) is False

        # Expect 3 checks due to parent directory inheritance:
        # 1. /file.txt with read (succeeds)
        # 2. /file.txt with write (fails)
        # 3. / (parent) with write (checked for inheritance)
        assert len(rebac.checks) == 3
        assert rebac.checks[0]["permission"] == "read"
        assert rebac.checks[0]["object"] == ("file", "/file.txt")
        assert rebac.checks[1]["permission"] == "write"
        assert rebac.checks[1]["object"] == ("file", "/file.txt")
        assert rebac.checks[2]["permission"] == "write"
        assert rebac.checks[2]["object"] == ("file", "/")

    def test_rebac_check_with_tenant_id(self):
        """Test ReBAC permission checking includes tenant ID."""

        class MockReBACManager:
            def __init__(self):
                self.last_tenant_id = None

            def rebac_check(self, subject, permission, object, tenant_id):
                self.last_tenant_id = tenant_id
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=["developers"], tenant_id="org_acme")

        enforcer.check("/file.txt", Permission.READ, ctx)
        assert rebac.last_tenant_id == "org_acme"

    def test_rebac_check_defaults_tenant_id(self):
        """Test ReBAC permission checking defaults to 'default' tenant."""

        class MockReBACManager:
            def __init__(self):
                self.last_tenant_id = None

            def rebac_check(self, subject, permission, object, tenant_id):
                self.last_tenant_id = tenant_id
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=["developers"])

        enforcer.check("/file.txt", Permission.READ, ctx)
        assert rebac.last_tenant_id == "default"

    def test_filter_list_admin_sees_all(self):
        """Test that admins see all files in filter_list."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user="admin",
            groups=[],
            is_admin=True,
            admin_capabilities={"admin:read:*"},
        )

        paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        assert filtered == paths

    def test_filter_list_system_sees_all(self):
        """Test that system context sees all files in filter_list."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user="system", groups=[], is_system=True)

        paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        assert filtered == paths

    def test_filter_list_filters_by_rebac_permission(self):
        """Test that filter_list removes files user can't read via ReBAC."""

        class MockReBACManager:
            def rebac_check(self, subject, permission, object, tenant_id):
                _, path = object
                if path == "/public.txt" and permission == "read":
                    return True
                if path == "/secret.txt" and permission == "read":
                    return False
                return False

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user="bob", groups=["designers"])

        paths = ["/public.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        assert filtered == ["/public.txt"]

    def test_permission_flags_map_correctly(self):
        """Test that Permission flags map to correct string permissions."""

        class MockReBACManager:
            def __init__(self):
                self.permissions_checked = []

            def rebac_check(self, subject, permission, object, tenant_id):
                self.permissions_checked.append(permission)
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user="alice", groups=["developers"])

        enforcer.check("/file.txt", Permission.READ, ctx)
        enforcer.check("/file.txt", Permission.WRITE, ctx)
        enforcer.check("/file.txt", Permission.EXECUTE, ctx)

        assert rebac.permissions_checked == ["read", "write", "execute"]

    def test_acl_store_deprecated_warning(self):
        """Test that providing acl_store parameter shows deprecation warning."""
        with pytest.warns(DeprecationWarning, match="acl_store parameter is deprecated"):
            PermissionEnforcer(acl_store="dummy_acl_store")

    def test_subject_type_passed_to_rebac(self):
        """Test that subject type is correctly passed to ReBAC manager."""

        class MockReBACManager:
            def __init__(self):
                self.last_subject = None

            def rebac_check(self, subject, permission, object, tenant_id):
                self.last_subject = subject
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        ctx = OperationContext(
            user="claude", groups=["ai_agents"], subject_type="agent", subject_id="claude_001"
        )

        enforcer.check("/file.txt", Permission.READ, ctx)
        assert rebac.last_subject == ("agent", "claude_001")

    def test_path_normalization_adds_leading_slash(self):
        """Test that paths without leading slash are normalized during permission checks.

        This tests the fix for the bug where router strips leading slashes from backend_path,
        but ReBAC tuples are created with leading slashes, causing permission checks to fail.
        """

        class MockRouter:
            """Mock router that returns backend_path without leading slash (as the real router does)."""

            def route(self, path, tenant_id=None, is_admin=False, check_write=False):
                class MockBackend:
                    def get_object_type(self, backend_path):
                        return "file"

                    def get_object_id(self, backend_path):
                        # Router returns path without leading slash (relative to backend root)
                        return backend_path.lstrip("/")

                class MockRoute:
                    def __init__(self):
                        self.backend = MockBackend()
                        # Simulate router stripping leading slash
                        self.backend_path = path.lstrip("/")

                return MockRoute()

        class MockReBACManager:
            def __init__(self):
                self.last_object_id = None

            def rebac_check(self, subject, permission, object, tenant_id):
                _, object_id = object
                self.last_object_id = object_id
                # Check that object_id has leading slash (normalized)
                return object_id.startswith("/")

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=MockRouter())
        ctx = OperationContext(user="alice", groups=["developers"])

        # Test that permission check normalizes path to have leading slash
        result = enforcer.check("/workspace/alice", Permission.WRITE, ctx)

        # Should succeed because path was normalized to have leading slash
        assert result is True
        # Verify the normalized path was passed to ReBAC
        assert rebac.last_object_id == "/workspace/alice"
