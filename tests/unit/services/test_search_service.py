"""Unit tests for SearchService.

Tests initialization, glob search, grep search, file listing,
helper methods, and error handling for missing dependencies.

SearchService uses dependency injection with MetastoreABC,
PermissionEnforcer, PathRouter, and NexusFSGateway.
"""

from unittest.mock import MagicMock, patch

import pytest

from nexus.contracts.types import OperationContext
from nexus.services.search.search_service import (
    DEFAULT_IGNORE_PATTERNS,
    SearchService,
    _filter_ignored_paths,
    _should_ignore_path,
)

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
    """Create a mock PermissionEnforcer (permissive by default)."""
    enforcer = MagicMock()
    enforcer.check_permission.return_value = True
    return enforcer


@pytest.fixture
def mock_router():
    """Create a mock PathRouter."""
    router = MagicMock()
    router.get_mount_points.return_value = [
        "/archives",
        "/external",
        "/shared",
        "/system",
        "/workspace",
    ]
    return router


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway."""
    gw = MagicMock()
    gw.read_file.return_value = b"test content"
    gw.read_bulk.return_value = {}
    gw.get_routing_params.return_value = (None, None, False)
    gw.has_descendant_access.return_value = True
    gw.get_backend_directory_entries.return_value = set()
    gw.record_read_if_tracking.return_value = None
    gw.session_factory = MagicMock()
    gw.backend = MagicMock()
    return gw


@pytest.fixture
def service(mock_metadata_store, mock_permission_enforcer, mock_router, mock_gateway):
    """Create a SearchService with all mocked dependencies."""
    return SearchService(
        metadata_store=mock_metadata_store,
        permission_enforcer=mock_permission_enforcer,
        router=mock_router,
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
        self, mock_metadata_store, mock_permission_enforcer, mock_router, mock_gateway
    ):
        """Service stores all injected dependencies."""
        svc = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=mock_permission_enforcer,
            router=mock_router,
            gateway=mock_gateway,
            enforce_permissions=True,
        )
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is mock_permission_enforcer
        assert svc.router is mock_router
        assert svc._gw is mock_gateway
        assert svc._enforce_permissions is True

    def test_init_minimal(self, mock_metadata_store):
        """Service can be created with just a metadata store."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is None
        assert svc.router is None
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

    def test_read_delegates_to_gateway(self, service, mock_gateway):
        """_read delegates to gateway.read_file."""
        mock_gateway.read_file.return_value = b"file content"
        result = service._read("/test.txt")
        assert result == b"file content"
        mock_gateway.read_file.assert_called_once()

    def test_read_raises_without_gateway(self, mock_metadata_store):
        """_read raises NotImplementedError without gateway."""
        svc = SearchService(metadata_store=mock_metadata_store)
        with pytest.raises(NotImplementedError, match="gateway not provided"):
            svc._read("/test.txt")

    def test_read_converts_str_to_bytes(self, service, mock_gateway):
        """_read converts string response to bytes."""
        mock_gateway.read_file.return_value = "string content"
        result = service._read("/test.txt")
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

    def test_get_namespace_prefixes_from_router(self, service, mock_router):
        """_get_namespace_prefixes reads from router's get_mount_points."""
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

    def test_invalid_regex_raises_value_error(self, service, context):
        """grep raises ValueError for invalid regex patterns."""
        with pytest.raises(ValueError, match="Invalid regex pattern"):
            service.grep(pattern="[invalid", context=context)

    def test_valid_regex_accepted(self, service, mock_metadata_store, context):
        """grep accepts valid regex patterns."""
        mock_metadata_store.list_paths.return_value = []
        # list() returns empty so grep short-circuits
        with patch.object(service, "list", return_value=[]):
            results = service.grep(pattern="def\\s+\\w+", context=context)
            assert results == []
