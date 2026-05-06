"""Conformance test: every backend satisfies SearchBackend.

Parameterized so the same suite runs against pg/sqlite backends as they
land in later tasks. Initially only the protocol shape is checked.
"""

from __future__ import annotations

from nexus.bricks.search.protocols import SearchBackend


def test_search_backend_protocol_is_runtime_checkable():
    assert isinstance(SearchBackend, type)
    # runtime_checkable enables isinstance() checks
    assert hasattr(SearchBackend, "_is_runtime_protocol")


def test_search_backend_protocol_has_required_methods():
    required = {
        "add",
        "upsert",
        "delete",
        "keyword_search",
        "semantic_search",
        "startup",
        "shutdown",
    }
    actual = {m for m in dir(SearchBackend) if not m.startswith("_")}
    missing = required - actual
    # Method-signature drift is caught by isinstance() checks in T5-T8 (real backends).
    assert not missing, f"SearchBackend missing methods: {missing}"
