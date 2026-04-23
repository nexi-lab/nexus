"""TDD tests for CacheBrick facade (Issue #1524).

These tests are written FIRST (RED phase). The CacheBrick implementation
will be created to make them pass.

Tests cover:
- Default construction (NullCacheStore fallback)
- Real store injection
- Protocol-typed accessor properties
- CachingBackendWrapper factory method (DELETED — CachingBackendWrapper removed)
- Immutability
- Zero nexus.core imports (brick isolation)
"""

from unittest.mock import AsyncMock, MagicMock

from nexus.cache.base import (
    EmbeddingCacheProtocol,
    PermissionCacheProtocol,
    ResourceMapCacheProtocol,
    TigerCacheProtocol,
)
from nexus.cache.settings import CacheSettings
from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_store() -> CacheStoreABC:
    """Create a mock CacheStoreABC for injection."""
    store = AsyncMock(spec=CacheStoreABC)
    store.health_check = AsyncMock(return_value=True)
    store.close = AsyncMock()
    store.get = AsyncMock(return_value=None)
    store.set = AsyncMock()
    store.delete = AsyncMock(return_value=False)
    store.exists = AsyncMock(return_value=False)
    store.delete_by_pattern = AsyncMock(return_value=0)
    store.get_many = AsyncMock(return_value={})
    store.set_many = AsyncMock()
    store.publish = AsyncMock(return_value=0)
    return store


# ---------------------------------------------------------------------------
# Construction tests
# ---------------------------------------------------------------------------


class TestCacheBrickConstruction:
    """Test CacheBrick constructor and default behaviors."""

    def test_init_with_defaults(self) -> None:
        """CacheBrick() with no args should use NullCacheStore fallback."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        assert isinstance(brick.cache_store, NullCacheStore)

    def test_init_with_real_store(self) -> None:
        """CacheBrick with injected store should use that store."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        brick = CacheBrick(cache_store=store)
        assert brick.cache_store is store

    def test_init_with_settings(self) -> None:
        """CacheBrick with custom settings should use them."""
        from nexus.cache.brick import CacheBrick

        settings = CacheSettings(
            dragonfly_url=None,
            permission_ttl=600,
            tiger_ttl=7200,
            embedding_ttl=172800,
        )
        brick = CacheBrick(settings=settings)
        assert brick.settings.permission_ttl == 600
        assert brick.settings.tiger_ttl == 7200

    def test_init_with_record_store(self) -> None:
        """CacheBrick with record_store should store it for postgres fallback."""
        from nexus.cache.brick import CacheBrick

        record_store = MagicMock()
        brick = CacheBrick(record_store=record_store)
        assert brick._record_store is record_store


# ---------------------------------------------------------------------------
# Protocol accessor tests
# ---------------------------------------------------------------------------


class TestCacheBrickProtocols:
    """Test that CacheBrick returns protocol-typed domain caches."""

    def test_permission_cache_protocol(self) -> None:
        """permission_cache should satisfy PermissionCacheProtocol."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        cache = brick.permission_cache
        assert isinstance(cache, PermissionCacheProtocol)

    def test_tiger_cache_protocol(self) -> None:
        """tiger_cache should satisfy TigerCacheProtocol."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        cache = brick.tiger_cache
        assert isinstance(cache, TigerCacheProtocol)

    def test_resource_map_cache_protocol(self) -> None:
        """resource_map_cache should satisfy ResourceMapCacheProtocol."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        cache = brick.resource_map_cache
        assert isinstance(cache, ResourceMapCacheProtocol)

    def test_embedding_cache_protocol(self) -> None:
        """embedding_cache should satisfy EmbeddingCacheProtocol."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        cache = brick.embedding_cache
        assert isinstance(cache, EmbeddingCacheProtocol)

    def test_get_cache_store(self) -> None:
        """cache_store property should return the injected CacheStoreABC."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        brick = CacheBrick(cache_store=store)
        assert brick.cache_store is store


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestCacheBrickImmutability:
    """Test that CacheBrick properties are stable after construction."""

    def test_settings_are_stable(self) -> None:
        """settings property should return the same object each time."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        assert brick.settings is brick.settings

    def test_domain_caches_are_stable(self) -> None:
        """Domain cache accessors should return the same instance each call."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        assert brick.permission_cache is brick.permission_cache
        assert brick.tiger_cache is brick.tiger_cache
        assert brick.resource_map_cache is brick.resource_map_cache
        assert brick.embedding_cache is brick.embedding_cache


# ---------------------------------------------------------------------------
# Zero core imports (brick isolation)
# ---------------------------------------------------------------------------


class TestCacheBrickIsolation:
    """Test that CacheBrick module has zero nexus.core imports at runtime."""

    def test_zero_core_imports(self) -> None:
        """brick.py should not import from nexus.core at runtime.

        Only TYPE_CHECKING imports are allowed.
        """
        import ast
        import pathlib

        brick_path = (
            pathlib.Path(__file__).parent.parent.parent.parent
            / "src"
            / "nexus"
            / "cache"
            / "brick.py"
        )
        source = brick_path.read_text()
        tree = ast.parse(source)

        # Check only top-level imports (not TYPE_CHECKING guarded ones)
        runtime_violations = []
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("nexus.core")
            ):
                runtime_violations.append(f"line {node.lineno}: from {node.module} import ...")

        assert runtime_violations == [], (
            "brick.py has runtime imports from nexus.core:\n" + "\n".join(runtime_violations)
        )


# ---------------------------------------------------------------------------
# Backend name reporting
# ---------------------------------------------------------------------------


class TestCacheBrickBackendName:
    """Test backend_name property for health/status reporting."""

    def test_backend_name_null(self) -> None:
        """NullCacheStore should report 'NullCacheStore'."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        assert brick.backend_name == "NullCacheStore"

    def test_backend_name_injected(self) -> None:
        """Injected store should report its class name."""
        from nexus.cache.brick import CacheBrick

        store = _make_mock_store()
        type(store).__name__ = "DragonflyCacheStore"
        brick = CacheBrick(cache_store=store)
        assert "Dragonfly" in brick.backend_name or "Mock" in brick.backend_name

    def test_has_cache_store_null(self) -> None:
        """NullCacheStore should report has_cache_store=False."""
        from nexus.cache.brick import CacheBrick

        brick = CacheBrick()
        assert brick.has_cache_store is False

    def test_has_cache_store_real(self) -> None:
        """Real store should report has_cache_store=True."""
        from nexus.cache.brick import CacheBrick
        from nexus.cache.inmemory import InMemoryCacheStore

        brick = CacheBrick(cache_store=InMemoryCacheStore())
        assert brick.has_cache_store is True
