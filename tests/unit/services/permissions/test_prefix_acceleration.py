"""Tests for Rust-accelerated path prefix matching (Issue #1565).

Tests both the Rust functions (when available) and the Python fallback path.
"""

import pytest

pytest.importorskip("pyroaring")


from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext

# Check if Rust module is available
try:
    import nexus_runtime

    RUST_AVAILABLE = hasattr(nexus_runtime, "batch_prefix_check")
except ImportError:
    RUST_AVAILABLE = False


class TestRustPrefixFunctions:
    """Direct tests for Rust prefix matching functions."""

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_any_path_starts_with_via_rust(self):
        """Verify Rust any_path_starts_with works through Python."""
        paths = ["/docs/readme.md", "/skills/python.md", "/archive/old.txt"]
        assert nexus_runtime.any_path_starts_with(paths, "/docs") is True
        assert nexus_runtime.any_path_starts_with(paths, "/missing") is False

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_batch_prefix_check_via_rust(self):
        """Verify Rust batch_prefix_check works through Python."""
        paths = ["/docs/readme.md", "/docs/guide.md", "/skills/python.md"]
        prefixes = ["/docs", "/skills", "/archive"]
        results = nexus_runtime.batch_prefix_check(paths, prefixes)
        assert results == [True, True, False]

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_filter_paths_by_prefix_via_rust(self):
        """Verify Rust filter_paths_by_prefix works through Python."""
        paths = ["/a/b/c.txt", "/a/b/d.txt", "/x/y/z.txt"]
        result = nexus_runtime.filter_paths_by_prefix(paths, "/a/b")
        assert sorted(result) == ["/a/b/c.txt", "/a/b/d.txt"]

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_prefix_collision_via_rust(self):
        """Rust must not match /workspace-old when prefix is /workspace."""
        paths = ["/workspace-old/file.txt"]
        assert nexus_runtime.any_path_starts_with(paths, "/workspace") is False

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_trailing_slash_via_rust(self):
        """Trailing slash in prefix should be normalized."""
        paths = ["/a/b/c.txt"]
        assert nexus_runtime.any_path_starts_with(paths, "/a/b/") is True
        assert nexus_runtime.any_path_starts_with(paths, "/a/b") is True

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    def test_root_prefix_via_rust(self):
        """Root prefix '/' should match everything."""
        paths = ["/a/b/c.txt", "/d/e.txt"]
        assert nexus_runtime.any_path_starts_with(paths, "/") is True
        results = nexus_runtime.batch_prefix_check(paths, ["/"])
        assert results == [True]


class TestPythonFallback:
    """Tests that Python fallback works when Rust is unavailable."""

    def test_fallback_when_rust_unavailable(self):
        """Verify Python fallback produces correct results."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md", "/docs/guide.md"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])

        # This will use Python fallback since mock doesn't have nexus_runtime
        result = enforcer.has_accessible_descendants_batch(["/docs", "/skills", "/archive"], ctx)
        assert result["/docs"] is True
        assert result["/skills"] is False
        assert result["/archive"] is False

    def test_single_wraps_batch(self):
        """Verify has_accessible_descendants() delegates to batch."""

        class MockTigerCache:
            def get_accessible_paths(self, **kwargs):
                return {"/docs/readme.md"}

        class MockReBACManager:
            _tiger_cache = MockTigerCache()

        enforcer = PermissionEnforcer(rebac_manager=MockReBACManager())
        ctx = OperationContext(user_id="alice", groups=["dev"])

        assert enforcer.has_accessible_descendants("/docs", ctx) is True
        assert enforcer.has_accessible_descendants("/skills", ctx) is False


class TestResultsParity:
    """Verify Rust and Python produce identical results."""

    @pytest.mark.skipif(not RUST_AVAILABLE, reason="nexus_runtime not available")
    @pytest.mark.parametrize(
        "paths,prefixes",
        [
            (["/a/b/c.txt", "/d/e/f.txt"], ["/a/b", "/d/e", "/x/y"]),
            (["/workspace/proj-a/file.txt"], ["/workspace", "/workspace-old"]),
            (["/a/b"], ["/a/b"]),  # exact match
            (["/a/b/c.txt"], ["/"]),  # root prefix
            ([], ["/a"]),  # empty paths
            (["/a/b/c.txt"], ["/a/b/", "/a/b"]),  # trailing slash normalization
        ],
    )
    def test_results_match_python_and_rust(self, paths, prefixes):
        """Parameterized: run both implementations and compare results."""
        # Rust result
        rust_results = nexus_runtime.batch_prefix_check(list(paths), list(prefixes))

        # Python result (same algorithm as fallback in enforcer)
        python_results = []
        for prefix in prefixes:
            prefix_normalized = prefix.rstrip("/") + "/"
            prefix_exact = prefix.rstrip("/")
            found = any(p.startswith(prefix_normalized) or p == prefix_exact for p in paths)
            python_results.append(found)

        assert rust_results == python_results, (
            f"Mismatch for paths={paths}, prefixes={prefixes}: "
            f"rust={rust_results}, python={python_results}"
        )
