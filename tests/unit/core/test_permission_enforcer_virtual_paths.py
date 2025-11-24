"""Tests for PermissionEnforcer virtual path handling (fix for mounted backends).

This module tests the critical fix for issue #494 where permission checks
were using backend-relative paths instead of virtual paths, breaking
permission inheritance for mounted backends.

The bug:
- Virtual path: /mnt/gcs/file.csv
- Router transforms to backend path: file.csv
- Old code used: /file.csv for permission checks ❌
- Parent check only looked at "/" instead of "/mnt/gcs"
- Result: Permission inheritance broken for mounts

The fix:
- Always use virtual paths for file permission checks
- Parent directory inheritance works correctly
- Mount point ownership propagates to files
"""

from nexus.core.permissions import OperationContext, Permission, PermissionEnforcer


class MockRoute:
    """Mock route object returned by router."""

    def __init__(self, backend_path: str, backend=None):
        self.backend_path = backend_path
        self.backend = backend or MockBackend()


class MockBackend:
    """Mock backend for testing."""

    def __init__(self, object_type: str = "file", object_id: str | None = None):
        self._object_type = object_type
        self._object_id = object_id
        self.name = "mock_backend"

    def get_object_type(self, backend_path: str) -> str:
        return self._object_type

    def get_object_id(self, backend_path: str) -> str:
        return self._object_id or backend_path


class MockRouter:
    """Mock router that simulates mount point stripping."""

    def __init__(self, mount_point: str = "/mnt/gcs"):
        self.mount_point = mount_point

    def route(self, path: str, tenant_id=None, agent_id=None, is_admin=False, check_write=False):
        # Simulate mount point stripping
        if path.startswith(self.mount_point):
            backend_path = path[len(self.mount_point) + 1 :]  # Strip /mnt/gcs/
            if not backend_path:
                backend_path = ""
        else:
            backend_path = path.lstrip("/")

        return MockRoute(backend_path)


class MockReBACManager:
    """Mock ReBAC manager for testing permission checks."""

    def __init__(self):
        self.checks = []
        self.granted_paths = set()

    def grant_permission(self, path: str):
        """Grant permission for a path."""
        self.granted_paths.add(path)

    def rebac_check(self, subject, permission, object, tenant_id):
        """Record check and return True if path is in granted set."""
        object_type, object_id = object
        self.checks.append(
            {
                "subject": subject,
                "permission": permission,
                "object_type": object_type,
                "object_id": object_id,
                "tenant_id": tenant_id,
            }
        )
        # Grant permission if path is in granted set
        return object_id in self.granted_paths


class TestVirtualPathPermissionChecks:
    """Test that permission checks use virtual paths, not backend paths."""

    def test_permission_check_uses_virtual_path_not_backend_path(self):
        """Test the critical fix: permission checks must use virtual paths.

        This is the exact bug that caused the integration test failure in PR #494.

        Scenario:
        - Virtual path: /mnt/gcs/file.csv
        - Backend path: file.csv (mount point stripped)
        - Permission should be checked on: /mnt/gcs/file.csv (virtual)
        - NOT on: /file.csv (backend path with / prefix)
        """
        rebac = MockReBACManager()
        router = MockRouter(mount_point="/mnt/gcs")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        # Try to read file
        enforcer.check("/mnt/gcs/file.csv", Permission.READ, ctx)

        # Verify permission check used VIRTUAL path, not backend path
        checked_paths = [check["object_id"] for check in rebac.checks]

        # Should check: /mnt/gcs/file.csv (virtual path)
        assert "/mnt/gcs/file.csv" in checked_paths, (
            f"Expected virtual path '/mnt/gcs/file.csv' in checks, but got: {checked_paths}"
        )

        # Should NOT check /file.csv (backend path with / prefix)
        assert "/file.csv" not in checked_paths, (
            f"Backend path '/file.csv' should not be checked, but found in: {checked_paths}"
        )

    def test_mounted_backend_permission_inheritance(self):
        """Test that permission inheritance works through mount points.

        This simulates the real-world scenario:
        1. User owns mount point /mnt/gcs
        2. Files exist at /mnt/gcs/data.csv
        3. Parent directory check should walk: /mnt/gcs/data.csv → /mnt/gcs → /
        4. Permission should be granted via ownership of /mnt/gcs
        """
        rebac = MockReBACManager()
        # Grant ownership of mount point
        rebac.grant_permission("/mnt/gcs")

        router = MockRouter(mount_point="/mnt/gcs")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        # Try to read file in mounted backend
        result = enforcer.check("/mnt/gcs/data.csv", Permission.READ, ctx)

        # Should succeed due to permission inheritance from /mnt/gcs
        assert result is True, "Permission should be granted via mount point ownership"

        # Verify parent directory was checked
        checked_paths = [check["object_id"] for check in rebac.checks]
        assert "/mnt/gcs" in checked_paths, (
            f"Mount point should be checked for inheritance, "
            f"but checks only included: {checked_paths}"
        )

    def test_nested_mount_path_inheritance(self):
        """Test permission inheritance for deeply nested paths in mounts."""
        rebac = MockReBACManager()
        rebac.grant_permission("/mnt/gcs/team-data")

        router = MockRouter(mount_point="/mnt/gcs")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        # Try to read deeply nested file
        result = enforcer.check(
            "/mnt/gcs/team-data/2024/q4/reports/sales.csv", Permission.READ, ctx
        )

        assert result is True, "Permission should inherit from /mnt/gcs/team-data"

        # Verify the permission checker walked up the tree
        checked_paths = [check["object_id"] for check in rebac.checks]

        # Should include parent directories
        assert "/mnt/gcs/team-data" in checked_paths

    def test_multiple_mounts_use_correct_virtual_paths(self):
        """Test that multiple mounts each use their own virtual paths."""
        rebac = MockReBACManager()
        rebac.grant_permission("/mnt/alice")
        # Bob's mount has no permissions

        # Alice's mount
        router_alice = MockRouter(mount_point="/mnt/alice")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router_alice)

        ctx_alice = OperationContext(user="alice", groups=[])

        # Alice can read from her mount
        assert enforcer.check("/mnt/alice/file.txt", Permission.READ, ctx_alice) is True

        # Bob's mount (using same enforcer but different path)
        router_bob = MockRouter(mount_point="/mnt/bob")
        enforcer_bob = PermissionEnforcer(rebac_manager=rebac, router=router_bob)

        ctx_bob = OperationContext(user="bob", groups=[])

        # Bob cannot read from his mount (no permission granted)
        assert enforcer_bob.check("/mnt/bob/file.txt", Permission.READ, ctx_bob) is False


class TestNonFileBackendObjectId:
    """Test that non-file backends still use backend-provided object IDs."""

    def test_non_file_backends_use_backend_object_id(self):
        """Test that DB tables, Redis keys, etc. use backend.get_object_id()."""
        rebac = MockReBACManager()

        # Mock backend for database tables
        db_backend = MockBackend(object_type="postgres:table", object_id="users")

        class DatabaseRouter:
            def route(self, path, **kwargs):
                return MockRoute(backend_path="public.users", backend=db_backend)

        router = DatabaseRouter()
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        enforcer.check("/db/users", Permission.READ, ctx)

        # For non-file backends, should use backend-provided object_id
        checked = rebac.checks[0]
        assert checked["object_type"] == "postgres:table"
        assert checked["object_id"] == "users"  # Backend-provided ID, not "/db/users"

    def test_redis_backend_uses_key_as_object_id(self):
        """Test Redis backend uses keys, not paths."""
        rebac = MockReBACManager()

        redis_backend = MockBackend(object_type="redis:key", object_id="session:abc123")

        class RedisRouter:
            def route(self, path, **kwargs):
                return MockRoute(backend_path="session:abc123", backend=redis_backend)

        router = RedisRouter()
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        enforcer.check("/cache/session:abc123", Permission.READ, ctx)

        checked = rebac.checks[0]
        assert checked["object_type"] == "redis:key"
        assert checked["object_id"] == "session:abc123"


class TestRouterFailureFallback:
    """Test graceful handling when router fails."""

    def test_router_failure_fallback_to_virtual_path(self):
        """Test that router exceptions fall back to virtual path with object_type='file'."""

        class FailingRouter:
            def route(self, path, **kwargs):
                raise RuntimeError("Router unavailable")

        rebac = MockReBACManager()
        router = FailingRouter()
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        # Should not raise, should fall back gracefully
        enforcer.check("/some/file.txt", Permission.READ, ctx)

        # Should use virtual path and default to "file" type
        checked = rebac.checks[0]
        assert checked["object_type"] == "file"
        assert checked["object_id"] == "/some/file.txt"  # Virtual path preserved

    def test_no_router_uses_virtual_path(self):
        """Test that when router is None, virtual path is used."""
        rebac = MockReBACManager()
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=None)

        ctx = OperationContext(user="alice", groups=[])

        enforcer.check("/workspace/file.txt", Permission.READ, ctx)

        # Should use virtual path with default "file" type
        checked = rebac.checks[0]
        assert checked["object_type"] == "file"
        assert checked["object_id"] == "/workspace/file.txt"


class TestRegressionPrevention:
    """Tests to ensure the bug doesn't reoccur."""

    def test_root_mount_permission_inheritance(self):
        """Regression test: Ensure root mount permissions work."""
        rebac = MockReBACManager()
        rebac.grant_permission("/")

        router = MockRouter(mount_point="/")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="admin", groups=[])

        # Should inherit from root permission
        assert enforcer.check("/any/deep/nested/file.txt", Permission.READ, ctx) is True

    def test_permission_check_parent_directory_walking(self):
        """Test that parent directory walking uses virtual paths."""
        rebac = MockReBACManager()

        # Grant permission only at intermediate directory
        rebac.grant_permission("/mnt/gcs/team-data")

        router = MockRouter(mount_point="/mnt/gcs")
        enforcer = PermissionEnforcer(rebac_manager=rebac, router=router)

        ctx = OperationContext(user="alice", groups=[])

        # File deep in hierarchy
        result = enforcer.check("/mnt/gcs/team-data/2024/reports/q4.csv", Permission.READ, ctx)

        assert result is True

        # Verify walking pattern
        checked_paths = [check["object_id"] for check in rebac.checks]

        # Should walk: file → 2024 → team-data (found!) → stops
        # All paths should be virtual paths with mount point
        for path in checked_paths:
            if path != "/":  # Root is special
                assert path.startswith("/mnt/gcs"), (
                    f"All checked paths should be virtual paths with mount point, but found: {path}"
                )
