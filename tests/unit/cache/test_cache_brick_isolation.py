"""Test that nexus.cache is a self-contained brick.

The only allowed kernel dependency is CacheStoreABC (the Fourth Pillar).
All other imports must be standard library, third-party, or intra-package.

Forbidden runtime imports:
    - nexus.core.* (except nexus.core.cache_store)
    - nexus.backends.*
    - nexus.storage.*
    - nexus.server.*
    - nexus.services.*
"""

import importlib
import sys
import types

import pytest

# Modules within nexus.cache that are allowed to import
_ALLOWED_NEXUS_DEPS = frozenset({
    "nexus.core.cache_store",  # Fourth Pillar — the only allowed kernel dep
})

# Top-level nexus packages that are FORBIDDEN for cache/ runtime imports
_FORBIDDEN_PREFIXES = (
    "nexus.core.",
    "nexus.backends.",
    "nexus.storage.",
    "nexus.server.",
    "nexus.services.",
    "nexus.raft.",
    "nexus.remote.",
)


def _is_forbidden(mod_name: str) -> bool:
    """Check if a module name is a forbidden dependency for the cache brick."""
    if mod_name in _ALLOWED_NEXUS_DEPS:
        return False
    return any(mod_name.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES)


class TestCacheBrickIsolation:
    """Verify nexus.cache only imports from allowed modules at runtime."""

    def _get_cache_imports(self) -> set[str]:
        """Import nexus.cache and collect all nexus.* modules it pulls in."""
        # Snapshot modules before import
        before = set(sys.modules.keys())

        # Force re-import of nexus.cache submodules
        cache_mods = [k for k in sys.modules if k.startswith("nexus.cache")]
        for mod_name in cache_mods:
            sys.modules.pop(mod_name, None)

        importlib.import_module("nexus.cache")

        # Collect newly imported nexus modules
        after = set(sys.modules.keys())
        new_mods = after - before
        return {m for m in new_mods if m.startswith("nexus.") and not m.startswith("nexus.cache")}

    def test_no_forbidden_runtime_imports(self) -> None:
        """nexus.cache must not import forbidden kernel/storage/backend modules."""
        nexus_deps = self._get_cache_imports()
        forbidden = {m for m in nexus_deps if _is_forbidden(m)}

        if forbidden:
            msg = (
                f"nexus.cache has {len(forbidden)} forbidden runtime import(s):\n"
                + "\n".join(f"  - {m}" for m in sorted(forbidden))
                + "\n\nAllowed: " + ", ".join(sorted(_ALLOWED_NEXUS_DEPS))
            )
            pytest.fail(msg)

    def test_allowed_dep_is_cache_store_abc(self) -> None:
        """The only allowed kernel dependency is nexus.core.cache_store."""
        nexus_deps = self._get_cache_imports()
        allowed_found = {m for m in nexus_deps if m in _ALLOWED_NEXUS_DEPS}
        # CacheStoreABC should be imported (it's the foundation)
        assert "nexus.core.cache_store" in allowed_found or "nexus.core.cache_store" in sys.modules

    def test_cache_submodules_importable(self) -> None:
        """All core cache submodules should be importable without errors."""
        submodules = [
            "nexus.cache.base",
            "nexus.cache.domain",
            "nexus.cache.inmemory",
            "nexus.cache.settings",
            "nexus.cache.factory",
        ]
        for mod_name in submodules:
            mod = importlib.import_module(mod_name)
            assert isinstance(mod, types.ModuleType), f"{mod_name} failed to import"

    def test_dragonfly_optional_import(self) -> None:
        """nexus.cache.dragonfly should import even without redis package."""
        # dragonfly.py handles missing redis gracefully with REDIS_AVAILABLE flag
        mod = importlib.import_module("nexus.cache.dragonfly")
        assert hasattr(mod, "REDIS_AVAILABLE")
        assert hasattr(mod, "DragonflyClient")
        assert hasattr(mod, "DragonflyCacheStore")
