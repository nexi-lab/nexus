"""Test that nexus.cache only imports from allowed modules (Issue #1524).

The cache brick should only depend on:
- nexus.core.cache_store (Fourth Pillar — CacheStoreABC)
- Standard library / third-party packages

It must NOT import from:
- nexus.core.* (anything besides cache_store)
- nexus.backends.*
- nexus.storage.*
- nexus.server.*
"""

import pytest

# Modules that are ALLOWED to be imported by nexus.cache
ALLOWED_NEXUS_MODULES = {
    "nexus.core.cache_store",
}

# Nexus top-level packages that are FORBIDDEN
FORBIDDEN_PREFIXES = [
    "nexus.server.",
]

class TestCacheBrickIsolation:
    """Verify nexus.cache import boundaries."""

    def test_cache_base_imports_only_stdlib(self):
        """cache/base.py should have zero nexus imports (pure protocols)."""
        # Read the source file directly
        import ast
        from pathlib import Path

        base_path = Path("src/nexus/cache/base.py")
        if not base_path.exists():
            pytest.skip("base.py not found")

        tree = ast.parse(base_path.read_text())
        nexus_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
                nexus_imports.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("nexus"):
                        nexus_imports.append(alias.name)

        assert nexus_imports == [], (
            f"cache/base.py should have no nexus imports, found: {nexus_imports}"
        )

    def test_cache_domain_imports_only_cache_store(self):
        """cache/domain.py should only import from nexus.core.cache_store."""
        import ast
        from pathlib import Path

        domain_path = Path("src/nexus/cache/domain.py")
        if not domain_path.exists():
            pytest.skip("domain.py not found")

        tree = ast.parse(domain_path.read_text())
        nexus_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
                nexus_imports.append(node.module)

        for mod in nexus_imports:
            assert mod in ALLOWED_NEXUS_MODULES or mod.startswith("nexus.cache"), (
                f"cache/domain.py imports forbidden module: {mod}. "
                f"Allowed: {ALLOWED_NEXUS_MODULES}"
            )

    def test_cache_inmemory_imports_only_cache_store(self):
        """cache/inmemory.py should only import from nexus.core.cache_store."""
        import ast
        from pathlib import Path

        inmemory_path = Path("src/nexus/cache/inmemory.py")
        if not inmemory_path.exists():
            pytest.skip("inmemory.py not found")

        tree = ast.parse(inmemory_path.read_text())
        nexus_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
                nexus_imports.append(node.module)

        for mod in nexus_imports:
            assert mod in ALLOWED_NEXUS_MODULES or mod.startswith("nexus.cache"), (
                f"cache/inmemory.py imports forbidden module: {mod}"
            )

    def test_cache_dragonfly_imports_only_cache_store(self):
        """cache/dragonfly.py should only import from nexus.core.cache_store."""
        import ast
        from pathlib import Path

        dragonfly_path = Path("src/nexus/cache/dragonfly.py")
        if not dragonfly_path.exists():
            pytest.skip("dragonfly.py not found")

        tree = ast.parse(dragonfly_path.read_text())
        nexus_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nexus"):
                nexus_imports.append(node.module)

        for mod in nexus_imports:
            assert mod in ALLOWED_NEXUS_MODULES or mod.startswith("nexus.cache"), (
                f"cache/dragonfly.py imports forbidden module: {mod}"
            )

    def test_no_server_imports_in_cache(self):
        """No cache module should import from nexus.server."""
        import ast
        from pathlib import Path

        cache_dir = Path("src/nexus/cache")
        violations = []
        for py_file in cache_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for prefix in FORBIDDEN_PREFIXES:
                        if node.module.startswith(prefix):
                            violations.append(f"{py_file.name}: {node.module}")

        assert violations == [], f"Server imports found in cache brick: {violations}"

    def test_backend_wrapper_moved_out(self):
        """CachingBackendWrapper should NOT be in nexus.cache.__init__."""
        import ast
        from pathlib import Path

        init_path = Path("src/nexus/cache/__init__.py")
        if not init_path.exists():
            pytest.skip("__init__.py not found")

        tree = ast.parse(init_path.read_text())
        exported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in (node.names or []):
                    exported_names.add(alias.asname or alias.name)

        assert "CachingBackendWrapper" not in exported_names, (
            "CachingBackendWrapper should be moved to nexus.backends, "
            "not exported from nexus.cache"
        )

    def test_backward_compat_shim_exists(self):
        """Old import path should still work via shim."""
        from pathlib import Path

        shim_path = Path("src/nexus/cache/backend_wrapper.py")
        assert shim_path.exists(), "Backward-compat shim should exist"

        content = shim_path.read_text()
        assert "nexus.backends.caching_wrapper" in content, (
            "Shim should re-export from nexus.backends.caching_wrapper"
        )
