"""Verify backward-compat re-exports are identity-preserving (Issue #2055).

After moving CacheStoreABC to bricks/cache/cache_store.py, all legacy import
paths must resolve to the exact same class objects. This test catches broken
re-export chains that would cause isinstance() failures.
"""

from __future__ import annotations


class TestCacheStoreABCIdentity:
    """CacheStoreABC must be the same object regardless of import path."""

    def test_core_cache_store_reexport(self) -> None:
        """core.cache_store re-exports from bricks.cache.cache_store."""
        from nexus.bricks.cache.cache_store import CacheStoreABC as canonical
        from nexus.core.cache_store import CacheStoreABC as legacy

        assert canonical is legacy

    def test_core_protocols_reexport(self) -> None:
        """core.protocols re-exports CacheStoreABC via core.cache_store chain."""
        from nexus.bricks.cache.cache_store import CacheStoreABC as canonical
        from nexus.core.protocols import CacheStoreABC as via_protocols

        assert canonical is via_protocols


class TestNullCacheStoreIdentity:
    """NullCacheStore must be the same object regardless of import path."""

    def test_core_cache_store_reexport(self) -> None:
        from nexus.bricks.cache.cache_store import NullCacheStore as canonical
        from nexus.core.cache_store import NullCacheStore as legacy

        assert canonical is legacy

    def test_core_protocols_reexport(self) -> None:
        from nexus.bricks.cache.cache_store import NullCacheStore as canonical
        from nexus.core.protocols import NullCacheStore as via_protocols

        assert canonical is via_protocols


class TestPersistentViewStoreShim:
    """PostgresPersistentViewStore must be accessible from all legacy paths."""

    def test_storage_is_canonical(self) -> None:
        from nexus.bricks.cache.persistent_view_postgres import (
            PostgresPersistentViewStore as via_brick_shim,
        )
        from nexus.storage.persistent_view_postgres import (
            PostgresPersistentViewStore as canonical,
        )

        assert canonical is via_brick_shim

    def test_cache_shim(self) -> None:
        from nexus.cache.persistent_view_postgres import (
            PostgresPersistentViewStore as via_cache_shim,
        )
        from nexus.storage.persistent_view_postgres import (
            PostgresPersistentViewStore as canonical,
        )

        assert canonical is via_cache_shim


class TestZeroViolations:
    """bricks/cache/ must have zero imports from nexus.core."""

    def test_no_core_imports_in_cache_brick(self) -> None:
        """Verify no runtime imports from nexus.core exist in bricks/cache/ modules."""
        import inspect

        import nexus.bricks.cache.base
        import nexus.bricks.cache.brick
        import nexus.bricks.cache.cache_store
        import nexus.bricks.cache.domain
        import nexus.bricks.cache.factory
        import nexus.bricks.cache.inmemory
        import nexus.bricks.cache.protocols
        import nexus.bricks.cache.settings

        modules_to_check = [
            nexus.bricks.cache.base,
            nexus.bricks.cache.brick,
            nexus.bricks.cache.cache_store,
            nexus.bricks.cache.domain,
            nexus.bricks.cache.factory,
            nexus.bricks.cache.inmemory,
            nexus.bricks.cache.protocols,
            nexus.bricks.cache.settings,
        ]

        for mod in modules_to_check:
            source = inspect.getsource(mod)
            # Check for runtime (non-TYPE_CHECKING) imports from nexus.core
            lines = source.split("\n")
            in_type_checking = False
            for line in lines:
                stripped = line.strip()
                if stripped == "if TYPE_CHECKING:":
                    in_type_checking = True
                    continue
                if (
                    in_type_checking
                    and stripped
                    and not stripped.startswith(("#", "from", "import"))
                ):
                    in_type_checking = False
                if (
                    not in_type_checking
                    and "from nexus.core" in stripped
                    and not stripped.startswith("#")
                ):
                    raise AssertionError(
                        f"{mod.__name__} has runtime import from nexus.core: {stripped!r}"
                    )
