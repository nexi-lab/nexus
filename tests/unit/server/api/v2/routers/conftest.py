"""Conftest for file-op router unit tests.

Stubs `nexus_kernel` before any test module import so that async_files.py
(which transitively imports glob_fast/grep_fast/trigram_fast → nexus_kernel)
can be imported without the compiled Rust extension being present.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock


def _stub_nexus_kernel() -> None:
    if "nexus_kernel" in sys.modules:
        return  # already present (native build or earlier stub)
    # Use a MagicMock as the module so every attribute access returns a callable
    # stub automatically — covers glob_match_bulk, grep_bulk, grep_files_mmap,
    # build_trigram_index, trigram_grep, etc.
    sys.modules["nexus_kernel"] = MagicMock()


_stub_nexus_kernel()
