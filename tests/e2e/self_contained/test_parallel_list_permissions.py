"""Integration tests for parallel directory listing + permission filtering (Issue #901).

Tests the pipeline: SearchService._list_dir_parallel() -> PermissionEnforcer.filter_list()
to verify that parallel directory traversal works correctly with ReBAC permission filtering.

Uses mock backends and rebac_manager to isolate the parallel listing + permission pipeline.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.core.permissions import OperationContext
from nexus.services.permissions.enforcer import PermissionEnforcer
from nexus.services.search_service import SearchService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(tree: dict[str, list[str]]) -> MagicMock:
    """Create a mock backend with a directory tree.

    Args:
        tree: Mapping of backend_path -> list of entries.
              Directories should have trailing '/'.
              Example: {"": ["file.txt", "subdir/"], "subdir": ["nested.txt"]}
    """
    backend = MagicMock()

    def list_dir(path: str, context: object = None) -> list[str]:
        if path in tree:
            return list(tree[path])  # Return a copy to avoid mutation
        raise FileNotFoundError(f"Directory not found: {path}")

    backend.list_dir = MagicMock(side_effect=list_dir)
    return backend


def _make_context(
    user: str = "alice",
    zone_id: str = "default",
    is_admin: bool = False,
) -> OperationContext:
    """Create an OperationContext for testing."""
    return OperationContext(
        user=user,
        groups=[],
        zone_id=zone_id,
        is_admin=is_admin,
    )


def _make_enforcer(
    allowed_paths: set[str] | None = None,
    allow_admin_bypass: bool = True,
) -> PermissionEnforcer:
    """Create a PermissionEnforcer with a mocked rebac_manager.

    The rebac_manager.rebac_check_bulk is stubbed to return True only for
    paths in `allowed_paths`. If allowed_paths is None, all paths are allowed.
    """
    rebac = MagicMock()

    if allowed_paths is not None:
        # Build a rebac_check_bulk that returns True for allowed paths only
        def rebac_check_bulk(checks, zone_id, consistency=None):
            results = {}
            for check in checks:
                _subject, _perm, (_obj_type, obj_id) = check
                results[check] = obj_id in allowed_paths
            return results

        rebac.rebac_check_bulk = MagicMock(side_effect=rebac_check_bulk)

        def rebac_check(subject, permission, object, zone_id):  # noqa: A002
            _obj_type, obj_id = object
            return obj_id in allowed_paths

        rebac.rebac_check = MagicMock(side_effect=rebac_check)
    else:
        # Allow everything
        rebac.rebac_check_bulk = MagicMock(return_value={})
        rebac.rebac_check = MagicMock(return_value=True)

    # Remove tiger cache to avoid bitmap code path
    rebac._tiger_cache = None

    enforcer = PermissionEnforcer(
        rebac_manager=rebac,
        allow_admin_bypass=allow_admin_bypass,
        enable_boundary_cache=False,
        enable_hotspot_tracking=False,
    )
    return enforcer


def _make_search_service(
    enforcer: PermissionEnforcer,
    rebac_manager: MagicMock | None = None,
) -> SearchService:
    """Create a SearchService with mocked metadata_store and router."""
    mock_metadata = MagicMock()
    # metadata.list should return empty (we are testing the dynamic connector path)
    mock_metadata.list = MagicMock(return_value=[])

    mock_router = MagicMock()

    svc = SearchService(
        metadata_store=mock_metadata,
        permission_enforcer=enforcer,
        router=mock_router,
        rebac_manager=rebac_manager or enforcer.rebac_manager,
        enforce_permissions=True,
    )
    return svc


# ===========================================================================
# Test 1: Parallel list with permission filtering
# ===========================================================================


class TestParallelListWithPermissionFiltering:
    """Verify that parallel listing + filter_list returns only allowed paths."""

    def test_parallel_list_with_permission_filtering(self) -> None:
        """Mock backend returns 50 file paths across 5 directories.
        Mock rebac allows only 20 paths. Verify filter_list returns exactly 20.
        """
        # Build a tree: 5 directories, each with 10 files = 50 files total
        tree: dict[str, list[str]] = {"": []}
        all_file_paths: list[str] = []

        for d in range(5):
            dir_name = f"dir{d}/"
            tree[""].append(dir_name)
            dir_key = f"dir{d}"
            tree[dir_key] = []
            for f in range(10):
                fname = f"file{f}.txt"
                tree[dir_key].append(fname)
                all_file_paths.append(f"/mount/dir{d}/{fname}")

        assert len(all_file_paths) == 50

        # Allow only the first 4 files in each directory (20 total)
        allowed: set[str] = set()
        for d in range(5):
            for f in range(4):
                allowed.add(f"/mount/dir{d}/file{f}.txt")

        assert len(allowed) == 20

        enforcer = _make_enforcer(allowed_paths=allowed)
        svc = _make_search_service(enforcer)
        backend = _make_backend(tree)
        ctx = _make_context()

        # Step 1: Parallel directory listing
        listed_paths = svc._list_dir_parallel(
            backend=backend,
            root_path="/mount",
            backend_path="",
            context=ctx,
        )

        # Should include all files and all directory entries
        file_paths_listed = [
            p for p in listed_paths if not any(p == f"/mount/dir{d}" for d in range(5))
        ]
        assert len(file_paths_listed) == 50

        # Step 2: Permission filtering
        filtered = enforcer.filter_list(file_paths_listed, ctx)

        assert len(filtered) == 20
        assert set(filtered) == allowed

    def test_parallel_list_large_tree_selective_filtering(self) -> None:
        """Larger tree where filter_list excludes most paths."""
        tree: dict[str, list[str]] = {"": []}
        all_file_paths: list[str] = []

        for d in range(10):
            dir_name = f"project{d}/"
            tree[""].append(dir_name)
            dir_key = f"project{d}"
            tree[dir_key] = []
            for f in range(5):
                fname = f"doc{f}.md"
                tree[dir_key].append(fname)
                all_file_paths.append(f"/repo/project{d}/{fname}")

        assert len(all_file_paths) == 50

        # Allow only files in project0 and project1 (10 total out of 50)
        allowed: set[str] = set()
        for d in range(2):
            for f in range(5):
                allowed.add(f"/repo/project{d}/doc{f}.md")

        assert len(allowed) == 10

        enforcer = _make_enforcer(allowed_paths=allowed)
        svc = _make_search_service(enforcer)
        backend = _make_backend(tree)
        ctx = _make_context()

        listed = svc._list_dir_parallel(
            backend=backend,
            root_path="/repo",
            backend_path="",
            context=ctx,
        )

        # Filter out directory entries for assertion
        file_only = [p for p in listed if p.count("/") >= 3]
        filtered = enforcer.filter_list(file_only, ctx)

        assert len(filtered) == 10
        assert set(filtered) == allowed


# ===========================================================================
# Test 2: Error handling during parallel listing
# ===========================================================================


class TestParallelListErrorHandling:
    """Verify graceful degradation when subdirectory listing fails."""

    def test_parallel_list_error_handling(self) -> None:
        """One subdirectory raises Exception; other directories still listed."""
        call_count = {"n": 0}

        def list_dir(path: str, context: object = None) -> list[str]:
            call_count["n"] += 1
            if path == "":
                return ["good_dir/", "bad_dir/", "another_good/", "root.txt"]
            if path == "good_dir":
                return ["file1.txt", "file2.txt"]
            if path == "bad_dir":
                raise ConnectionError("Simulated API timeout")
            if path == "another_good":
                return ["file3.txt"]
            raise FileNotFoundError(path)

        backend = MagicMock()
        backend.list_dir = MagicMock(side_effect=list_dir)

        enforcer = _make_enforcer(allowed_paths=None)
        svc = _make_search_service(enforcer)
        ctx = _make_context()

        result = svc._list_dir_parallel(
            backend=backend,
            root_path="/mount",
            backend_path="",
            context=ctx,
        )

        # good_dir and its contents should be present
        assert "/mount/good_dir" in result
        assert "/mount/good_dir/file1.txt" in result
        assert "/mount/good_dir/file2.txt" in result

        # another_good and its contents should be present
        assert "/mount/another_good" in result
        assert "/mount/another_good/file3.txt" in result

        # root file should be present
        assert "/mount/root.txt" in result

        # bad_dir entry itself is present (added before recursion)
        assert "/mount/bad_dir" in result

        # No crash, total result count is correct: 3 dirs + 4 files = 7
        assert len(result) == 7

    def test_multiple_subdirectory_failures(self) -> None:
        """Multiple subdirectories fail; remaining content is still returned."""

        def list_dir(path: str, context: object = None) -> list[str]:
            if path == "":
                return ["fail1/", "fail2/", "ok/", "root.txt"]
            if path == "fail1":
                raise TimeoutError("Backend timeout")
            if path == "fail2":
                raise PermissionError("Access denied from backend")
            if path == "ok":
                return ["success.txt"]
            raise FileNotFoundError(path)

        backend = MagicMock()
        backend.list_dir = MagicMock(side_effect=list_dir)

        svc = _make_search_service(_make_enforcer(allowed_paths=None))
        ctx = _make_context()

        result = svc._list_dir_parallel(
            backend=backend,
            root_path="/mnt",
            backend_path="",
            context=ctx,
        )

        # ok directory and root file still present
        assert "/mnt/ok" in result
        assert "/mnt/ok/success.txt" in result
        assert "/mnt/root.txt" in result

        # Failed dirs are present as entries (added before recursion)
        assert "/mnt/fail1" in result
        assert "/mnt/fail2" in result

        # Total: 3 dirs + 2 files = 5
        assert len(result) == 5


# ===========================================================================
# Test 3: Non-recursive listing
# ===========================================================================


class TestNonRecursiveListing:
    """Verify non-recursive listing with permission filtering."""

    def test_non_recursive_listing(self) -> None:
        """Non-recursive listing returns only top-level entries (files and dirs)."""
        tree: dict[str, list[str]] = {
            "": ["readme.txt", "src/", "docs/", "config.yaml"],
            "src": ["main.py", "utils/"],
            "src/utils": ["helper.py"],
            "docs": ["guide.md"],
        }

        backend = _make_backend(tree)
        svc = _make_search_service(_make_enforcer(allowed_paths=None))
        ctx = _make_context()

        result = svc._list_dir_parallel(
            backend=backend,
            root_path="/project",
            backend_path="",
            context=ctx,
            recursive=False,
        )

        # Non-recursive: only direct children of root
        assert "/project/readme.txt" in result
        assert "/project/src" in result
        assert "/project/docs" in result
        assert "/project/config.yaml" in result

        # Should NOT contain nested entries
        assert "/project/src/main.py" not in result
        assert "/project/src/utils" not in result
        assert "/project/docs/guide.md" not in result

        # Only 1 list_dir call (root level only)
        assert backend.list_dir.call_count == 1

    def test_non_recursive_with_permission_filtering(self) -> None:
        """Non-recursive listing + filter_list returns only allowed top-level entries."""
        tree: dict[str, list[str]] = {
            "": ["public.txt", "private.txt", "shared/", "secret/"],
        }

        allowed = {"/data/public.txt", "/data/shared"}
        backend = _make_backend(tree)
        enforcer = _make_enforcer(allowed_paths=allowed)
        svc = _make_search_service(enforcer)
        ctx = _make_context()

        listed = svc._list_dir_parallel(
            backend=backend,
            root_path="/data",
            backend_path="",
            context=ctx,
            recursive=False,
        )

        assert len(listed) == 4  # All 4 top-level entries listed

        filtered = enforcer.filter_list(listed, ctx)
        assert set(filtered) == allowed


# ===========================================================================
# Test 4: filter_list with empty paths
# ===========================================================================


class TestFilterListEmptyPaths:
    """Verify filter_list handles edge case of empty input."""

    def test_filter_list_empty_paths(self) -> None:
        """filter_list with an empty list returns an empty list."""
        enforcer = _make_enforcer(allowed_paths={"/some/path"})
        ctx = _make_context()

        result = enforcer.filter_list([], ctx)

        assert result == []
        # rebac_check_bulk should not be called for empty input
        assert enforcer.rebac_manager.rebac_check_bulk.call_count == 0

    def test_filter_list_all_denied(self) -> None:
        """filter_list where no paths are allowed returns empty list."""
        enforcer = _make_enforcer(allowed_paths=set())  # Nothing allowed
        ctx = _make_context()

        paths = ["/a/file1.txt", "/b/file2.txt", "/c/file3.txt"]
        result = enforcer.filter_list(paths, ctx)

        assert result == []


# ===========================================================================
# Test 5: Admin bypass in filter_list
# ===========================================================================


class TestFilterListAdminBypass:
    """Verify admin context bypasses permission checks in filter_list."""

    def test_filter_list_admin_bypass(self) -> None:
        """Admin user gets all paths returned without ReBAC checks."""
        # Create enforcer with restrictive permissions (allow nothing)
        enforcer = _make_enforcer(
            allowed_paths=set(),
            allow_admin_bypass=True,
        )
        admin_ctx = _make_context(user="admin_user", is_admin=True)

        all_paths = [f"/zone/dir{d}/file{f}.txt" for d in range(5) for f in range(10)]
        assert len(all_paths) == 50

        result = enforcer.filter_list(all_paths, admin_ctx)

        # Admin bypass returns ALL paths regardless of ReBAC rules
        assert len(result) == 50
        assert result == all_paths
        # rebac_check_bulk should NOT be called (bypass short-circuits)
        assert enforcer.rebac_manager.rebac_check_bulk.call_count == 0

    def test_filter_list_admin_bypass_disabled(self) -> None:
        """When allow_admin_bypass=False, admin does NOT bypass permissions."""
        enforcer = _make_enforcer(
            allowed_paths={"/zone/dir0/file0.txt"},
            allow_admin_bypass=False,
        )
        admin_ctx = _make_context(user="admin_user", is_admin=True)

        paths = ["/zone/dir0/file0.txt", "/zone/dir1/file1.txt"]
        result = enforcer.filter_list(paths, admin_ctx)

        # Admin bypass is disabled, so only allowed paths are returned
        assert result == ["/zone/dir0/file0.txt"]

    def test_filter_list_non_admin_no_bypass(self) -> None:
        """Non-admin user does not get bypass even with allow_admin_bypass=True."""
        allowed = {"/data/public.txt"}
        enforcer = _make_enforcer(
            allowed_paths=allowed,
            allow_admin_bypass=True,
        )
        regular_ctx = _make_context(user="regular_user", is_admin=False)

        paths = ["/data/public.txt", "/data/private.txt"]
        result = enforcer.filter_list(paths, regular_ctx)

        # Only the allowed path is returned (no bypass for non-admin)
        assert result == ["/data/public.txt"]
