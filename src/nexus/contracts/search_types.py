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
from dataclasses import dataclass
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
    "RECENCY_WORDS",
    "AGGREGATION_WORDS",
    "MULTIHOP_PATTERNS",
    "COMPLEX_PATTERNS",
    # Per-task semantic-degradation flag (Issue #3778 R2)
    "LAST_SEMANTIC_DEGRADED",
    # SearchBrickProtocol.search bundled request (#4553 follow-up B)
    "SearchRequest",
    # Positional per-query failure marker for batch_search
    "BatchQueryFailure",
]


# =============================================================================
# SearchRequest — bundled call shape for SearchBrickProtocol.search
# =============================================================================
#
# ``search()`` had grown to 13 keyword arguments (query, search_type, limit,
# path_filter, alpha, fusion_method, rrf_k, zone_id, expand, adaptive_k,
# recency, recency_weight, recency_half_life_days) with the request-param
# feature set steadily growing (#4541 fusion, #4553 recency, #4545 title
# arm).  Every new knob shipped as another keyword and mock stubs sprouted
# matching kwargs.  Bundling into one frozen dataclass stops the accretion —
# new fields are pure data additions that don't churn signatures.
#
# The public ``search`` method takes exactly one argument: a
# ``SearchRequest``.  Private internal-to-daemon methods
# (``_search_on_current_loop`` etc.) keep individual kwargs — the boundary
# is drawn at the public contract, not repeated inside the module.
@dataclass(frozen=True, kw_only=True)
class SearchRequest:
    """Bundled parameters for ``SearchBrickProtocol.search``."""

    query: str
    # Kept as ``str`` (not ``Literal``) so callers passing an HTTP-validated
    # string variable don't have to cast — runtime validation lives at the
    # HTTP boundary in ``routers/search.py``.
    search_type: str = "hybrid"
    limit: int = 10
    path_filter: str | None = None
    alpha: float = 0.5
    fusion_method: str = "rrf"
    rrf_k: int = 60
    zone_id: str | None = None
    expand: str = "none"
    adaptive_k: bool = False
    recency: str | None = None
    recency_weight: float | None = None
    recency_half_life_days: float | None = None
    # Pre-computed query embedding. When set, the daemon uses this vector
    # for the dense leg instead of embedding the query text itself — the
    # batch endpoint embeds all unique query texts in one embed_batch call
    # and hands each inner search its vector.
    query_vector: list[float] | None = None
    # Batch-mode failure semantics. The interactive path degrades several
    # backend failures to an empty result (query timeout, legacy semantic
    # backend errors, missing query embedding on a semantic-only search).
    # When True those failures raise to the caller instead, so the batch
    # endpoint can report a per-query failure rather than a healthy empty.
    propagate_failures: bool = False
    # Set when a shared batch pre-embed already failed: dense legs skip
    # their own embed attempt (hybrid degrades to keyword-only immediately)
    # instead of re-hammering a degraded embedding provider once per query.
    embedding_unavailable: bool = False


@dataclass(frozen=True, kw_only=True)
class BatchQueryFailure:
    """Positional per-query failure marker returned by ``batch_search``.

    The batch endpoint historically collapsed inner exceptions to ``[]``,
    making a backend failure indistinguishable from a genuine empty result.
    Returning this marker instead lets callers with fail-closed coverage
    contracts (e.g. cross-workspace fan-out) count the query as FAILED
    rather than "searched, no matches".
    """

    error: str


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

# Recency-intent words (Issue #4543). Deliberately distinct from
# TEMPORAL_WORDS: that set signals temporal *complexity* for query routing
# ("history", "before", "until" often want OLD documents), while these words
# signal the caller wants NEW material — used to gate the recency=auto boost.
RECENCY_WORDS: frozenset[str] = frozenset(
    {
        "latest",
        "newest",
        "recent",
        "recently",
        "current",
        "currently",
        "today",
        "yesterday",
        "now",
        "new",
    }
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
