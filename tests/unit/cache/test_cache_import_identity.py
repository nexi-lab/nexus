"""Verify CacheStoreABC import identity and nexus/cache/ zero-core-imports (Issue #2055).

After moving CacheStoreABC to contracts/cache_store.py, all import paths must
resolve to the exact same class objects. Shim files have been deleted — there
is now a single canonical path: nexus.contracts.cache_store.
"""


class TestCacheStoreABCIdentity:
    """CacheStoreABC must be the same object via contracts and core.protocols."""

    def test_contracts_is_canonical(self) -> None:
        """contracts.cache_store is the canonical location."""
        from nexus.contracts.cache_store import CacheStoreABC as canonical
        from nexus.core.protocols import CacheStoreABC as via_protocols

        assert canonical is via_protocols

    def test_null_cache_store_identity(self) -> None:
        from nexus.contracts.cache_store import NullCacheStore as canonical
        from nexus.core.protocols import NullCacheStore as via_protocols

        assert canonical is via_protocols


class TestZeroViolations:
    """nexus/cache/ must have zero imports from nexus.core."""

    def test_no_core_imports_in_cache_brick(self) -> None:
        """Verify no runtime imports from nexus.core exist in nexus/cache/ modules."""
        import inspect

        import nexus.cache.base
        import nexus.cache.brick
        import nexus.cache.domain
        import nexus.cache.factory
        import nexus.cache.inmemory
        import nexus.cache.protocols
        import nexus.cache.settings

        modules_to_check = [
            nexus.cache.base,
            nexus.cache.brick,
            nexus.cache.domain,
            nexus.cache.factory,
            nexus.cache.inmemory,
            nexus.cache.protocols,
            nexus.cache.settings,
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
