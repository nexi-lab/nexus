"""Search facade construction and listing resource lifecycle."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.search_service import SearchService
from nexus.contracts.types import OperationContext


@pytest.fixture
def mock_metadata_store():
    """Create a mock MetastoreABC."""
    store = MagicMock()
    store.list_paths.return_value = []
    store.list.return_value = []
    store.metastore_list.side_effect = lambda prefix="": store.list(prefix)
    store.get_file_metadata.return_value = None
    store.get_file_metadata_bulk.return_value = {}
    store.get_searchable_text_bulk.return_value = {}

    def _get_file_metadata(path, key):
        return store.get_file_metadata(path, key)

    def _get_file_metadata_bulk(paths, key):
        if key == "parsed_text":
            return store.get_searchable_text_bulk(paths)
        return store.get_file_metadata_bulk(paths, key)

    store.get_xattr.side_effect = _get_file_metadata
    store.get_xattr_bulk.side_effect = _get_file_metadata_bulk
    store.metastore_get_file_metadata.side_effect = _get_file_metadata
    store.metastore_get_file_metadata_bulk.side_effect = _get_file_metadata_bulk
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
    enforcer.check.return_value = True
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
        "/__sys__",
        "/workspace",
    ]
    return dlc


@pytest.fixture
def mock_gateway():
    """Create a mock NexusFSGateway."""
    gw = MagicMock()
    gw.read = AsyncMock(return_value=b"test content")
    gw.read_file = gw.read
    gw.read_bulk.return_value = {}
    gw._get_context_identity.return_value = (None, None, False)
    gw.get_routing_params = gw._get_context_identity
    gw._descendant_checker.has_access.return_value = True
    gw.has_descendant_access = gw._descendant_checker.has_access
    gw.record_read_if_tracking.return_value = None
    gw.SessionLocal = MagicMock()
    gw.session_factory = gw.SessionLocal
    gw.backend = MagicMock()
    gw.sys_readdir.return_value = []
    return gw


@pytest.fixture
def service(mock_metadata_store, mock_permission_enforcer, mock_dlc, mock_gateway):
    """Create a SearchService with all mocked dependencies."""
    return SearchService(
        metadata_store=mock_metadata_store,
        permission_enforcer=mock_permission_enforcer,
        dlc=mock_dlc,
        nexus_fs=mock_gateway,
        enforce_permissions=True,
    )


@pytest.fixture
def service_no_perms(mock_metadata_store, mock_gateway):
    """Create a SearchService with permissions disabled."""
    return SearchService(
        metadata_store=mock_metadata_store,
        nexus_fs=mock_gateway,
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
            nexus_fs=mock_gateway,
            enforce_permissions=True,
        )
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is mock_permission_enforcer
        assert svc._dlc is mock_dlc
        assert svc._nexus_fs is mock_gateway
        assert svc._enforce_permissions is True

    def test_init_minimal(self, mock_metadata_store):
        """Service can be created with just a metadata store."""
        svc = SearchService(metadata_store=mock_metadata_store)
        assert svc.metadata is mock_metadata_store
        assert svc._permission_enforcer is None
        assert svc._dlc is None
        assert svc._nexus_fs is None
        assert svc._enforce_permissions is True

    def test_init_defaults(self, mock_metadata_store):
        """Service initializes internal state to defaults."""
        svc = SearchService(metadata_store=mock_metadata_store)
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

    def test_search_hit_filter_uses_direct_check_for_inherited_grants(
        self, service, mock_permission_enforcer, context
    ):
        """Search post-filtering must keep hits readable via parent inheritance."""
        inherited_hit = {"path": "/workspace/demo/herb/customers/cust-002.md"}
        denied_hit = {"path": "/workspace/demo/restricted/internal.md"}
        mock_permission_enforcer.filter_search_results.return_value = []
        mock_permission_enforcer.filter_list.side_effect = None
        mock_permission_enforcer.filter_list.return_value = []
        mock_permission_enforcer.check.side_effect = lambda path, _permission, _context: (
            path == inherited_hit["path"]
        )

        filtered = service._filter_hit_dicts_by_read_permission(
            [inherited_hit, denied_hit],
            context,
        )

        assert filtered == [inherited_hit]

    def test_search_hit_filter_drops_deleted_file_rows(self, mock_metadata_store, context):
        """Stale backend hits should not survive after the file row is deleted."""

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return ["/workspace/demo/live.md"]

        session = MagicMock()
        session.execute.return_value = _Result()
        record_store = MagicMock()
        record_store.session_factory.return_value = session
        svc = SearchService(metadata_store=mock_metadata_store, record_store=record_store)

        assert svc._filter_existing_search_paths(
            ["/workspace/demo/live.md", "/workspace/demo/deleted.md"],
            context,
        ) == ["/workspace/demo/live.md"]

    def test_search_hit_filter_drops_paths_missing_from_vfs(self, mock_metadata_store, context):
        """A live SQL row is not enough when the authoritative VFS path is gone."""

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return [
                    "/workspace/demo/live.md",
                    "/workspace/demo/deleted.md",
                ]

        session = MagicMock()
        session.execute.return_value = _Result()
        record_store = MagicMock()
        record_store.session_factory.return_value = session
        nexus_fs = MagicMock()
        nexus_fs.sys_stat.side_effect = lambda path, context=None: (
            {"path": path} if path == "/workspace/demo/live.md" else None
        )
        svc = SearchService(
            metadata_store=mock_metadata_store,
            record_store=record_store,
            nexus_fs=nexus_fs,
        )

        assert svc._filter_existing_search_paths(
            ["/workspace/demo/live.md", "/workspace/demo/deleted.md"],
            context,
        ) == ["/workspace/demo/live.md"]

    def test_list_slow_path_passes_zone_id_to_tiger_pushdown(
        self, mock_metadata_store, mock_permission_enforcer, mock_dlc, mock_gateway
    ):
        """Predicate pushdown must request the bitmap for the current list zone."""
        # _list_slow_path scans via the §2.5 syscall surface — sys_readdir
        # detail dicts, not metadata_store.list FileMetadata objects.
        entry = {"path": "/visible.txt", "entry_type": 0, "size": 0}
        mock_gateway.sys_readdir.return_value = [entry]

        tiger_cache = MagicMock()
        tiger_cache.get_accessible_int_ids.return_value = {1}
        tiger_cache._resource_map.get_or_create_int_id.return_value = 1
        rebac_manager = MagicMock()
        rebac_manager._tiger_cache = tiger_cache

        svc = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=mock_permission_enforcer,
            dlc=mock_dlc,
            nexus_fs=mock_gateway,
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

        assert all_files == [entry]
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


class TestThreadPoolManagement:
    """Tests for thread pool lazy initialization and cleanup."""

    def test_thread_pool_starts_none(self, service):
        """Thread pools start as None."""
        assert service._list_thread_pool is None

    def test_get_list_thread_pool_creates_pool(self, service):
        """_get_list_thread_pool lazily creates a ThreadPoolExecutor."""
        pool = service._get_list_thread_pool()
        assert pool is not None
        assert service._list_thread_pool is pool
        pool.shutdown(wait=False)

    def test_close_shuts_down_pools(self, service):
        """close() shuts down the listing thread pool."""
        service._get_list_thread_pool()
        assert service._list_thread_pool is not None

        service.close()
        assert service._list_thread_pool is None

    def test_close_noop_when_no_pools(self, service):
        """close() is a no-op when pools were never created."""
        service.close()  # Should not raise
        assert service._list_thread_pool is None


class TestCrossZoneCache:
    """Tests for bounded TTL cache initialization."""

    def test_cross_zone_cache_initialized(self, service):
        """Cross-zone cache is initialized with correct bounds."""
        assert service._cross_zone_cache is not None
        assert service._cross_zone_cache.maxsize == 1024

    def test_cross_zone_cache_empty_initially(self, service):
        """Cross-zone cache starts empty."""
        assert len(service._cross_zone_cache) == 0
