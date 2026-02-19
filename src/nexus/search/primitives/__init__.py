"""Search primitives for Nexus.

Low-level search utilities extracted from core/ to properly place them
at brick tier rather than kernel tier (Issue #2123).

These primitives power SearchService brick operations:
- grep_fast: Fast content search with ripgrep
- glob_fast: Fast file pattern matching
- trigram_fast: Trigram-based search indexing

Re-exported for convenience. All modules follow Rust-accelerated pattern
with Python fallback when nexus_fast is unavailable.

Related: NEXUS-LEGO-ARCHITECTURE.md (minimal kernel, maximal bricks)
"""

from __future__ import annotations

# Re-export primitives for public API
from nexus.search.primitives.grep_fast import grep_bulk  # noqa: F401
from nexus.search.primitives.glob_fast import glob_match  # noqa: F401
from nexus.search.primitives.trigram_fast import (  # noqa: F401
    build_trigram_index,
    is_available as trigram_available,
    search_trigram,
)

__all__ = [
    "grep_bulk",
    "glob_match",
    "build_trigram_index",
    "search_trigram",
    "trigram_available",
]
