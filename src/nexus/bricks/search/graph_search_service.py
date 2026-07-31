"""Graph-enhanced search service — txtai backend (Issue #2663).

Delegates graph-augmented search to the txtai backend's semantic graph.
Replaces the legacy GraphStore + GraphEnhancedRetriever stack.
"""

import logging
from typing import Any

from nexus.bricks.search.results import BaseSearchResult
from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


class DaemonSemanticSearchWrapper:
    """Wraps search daemon as SemanticSearch interface.

    Preserves backward compatibility with callers that need the
    SearchableProtocol-shaped interface.
    """

    def __init__(self, daemon: Any, *, zone_id: str | None = None) -> None:
        self.daemon = daemon
        self.embedding_provider = getattr(daemon, "_embedding_provider", None)
        self._zone_id = zone_id

    async def search(
        self,
        query: str,
        path: str = "/",
        limit: int = 10,
        search_mode: str = "hybrid",
        alpha: float = 0.5,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> list[BaseSearchResult]:
        results = await self.daemon.search(
            query=query,
            search_type=search_mode,
            limit=limit,
            path_filter=path if path != "/" else None,
            zone_id=self._zone_id,
        )
        return [
            BaseSearchResult(
                path=r.path,
                chunk_index=r.chunk_index,
                chunk_text=r.chunk_text,
                score=r.score,
                start_offset=r.start_offset,
                end_offset=r.end_offset,
                line_start=r.line_start,
                line_end=r.line_end,
                keyword_score=r.keyword_score,
                vector_score=r.vector_score,
            )
            for r in results
        ]


async def graph_enhanced_search(
    query: str,
    search_type: str,  # noqa: ARG001
    limit: int,
    path_filter: str | None,  # noqa: ARG001
    alpha: float,  # noqa: ARG001
    graph_mode: str,  # noqa: ARG001
    *,
    record_store: Any,  # noqa: ARG001
    async_session_factory: Any,  # noqa: ARG001
    search_daemon: Any,
    zone_id: str | None = None,
) -> list[BaseSearchResult]:
    """Execute graph-enhanced search via txtai backend (Issue #2663).

    Delegates to the txtai backend's ``graph_search()`` method, which uses
    txtai's built-in semantic graph for entity-aware retrieval.

    Args:
        query: Search query text
        search_type: Unused (txtai handles internally)
        limit: Maximum results
        path_filter: Unused (txtai handles internally)
        alpha: Unused (txtai handles internally)
        graph_mode: Unused (txtai always uses its graph when available)
        record_store: Unused (txtai manages its own storage)
        async_session_factory: Unused
        search_daemon: SearchDaemon instance
        zone_id: Namespace for zone isolation

    Returns:
        List of BaseSearchResult
    """
    effective_zone_id = zone_id or ROOT_ZONE_ID
    backend = getattr(search_daemon, "_backend", None)
    if backend is None:
        logger.warning("graph_enhanced_search: no backend available on daemon")
        return []

    graph_search_fn = getattr(backend, "graph_search", None)
    if graph_search_fn is None:
        logger.warning("graph_enhanced_search: backend does not support graph_search")
        return []

    # Issue #4544 (Codex review R2): the graph path must honour the same
    # probe → over-fetch → pinned-attach → trim workflow as daemon searches,
    # otherwise a demoted top-N hit cannot be displaced by the backend's
    # rank-N+1 candidate (it was never fetched). getattr guards keep mocked
    # daemons (tests) working; a missing probe means no weights are applied.
    has_weights = False
    tier_snapshot: Any = None
    probe = getattr(search_daemon, "_tier_weight_probe", None)
    if probe is not None:
        has_weights, tier_snapshot = await probe(effective_zone_id)
    fetch_limit = limit
    if has_weights:
        factor = max(
            1, getattr(getattr(search_daemon, "config", None), "tier_boost_overfetch_factor", 1)
        )
        fetch_limit = limit * factor
        if fetch_limit <= limit:
            # Codex review R4: weights exist but the pool will not be
            # widened (factor <= 1) — ranking weights are suppressed for
            # this request; count it at the decision point.
            stats = getattr(search_daemon, "stats", None)
            if stats is not None:
                stats.tier_boost_suppressed_searches += 1
            logger.warning(
                "tier weights configured for zone=%r but graph pool not "
                "widened (factor<=1) — ranking weights suppressed",
                effective_zone_id,
            )

    results: list[BaseSearchResult] = await graph_search_fn(
        query, zone_id=effective_zone_id, limit=fetch_limit, path_filter=path_filter
    )
    # Issue #3773: attach admin-configured path contexts. Pass the caller's
    # effective zone so the daemon can fall back to it when the backend
    # returned ``BaseSearchResult`` without ``zone_id`` set — otherwise
    # non-root-zone graph searches would silently collapse to root and
    # attach the wrong (or no) descriptions (Round-4 review).
    attach = getattr(search_daemon, "_attach_path_contexts", None)
    if attach is not None:
        await attach(
            results,
            zone_id=effective_zone_id,
            pinned_snapshots=(
                {effective_zone_id: tier_snapshot} if tier_snapshot is not None else None
            ),
            # Same gate as the daemon paths (Codex review R3): weights may
            # only mutate ranking when the pool was actually widened.
            apply_weights=tier_snapshot is not None and fetch_limit > limit,
        )
    if fetch_limit != limit:
        results = results[:limit]
    return results
