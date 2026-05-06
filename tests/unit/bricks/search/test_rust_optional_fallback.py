"""Tests for optional Rust search accelerator fallback."""

from __future__ import annotations

import importlib
import sys
import types
from typing import cast
from unittest.mock import patch


def test_trigram_fast_imports_without_nexus_runtime() -> None:
    """Absent Rust runtime should disable trigram acceleration, not imports."""
    module_names = (
        "nexus_runtime",
        "nexus._rust_compat",
        "nexus.bricks.search.primitives",
        "nexus.bricks.search.primitives.trigram_fast",
    )
    saved = {name: sys.modules.get(name) for name in module_names}
    try:
        for name in module_names:
            sys.modules.pop(name, None)

        with patch.dict(sys.modules, {"nexus_runtime": cast(types.ModuleType, None)}):
            trigram_fast = importlib.import_module("nexus.bricks.search.primitives.trigram_fast")

            assert trigram_fast.is_available() is False
            assert trigram_fast.build_index(["/tmp/input.txt"], "/tmp/out.trgm") is False
            assert (
                trigram_fast.build_index_from_entries(
                    [("/tmp/input.txt", b"hello")], "/tmp/out.trgm"
                )
                is False
            )
            assert trigram_fast.grep("/tmp/out.trgm", "hello") is None
            assert trigram_fast.search_candidates("/tmp/out.trgm", "hello") is None
            assert trigram_fast.get_stats("/tmp/out.trgm") is None
            trigram_fast.invalidate_cache("/tmp/out.trgm")
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_glob_helpers_fall_back_when_rust_glob_is_unavailable(monkeypatch) -> None:
    """Glob helpers are documented as Python helpers and must not require Rust."""
    import nexus._rust_compat as rust_compat
    from nexus.bricks.search.primitives import glob_helpers

    monkeypatch.setattr(rust_compat, "glob_match_bulk", None)

    assert glob_helpers.glob_match("/src/main.py", ["**/*.py"]) is True
    assert glob_helpers.glob_match("/README.md", ["**/*.py"]) is False
    assert glob_helpers.glob_filter(
        ["/src/main.py", "/src/main.txt", "/src/test_main.py"],
        include_patterns=["**/*.py"],
        exclude_patterns=["**/test_*.py"],
    ) == ["/src/main.py"]
