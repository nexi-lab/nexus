"""Tests for PermissionEnforcer and OperationContext classes."""

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import (
    OperationContext,
    Permission,
)


class TestOperationContext:
    """Tests for OperationContext dataclass."""

    def test_create_regular_user_context(self):
        """Test creating a regular user context."""
        ctx = OperationContext(user_id="alice", groups=["developers"])
        assert ctx.user_id == "alice"
        assert ctx.groups == ["developers"]
        assert ctx.is_admin is False
        assert ctx.is_system is False
        assert ctx.subject_type == "user"
        assert ctx.subject_id == "alice"

    def test_create_admin_context(self):
        """Test creating an admin context."""
        ctx = OperationContext(user_id="admin", groups=["admins"], is_admin=True)
        assert ctx.user_id == "admin"
        assert ctx.groups == ["admins"]
        assert ctx.is_admin is True
        assert ctx.is_system is False

    def test_create_system_context(self):
        """Test creating a system context."""
        ctx = OperationContext(user_id="system", groups=[], is_system=True)
        assert ctx.user_id == "system"
        assert ctx.groups == []
        assert ctx.is_admin is False
        assert ctx.is_system is True

    def test_create_agent_context(self):
        """Test creating an AI agent context."""
        ctx = OperationContext(
            user_id="claude", groups=["ai_agents"], subject_type="agent", subject_id="claude_001"
        )
        assert ctx.subject_type == "agent"
        assert ctx.subject_id == "claude_001"
        assert ctx.get_subject() == ("agent", "claude_001")

    def test_create_service_context(self):
        """Test creating a service context."""
        ctx = OperationContext(
            user_id="backup",
            groups=["services"],
            subject_type="service",
            subject_id="backup_service",
        )
        assert ctx.subject_type == "service"
        assert ctx.subject_id == "backup_service"
        assert ctx.get_subject() == ("service", "backup_service")

    def test_zone_id_in_context(self):
        """Test zone ID in context for multi-zone isolation."""
        ctx = OperationContext(user_id="alice", groups=["developers"], zone_id="org_acme")
        assert ctx.zone_id == "org_acme"

    def test_requires_user(self):
        """Test that user is required."""
        with pytest.raises(ValueError, match="user_id is required"):
            OperationContext(user_id="", groups=[])

    def test_requires_groups_list(self):
        """Test that groups must be a list."""
        with pytest.raises(TypeError, match="groups must be list"):
            OperationContext(user_id="alice", groups="developers")  # type: ignore

    def test_empty_groups_allowed(self):
        """Test that empty groups list is allowed."""
        ctx = OperationContext(user_id="alice", groups=[])
        assert ctx.groups == []

    def test_get_subject_defaults_to_user(self):
        """Test that get_subject() defaults to user when subject_id is None."""
        ctx = OperationContext(user_id="alice", groups=["developers"])
        assert ctx.get_subject() == ("user", "alice")


class TestPermissionEnforcer:
    """Tests for PermissionEnforcer class with ReBAC-only model."""

    def test_admin_bypass(self):
        """Test that admin users bypass all checks."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user_id="admin",
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
        ctx = OperationContext(user_id="system", groups=[], is_system=True)

        # Read operations allowed on any path
        assert enforcer.check("/any/path", Permission.READ, ctx) is True
        # Write/execute operations only allowed on /system/* paths
        assert enforcer.check("/system/any/path", Permission.WRITE, ctx) is True
        assert enforcer.check("/system/any/path", Permission.EXECUTE, ctx) is True

    def test_no_rebac_manager_denies_all(self):
        """Test that without ReBAC manager, access is denied (secure by default)."""
        enforcer = PermissionEnforcer(metadata_store=None, rebac_manager=None)
        ctx = OperationContext(user_id="alice", groups=["developers"])

        assert enforcer.check("/any/path", Permission.READ, ctx) is False
        assert enforcer.check("/any/path", Permission.WRITE, ctx) is False
        assert enforcer.check("/any/path", Permission.EXECUTE, ctx) is False

    def test_rebac_check_with_mock_manager(self):
        """Test ReBAC permission checking with mock manager."""

        class MockReBACManager:
            def __init__(self):
                self.checks = []

            def rebac_check(self, subject, permission, object, zone_id):
                self.checks.append(
                    {
                        "subject": subject,
                        "permission": permission,
                        "object": object,
                        "zone_id": zone_id,
                    }
                )
                return subject == ("user", "alice") and permission == "read"

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user_id="alice", groups=["developers"])

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

    def test_rebac_check_with_zone_id(self):
        """Test ReBAC permission checking includes zone ID."""

        class MockReBACManager:
            def __init__(self):
                self.last_zone_id = None

            def rebac_check(self, subject, permission, object, zone_id):
                self.last_zone_id = zone_id
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user_id="alice", groups=["developers"], zone_id="org_acme")

        enforcer.check("/file.txt", Permission.READ, ctx)
        assert rebac.last_zone_id == "org_acme"

    def test_rebac_check_defaults_zone_id(self):
        """Test ReBAC permission checking defaults to 'default' zone."""

        class MockReBACManager:
            def __init__(self):
                self.last_zone_id = None

            def rebac_check(self, subject, permission, object, zone_id):
                self.last_zone_id = zone_id
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user_id="alice", groups=["developers"])

        enforcer.check("/file.txt", Permission.READ, ctx)
        assert rebac.last_zone_id == "root"

    def test_filter_list_admin_sees_all(self):
        """Test that admins see all files in filter_list."""
        enforcer = PermissionEnforcer(allow_admin_bypass=True)
        ctx = OperationContext(
            user_id="admin",
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
        ctx = OperationContext(user_id="system", groups=[], is_system=True)

        paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        assert filtered == paths

    def test_filter_list_filters_by_rebac_permission(self):
        """Test that filter_list removes files user can't read via ReBAC."""

        class MockReBACManager:
            def rebac_check(self, subject, permission, object, zone_id):
                _, path = object
                if path == "/public.txt" and permission == "read":
                    return True
                if path == "/secret.txt" and permission == "read":
                    return False
                return False

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="bob", groups=["designers"])

        paths = ["/public.txt", "/secret.txt"]
        filtered = enforcer.filter_list(paths, ctx)

        assert filtered == ["/public.txt"]

    def test_permission_flags_map_correctly(self):
        """Test that Permission flags map to correct string permissions."""

        class MockReBACManager:
            def __init__(self):
                self.permissions_checked = []

            def rebac_check(self, subject, permission, object, zone_id):
                self.permissions_checked.append(permission)
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)
        ctx = OperationContext(user_id="alice", groups=["developers"])

        enforcer.check("/file.txt", Permission.READ, ctx)
        enforcer.check("/file.txt", Permission.WRITE, ctx)
        enforcer.check("/file.txt", Permission.EXECUTE, ctx)

        assert rebac.permissions_checked == ["read", "write", "execute"]

    def test_subject_type_passed_to_rebac(self):
        """Test that subject type is correctly passed to ReBAC manager."""

        class MockReBACManager:
            def __init__(self):
                self.last_subject = None

            def rebac_check(self, subject, permission, object, zone_id):
                self.last_subject = subject
                return True

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac)

        ctx = OperationContext(
            user_id="claude", groups=["ai_agents"], subject_type="agent", subject_id="claude_001"
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

            def route(self, path, zone_id: str = ROOT_ZONE_ID):
                del zone_id  # production enforcer passes zone_id; Mock ignores

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

            def rebac_check(self, subject, permission, object, zone_id):
                _, object_id = object
                self.last_object_id = object_id
                # Check that object_id has leading slash (normalized)
                return object_id.startswith("/")

        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, kernel=MockRouter())
        ctx = OperationContext(user_id="alice", groups=["developers"])

        # Test that permission check normalizes path to have leading slash
        result = enforcer.check("/workspace/alice", Permission.WRITE, ctx)

        # Should succeed because path was normalized to have leading slash
        assert result is True
        # Verify the normalized path was passed to ReBAC
        assert rebac.last_object_id == "/workspace/alice"


class TestHasAccessibleDescendantsBatch:
    """Tests for has_accessible_descendants_batch() (Issue #1298)."""

    def test_empty_prefixes_returns_empty_dict(self):
        """Empty input returns empty dict without touching Tiger cache."""
        enforcer = PermissionEnforcer()
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch([], ctx)
        assert result == {}

    def test_no_tiger_cache_returns_all_true(self):
        """When Tiger cache is missing, fallback returns True for all prefixes."""

        class MockReBACManager:
            pass  # No _tiger_cache attribute

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills", "/archive"], ctx)
        assert result == {"/docs": True, "/skills": True, "/archive": True}

    def test_no_bitmap_returns_all_false(self):
        """When bitmap is None (cache miss), fail-closed returns False for all.

        Fix(#3709): previously returned all-True (fail-open vulnerability).
        """

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return None  # No bitmap — cache miss

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills"], ctx)
        assert result == {"/docs": False, "/skills": False}

    def test_all_accessible(self):
        """All prefixes have matching descendants in the bitmap."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md", "/skills/python.md", "/archive/old.txt"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills", "/archive"], ctx)
        assert result == {"/docs": True, "/skills": True, "/archive": True}

    def test_mixed_accessible(self):
        """Some prefixes have descendants, some don't."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md", "/docs/guide.md"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills", "/archive"], ctx)
        assert result["/docs"] is True
        assert result["/skills"] is False
        assert result["/archive"] is False

    def test_decode_error_returns_all_false(self):
        """On decode error, fail-closed returns False for all prefixes (security)."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                raise RuntimeError("bitmap decode error")

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills"], ctx)
        # Fail-closed: deny access on error (security-critical)
        assert result == {"/docs": False, "/skills": False}

    def test_prefix_collision_workspace_old(self):
        """/workspace must NOT match /workspace-old/x (Issue #1565)."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/workspace-old/file.txt"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/workspace"], ctx)
        assert result["/workspace"] is False

    def test_trailing_slash_normalization(self):
        """/a/b/ and /a/b should be equivalent prefixes."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/a/b/c.txt"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/a/b/", "/a/b"], ctx)
        assert result["/a/b/"] is True
        assert result["/a/b"] is True

    def test_exact_path_match(self):
        """path /a/b matches prefix /a/b (exact match, not just descendants)."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/a/b"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/a/b"], ctx)
        assert result["/a/b"] is True

    def test_root_prefix_matches_all(self):
        """prefix '/' matches everything."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/"], ctx)
        assert result["/"] is True

    def test_empty_accessible_paths(self):
        """Bitmap exists but maps to no paths."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return set()

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        result = enforcer.has_accessible_descendants_batch(["/docs"], ctx)
        assert result["/docs"] is False

    def test_single_wraps_batch(self):
        """has_accessible_descendants() delegates to batch."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])
        assert enforcer.has_accessible_descendants("/docs", ctx) is True
        assert enforcer.has_accessible_descendants("/skills", ctx) is False


class TestFailClosedOnError:
    """T3: Verify that check() path is fail-closed on ReBAC errors."""

    def test_rebac_error_propagates_as_denial(self):
        """ReBAC exceptions propagate (fail-closed), not silently allowed.

        When rebac_check raises an exception, check() must NOT return True.
        The exception should propagate to the caller, ensuring fail-closed
        behavior in the hot permission-check path.
        """

        class BrokenReBACManager:
            def rebac_check(self, **kwargs):
                raise RuntimeError("simulated ReBAC failure")

        enforcer = PermissionEnforcer(rebac_manager=BrokenReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])

        with pytest.raises(RuntimeError, match="simulated ReBAC failure"):
            enforcer.check("/file.txt", Permission.READ, ctx)
