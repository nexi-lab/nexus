"""Smoke test: nexus.extensions package importable with zero side effects."""

import sys


def test_package_imports_without_backend_deps():
    """Importing nexus.extensions must NOT pull in backend/brick/plugin runtime."""
    # Snapshot loaded modules, then import.
    before = set(sys.modules)
    import nexus.extensions  # noqa: F401

    new_modules = set(sys.modules) - before
    forbidden_prefixes = (
        "nexus.backends.connectors.",
        "nexus.bricks.",
        "nexus.plugins.base",
    )
    leaked = [m for m in new_modules if m.startswith(forbidden_prefixes)]
    assert not leaked, f"Importing nexus.extensions leaked: {leaked}"
