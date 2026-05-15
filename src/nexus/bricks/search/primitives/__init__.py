"""Search primitives for Nexus.

Low-level search utilities at the brick tier (Issue #2123).

These primitives power Search brick operations:
- glob_helpers: pure-Python glob query helpers (extract_static_prefix,
  is_simple_pattern, glob_match, glob_filter)
- trigram_fast: Trigram-based search indexing (Rust-accelerated)

The Rust-accelerated grep / glob primitives (`grep_bulk`,
`grep_files_mmap`, `glob_match_bulk`) are imported directly from
`nexus._rust_compat` — they are kernel-resident lib-tier helpers, not
brick-tier wrappers.

Related: NEXUS-LEGO-ARCHITECTURE.md (minimal kernel, maximal bricks)
"""

from nexus._rust_compat import grep_bulk  # noqa: F401
from nexus.bricks.search.primitives.glob_helpers import glob_match  # noqa: F401
from nexus.bricks.search.primitives.trigram_fast import (  # noqa: F401
    build_trigram_index,
    search_trigram,
)
from nexus.bricks.search.primitives.trigram_fast import (
    is_available as trigram_available,
)

__all__ = [
    "build_trigram_index",
    "glob_match",
    "grep_bulk",
    "search_trigram",
    "trigram_available",
]
