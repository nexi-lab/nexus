"""Tier-neutral search strategy types for cross-brick use (Issue #2190).

Canonical home for adaptive algorithm selection enums and threshold constants
used by search service, grep mixin, and query router.

This module has **zero** runtime imports from ``nexus.*`` --- only stdlib ---
so bricks, services, and backends can depend on it without pulling in the
search brick.

Backward-compat shim: ``nexus.search.strategies`` re-exports everything.

Issue #929: Adaptive algorithm selection for search operations.
Issue #1499: Shared query analysis patterns for query routing and expansion.
"""

import contextvars
from enum import StrEnum

__all__ = [
    # Strategy enums
    "SearchStrategy",
    "GlobStrategy",
    # Grep thresholds
    "GREP_SEQUENTIAL_THRESHOLD",
    "GREP_PARALLEL_THRESHOLD",
    "GREP_TRIGRAM_THRESHOLD",
    "GREP_ZOEKT_THRESHOLD",
    "GREP_CACHED_TEXT_RATIO",
    "GREP_PARALLEL_WORKERS",
    # Glob thresholds
    "GLOB_RUST_THRESHOLD",
    # Query analysis patterns
    "COMPARISON_WORDS",
    "TEMPORAL_WORDS",
    "AGGREGATION_WORDS",
    "MULTIHOP_PATTERNS",
    "COMPLEX_PATTERNS",
    # Per-task semantic-degradation flag (Issue #3778 R2)
    "LAST_SEMANTIC_DEGRADED",
]

# Per-task flag recording whether the last SANDBOX semantic_search call
# degraded to BM25S (Issue #3778 R2 review). Response-envelope builders
# (MCP, HTTP routers) can read this after awaiting semantic_search so the
# degradation flag surfaces even when the fallback returned zero results.
# Living in contracts (not the search brick) keeps cross-brick callers
# legal under the LEGO architecture principle.
LAST_SEMANTIC_DEGRADED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "nexus_last_semantic_degraded", default=False
)

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
