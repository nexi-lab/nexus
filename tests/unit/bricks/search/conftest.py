"""Conftest for search brick unit tests.

Stubs `nexus_runtime` before any test module import so that search modules
(which transitively import primitives.trigram_fast → nexus_runtime) can be
imported without the compiled Rust extension being present.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock


def _stub_nexus_runtime() -> None:
    if "nexus_runtime" in sys.modules:
        return  # already present (native build or earlier stub)
    # Use a MagicMock as the module so every attribute access returns a callable
    # stub automatically — covers glob_match_bulk, grep_bulk, grep_files_mmap,
    # build_trigram_index, trigram_grep, etc.
    sys.modules["nexus_runtime"] = MagicMock()


_stub_nexus_runtime()
