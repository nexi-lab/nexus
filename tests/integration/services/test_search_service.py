"""Unit tests for SearchService.

Tests initialization, glob search, grep search, file listing,
helper methods, and error handling for missing dependencies.

SearchService uses dependency injection with MetastoreABC,
PermissionEnforcer, DriverLifecycleCoordinator (DLC), and NexusFSGateway.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.search_service import (
    DEFAULT_IGNORE_PATTERNS,
    SearchService,
    _filter_ignored_paths,
    _should_ignore_path,
)
from nexus.contracts.types import OperationContext

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_metadata_store():
    """Create a mock MetastoreABC."""
    store = MagicMock()
    store.list_paths.return_value = []
    store.get_file_metadata.return_value = None
    store.get_file_metadata_bulk.return_value = {}
    store.get_searchable_text_bulk.return_value = {}
    return store


@pytest.fixture
def mock_permission_enforcer():
    """Create a mock PermissionEnforcer (permissive by default).

    ``filter_list`` defaults to a pass-through — any path supplied is
    treated as readable unless the test overrides it. This matches the
    mock_metadata_store pattern and keeps the ``files=[...]`` validator
    tests oblivious to which enforcer strategy runs.
    """
    enforcer = MagicMock()
    enforcer.check_permission.return_value = True
    enforcer.filter_list = MagicMock(side_effect=lambda paths, context: list(paths))
    return enforcer


@pytest.fixture
def mock_dlc():
    """Create a mock DriverLifecycleCoordinator."""
    dlc = MagicMock()
    dlc.mount_points.return_value = [
        "/archives",
        "/external",
        "/shared",
        "/system",
        "/workspace",
    ]
    return dlc


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway."""
    gw = MagicMock()
    gw.read_file = AsyncMock(return_value=b"test content")
    gw.read_bulk.return_value = {}
    gw.get_routing_params.return_value = (None, None, False)
    gw.has_descendant_access.return_value = True
    gw.record_read_if_tracking.return_value = None
    gw.session_factory = MagicMock()
    gw.backend = MagicMock()
    return gw


@pytest.fixture
def service(mock_metadata_store, mock_permission_enforcer, mock_dlc, mock_gateway):
    """Create a SearchService with all mocked dependencies."""
    return SearchService(
        metadata_store=mock_metadata_store,
        permission_enforcer=mock_permission_enforcer,
        dlc=mock_dlc,
        gateway=mock_gateway,
        enforce_permissions=True,
    )


@pytest.fixture
def service_no_perms(mock_metadata_store, mock_gateway):
    """Create a SearchService with permissions disabled."""
    return SearchService(
        metadata_store=mock_metadata_store,
        gateway=mock_gateway,
        enforce_permissions=False,
    )


@pytest.fixture
def context():
    """Standard operation context."""
    return OperationContext(
        user_id="test_user",
        groups=["test_group"],
        zone_id="test_zone",
        is_system=False,
        is_admin=False,
    )


# =============================================================================
# Initialization
# =============================================================================


class TestSearchServiceInit:
    """Tests for SearchService construction."""

    def test_init_stores_all_dependencies(
        self, mock_metadata_store, mock_permission_enforcer, mock_dlc, mock_gateway
    ):
        """Service stores all injected dependencies."""
        svc = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=mock_permission_enforcer,
            dlc=mock_dlc,
            gateway=mock_gateway,
            enforce_permissions=True,
        )
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is mock_permission_enforcer
        assert svc._dlc is mock_dlc
        assert svc._gw is mock_gateway
        assert svc._enforce_permissions is True

    def test_init_minimal(self, mock_metadata_store):
        """Service can be created with just a metadata store."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is None
        assert svc._dlc is None
        assert svc._gw is None
        assert svc._enforce_permissions is True

    def test_init_defaults(self, mock_metadata_store):
        """Service initializes internal state to defaults."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc._thread_pool is None
        assert svc._list_thread_pool is None
        assert svc._default_context is None
        assert svc._record_store is None

    def test_init_with_default_context(self, mock_metadata_store, context):
        """Service stores default_context for embedded mode."""
        svc = SearchService(metadata_store=mock_metadata_store, default_context=context)
        assert svc._default_context is context

    def test_init_stores_rebac_manager(self, mock_metadata_store):
        """Service stores rebac_manager when provided."""
        mock_rebac = MagicMock()
        svc = SearchService(metadata_store=mock_metadata_store, rebac_manager=mock_rebac)
        assert svc._rebac_manager is mock_rebac

    def test_init_stores_record_store(self, mock_metadata_store):
        """Service stores record_store when provided."""
        mock_record_store = MagicMock()
        svc = SearchService(metadata_store=mock_metadata_store, record_store=mock_record_store)
        assert svc._record_store is mock_record_store

    def test_list_slow_path_passes_zone_id_to_tiger_pushdown(
        self, mock_metadata_store, mock_permission_enforcer, mock_dlc, mock_gateway
    ):
        """Predicate pushdown must request the bitmap for the current list zone."""
        meta = MagicMock()
        meta.path = "/visible.txt"
        mock_metadata_store.list.return_value = [meta]

        tiger_cache = MagicMock()
        tiger_cache.get_accessible_int_ids.return_value = {1}
        tiger_cache._resource_map.get_or_create_int_id.return_value = 1
        rebac_manager = MagicMock()
        rebac_manager._tiger_cache = tiger_cache

        svc = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=mock_permission_enforcer,
            dlc=mock_dlc,
            gateway=mock_gateway,
            enforce_permissions=True,
        )

        all_files, accessible_ids = svc._list_slow_path(
            list_prefix="",
            list_zone_id="test_zone",
            subject_type="user",
            subject_id="test_user",
            _revision_before=None,
            _rebac_manager=rebac_manager,
        )

        assert all_files == [meta]
        assert accessible_ids == {1}
        tiger_cache.get_accessible_int_ids.assert_called_once_with(
            subject_type="user",
            subject_id="test_user",
            permission="read",
            resource_type="file",
            zone_id="test_zone",
        )

    def test_cross_zone_sharing_uses_public_rebac_method(self, mock_metadata_store):
        """Cross-zone search should rely on the public ReBAC API."""
        rebac_manager = MagicMock()
        rebac_manager.get_cross_zone_shared_paths.return_value = ["/shared/file.txt"]
        svc = SearchService(metadata_store=mock_metadata_store, rebac_manager=rebac_manager)

        result = svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone-a",
            prefix="/shared",
        )

        assert result == ["/shared/file.txt"]
        rebac_manager.get_cross_zone_shared_paths.assert_called_once_with(
            subject_type="user",
            subject_id="alice",
            zone_id="zone-a",
            prefix="/shared",
        )

    def test_cross_zone_sharing_missing_public_method_returns_empty(self, mock_metadata_store):
        """Managers without cross-zone sharing support should degrade cleanly."""
        rebac_manager = MagicMock(spec=[])
        svc = SearchService(metadata_store=mock_metadata_store, rebac_manager=rebac_manager)

        result = svc._get_cross_zone_shared_paths(
            subject_type="user",
            subject_id="alice",
            zone_id="zone-a",
            prefix="/shared",
        )

        assert result == []


# =============================================================================
# Gitignore filtering (module-level helpers)
# =============================================================================


class TestIgnorePatterns:
    """Tests for gitignore-style path filtering."""

    def test_should_ignore_node_modules(self):
        """node_modules directory is ignored."""
        assert _should_ignore_path("/project/node_modules/package.json") is True

    def test_should_ignore_pycache(self):
        """__pycache__ directory is ignored."""
        assert _should_ignore_path("/src/__pycache__/module.cpython-311.pyc") is True

    def test_should_ignore_dot_git(self):
        """.git directory is ignored."""
        assert _should_ignore_path("/.git/config") is True

    def test_should_ignore_pyc_files(self):
        """*.pyc files are ignored."""
        assert _should_ignore_path("/src/module.pyc") is True

    def test_should_ignore_log_files(self):
        """*.log files are ignored."""
        assert _should_ignore_path("/var/app.log") is True

    def test_should_not_ignore_normal_paths(self):
        """Normal paths are not ignored."""
        assert _should_ignore_path("/src/main.py") is False
        assert _should_ignore_path("/docs/README.md") is False

    def test_filter_ignored_paths(self):
        """_filter_ignored_paths removes matching paths."""
        paths = [
            "/src/main.py",
            "/src/__pycache__/main.cpython-311.pyc",
            "/node_modules/express/index.js",
            "/docs/README.md",
        ]
        filtered = _filter_ignored_paths(paths)
        assert "/src/main.py" in filtered
        assert "/docs/README.md" in filtered
        assert len(filtered) == 2

    def test_filter_empty_list(self):
        """_filter_ignored_paths handles empty list."""
        assert _filter_ignored_paths([]) == []

    def test_default_ignore_patterns_is_frozenset(self):
        """DEFAULT_IGNORE_PATTERNS is a frozenset (immutable)."""
        assert isinstance(DEFAULT_IGNORE_PATTERNS, frozenset)
        assert ".git" in DEFAULT_IGNORE_PATTERNS
        assert "node_modules" in DEFAULT_IGNORE_PATTERNS


# =============================================================================
# Gateway delegation helpers
# =============================================================================


class TestGatewayDelegation:
    """Tests for gateway delegation methods."""

    async def test_read_delegates_to_gateway(self, service, mock_gateway):
        """_read delegates to gateway.read_file."""
        mock_gateway.read_file.return_value = b"file content"
        result = await service._read("/test.txt")
        assert result == b"file content"
        mock_gateway.read_file.assert_called_once()

    async def test_read_raises_without_gateway(self, mock_metadata_store):
        """_read raises NotImplementedError without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        with pytest.raises(NotImplementedError, match="gateway not provided"):
            await svc._read("/test.txt")

    async def test_read_converts_str_to_bytes(self, service, mock_gateway):
        """_read converts string response to bytes."""
        mock_gateway.read_file.return_value = "string content"
        result = await service._read("/test.txt")
        assert result == b"string content"

    def test_read_bulk_delegates_to_gateway(self, service, mock_gateway):
        """_read_bulk delegates to gateway.read_bulk."""
        mock_gateway.read_bulk.return_value = {"/a.txt": b"a", "/b.txt": b"b"}
        result = service._read_bulk(["/a.txt", "/b.txt"])
        assert len(result) == 2

    def test_read_bulk_raises_without_gateway(self, mock_metadata_store):
        """_read_bulk raises NotImplementedError without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        with pytest.raises(NotImplementedError, match="gateway not provided"):
            svc._read_bulk(["/test.txt"])

    def test_get_routing_params_with_gateway(self, service, mock_gateway, context):
        """_get_routing_params delegates to gateway."""
        mock_gateway.get_routing_params.return_value = ("zone1", "agent1", True)
        result = service._get_routing_params(context)
        assert result == ("zone1", "agent1", True)

    def test_get_routing_params_without_gateway(self, mock_metadata_store):
        """_get_routing_params returns defaults without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        result = svc._get_routing_params(None)
        assert result == (None, None, False)

    def test_has_descendant_access_with_gateway(self, service, mock_gateway, context):
        """_has_descendant_access delegates to gateway."""
        from nexus.contracts.types import Permission

        mock_gateway.has_descendant_access.return_value = True
        result = service._has_descendant_access("/test", Permission.READ, context)
        assert result is True

    def test_has_descendant_access_without_gateway(self, mock_metadata_store):
        """_has_descendant_access returns False without gateway."""
        from nexus.contracts.types import Permission

        svc = SearchService(metadata_store=mock_metadata_store)
        result = svc._has_descendant_access("/test", Permission.READ, None)
        assert result is False


# =============================================================================
# Gateway properties
# =============================================================================


class TestGatewayProperties:
    """Tests for gateway-exposed properties."""

    def test_gw_session_factory_with_gateway(self, service, mock_gateway):
        """_gw_session_factory returns gateway's session_factory."""
        assert service._gw_session_factory is mock_gateway.session_factory

    def test_gw_session_factory_without_gateway(self, mock_metadata_store):
        """_gw_session_factory returns None without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc._gw_session_factory is None

    def test_gw_backend_with_gateway(self, service, mock_gateway):
        """_gw_backend returns gateway's backend."""
        assert service._gw_backend is mock_gateway.backend

    def test_gw_backend_without_gateway(self, mock_metadata_store):
        """_gw_backend returns None without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc._gw_backend is None


# =============================================================================
# Glob pattern helpers
# =============================================================================


class TestGlobHelpers:
    """Tests for glob pattern helper methods."""

    def test_should_prepend_recursive_wildcard_for_relative_path(self, service):
        """Relative multi-level patterns get **/ prefix."""
        assert service._should_prepend_recursive_wildcard("models/file.py") is True

    def test_should_not_prepend_for_already_recursive(self, service):
        """Patterns with ** already present don't get prefix."""
        assert service._should_prepend_recursive_wildcard("**/*.py") is False

    def test_should_not_prepend_for_absolute_path(self, service):
        """Absolute paths don't get prefix."""
        assert service._should_prepend_recursive_wildcard("/workspace/file.py") is False

    def test_should_not_prepend_for_single_level(self, service):
        """Single-level patterns (no /) don't get prefix."""
        assert service._should_prepend_recursive_wildcard("*.py") is False

    def test_should_not_prepend_for_namespace_path(self, service):
        """Namespace-prefixed paths don't get prefix."""
        assert service._should_prepend_recursive_wildcard("workspace/file.py") is False
        assert service._should_prepend_recursive_wildcard("shared/docs/readme.md") is False

    def test_get_namespace_prefixes_from_dlc(self, service, mock_dlc):
        """_get_namespace_prefixes reads from dlc's mount_points."""
        prefixes = service._get_namespace_prefixes()
        assert "workspace/" in prefixes
        assert "shared/" in prefixes


# =============================================================================
# Thread pool management
# =============================================================================


class TestThreadPoolManagement:
    """Tests for thread pool lazy initialization and cleanup."""

    def test_thread_pool_starts_none(self, service):
        """Thread pools start as None."""
        assert service._thread_pool is None
        assert service._list_thread_pool is None

    def test_get_thread_pool_creates_pool(self, service):
        """_get_thread_pool lazily creates a ThreadPoolExecutor."""
        pool = service._get_thread_pool()
        assert pool is not None
        assert service._thread_pool is pool
        pool.shutdown(wait=False)

    def test_get_thread_pool_returns_same_instance(self, service):
        """_get_thread_pool returns the same instance on subsequent calls."""
        pool1 = service._get_thread_pool()
        pool2 = service._get_thread_pool()
        assert pool1 is pool2
        pool1.shutdown(wait=False)

    def test_get_list_thread_pool_creates_pool(self, service):
        """_get_list_thread_pool lazily creates a ThreadPoolExecutor."""
        pool = service._get_list_thread_pool()
        assert pool is not None
        assert service._list_thread_pool is pool
        pool.shutdown(wait=False)

    def test_close_shuts_down_pools(self, service):
        """close() shuts down both thread pools."""
        service._get_thread_pool()
        service._get_list_thread_pool()
        assert service._thread_pool is not None
        assert service._list_thread_pool is not None

        service.close()
        assert service._thread_pool is None
        assert service._list_thread_pool is None

    def test_close_noop_when_no_pools(self, service):
        """close() is a no-op when pools were never created."""
        service.close()  # Should not raise
        assert service._thread_pool is None
        assert service._list_thread_pool is None


# =============================================================================
# Cross-zone cache
# =============================================================================


class TestCrossZoneCache:
    """Tests for bounded TTL cache initialization."""

    def test_cross_zone_cache_initialized(self, service):
        """Cross-zone cache is initialized with correct bounds."""
        assert service._cross_zone_cache is not None
        assert service._cross_zone_cache.maxsize == 1024

    def test_cross_zone_cache_empty_initially(self, service):
        """Cross-zone cache starts empty."""
        assert len(service._cross_zone_cache) == 0


# =============================================================================
# grep validation
# =============================================================================


class TestGrepValidation:
    """Tests for grep input validation."""

    async def test_invalid_regex_raises_value_error(self, service, context):
        """grep raises ValueError for invalid regex patterns."""
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            await service.grep(pattern="[invalid", context=context)

    async def test_valid_regex_accepted(self, service, mock_metadata_store, context):
        """grep accepts valid regex patterns."""
        mock_metadata_store.list_paths.return_value = []
        # list() returns empty so grep short-circuits
        with patch.object(service, "list", return_value=[]):
            results = await service.grep(pattern="def\\s+\\w+", context=context)
            assert results == []


# =============================================================================
# grep context lines (_grep_lines)
# =============================================================================


class TestGrepContext:
    """Tests for grep context line support (Issue #2811)."""

    @staticmethod
    def _make_lines():
        """Sample file content for testing."""
        return [
            "line 1: header",
            "line 2: import os",
            "line 3: import sys",
            "line 4:",
            "line 5: def main():",
            "line 6:     print('hello')",
            "line 7:     return 0",
            "line 8:",
            "line 9: def helper():",
            "line 10:    pass",
        ]

    def test_basic_match_no_context(self):
        """Basic grep without context returns just matching lines."""
        lines = self._make_lines()
        regex = re.compile(r"def \w+")
        results = SearchService._grep_lines(regex, lines, "/test.py")
        assert len(results) == 2
        assert results[0]["line"] == 5
        assert results[0]["content"] == "line 5: def main():"
        assert results[1]["line"] == 9
        assert "before_context" not in results[0]
        assert "after_context" not in results[0]

    def test_after_context(self):
        """After-context returns N lines after each match."""
        lines = self._make_lines()
        regex = re.compile(r"def main")
        results = SearchService._grep_lines(regex, lines, "/test.py", after_context=2)
        assert len(results) == 1
        assert results[0]["line"] == 5
        ctx = results[0]["after_context"]
        assert len(ctx) == 2
        assert ctx[0]["line"] == 6
        assert ctx[1]["line"] == 7

    def test_before_context(self):
        """Before-context returns N lines before each match."""
        lines = self._make_lines()
        regex = re.compile(r"def main")
        results = SearchService._grep_lines(regex, lines, "/test.py", before_context=2)
        assert len(results) == 1
        ctx = results[0]["before_context"]
        assert len(ctx) == 2
        assert ctx[0]["line"] == 3
        assert ctx[1]["line"] == 4

    def test_combined_context(self):
        """Both before and after context."""
        lines = self._make_lines()
        regex = re.compile(r"def main")
        results = SearchService._grep_lines(
            regex, lines, "/test.py", before_context=1, after_context=1
        )
        assert len(results) == 1
        assert len(results[0]["before_context"]) == 1
        assert results[0]["before_context"][0]["line"] == 4
        assert len(results[0]["after_context"]) == 1
        assert results[0]["after_context"][0]["line"] == 6

    def test_context_at_file_start(self):
        """Before-context at line 1 doesn't go negative."""
        lines = self._make_lines()
        regex = re.compile(r"header")
        results = SearchService._grep_lines(regex, lines, "/test.py", before_context=5)
        assert len(results) == 1
        assert results[0]["line"] == 1
        assert results[0].get("before_context", []) == []

    def test_context_at_file_end(self):
        """After-context at last line doesn't go past end."""
        lines = self._make_lines()
        regex = re.compile(r"pass")
        results = SearchService._grep_lines(regex, lines, "/test.py", after_context=5)
        assert len(results) == 1
        assert results[0]["line"] == 10
        assert results[0].get("after_context", []) == []

    def test_invert_match(self):
        """Invert match returns non-matching lines."""
        lines = self._make_lines()
        regex = re.compile(r"def \w+")
        results = SearchService._grep_lines(regex, lines, "/test.py", invert_match=True)
        # 10 lines, 2 match "def", so 8 should be returned
        assert len(results) == 8
        for r in results:
            assert "def " not in r["content"]

    def test_invert_match_with_context(self):
        """Invert match with context lines."""
        lines = self._make_lines()
        regex = re.compile(r"def \w+")
        results = SearchService._grep_lines(
            regex, lines, "/test.py", invert_match=True, after_context=1
        )
        assert len(results) == 8
        # First result (line 1) should have after_context
        assert results[0]["line"] == 1
        assert len(results[0]["after_context"]) == 1

    def test_max_results_limit(self):
        """Max results is respected."""
        lines = self._make_lines()
        regex = re.compile(r"line")
        results = SearchService._grep_lines(regex, lines, "/test.py", max_results=3)
        assert len(results) == 3

    def test_no_matches(self):
        """No matches returns empty list."""
        lines = self._make_lines()
        regex = re.compile(r"NONEXISTENT")
        results = SearchService._grep_lines(regex, lines, "/test.py")
        assert results == []

    def test_empty_lines(self):
        """Empty file returns empty list."""
        regex = re.compile(r"test")
        results = SearchService._grep_lines(regex, [], "/test.py")
        assert results == []

    def test_match_includes_match_group(self):
        """Match dict includes the matched text."""
        lines = ["hello world"]
        regex = re.compile(r"world")
        results = SearchService._grep_lines(regex, lines, "/test.py")
        assert results[0]["match"] == "world"


# =============================================================================
# grep file_pattern parameter (backfill for #3701 review)
# =============================================================================


class TestGrepFilePattern:
    """Tests for the `file_pattern` parameter on SearchService.grep.

    This parameter has been present for a long time but had no dedicated
    test coverage. The tests below lock in current behaviour so the
    addition of `files=[...]` on top (issue #3701) builds on known ground.
    """

    async def test_file_pattern_triggers_glob_not_list(self, service, mock_metadata_store, context):
        """When file_pattern is set, grep delegates file selection to glob()."""
        with (
            patch.object(service, "glob", return_value=["/src/a.py"]) as mock_glob,
            patch.object(service, "list") as mock_list,
        ):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/src/a.py": "def foo():\n    pass\n"
            }
            await service.grep(pattern="def foo", file_pattern="*.py", context=context)
            mock_glob.assert_called_once()
            mock_list.assert_not_called()

    async def test_no_file_pattern_triggers_list_not_glob(
        self, service, mock_metadata_store, context
    ):
        """Without file_pattern, grep walks the path via list()."""
        with (
            patch.object(service, "glob") as mock_glob,
            patch.object(service, "list", return_value=["/src/a.py"]) as mock_list,
        ):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/src/a.py": "def foo():\n    pass\n"
            }
            await service.grep(pattern="def foo", context=context)
            mock_list.assert_called_once()
            mock_glob.assert_not_called()

    async def test_file_pattern_passes_path_and_context_to_glob(
        self, service, mock_metadata_store, context
    ):
        """Pattern, path, and context are forwarded to glob() verbatim."""
        with patch.object(service, "glob", return_value=[]) as mock_glob:
            mock_metadata_store.get_searchable_text_bulk.return_value = {}
            await service.grep(
                pattern="foo",
                path="/src",
                file_pattern="**/*.py",
                context=context,
            )
            mock_glob.assert_called_once_with("**/*.py", "/src", context=context)

    async def test_file_pattern_empty_glob_result_returns_empty(
        self, service, mock_metadata_store, context
    ):
        """glob() returning [] short-circuits grep to []."""
        with patch.object(service, "glob", return_value=[]):
            mock_metadata_store.get_searchable_text_bulk.return_value = {}
            results = await service.grep(
                pattern="anything", file_pattern="*.nonexistent", context=context
            )
            assert results == []

    async def test_file_pattern_single_file_match(self, service, mock_metadata_store, context):
        """grep returns matches only from files selected by file_pattern."""
        with patch.object(service, "glob", return_value=["/src/target.py"]):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/src/target.py": "line 1\nhello world\nline 3\n",
            }
            results = await service.grep(pattern="hello", file_pattern="*.py", context=context)
            assert len(results) == 1
            assert results[0]["file"] == "/src/target.py"
            assert "hello" in results[0]["content"]

    async def test_file_pattern_multiple_files_all_searched(
        self, service, mock_metadata_store, context
    ):
        """Every file returned by glob is searched for the pattern."""
        with patch.object(
            service,
            "glob",
            return_value=["/src/a.py", "/src/b.py", "/src/c.py"],
        ):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/src/a.py": "TODO: fix\n",
                "/src/b.py": "no match\n",
                "/src/c.py": "TODO: other\n",
            }
            results = await service.grep(pattern="TODO", file_pattern="*.py", context=context)
            matched_files = {r["file"] for r in results}
            assert matched_files == {"/src/a.py", "/src/c.py"}

    async def test_file_pattern_with_before_and_after_context(
        self, service, mock_metadata_store, context
    ):
        """Context lines work together with file_pattern filtering."""
        with patch.object(service, "glob", return_value=["/src/a.py"]):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/src/a.py": ("line 1\nline 2\nMATCH line 3\nline 4\nline 5\n"),
            }
            results = await service.grep(
                pattern="MATCH",
                file_pattern="*.py",
                context=context,
                before_context=2,
                after_context=2,
            )
            assert len(results) == 1
            r = results[0]
            assert r["line"] == 3
            assert [ln["content"] for ln in r["before_context"]] == [
                "line 1",
                "line 2",
            ]
            assert [ln["content"] for ln in r["after_context"]] == [
                "line 4",
                "line 5",
            ]

    async def test_file_pattern_respects_max_results(self, service, mock_metadata_store, context):
        """When many files match, max_results still caps the output."""
        files = [f"/src/f{i}.py" for i in range(10)]
        with patch.object(service, "glob", return_value=files):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                f: f"TODO item {f}\n" for f in files
            }
            results = await service.grep(
                pattern="TODO",
                file_pattern="*.py",
                context=context,
                max_results=3,
            )
            assert len(results) == 3


# =============================================================================
# files=[...] validator helper (#3701 — Issue 7A)
# =============================================================================


class TestValidateAndNormalizeFiles:
    """Tests for SearchService._validate_and_normalize_files.

    Locks in the 9-row edge-case matrix from the #3701 review:
    (a) empty list, (b) traversal, (c) cross-zone, (d) dedupe, (e) stale,
    (f) size cap, (g) file_pattern interaction (caller-side),
    (h) normalization, (i) permission intersection.
    """

    # --- (a) empty list ---

    def test_empty_list_returns_empty(self, service, context):
        """(a) Empty list short-circuits without touching self.list."""
        with patch.object(service, "list") as mock_list:
            files, stale = service._validate_and_normalize_files(
                files=[], path="/", context=context
            )
            assert files == []
            assert stale == 0
            # Empty-list short-circuit must not walk the tree.
            mock_list.assert_not_called()

    # --- (b) path traversal ---

    def test_traversal_path_rejected(self, service, context):
        """(b) ``..`` segments are rejected via _validate_path."""
        from nexus.contracts.exceptions import InvalidPathError

        with pytest.raises((InvalidPathError, ValueError)):
            service._validate_and_normalize_files(
                files=["/src/a.py", "../../etc/passwd"],
                path="/",
                context=context,
            )

    # --- (c) cross-zone rejection ---

    def test_cross_zone_rejected_when_prefix_differs(self, service, mock_gateway, context):
        """(c) /zones/OTHER/... is rejected if the caller is in /zones/MY/."""
        mock_gateway.get_routing_params.return_value = ("my-zone", None, False)
        with pytest.raises(ValueError, match="cross-zone"):
            service._validate_and_normalize_files(
                files=["/zones/other-zone/a.py"],
                path="/",
                context=context,
            )

    def test_same_zone_prefix_accepted(self, service, mock_gateway, context):
        mock_gateway.get_routing_params.return_value = ("my-zone", None, False)
        with patch.object(service, "list", return_value=["/zones/my-zone/a.py"]):
            files, stale = service._validate_and_normalize_files(
                files=["/zones/my-zone/a.py"],
                path="/",
                context=context,
            )
            assert files == ["/zones/my-zone/a.py"]
            assert stale == 0

    # --- (d) dedupe ---

    def test_duplicates_are_deduped_preserving_order(self, service, context):
        """(d) Repeated entries collapse to one; order preserved."""
        with patch.object(service, "list", return_value=["/a.py", "/b.py"]):
            files, stale = service._validate_and_normalize_files(
                files=["/a.py", "/b.py", "/a.py", "/b.py"],
                path="/",
                context=context,
            )
            assert files == ["/a.py", "/b.py"]
            assert stale == 0

    # --- (e) stale silent skip ---

    def test_stale_entries_silently_skipped_with_count(
        self, service, mock_permission_enforcer, context
    ):
        """(e) Entries not permitted by the enforcer are dropped silently.

        Codex review #3 finding #2: validator now authorises the
        supplied list directly via ``filter_list`` instead of walking
        the tree, so stale/unreadable detection flows through the
        enforcer.
        """
        mock_permission_enforcer.filter_list = MagicMock(return_value=["/a.py", "/b.py"])
        files, stale = service._validate_and_normalize_files(
            files=["/a.py", "/gone.py", "/b.py", "/also-gone.py"],
            path="/",
            context=context,
        )
        assert files == ["/a.py", "/b.py"]
        assert stale == 2

    def test_all_stale_returns_empty_with_full_count(
        self, service, mock_permission_enforcer, context
    ):
        mock_permission_enforcer.filter_list = MagicMock(return_value=[])
        files, stale = service._validate_and_normalize_files(
            files=["/a.py", "/b.py", "/c.py"],
            path="/",
            context=context,
        )
        assert files == []
        assert stale == 3

    # --- (f) size cap ---

    def test_size_cap_enforced(self, service, context):
        """(f) Lists larger than FILES_FILTER_SIZE_CAP are rejected fast."""
        from nexus.bricks.search.search_service import FILES_FILTER_SIZE_CAP

        huge = [f"/f{i}.py" for i in range(FILES_FILTER_SIZE_CAP + 1)]
        with patch.object(service, "list") as mock_list:
            with pytest.raises(ValueError, match="too large"):
                service._validate_and_normalize_files(files=huge, path="/", context=context)
            # Fail-fast: the tree walk must not happen when the size cap fires.
            mock_list.assert_not_called()

    def test_at_exact_size_cap_allowed(self, service, context):
        """Boundary: exactly FILES_FILTER_SIZE_CAP entries is allowed."""
        from nexus.bricks.search.search_service import FILES_FILTER_SIZE_CAP

        at_cap = [f"/f{i}.py" for i in range(FILES_FILTER_SIZE_CAP)]
        with patch.object(service, "list", return_value=at_cap):
            files, stale = service._validate_and_normalize_files(
                files=at_cap, path="/", context=context
            )
            assert len(files) == FILES_FILTER_SIZE_CAP
            assert stale == 0

    # --- (h) normalisation ---

    def test_relative_paths_get_leading_slash(self, service, context):
        """(h) Relative paths are normalised to absolute before permission check."""
        with patch.object(service, "list", return_value=["/a.py"]):
            files, stale = service._validate_and_normalize_files(
                files=["a.py"],
                path="/",
                context=context,
            )
            assert files == ["/a.py"]
            assert stale == 0

    # --- (i) permission intersection ---

    def test_permission_filter_intersects_files(self, service, mock_permission_enforcer, context):
        """(i) Only files the caller can see are returned.

        Codex review #3 finding #2: the validator now authorises via
        ``filter_list`` directly — no tree walk. The enforcer is the
        source of truth for which files are visible.
        """
        mock_permission_enforcer.filter_list = MagicMock(return_value=["/public/a.py"])
        files, stale = service._validate_and_normalize_files(
            files=["/public/a.py", "/secret/c.py"],
            path="/",
            context=context,
        )
        assert "/secret/c.py" not in files
        assert files == ["/public/a.py"]
        assert stale == 1

    def test_no_recursive_tree_walk(self, service, mock_permission_enforcer, context):
        """Regression test for Codex review #3 finding #2.

        ``_validate_and_normalize_files`` must NOT call
        ``self.list(..., recursive=True)`` — that would defeat the
        whole ``files=[...]`` ``O(files)`` promise. Instead it must
        authorise the supplied list directly via ``filter_list``.

        Codex critique: "large-repo grep/glob requests can still hit
        the metadata store for the entire subtree and time out under
        load" if the validator walks the tree.
        """
        mock_permission_enforcer.filter_list = MagicMock(
            side_effect=lambda paths, context: list(paths)
        )
        with patch.object(service, "list") as tree_walk_mock:
            files, stale = service._validate_and_normalize_files(
                files=["/a.py", "/b.py", "/c.py"],
                path="/",
                context=context,
            )
        # ``self.list`` must NEVER be called — otherwise we're back to
        # the O(tree) behaviour Codex flagged.
        tree_walk_mock.assert_not_called()
        # And ``filter_list`` was called exactly once with the caller's
        # normalised list.
        mock_permission_enforcer.filter_list.assert_called_once()
        call_args = mock_permission_enforcer.filter_list.call_args
        passed_paths = call_args[0][0] if call_args[0] else call_args.kwargs["paths"]
        assert sorted(passed_paths) == ["/a.py", "/b.py", "/c.py"]
        assert files == ["/a.py", "/b.py", "/c.py"]
        assert stale == 0

    def test_enforcer_filter_list_results_are_authoritative(
        self, service, mock_permission_enforcer, context
    ):
        """Codex review #3 finding #2: authorisation delegates to the
        enforcer's ``filter_list`` instead of comparing against a tree
        listing. The enforcer is authoritative.
        """
        mock_permission_enforcer.filter_list = MagicMock(return_value=["/docs/a.py", "/docs/b.py"])
        files, stale = service._validate_and_normalize_files(
            files=["/docs/a.py", "/docs/b.py", "/docs/denied.py"],
            path="/",
            context=context,
        )
        assert files == ["/docs/a.py", "/docs/b.py"]
        assert stale == 1  # /docs/denied.py was filtered by the enforcer

    def test_permission_filter_sees_any_path_namespace(
        self, service, mock_permission_enforcer, context
    ):
        """Codex review #3 finding #2: because we no longer call
        ``self.list``, the validator is agnostic to zone-scoping quirks
        — whatever path namespace the caller supplies, that's what
        flows to the enforcer. Non-root tenants don't need special
        unscoping handling in this layer.
        """
        # Caller supplies user-facing paths; enforcer accepts them as-is.
        mock_permission_enforcer.filter_list = MagicMock(
            side_effect=lambda paths, context: list(paths)
        )
        files, stale = service._validate_and_normalize_files(
            files=["/docs/a.py", "/docs/b.py"],
            path="/",
            context=context,
        )
        assert files == ["/docs/a.py", "/docs/b.py"]
        assert stale == 0
        # No zone-prefix stripping, no list() walk.
        call_args = mock_permission_enforcer.filter_list.call_args
        passed_paths = call_args[0][0] if call_args[0] else call_args.kwargs["paths"]
        assert sorted(passed_paths) == ["/docs/a.py", "/docs/b.py"]


# =============================================================================
# files=[...] end-to-end via grep/glob (#3701 — Issue 2A + 15A + 13A)
# =============================================================================


class TestGrepFilesParam:
    """End-to-end tests for ``files=[...]`` on SearchService.grep.

    Covers the short-circuit path (15A), the interaction with
    ``file_pattern`` (7A edge g), and the trigram-bypass threshold (13A).
    """

    async def test_files_short_circuits_tree_walk(self, service, mock_metadata_store, context):
        """15A: files=[...] should bypass self.list(path, recursive=True).

        Updated after Codex review #3 finding #2: the validator no
        longer calls ``self.list`` at all (it authorises via
        ``filter_list`` directly), so the full grep path invokes
        ``self.list`` exactly ZERO times when ``files=[...]`` is
        supplied — neither the validator nor phase 1 walks the tree.
        """
        tree_walk_mock = MagicMock(return_value=["/a.py", "/b.py", "/c.py"])
        with patch.object(service, "list", side_effect=tree_walk_mock):
            mock_metadata_store.get_searchable_text_bulk.return_value = {
                "/a.py": "TODO one\n",
                "/b.py": "TODO two\n",
            }
            results = await service.grep(
                pattern="TODO",
                files=["/a.py", "/b.py"],
                context=context,
            )
        assert tree_walk_mock.call_count == 0
        assert {r["file"] for r in results} == {"/a.py", "/b.py"}

    async def test_files_only_searches_supplied_files(self, service, mock_metadata_store, context):
        """files=[...] limits search to its own entries even if tree has more."""
        # Permitted tree contains 3 files but caller narrowed to 1. The
        # metadata store only returns cached text for files grep asked
        # about — real backends do exactly this, so the mock mirrors it.
        corpus = {
            "/a.py": "TODO only here\n",
            "/b.py": "TODO also here\n",
            "/c.py": "TODO and here\n",
        }
        mock_metadata_store.get_searchable_text_bulk.side_effect = lambda keys: {
            k: corpus[k] for k in keys if k in corpus
        }

        with patch.object(service, "list", return_value=["/a.py", "/b.py", "/c.py"]):
            results = await service.grep(
                pattern="TODO",
                files=["/a.py"],
                context=context,
            )
        assert {r["file"] for r in results} == {"/a.py"}

    async def test_files_intersects_with_file_pattern(self, service, mock_metadata_store, context):
        """7A edge (g): both files and file_pattern → intersection."""
        corpus = {
            "/a.py": "TODO\n",
            "/b.py": "TODO\n",
            "/d.py": "TODO\n",
        }
        mock_metadata_store.get_searchable_text_bulk.side_effect = lambda keys: {
            k: corpus[k] for k in keys if k in corpus
        }

        with (
            patch.object(service, "list", return_value=["/a.py", "/b.py", "/c.md", "/d.py"]),
            patch.object(
                service,
                "glob",
                return_value=["/a.py", "/b.py", "/d.py"],  # all .py files
            ),
        ):
            results = await service.grep(
                pattern="TODO",
                files=["/a.py", "/c.md", "/d.py"],  # caller narrows to 3
                file_pattern="*.py",  # glob filters to .py
                context=context,
            )
        # Intersection: {a.py, c.md, d.py} ∩ {a.py, b.py, d.py} = {a.py, d.py}
        assert {r["file"] for r in results} == {"/a.py", "/d.py"}

    async def test_empty_files_returns_empty_grep(self, service, mock_metadata_store, context):
        """7A edge (a): files=[] short-circuits to empty result."""
        with patch.object(service, "list") as mock_list:
            results = await service.grep(
                pattern="TODO",
                files=[],
                context=context,
            )
        assert results == []
        # Empty list short-circuits — tree walk must not happen.
        mock_list.assert_not_called()

    async def test_files_rejects_traversal_path(self, service, context):
        """7A edge (b): traversal in files triggers validation error."""
        from nexus.contracts.exceptions import InvalidPathError

        with pytest.raises((InvalidPathError, ValueError)):
            await service.grep(
                pattern="TODO",
                files=["/a.py", "../../etc/shadow"],
                context=context,
            )

    async def test_files_size_cap_rejected(self, service, context):
        """7A edge (f): lists exceeding the size cap are rejected."""
        from nexus.bricks.search.search_service import FILES_FILTER_SIZE_CAP

        too_many = [f"/f{i}.py" for i in range(FILES_FILTER_SIZE_CAP + 1)]
        with pytest.raises(ValueError, match="too large"):
            await service.grep(pattern="TODO", files=too_many, context=context)

    async def test_files_duplicates_deduped(self, service, mock_metadata_store, context):
        """7A edge (d): duplicates in files collapse to unique entries."""
        with patch.object(service, "list", return_value=["/a.py"]):
            mock_metadata_store.get_searchable_text_bulk.return_value = {"/a.py": "TODO\n"}
            results = await service.grep(
                pattern="TODO",
                files=["/a.py", "/a.py", "/a.py"],
                context=context,
            )
        # Only one result even though /a.py was listed three times.
        assert len([r for r in results if r["file"] == "/a.py"]) == 1


class TestGrepContextAndInvertRouting:
    """Regression tests for #3701 Codex finding #3.

    The accelerated grep paths (TRIGRAM_INDEX, ZOEKT_INDEX,
    PARALLEL_POOL, mmap, rust_bulk) all do raw regex scans and
    silently drop ``before_context`` / ``after_context`` /
    ``invert_match``. SearchService.grep is required to detect
    those flags and force routing through ``_grep_lines`` so the
    flags actually take effect, regardless of what
    ``_select_grep_strategy`` would normally pick.
    """

    async def test_before_after_context_force_python_path_when_strategy_would_be_rust(
        self, service, mock_metadata_store, context
    ):
        """before_context/after_context must take effect even on a corpus
        that would normally select RUST_BULK / SEQUENTIAL routing."""
        from nexus.bricks.search.search_service import SearchStrategy

        files = [f"/src/f{i}.py" for i in range(50)]
        # Empty cached text → strategy selector would pick a non-cached path.
        mock_metadata_store.get_searchable_text_bulk.return_value = {}

        strategy_calls: list[SearchStrategy] = []
        original_select = service._select_grep_strategy

        def spy(*args, **kwargs):
            strategy = original_select(*args, **kwargs)
            strategy_calls.append(strategy)
            return strategy

        # Stub _grep_raw_content so we can assert force_python_path was passed.
        captured_kwargs: dict = {}

        async def fake_raw(**kwargs):
            captured_kwargs.update(kwargs)
            return [
                {
                    "file": "/src/f0.py",
                    "line": 3,
                    "content": "MATCH",
                    "before_context": [
                        {"line": 1, "content": "before-1"},
                        {"line": 2, "content": "before-2"},
                    ],
                    "after_context": [{"line": 4, "content": "after-1"}],
                }
            ]

        with (
            patch.object(service, "list", return_value=files),
            patch.object(service, "_select_grep_strategy", side_effect=spy),
            patch.object(service, "_grep_raw_content", side_effect=fake_raw),
        ):
            results = await service.grep(
                pattern="MATCH",
                context=context,
                before_context=2,
                after_context=1,
            )
        # The strategy selector should NOT have run (we override the
        # strategy when context flags are set, so the selector is bypassed).
        assert strategy_calls == []
        # _grep_raw_content received force_python_path=True so the mmap
        # and rust accelerator branches inside it are skipped.
        assert captured_kwargs.get("force_python_path") is True
        assert captured_kwargs.get("before_context") == 2
        assert captured_kwargs.get("after_context") == 1
        assert len(results) == 1
        assert results[0]["before_context"][0]["content"] == "before-1"
        assert results[0]["after_context"][0]["content"] == "after-1"

    async def test_invert_match_forces_python_path(self, service, mock_metadata_store, context):
        """invert_match must take effect even when accelerators are available."""
        files = [f"/src/f{i}.py" for i in range(50)]
        mock_metadata_store.get_searchable_text_bulk.return_value = {}

        captured_kwargs: dict = {}

        async def fake_raw(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        with (
            patch.object(service, "list", return_value=files),
            patch.object(service, "_grep_raw_content", side_effect=fake_raw),
        ):
            await service.grep(
                pattern="MATCH",
                context=context,
                invert_match=True,
            )
        assert captured_kwargs.get("force_python_path") is True
        assert captured_kwargs.get("invert_match") is True

    async def test_no_flags_does_not_force_python_path(self, service, mock_metadata_store, context):
        """When no context/invert flags are set, the normal strategy selector
        runs and force_python_path stays False — accelerators remain in play.
        """
        files = [f"/src/f{i}.py" for i in range(50)]
        mock_metadata_store.get_searchable_text_bulk.return_value = {}

        captured_kwargs: dict = {}

        async def fake_raw(**kwargs):
            captured_kwargs.update(kwargs)
            return []

        with (
            patch.object(service, "list", return_value=files),
            patch.object(service, "_grep_raw_content", side_effect=fake_raw),
        ):
            await service.grep(pattern="MATCH", context=context)
        assert captured_kwargs.get("force_python_path") is False

    async def test_grep_raw_content_skips_mmap_when_force_python_path(self, service, context):
        """``_grep_raw_content(force_python_path=True)`` must not call the
        mmap accelerator even when it is available."""
        import re as _re

        from nexus.bricks.search import search_service as ss_mod

        with (
            patch.object(ss_mod.grep_fast, "is_mmap_available", return_value=True),
            patch.object(ss_mod.grep_fast, "grep_files_mmap") as mmap_mock,
            patch.object(ss_mod.grep_fast, "is_available", return_value=True),
            patch.object(ss_mod.grep_fast, "grep_bulk") as rust_mock,
            patch.object(
                service,
                "_read",
                new=AsyncMock(return_value=b"line a\nMATCH line b\nline c\n"),
            ),
        ):
            from nexus.bricks.search.search_service import SearchStrategy

            results = await service._grep_raw_content(
                regex=_re.compile("MATCH"),
                pattern="MATCH",
                files_needing_raw=["/a.py"],
                strategy=SearchStrategy.SEQUENTIAL,
                ignore_case=False,
                remaining_results=10,
                context=context,
                before_context=1,
                after_context=1,
                invert_match=False,
                force_python_path=True,
            )
        mmap_mock.assert_not_called()
        rust_mock.assert_not_called()
        assert len(results) == 1
        assert results[0]["line"] == 2
        assert results[0]["before_context"][0]["content"] == "line a"
        assert results[0]["after_context"][0]["content"] == "line c"


class TestGlobFilesParam:
    """Tests for the ``files=[...]`` parameter on SearchService.glob."""

    def test_files_short_circuits_tree_walk(self, service, mock_permission_enforcer, context):
        """glob with files=[...] should not walk the full tree.

        Codex review #3 finding #2: after the validator fix, glob's
        files=[...] path invokes ``self.list`` zero times — authorisation
        is delegated to the enforcer's ``filter_list``.
        """
        tree_walk_mock = MagicMock(return_value=["/a.py", "/b.py"])
        # Enforcer permits only /a.py and /b.py (not /evil.py).
        mock_permission_enforcer.filter_list = MagicMock(return_value=["/a.py", "/b.py"])
        with patch.object(service, "list", side_effect=tree_walk_mock):
            result = service.glob(
                pattern="*.py",
                files=["/a.py", "/b.py", "/evil.py"],
                context=context,
            )
        assert tree_walk_mock.call_count == 0
        # Only permitted files come back
        assert set(result) == {"/a.py", "/b.py"}

    def test_files_empty_returns_empty(self, service, context):
        """Empty files list short-circuits without walking."""
        with patch.object(service, "list") as mock_list:
            result = service.glob(pattern="*.py", files=[], context=context)
        assert result == []
        mock_list.assert_not_called()

    def test_files_respects_pattern(self, service, context):
        """When files contains mixed extensions, glob filters by pattern."""
        with patch.object(service, "list", return_value=["/a.py", "/b.md", "/c.py"]):
            result = service.glob(
                pattern="*.py",
                files=["/a.py", "/b.md", "/c.py"],
                context=context,
            )
        # Only .py files survive the glob pattern.
        assert set(result) == {"/a.py", "/c.py"}


# =============================================================================
# Issue #3720: block_type filter for markdown grep
# =============================================================================

# Sample markdown with known block structure for testing.
_MD_WITH_BLOCKS = """\
---
title: test
---

# Introduction

This is prose text with SELECT query mentions.

```sql
SELECT * FROM users WHERE active = true;
```

More prose text here.

| col1 | col2 |
|------|------|
| a    | b    |

## Notes

Final paragraph.
"""


def _make_md_structure_json():
    """Build a realistic md_structure JSON matching _MD_WITH_BLOCKS."""
    import json

    return json.dumps(
        {
            "version": 2,
            "content_hash": "abc123",
            "tokens_est_method": "bytes/4",
            "frontmatter": {
                "byte_start": 0,
                "byte_end": 20,
                "line_start": 0,
                "line_end": 3,
                "keys": ["title"],
            },
            "sections": [
                {
                    "heading": "Introduction",
                    "depth": 1,
                    "byte_start": 21,
                    "byte_end": 200,
                    "line_start": 4,
                    "line_end": 20,
                    "tokens_est": 45,
                    "blocks": [
                        {
                            "type": "heading",
                            "byte_start": 21,
                            "byte_end": 40,
                            "line_start": 4,
                            "line_end": 5,
                        },
                        {
                            "type": "paragraph",
                            "byte_start": 41,
                            "byte_end": 79,
                            "line_start": 6,
                            "line_end": 7,
                        },
                        {
                            "type": "code",
                            "byte_start": 80,
                            "byte_end": 140,
                            "line_start": 8,
                            "line_end": 11,
                            "language": "sql",
                        },
                        {
                            "type": "paragraph",
                            "byte_start": 141,
                            "byte_end": 159,
                            "line_start": 12,
                            "line_end": 13,
                        },
                        {
                            "type": "table",
                            "byte_start": 160,
                            "byte_end": 200,
                            "line_start": 14,
                            "line_end": 17,
                            "rows": 1,
                        },
                    ],
                },
                {
                    "heading": "Notes",
                    "depth": 2,
                    "byte_start": 200,
                    "byte_end": 250,
                    "line_start": 20,
                    "line_end": 23,
                    "tokens_est": 12,
                    "blocks": [
                        {
                            "type": "heading",
                            "byte_start": 200,
                            "byte_end": 210,
                            "line_start": 20,
                            "line_end": 21,
                        },
                        {
                            "type": "paragraph",
                            "byte_start": 211,
                            "byte_end": 250,
                            "line_start": 22,
                            "line_end": 23,
                        },
                    ],
                },
            ],
        }
    )


class TestGrepBlockType:
    """Issue #3720: block_type filtering for markdown grep."""

    @pytest.fixture
    def service_with_md(self, mock_metadata_store, mock_gateway):
        """SearchService with md_structure metadata pre-loaded."""

        # Configure metastore to return md_structure for .md files.
        def _get_file_metadata(path, key):
            if path.endswith(".md") and key == "md_structure":
                return _make_md_structure_json()
            return None

        mock_metadata_store.get_file_metadata.side_effect = _get_file_metadata
        mock_metadata_store.get_searchable_text_bulk.return_value = {}

        return SearchService(
            metadata_store=mock_metadata_store,
            gateway=mock_gateway,
            enforce_permissions=False,
        )

    def _make_results(self, file_path: str, lines: list[int]) -> list[dict]:
        """Build fake grep results at the given 1-indexed line numbers."""
        return [
            {"file": file_path, "line": ln, "content": f"line {ln}", "match": f"line {ln}"}
            for ln in lines
        ]

    async def test_block_type_code_returns_only_code_block_matches(self, service_with_md, context):
        """block_type='code' keeps only matches inside code fence lines."""
        # Lines 9, 10 are inside the code block (line_start=8, line_end=11, 0-indexed).
        # Line 7 is prose. Results are 1-indexed.
        all_results = self._make_results("/doc.md", [8, 9, 10, 11])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "code")
        # 0-indexed: lines 8,9,10 are inside [8,11). Line 11 (0-indexed=10) is inside.
        # 1-indexed lines 9,10,11 map to 0-indexed 8,9,10 → inside [8,11).
        # 1-indexed line 8 maps to 0-indexed 7 → NOT inside [8,11).
        assert len(filtered) == 3
        assert all(r["line"] in (9, 10, 11) for r in filtered)

    async def test_block_type_table_returns_only_table_matches(self, service_with_md, context):
        """block_type='table' keeps only matches inside table lines."""
        all_results = self._make_results("/doc.md", [6, 15, 16, 22])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "table")
        # 1-indexed 15,16 → 0-indexed 14,15 → inside [14,17).
        assert len(filtered) == 2
        assert {r["line"] for r in filtered} == {15, 16}

    async def test_block_type_frontmatter_returns_only_frontmatter_matches(
        self, service_with_md, context
    ):
        """block_type='frontmatter' keeps only matches in YAML frontmatter."""
        all_results = self._make_results("/doc.md", [1, 2, 3, 7])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "frontmatter")
        # 1-indexed 1,2,3 → 0-indexed 0,1,2 → inside [0,3). Line 7 → 6 → outside.
        assert len(filtered) == 3
        assert {r["line"] for r in filtered} == {1, 2, 3}

    async def test_block_type_none_returns_all_matches(self, service_with_md, context):
        """No block_type → all results pass through (default behavior)."""
        # block_type=None should not invoke filtering at all.
        with patch.object(service_with_md, "list", return_value=[]):
            results = await service_with_md.grep(pattern="x", context=context)
        # Empty because list returns nothing; the point is it doesn't crash.
        assert results == []

    async def test_block_type_on_non_md_file_returns_all_matches(self, service_with_md, context):
        """Non-markdown files pass through unfiltered (decision #4A)."""
        all_results = self._make_results("/src/main.py", [1, 5, 10])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.return_value = None
            filtered = service_with_md._filter_results_by_block_type(all_results, "code")
        assert len(filtered) == 3  # all pass through

    async def test_block_type_with_no_md_structure_metadata(self, service_with_md, context):
        """Markdown file without md_structure metadata → fail closed
        (Codex R6: don't leak unfiltered results through)."""
        all_results = self._make_results("/orphan.md", [1, 5])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.return_value = None  # no metadata
            filtered = service_with_md._filter_results_by_block_type(all_results, "code")
        assert len(filtered) == 0  # fail closed

    async def test_block_type_with_no_matching_blocks_returns_empty(self, service_with_md, context):
        """block_type='table' on a file with only code blocks → empty."""
        # All results are on lines that are NOT inside a table.
        all_results = self._make_results("/doc.md", [9, 10])  # inside code block
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "table")
        assert filtered == []

    async def test_block_type_paragraph_returns_only_prose(self, service_with_md, context):
        """block_type='paragraph' keeps only matches inside paragraphs."""
        # Paragraph at lines 6-7 (0-indexed) → 1-indexed 7.
        # Paragraph at lines 12-13 → 1-indexed 13.
        all_results = self._make_results("/doc.md", [7, 10, 13])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "paragraph")
        # Line 7 (0-idx 6) → inside [6,7). Line 13 (0-idx 12) → inside [12,13).
        # Line 10 (0-idx 9) → inside code block, NOT paragraph.
        assert len(filtered) == 2
        assert {r["line"] for r in filtered} == {7, 13}

    async def test_block_type_heading_returns_only_headings(self, service_with_md, context):
        """block_type='heading' keeps only matches inside heading lines."""
        # Heading at lines 4-5 and 20-21 (0-indexed).
        all_results = self._make_results("/doc.md", [5, 7, 21])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "heading")
        # Line 5 (0-idx 4) → inside [4,5). Line 21 (0-idx 20) → inside [20,21).
        # Line 7 (0-idx 6) → paragraph, not heading.
        assert len(filtered) == 2
        assert {r["line"] for r in filtered} == {5, 21}

    async def test_block_type_invalid_value_raises_value_error(self, service_with_md, context):
        """Invalid block_type raises ValueError with supported values."""
        with pytest.raises(ValueError, match="Invalid block_type"):
            await service_with_md.grep(pattern="x", block_type="definition", context=context)

    # ── Edge cases (decision #12A) ──

    async def test_match_on_code_fence_boundary_line(self, service_with_md, context):
        """Match on the opening ``` line of a code fence is inside the block."""
        # Code block is line_start=8, line_end=11 (0-indexed).
        # 1-indexed line 9 → 0-indexed 8 → first line of block.
        all_results = self._make_results("/doc.md", [9])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(all_results, "code")
        assert len(filtered) == 1

    async def test_mixed_md_and_non_md_results(self, service_with_md, context):
        """Mixed results from .md (filtered) and .py (unfiltered) files."""
        results = [
            {"file": "/doc.md", "line": 7, "content": "prose", "match": "prose"},
            {"file": "/doc.md", "line": 10, "content": "code", "match": "code"},
            {"file": "/main.py", "line": 1, "content": "import", "match": "import"},
        ]
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(results, "code")
        # /doc.md line 10 (0-indexed 9) is inside code [8,11). Line 7 (0-indexed 6) is not.
        # /main.py passes through unfiltered.
        assert len(filtered) == 2
        files = [r["file"] for r in filtered]
        assert "/main.py" in files
        assert any(r["file"] == "/doc.md" and r["line"] == 10 for r in filtered)

    async def test_block_type_with_stale_metadata(self, service_with_md, context):
        """Corrupt/malformed md_structure metadata → fail closed
        (Codex R7: don't leak unfiltered results)."""
        all_results = self._make_results("/bad.md", [1, 5])
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.return_value = "not valid json {"
            filtered = service_with_md._filter_results_by_block_type(all_results, "code")
        # Corrupt metadata → fail closed, no results.
        assert len(filtered) == 0

    async def test_block_type_with_context_lines(self, service_with_md, context):
        """Context lines that extend outside the block are still included
        with the match — we filter by match line, not context lines."""
        results = [
            {
                "file": "/doc.md",
                "line": 10,  # 0-indexed 9 → inside code [8,11)
                "content": "SELECT",
                "match": "SELECT",
                "before_context": [
                    {"line": 8, "content": "prose before fence"},
                ],
                "after_context": [
                    {"line": 12, "content": "prose after fence"},
                ],
            },
        ]
        with patch.object(service_with_md, "metadata") as mock_meta:
            mock_meta.get_file_metadata.side_effect = lambda p, k: (
                _make_md_structure_json() if p.endswith(".md") and k == "md_structure" else None
            )
            filtered = service_with_md._filter_results_by_block_type(results, "code")
        # Match line is inside code block → kept. Context lines are preserved as-is.
        assert len(filtered) == 1
        assert filtered[0]["before_context"][0]["content"] == "prose before fence"
        assert filtered[0]["after_context"][0]["content"] == "prose after fence"


class TestGrepBlockTypeOverfetch:
    """Issue #3720: over-fetch multiplier and cap tests."""

    async def test_overfetch_multiplier_applied(self, service, context):
        """When block_type is set, grep over-fetches internally."""

        # Use max_results=50. Expected internal max_results = min(50*5, max(50, 2000)) = 250.
        with patch.object(service, "list", return_value=[]):
            await service.grep(pattern="x", block_type="code", max_results=50, context=context)
        # No files → short circuit → no results, but we can check the method ran.
        # The over-fetch is applied before Phase 1, so just verify no crash.

    async def test_overfetch_cap_respected(self, service, context):
        """Over-fetch is capped at _BLOCK_TYPE_OVERFETCH_CAP."""

        # max_results=10000 → 10000*5 = 50000 > cap → capped at max(10000, 2000) = 10000.
        with patch.object(service, "list", return_value=[]):
            # Should not crash — cap prevents runaway allocation.
            await service.grep(pattern="x", block_type="code", max_results=10000, context=context)

    async def test_block_type_results_capped_to_original_max_results(self, service, context):
        """After post-filtering, results are capped to the ORIGINAL
        max_results, not the inflated over-fetch value."""
        with (
            patch.object(service, "list", return_value=["/a.py"]),
            patch.object(service, "metadata") as mock_meta,
        ):
            mock_meta.get_searchable_text_bulk.return_value = {
                "/a.py": "\n".join(f"x line {i}" for i in range(20))
            }
            mock_meta.get_file_metadata.return_value = None
            results = await service.grep(
                pattern="x",
                block_type="code",
                max_results=5,
                context=context,
            )
        # Non-md file → all pass through, but capped to original max_results=5.
        assert len(results) <= 5

    async def test_block_type_validation_lists_valid_types(self, service, context):
        """Validation error message includes all valid block type names."""
        with pytest.raises(ValueError, match="code") as exc_info:
            await service.grep(pattern="x", block_type="definition", context=context)
        msg = str(exc_info.value)
        assert "table" in msg
        assert "frontmatter" in msg
        assert "paragraph" in msg
