"""Search strategy enums and constants (Issue #929, #1520).

Canonical source for adaptive algorithm selection configuration.
Previously duplicated in nexus.core.nexus_fs_search and nexus.services.search_service.

Issue #929: Adaptive algorithm selection for search operations.
Issue #1499: Shared query analysis patterns for query routing and expansion.
"""

from enum import StrEnum

# Grep strategy thresholds (Issue #2071: non-resource thresholds stay as constants)
GREP_SEQUENTIAL_THRESHOLD = 10  # Below this file count, use sequential (no overhead)
GREP_PARALLEL_THRESHOLD = 100  # Above this, consider parallel processing
GREP_TRIGRAM_THRESHOLD = 500  # Above this, prefer trigram index if available
GREP_ZOEKT_THRESHOLD = 1000  # Above this, prefer Zoekt if available
GREP_CACHED_TEXT_RATIO = 0.8  # Use cached text path if > 80% files have cached text

# Issue #2071: GREP_PARALLEL_WORKERS moved to ProfileTuning.search.grep_parallel_workers
# Kept as fallback default for callers that don't receive tuning via DI.
GREP_PARALLEL_WORKERS = 4  # Thread pool size for parallel grep (FULL profile default)

# Glob strategy thresholds
GLOB_RUST_THRESHOLD = 50  # Use Rust acceleration above this file count


class SearchStrategy(StrEnum):
    """Strategy for grep operations (Issue #929).

    Selected at runtime based on file count, cached text ratio, and backends.
    """

    SEQUENTIAL = "sequential"  # < 10 files - no parallelization overhead
    CACHED_TEXT = "cached_text"  # > 80% files have pre-parsed text
    RUST_BULK = "rust_bulk"  # 10-1000 files with Rust available
    PARALLEL_POOL = "parallel_pool"  # 100-10000 files, parallel processing
    TRIGRAM_INDEX = "trigram_index"  # > 500 files with trigram index
    ZOEKT_INDEX = "zoekt_index"  # > 1000 files with Zoekt index


class GlobStrategy(StrEnum):
    """Strategy for glob operations (Issue #929)."""

    FNMATCH_SIMPLE = "fnmatch_simple"  # Simple patterns without **
    REGEX_COMPILED = "regex_compiled"  # Complex patterns with **
    RUST_BULK = "rust_bulk"  # > 50 files with Rust available
    DIRECTORY_PRUNED = "directory_pruned"  # Pattern has static prefix


# =============================================================================
# Query Analysis Patterns (Issue #1499)
# =============================================================================
# Shared constants for query complexity estimation and routing.
# Used by query_router.py and available for query_expansion.py, ranking.py.

COMPARISON_WORDS: frozenset[str] = frozenset(
    {"vs", "versus", "compare", "comparison", "difference", "between"}
)

TEMPORAL_WORDS: frozenset[str] = frozenset(
    {"when", "before", "after", "history", "timeline", "since", "until"}
)

AGGREGATION_WORDS: frozenset[str] = frozenset(
    {"all", "every", "summary", "overview", "list", "total"}
)

MULTIHOP_PATTERNS: tuple[str, ...] = (
    "how does",
    "how do",
    "why does",
    "why do",
    "what happens when",
    "relationship between",
    "impact of",
    "effect of",
)

COMPLEX_PATTERNS: tuple[str, ...] = (
    "explain",
    "analyze",
    "evaluate",
    "describe how",
)
