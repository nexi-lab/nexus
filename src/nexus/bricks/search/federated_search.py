"""Federated cross-zone search dispatcher (Issue #3147, Phases 1-3).

Fans out search queries across accessible zones, fuses results via N-way
RRF fusion, and returns merged results with zone provenance metadata.

Phase 1: Single daemon, multi-zone fan-out via zone_id parameter.
Phase 2: Per-zone daemons via ZoneSearchRegistry, SearchDelegation auth.
Phase 3: Zone-capability-aware query routing, result caching, partial results.

Design decisions (from review):
- 1A: No score normalization — RRF handles heterogeneous score distributions.
- 2A: Zone-level auth only (no per-file ReBAC in Phase 1).
- 5A: Reuses existing rrf_multi_fusion from fusion.py.
- 8A: Returns zones_searched / zones_failed metadata.
- 13B: Forces semantic path for keyword search to avoid BM25S/Zoekt zone leak.
- 14A: Per-zone timeout via asyncio.wait_for.
- 15A: Short-TTL cache on zone discovery.
- 16A: Bounded fan-out via asyncio.Semaphore.
"""

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nexus.bricks.search.daemon import _BACKEND_LEG_TIMING_KEYS as _TIMING_LEG_KEYS
from nexus.bricks.search.fusion import rrf_multi_fusion
from nexus.bricks.search.result_builders import cap_chunks_per_page
from nexus.contracts.protocols.activity import EventKind, Result, emit

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_ZONE_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CONCURRENT_ZONES = 5
DEFAULT_ZONE_CACHE_TTL_SECONDS = 60.0
DEFAULT_OVER_FETCH_FACTOR = 2
DEFAULT_RESULT_CACHE_TTL_SECONDS = 30.0
DEFAULT_RESULT_CACHE_MAX_ENTRIES = 256


@dataclass
class FederatedFusionStrategy:
    """How to merge results across zones.

    RAW_SCORE: Direct merge-sort by raw score. Use when all zones have
        the same scoring function (same embedding model, same FTS config).
        This is what Elasticsearch, Solr, and Vespa do for cross-shard merging.
        Scores are directly comparable — no normalization needed.

    RRF: Reciprocal Rank Fusion. Merges by rank position, ignoring score
        magnitudes. Use when zones have DIFFERENT scoring functions (different
        embedding models, different backends). Robust to heterogeneous scores
        but loses score magnitude information.
    """

    RAW_SCORE = "raw_score"
    RRF = "rrf"


@dataclass
class FederatedSearchConfig:
    """Configuration for federated search dispatcher."""

    zone_timeout_seconds: float = DEFAULT_ZONE_TIMEOUT_SECONDS
    max_concurrent_zones: int = DEFAULT_MAX_CONCURRENT_ZONES
    zone_cache_ttl_seconds: float = DEFAULT_ZONE_CACHE_TTL_SECONDS
    over_fetch_factor: int = DEFAULT_OVER_FETCH_FACTOR
    # Cross-zone fusion strategy (default: raw_score for homogeneous zones)
    fusion_strategy: str = FederatedFusionStrategy.RAW_SCORE
    # Phase 3: Result caching
    result_cache_ttl_seconds: float = DEFAULT_RESULT_CACHE_TTL_SECONDS
    result_cache_max_entries: int = DEFAULT_RESULT_CACHE_MAX_ENTRIES
    result_cache_enabled: bool = False  # Opt-in


@dataclass
class ZoneFailure:
    """Metadata about a zone that failed during federated search."""

    zone_id: str
    error: str


@dataclass
class FederatedSearchResponse:
    """Response from a federated search including zone metadata."""

    results: list[dict[str, Any]]
    zones_searched: list[str]
    zones_failed: list[ZoneFailure]
    zones_skipped: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    cached: bool = False
    # Issue #4269 (Codex R6): per-leg backend phase timings (index_load_ms,
    # keyword_ms, vector_ms, fusion_ms, ...) SUMMED across the LOCAL zones that
    # served this query, so a cold federated search exposes the same index-load
    # phase split as a single-zone one. Remote zones (gRPC peers) do not return
    # per-leg timing through the delegation path, so they don't contribute here.
    search_timing: dict[str, float] = field(default_factory=dict)
    # #3778 marker (#4541 review round 9): True when any zone served this
    # query with a degraded dense leg — captured BEFORE ReBAC filtering so
    # the signal survives empty and fully-filtered responses (and cache hits,
    # since the cached object carries it).
    semantic_degraded: bool = False


class FederationUnreachableError(Exception):
    """Raised (or signaled) when federated search cannot reach any peer.

    Issue #3778: SANDBOX profile treats this as a signal to fall back to
    local BM25S and stamp results with ``semantic_degraded=True``.
    """


def is_all_peers_failed(response: FederatedSearchResponse) -> bool:
    """Return True when the response reflects zero reachable peers.

    Equivalent to: zero peers configured, or every configured peer
    failed to respond.

    Issue #3778.
    """
    if not response.zones_searched and not response.zones_failed:
        return True
    return bool(not response.results and len(response.zones_failed) >= len(response.zones_searched))


def _zone_results_degraded(results: Any) -> bool:
    """True when a zone's results carry the #3778 degradation marker — either
    list-level (``SearchResultList.semantic_degraded``, which survives empty
    responses) or per-result (local result objects / remote result dicts).
    Checked BEFORE ReBAC filtering so a fully-filtered response keeps the
    signal (#4541 review round 9)."""
    if getattr(results, "semantic_degraded", False):
        return True
    for r in results:
        if isinstance(r, dict):
            if r.get("semantic_degraded"):
                return True
        elif getattr(r, "semantic_degraded", None):
            return True
    return False


def _aggregate_zone_timing(timings: list[dict[str, float]]) -> dict[str, float]:
    """Sum per-leg backend phase timings across the local zones that served a
    federated query (Issue #4269 Codex R6). Legs represent cumulative backend
    work, so summing gives the total per-leg cost across zones; the dispatcher's
    wall-clock ``latency_ms`` remains the end-to-end total."""
    agg: dict[str, float] = {}
    for timing in timings:
        if not isinstance(timing, dict):
            continue
        for key in _TIMING_LEG_KEYS:
            val = timing.get(key)
            if isinstance(val, int | float):
                agg[key] = agg.get(key, 0.0) + float(val)
    return agg


class FederatedSearchDispatcher:
    """Fans out search queries across zones and fuses results via RRF.

    Phase 1: Uses a single daemon for all zones.
    Phase 2: Uses ZoneSearchRegistry to dispatch to per-zone daemons.
    Phase 3: Considers zone capabilities to skip unsupported search modes.

    The daemon's SQL WHERE zone_id filtering handles zone isolation
    for database-backed searches (pgvector, FTS). In-memory backends
    (BM25S, Zoekt) do not support zone_id filtering, so federated
    keyword search forces the semantic path (decision 13B).
    """

    def __init__(
        self,
        daemon: Any,
        rebac: Any,
        config: FederatedSearchConfig | None = None,
        *,
        registry: Any | None = None,
        enable_per_file_rebac: bool = False,
    ):
        self._daemon = daemon  # Default/fallback daemon
        self._rebac = rebac
        self._config = config or FederatedSearchConfig()
        self._registry = registry  # Phase 2: ZoneSearchRegistry
        self._enable_per_file_rebac = enable_per_file_rebac  # Phase 2: per-file filtering
        # Zone discovery cache: subject_key -> (zones, expiry_time)
        self._zone_cache: dict[str, tuple[list[str], float]] = {}
        # Phase 3: Result cache: cache_key -> (response, expiry_time)
        self._result_cache: dict[str, tuple[FederatedSearchResponse, float]] = {}

    def _get_daemon_for_zone(self, zone_id: str) -> Any:
        """Get the daemon to use for a specific zone.

        Phase 2: Checks the registry first, falls back to default daemon.
        """
        if self._registry is not None:
            daemon = self._registry.get_daemon(zone_id)
            if daemon is not None:
                return daemon
        return self._daemon

    async def _get_accessible_zones(self, subject: tuple[str, str]) -> list[str]:
        """Get zones accessible to subject, with TTL cache (decision 15A)."""
        cache_key = f"{subject[0]}:{subject[1]}"
        now = time.monotonic()

        cached = self._zone_cache.get(cache_key)
        if cached is not None:
            zones, expiry = cached
            if now < expiry:
                return zones

        zones = list(await self._rebac.list_accessible_zones(subject=subject))
        self._zone_cache[cache_key] = (
            zones,
            now + self._config.zone_cache_ttl_seconds,
        )
        return zones

    def _get_effective_search_type(
        self,
        zone_id: str,
        search_type: str,
    ) -> tuple[str, float | None]:
        """Determine effective search type for a zone based on capabilities.

        Phase 3: Skips semantic queries to keyword-only zones.

        Returns:
            (effective_search_type, alpha_override_or_None)
        """
        # Decision 13B rationale: BM25S and Zoekt in-memory indexes don't
        # filter by zone_id, so keyword search through them leaks cross-zone
        # results. When they're active, we force the semantic path which
        # uses pgvector (zone-filtered).
        #
        # However, when BM25S/Zoekt are NOT available (bm25_documents=0,
        # zoekt_available=False), keyword search falls through to PostgreSQL
        # FTS which IS zone-filtered. In that case, forcing semantic would
        # break search on DBs without embeddings. So we only force semantic
        # when the leaky backends are actually active.
        # Check if the daemon has leaky (non-zone-filtered) keyword backends.
        # BM25S and Zoekt don't filter by zone_id, so keyword through them
        # leaks cross-zone results. When they have data, we force semantic.
        # When they're empty or absent, FTS (zone-filtered) handles keyword.
        try:
            daemon = self._get_daemon_for_zone(zone_id)
            _stats = (
                daemon.get_stats()
                if hasattr(daemon, "get_stats") and callable(getattr(daemon, "get_stats", None))
                else {}
            )
            has_bm25s = _stats.get("bm25_documents", 0) > 0 if isinstance(_stats, dict) else False
            has_zoekt = _stats.get("zoekt_available", False) if isinstance(_stats, dict) else False
        except Exception:
            has_bm25s = False
            has_zoekt = False
        has_leaky_keyword = has_bm25s or has_zoekt

        if self._registry is None:
            if search_type == "keyword" and has_leaky_keyword:
                return ("hybrid", 1.0)  # 13B: force semantic to avoid zone leak
            return (search_type, None)

        caps = self._registry.get_capabilities(zone_id)
        if caps is None:
            if search_type == "keyword" and has_leaky_keyword:
                return ("hybrid", 1.0)
            return (search_type, None)

        # Phase 3: Route based on zone capabilities
        if search_type in ("semantic", "hybrid") and not caps.supports_semantic:
            return ("keyword", None)

        if search_type == "keyword" and has_leaky_keyword:
            return ("hybrid", 1.0)

        return (search_type, None)

    def _mint_search_delegation(
        self,
        subject: tuple[str, str],
        source_zone_id: str,
        target_zones: frozenset[str],
    ) -> Any:
        """Mint a short-lived SearchDelegation for remote zone queries.

        This credential authorizes the remote zone to execute search RPCs
        on behalf of the original requester. The delegation is:
        - Read-only (hard method allowlist: search, semantic_search)
        - Short-lived (30s TTL)
        - Scoped to specific target zones

        Called by the dispatcher when a zone is served by a remote daemon
        (Phase 2). The delegation is sent as part of the gRPC auth context.
        """
        from nexus.contracts.search_delegation import SearchDelegation

        return SearchDelegation(
            delegation_id=f"sd_{uuid.uuid4().hex[:12]}",
            source_zone_id=source_zone_id,
            target_zones=target_zones,
            subject=subject,
        )

    async def _search_zone(
        self,
        zone_id: str,
        query: str,
        search_type: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
        subject: tuple[str, str] | None = None,
        rrf_k: int = 60,
    ) -> list[Any]:
        """Search a single zone with capability-aware routing."""
        effective_type, alpha_override = self._get_effective_search_type(zone_id, search_type)
        effective_alpha = alpha_override if alpha_override is not None else alpha
        # 13B safety promotion (keyword -> hybrid alpha=1.0 under leaky BM25S/
        # Zoekt) must not run WEIGHTED fusion: the cross-zone merge guard only
        # sees the original request type, and weighted scores are shard-local
        # min-max normalized. rrf_weighted honours the alpha=1.0 override
        # (semantic-only, preserving 13B) while producing reciprocal-rank
        # scores that stay comparable across zones (#4541 review round 10).
        effective_fusion = fusion_method
        if effective_type != search_type and fusion_method == "weighted":
            effective_fusion = "rrf_weighted"

        # Phase 2: Check if this zone has a remote transport in the registry.
        # If so, search via gRPC with a SearchDelegation credential.
        # rrf_k travels on the wire like alpha/fusion_method so the payload is
        # complete whenever the remote side can serve it. KNOWN GAP (pre-#4541,
        # applies to every field including query): the remote ``search`` RPC is
        # rejected by ``parse_method_params`` as an unknown method — it has no
        # METHOD_PARAMS schema and is not @rpc_expose'd — so registry-remote
        # zones currently land in ``zones_failed`` and no fusion knob (or any
        # param) reaches them. Tracked as #4556; fixing the RPC
        # surface is out of scope for the fusion-param plumbing.
        if self._registry is not None and self._registry.is_remote(zone_id):
            return await self._search_remote_zone(
                zone_id=zone_id,
                query=query,
                search_type=effective_type,
                limit=limit,
                path_filter=path_filter,
                alpha=effective_alpha,
                fusion_method=effective_fusion,
                rrf_k=rrf_k,
                subject=subject,
            )

        # Local zone: call daemon.search() directly
        daemon = self._get_daemon_for_zone(zone_id)
        results = await daemon.search(
            query=query,
            search_type=effective_type,
            limit=limit,
            path_filter=path_filter,
            alpha=effective_alpha,
            fusion_method=effective_fusion,
            rrf_k=rrf_k,
            zone_id=zone_id,
        )

        # Tag results with zone provenance. Return the SearchResultList as-is
        # (it is a list subclass) rather than collapsing via list(), so its
        # ``.search_timing`` per-leg snapshot survives for federated phase-timing
        # aggregation (Issue #4269 Codex R6). Remote zones return a plain list.
        # Bind to a typed local so the SearchResultList object (and its timing)
        # is preserved at runtime without returning ``Any``.
        tagged_results: list[Any] = results
        for r in tagged_results:
            r.zone_id = zone_id

        return tagged_results

    async def _search_remote_zone(
        self,
        zone_id: str,
        query: str,
        search_type: str,
        limit: int,
        path_filter: str | None,
        alpha: float,
        fusion_method: str,
        subject: tuple[str, str] | None = None,
        rrf_k: int = 60,
    ) -> list[Any]:
        """Search a remote zone via gRPC with SearchDelegation auth.

        Mints a short-lived delegation, sends it as the auth_token in
        a Call RPC to the remote node's search method, and converts
        the response back into result dicts.
        """
        assert self._registry is not None  # Checked by caller
        transport = self._registry.get_transport(zone_id)
        if transport is None:
            raise RuntimeError(f"No transport registered for remote zone {zone_id}")

        # Mint delegation scoped to this zone
        delegation = self._mint_search_delegation(
            subject=subject or ("user", "anonymous"),
            source_zone_id="local",
            target_zones=frozenset({zone_id}),
        )

        logger.debug(
            "[FEDERATED] Remote search zone=%s delegation=%s",
            zone_id,
            delegation.delegation_id,
        )

        # Build search params for the remote Call RPC
        params = {
            "query": query,
            "search_type": search_type,
            "limit": limit,
            "zone_id": zone_id,
            "alpha": alpha,
            "fusion_method": fusion_method,
            "rrf_k": rrf_k,
        }
        if path_filter:
            params["path_filter"] = path_filter

        # Send via gRPC — the delegation_id is passed as auth_token
        # so the remote servicer's SearchDelegation guard can validate it.
        raw_result = await asyncio.to_thread(
            transport.call_rpc,
            "search",
            params,
            None,  # read_timeout (use default)
            delegation.delegation_id,  # auth_token override
        )

        # Convert remote response to result dicts with zone tagging.
        # Issue #4544 (Codex review R1) / #4541 review: the server-side RPC
        # search handler returns a ``{"results": [...]}`` envelope
        # (handle_search in src/nexus/server/rpc/handlers/filesystem.py),
        # which the bare-list check silently discarded — real remote zones
        # contributed zero results. Older transports may hand back a bare
        # list — accept both shapes.
        if isinstance(raw_result, dict):
            raw_result = raw_result.get("results", [])
        results = raw_result if isinstance(raw_result, list) else []
        for r in results:
            if isinstance(r, dict):
                r["zone_id"] = zone_id
                r["zone_qualified_path"] = f"{zone_id}:{r.get('path', '')}"

        return results

    def _should_skip_zone(self, zone_id: str, search_type: str) -> bool:
        """Phase 3: Check if a zone should be skipped entirely.

        A keyword-only zone is skipped for pure semantic queries
        (no point querying a zone that can't satisfy the search type).
        """
        if self._registry is None:
            return False

        caps = self._registry.get_capabilities(zone_id)
        if caps is None:
            return False

        return search_type == "semantic" and not caps.supports_semantic

    def _make_cache_key(
        self,
        query: str,
        subject: tuple[str, str],
        search_type: str,
        limit: int,
        path_filter: str | None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        rrf_k: int = 60,
        zone_filter: frozenset[str] | None = None,
    ) -> str:
        """Phase 3: Create a cache key for result caching.

        Fusion knobs are part of the key: they change result ordering now
        that the daemon honours them (#4541), so requests differing only in
        alpha / fusion_method / rrf_k must not share a cache entry.

        The token's zone allow-list (#3785) is also part of the key: cache
        lookup happens before accessible zones are intersected with the
        filter, so without it a broadly-scoped request could seed a cache
        entry that a later narrowly-scoped token would read back — leaking
        results from zones outside that token's scope (#4541 review).

        Canonical-JSON serialization (round-5 review): delimiter
        concatenation allowed cross-field collisions — subject_id
        ``alice|foo`` + query ``bar`` hashed identically to ``alice`` +
        ``foo|bar`` — and cache lookup precedes ReBAC, so a collision would
        leak another subject's results. JSON escaping makes field
        boundaries unambiguous, and the empty allow-list (``[]``, zero
        zones) stays distinct from the wildcard (``null``).
        """
        raw = json.dumps(
            [
                subject[0],
                subject[1],
                query,
                search_type,
                limit,
                path_filter,
                alpha,
                fusion_method,
                rrf_k,
                sorted(zone_filter) if zone_filter is not None else None,
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _get_cached_result(
        self, cache_key: str, start: float | None = None
    ) -> FederatedSearchResponse | None:
        """Phase 3: Check result cache.

        ``start`` is the current request's perf_counter() origin; when provided,
        the cache hit reports the ACTUAL cache-lookup elapsed rather than
        replaying the original miss's wall time (Codex R9) — so a cache hit that
        executed no backend work does not masquerade as a slow cold query.
        """
        if not self._config.result_cache_enabled:
            return None
        cached = self._result_cache.get(cache_key)
        if cached is None:
            return None
        response, expiry = cached
        if time.monotonic() > expiry:
            del self._result_cache[cache_key]
            return None
        hit_latency_ms = (time.perf_counter() - start) * 1000 if start is not None else 0.0
        return FederatedSearchResponse(
            results=response.results,
            zones_searched=response.zones_searched,
            zones_failed=response.zones_failed,
            zones_skipped=response.zones_skipped,
            latency_ms=hit_latency_ms,
            cached=True,
            # A cache hit executes no BM25/vector/index work, so it must NOT
            # replay the original query's per-leg phase timings — that would
            # report backend work that did not happen (Codex R8). Leave empty;
            # the ``cached=True`` flag marks the response.
            search_timing={},
            # Degradation IS a property of the cached payload — a hit serves
            # the same (possibly empty) degraded results, so the marker must
            # survive the clone (#4541 review round 10).
            semantic_degraded=response.semantic_degraded,
        )

    def _cache_result(self, cache_key: str, response: FederatedSearchResponse) -> None:
        """Phase 3: Store result in cache."""
        if not self._config.result_cache_enabled:
            return
        # Evict oldest if at capacity
        if len(self._result_cache) >= self._config.result_cache_max_entries:
            oldest_key = min(self._result_cache, key=lambda k: self._result_cache[k][1])
            del self._result_cache[oldest_key]
        self._result_cache[cache_key] = (
            response,
            time.monotonic() + self._config.result_cache_ttl_seconds,
        )

    async def search(
        self,
        query: str,
        subject: tuple[str, str],
        search_type: str = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        zone_filter: frozenset[str] | None = None,  # NEW (#3785)
        rrf_k: int = 60,
    ) -> FederatedSearchResponse:
        """Public federated search entry point with activity-event instrumentation (#3791).

        Wraps :meth:`_search_impl` to record SEARCH events on success and
        BLOCKED events on exceptions. Wall-clock latency is captured via
        ``time.monotonic``. ``subject_zone`` is set to ``"federated"``
        because the call spans zones; ``token_hash`` is not currently
        extractable from the ``subject`` tuple so it is left as ``None``.
        """
        _start = time.monotonic()
        try:
            response = await self._search_impl(
                query=query,
                subject=subject,
                search_type=search_type,
                limit=limit,
                path_filter=path_filter,
                alpha=alpha,
                fusion_method=fusion_method,
                rrf_k=rrf_k,
                zone_filter=zone_filter,
            )
        except Exception:
            emit(
                kind=EventKind.SEARCH,
                result=Result.BLOCKED,
                actor_token_hash=None,
                subject_zone="federated",
                latency_ms=int((time.monotonic() - _start) * 1000),
            )
            raise
        emit(
            kind=EventKind.SEARCH,
            result=Result.OK,
            actor_token_hash=None,
            subject_zone="federated",
            subject_extra={
                "hits": len(response.results) if hasattr(response, "results") else None,
                "federated": True,
            },
            latency_ms=int((time.monotonic() - _start) * 1000),
        )
        return response

    async def _search_impl(
        self,
        query: str,
        subject: tuple[str, str],
        search_type: str = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        zone_filter: frozenset[str] | None = None,  # NEW (#3785)
        rrf_k: int = 60,
    ) -> FederatedSearchResponse:
        """Execute a federated search across all accessible zones.

        Args:
            query: Search query text.
            subject: (subject_type, subject_id) tuple for the caller.
            search_type: "keyword", "semantic", or "hybrid".
            limit: Maximum results to return after fusion.
            path_filter: Optional path prefix filter.
            alpha: Semantic vs keyword weight.
            fusion_method: Fusion method for intra-zone hybrid search.
            rrf_k: RRF rank constant for intra-zone hybrid fusion (#4541).
                Applied to local zones only; remote zones use their default.
            zone_filter: Optional upper-bound zone allow-list. When set,
                intersects with accessible_zones to enforce per-token zone
                scoping (#3785).

        Returns:
            FederatedSearchResponse with fused results and zone metadata.
        """
        start = time.perf_counter()

        # Phase 3: Check result cache
        cache_key = self._make_cache_key(
            query,
            subject,
            search_type,
            limit,
            path_filter,
            alpha,
            fusion_method,
            rrf_k,
            zone_filter=zone_filter,
        )
        cached = self._get_cached_result(cache_key, start=start)
        if cached is not None:
            return cached

        # 1. Zone discovery (decision 2A: zone-level auth is sufficient)
        accessible_zones = await self._get_accessible_zones(subject)

        # #3785: intersect with token's zone allow-list if provided.
        if zone_filter is not None:
            accessible_zones = [z for z in accessible_zones if z in zone_filter]

        if not accessible_zones:
            return FederatedSearchResponse(
                results=[],
                zones_searched=[],
                zones_failed=[],
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # Phase 3: Filter out zones that can't handle this search type
        zones_skipped: list[str] = []
        searchable_zones: list[str] = []
        for z in accessible_zones:
            if self._should_skip_zone(z, search_type):
                zones_skipped.append(z)
            else:
                searchable_zones.append(z)

        if not searchable_zones:
            return FederatedSearchResponse(
                results=[],
                zones_searched=[],
                zones_failed=[],
                zones_skipped=zones_skipped,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # Issue #4542 (round-3 review): the per-document cap applies on EVERY
        # federated control path, so behavior does not depend on how many
        # zones happen to be accessible. Resolved once here.
        pooling_cap = self._pooling_chunks_per_page()

        def _zone_fetch_limit(zone_id: str, base: int) -> int:
            """Cap-aware per-zone fetch window (rounds 3+6 review).

            A zone window saturated by one long doc leaves nothing to
            backfill after the per-doc cap. Local HYBRID zones already cap
            internally at the daemon (full-union backfill) — widening them
            again would compound multipliers into pathological retrieval
            windows (round-6 review), so they keep the base window. The
            wider window applies only where dispatcher-side capping is the
            only protection: semantic/keyword effective types and remote
            zones. A fixed window remains theoretically saturable —
            adaptive pagination is deliberately out of scope; flag-off
            keeps the historical window everywhere.
            """
            if pooling_cap is None:
                return base
            effective_type, _ = self._get_effective_search_type(zone_id, search_type)
            is_remote = self._registry is not None and self._registry.is_remote(zone_id)
            if effective_type == "hybrid" and not is_remote:
                return base
            return base * (pooling_cap + 1)

        # Single zone: skip fusion overhead
        if len(searchable_zones) == 1:
            zone_id = searchable_zones[0]
            single_zone_fetch = _zone_fetch_limit(zone_id, limit)
            try:
                results = await asyncio.wait_for(
                    self._search_zone(
                        zone_id,
                        query,
                        search_type,
                        single_zone_fetch,
                        path_filter,
                        alpha,
                        fusion_method,
                        rrf_k=rrf_k,
                        subject=subject,
                    ),
                    timeout=self._config.zone_timeout_seconds,
                )
                # Capture the local zone's per-leg timing BEFORE ReBAC filtering
                # (which returns a plain list, dropping search_timing) — #4269 R6.
                zone_timing = _aggregate_zone_timing(
                    [dict(getattr(results, "search_timing", {}) or {})]
                )
                zone_degraded = _zone_results_degraded(results)
                # Per-file ReBAC post-filter (Phase 2+)
                if self._enable_per_file_rebac:
                    results = await filter_federated_results(
                        results,
                        subject=subject,
                        rebac=self._rebac,
                    )
                result_dicts = [_result_to_dict(r) for r in results]
                if pooling_cap is not None:
                    result_dicts = cap_chunks_per_page(
                        result_dicts, chunks_per_page=pooling_cap
                    )
                result_dicts = result_dicts[:limit]
                resp = FederatedSearchResponse(
                    results=result_dicts,
                    zones_searched=[zone_id],
                    zones_failed=[],
                    zones_skipped=zones_skipped,
                    latency_ms=(time.perf_counter() - start) * 1000,
                    search_timing=zone_timing,
                    semantic_degraded=zone_degraded,
                )
                self._cache_result(cache_key, resp)
                return resp
            except Exception as e:
                logger.warning("[FEDERATED] Zone %s failed: %s", zone_id, e)
                return FederatedSearchResponse(
                    results=[],
                    zones_searched=[],
                    zones_failed=[ZoneFailure(zone_id=zone_id, error=str(e))],
                    zones_skipped=zones_skipped,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )

        # 2. Multi-zone fan-out with concurrency bound (decision 16A).
        # Per-zone fetch windows are cap-aware via _zone_fetch_limit.
        semaphore = asyncio.Semaphore(self._config.max_concurrent_zones)

        async def _bounded_search(
            zone_id: str,
        ) -> tuple[str, list[Any] | BaseException]:
            async with semaphore:
                try:
                    results = await asyncio.wait_for(
                        self._search_zone(
                            zone_id,
                            query,
                            search_type,
                            _zone_fetch_limit(
                                zone_id, limit * self._config.over_fetch_factor
                            ),
                            path_filter,
                            alpha,
                            fusion_method,
                            rrf_k=rrf_k,
                            subject=subject,
                        ),
                        timeout=self._config.zone_timeout_seconds,
                    )
                    return (zone_id, results)
                except Exception as e:
                    return (zone_id, e)

        zone_outcomes = await asyncio.gather(
            *[_bounded_search(z) for z in searchable_zones],
        )

        # 3. Collect results and failures (decision 8A)
        zones_searched: list[str] = []
        zones_failed: list[ZoneFailure] = []
        zone_result_lists: list[tuple[str, list[Any]]] = []
        # Issue #4269 (Codex R6): collect each local zone's per-leg timing
        # snapshot (carried on the SearchResultList) so the aggregate phase
        # split survives fan-out, even for zones that returned no results.
        zone_timings: list[dict[str, float]] = []

        any_zone_degraded = False
        for zone_id, outcome in zone_outcomes:
            if isinstance(outcome, BaseException):
                logger.warning("[FEDERATED] Zone %s failed: %s", zone_id, outcome)
                zones_failed.append(ZoneFailure(zone_id=zone_id, error=str(outcome)))
            else:
                zones_searched.append(zone_id)
                timing = getattr(outcome, "search_timing", None)
                if isinstance(timing, dict):
                    zone_timings.append(timing)
                if not any_zone_degraded and _zone_results_degraded(outcome):
                    any_zone_degraded = True
                if outcome:  # non-empty results
                    zone_result_lists.append((zone_id, outcome))

        # 4. Per-file ReBAC post-filter before fusion (Phase 2+)
        if self._enable_per_file_rebac:
            filtered_lists: list[tuple[str, list[Any]]] = []
            for zid, zone_results in zone_result_lists:
                filtered = await filter_federated_results(
                    zone_results,
                    subject=subject,
                    rebac=self._rebac,
                )
                if filtered:
                    filtered_lists.append((zid, filtered))
            zone_result_lists = filtered_lists

        # 5. Merge results across zones.
        #    Default: raw score merge-sort (all zones use identical scoring
        #    functions, so scores are directly comparable — same approach as
        #    Elasticsearch cross-shard, Solr distributed, Vespa federation).
        #    Fallback: RRF for heterogeneous zones (different scoring functions).
        if not zone_result_lists:
            fused_results: list[dict[str, Any]] = []
        elif len(zone_result_lists) == 1:
            # Issue #4542 (round-4 review): the one-surviving-zone branch must
            # honor the cap too — zone failures or empty zones must not
            # silently change the flag semantics.
            _zone_id, results = zone_result_lists[0]
            fused_results = [_result_to_dict(r) for r in results]
            if pooling_cap is not None:
                fused_results = cap_chunks_per_page(fused_results, chunks_per_page=pooling_cap)
            fused_results = fused_results[:limit]
        elif self._config.fusion_strategy == FederatedFusionStrategy.RRF or (
            fusion_method == "weighted" and search_type == "hybrid"
        ):
            # RRF: for heterogeneous zones with different scoring functions.
            # ALSO forced for weighted HYBRID fusion (#4541 review rounds
            # 8-9): weighted fusion min-max normalizes scores INSIDE each
            # zone, so raw values are shard-local — the top hit of a weak
            # zone scores 1.0 and would outrank materially stronger hits from
            # another zone under a raw-score merge. Keyword/semantic requests
            # never run weighted fusion (the knob is hybrid-only), so their
            # raw scores stay comparable and keep the default merge;
            # rrf/rrf_weighted scores share one reciprocal-rank formula and
            # stay comparable too.
            # Issue #4542 (round-4 review): with the cap active, cap each zone
            # list first (zone-scoped budgets) and fuse at CHUNK grain — the
            # page-grain zone_qualified_path key would collapse every doc back
            # to one chunk, silently overriding chunks_per_page > 1 and
            # underfilling the page. Flag-off keeps the historical page-grain
            # fusion identity exactly.
            rrf_lists: list[tuple[str, list[Any]]] = zone_result_lists
            rrf_id_key = "zone_qualified_path"
            if pooling_cap is not None:
                capped_lists: list[tuple[str, list[Any]]] = []
                for zid, zresults in zone_result_lists:
                    zdicts = [_result_to_dict(r) for r in zresults]
                    zdicts = cap_chunks_per_page(zdicts, chunks_per_page=pooling_cap)
                    for d in zdicts:
                        page_key = d.get("zone_qualified_path") or (
                            f"{d.get('zone_id', '')}:{d.get('path', '')}"
                        )
                        d["_zq_chunk"] = f"{page_key}:{d.get('chunk_index', 0)}"
                    capped_lists.append((zid, zdicts))
                rrf_lists = capped_lists
                rrf_id_key = "_zq_chunk"
            fused_results = rrf_multi_fusion(
                result_lists=rrf_lists,
                k=60,
                limit=limit,
                id_key=rrf_id_key,
            )
            for d in fused_results:
                d.pop("_zq_chunk", None)
            # Issue #3773 (Round-6 review): rrf_multi_fusion emits dicts built
            # from __dataclass_fields__ verbatim, so ``context: None`` leaks
            # into the wire. Normalize here so every federated code path
            # matches the non-federated router's omit-when-None contract.
            fused_results = [_strip_none_context(d) for d in fused_results]
        else:
            # Raw score merge-sort: for homogeneous zones (default)
            fused_results = _merge_by_raw_score(
                zone_result_lists,
                limit,
                chunks_per_page=pooling_cap,
            )

        resp = FederatedSearchResponse(
            results=fused_results,
            zones_searched=zones_searched,
            zones_failed=zones_failed,
            zones_skipped=zones_skipped,
            latency_ms=(time.perf_counter() - start) * 1000,
            search_timing=_aggregate_zone_timing(zone_timings),
            semantic_degraded=any_zone_degraded,
        )
        self._cache_result(cache_key, resp)
        return resp

    def _pooling_chunks_per_page(self) -> int | None:
        """Per-document pooling cap for the flat merge (Issue #4542).

        Reads the default daemon's ``DaemonConfig`` — the single place where
        NEXUS_SEARCH_PAGE_AGGREGATION / NEXUS_SEARCH_CHUNKS_PER_PAGE land —
        so one env toggle governs both single-zone and federated pooling.
        The strict ``is True`` / ``isinstance`` checks keep Mock daemons
        (tests) from enabling pooling via auto-created attributes.
        """
        cfg = getattr(self._daemon, "config", None)
        if getattr(cfg, "page_aggregation", None) is not True:
            return None
        cap = getattr(cfg, "chunks_per_page", None)
        return cap if isinstance(cap, int) and cap > 0 else None

    def invalidate_zone_cache(self, subject: tuple[str, str] | None = None) -> None:
        """Invalidate zone discovery cache."""
        if subject is None:
            self._zone_cache.clear()
        else:
            cache_key = f"{subject[0]}:{subject[1]}"
            self._zone_cache.pop(cache_key, None)

    def invalidate_result_cache(self) -> None:
        """Clear the result cache."""
        self._result_cache.clear()


def _merge_by_raw_score(
    zone_result_lists: list[tuple[str, list[Any]]],
    limit: int,
    *,
    chunks_per_page: int | None = None,
) -> list[dict[str, Any]]:
    """Merge results from multiple zones by the daemon's score (global sort).

    All zones use the same search daemon with the same scoring pipeline
    (same embedding model, same FTS config, same intra-zone RRF k=60),
    so the daemon's score field is directly comparable across zones.

    For keyword/semantic: the score is the raw FTS ts_rank or cosine sim.
    For hybrid: the score is the intra-zone RRF fusion score. While the
    absolute values are small (~0.016), the relative ordering IS correct
    and comparable across zones because all zones use the same k and pipeline.

    This is the same approach as Elasticsearch query_then_fetch and Solr
    distributed search: merge-sort by the score each shard produced.
    """
    all_results: list[dict[str, Any]] = []
    for _zone_id, results in zone_result_lists:
        zone_dicts = [_result_to_dict(r) for r in results]
        # Issue #4542: cap per zone (not on the concatenated list) so the
        # same path in two zones stays two distinct documents. The cap is
        # order-preserving over the zone's daemon-sorted list.
        if chunks_per_page is not None:
            zone_dicts = cap_chunks_per_page(zone_dicts, chunks_per_page=chunks_per_page)
        all_results.extend(zone_dicts)

    # Dedup, keeping highest score. With pooling OFF this is the historical
    # page-grain zone_qualified_path key (one chunk per doc per zone). With
    # pooling ON the page-grain key would collapse every doc straight back
    # to one chunk — ``zone_qualified_path`` carries no chunk index on
    # either the local dataclass or the remote-dict shape — so dedup
    # switches to chunk grain and lets ``chunks_per_page`` govern
    # per-document emission (Issue #4542 review hardening).
    seen: dict[str, dict[str, Any]] = {}
    for r in all_results:
        if chunks_per_page is None:
            key = r.get(
                "zone_qualified_path",
                f"{r.get('zone_id', '')}:{r.get('path', '')}:{r.get('chunk_index', 0)}",
            )
        else:
            page_key = r.get("zone_qualified_path") or (
                f"{r.get('zone_id', '')}:{r.get('path', '')}"
            )
            key = f"{page_key}:{r.get('chunk_index', 0)}"
        existing = seen.get(key)
        if existing is None or r.get("score", 0.0) > existing.get("score", 0.0):
            seen[key] = r

    return sorted(
        seen.values(),
        key=lambda x: x.get("score", 0.0),
        reverse=True,
    )[:limit]


def _strip_none_context(d: dict[str, Any]) -> dict[str, Any]:
    """Match the non-federated router's omit-when-None contract for
    ``context`` (Issue #3773, review Rounds 5-6) and ``tier_boost``
    (Issue #4544): every federated emission path must route through this so
    ``null`` never leaks onto the wire and the fusion strategies stay
    shape-consistent."""
    for key in ("context", "tier_boost"):
        if d.get(key) is None:
            d.pop(key, None)
    # Issue #4544: round surviving tier_boost to 4 places to match the
    # non-federated router's serialization (_serialize_search_result).
    # Remote dicts cross a trust boundary (Codex review R7): a malformed or
    # version-skewed peer sending a string/NaN/inf here must lose the
    # attribution field, not abort the whole federated fusion via
    # round() TypeError.
    tb = d.get("tier_boost")
    if tb is not None:
        keep = False
        value = 0.0
        if isinstance(tb, (int, float)) and not isinstance(tb, bool):
            try:
                # float() on an astronomically large JSON int raises
                # OverflowError (Codex review R8) — that too must drop the
                # field, never abort the fusion.
                value = float(tb)
            except OverflowError:
                keep = False
            else:
                # Enforce the documented attribution range (mirrors the
                # 0.1–10.0 API validation) — a value outside it cannot be a
                # legitimately stamped weight.
                keep = math.isfinite(value) and 0.1 <= value <= 10.0
        if keep:
            d["tier_boost"] = round(value, 4)
        else:
            d.pop("tier_boost", None)
    return d


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Convert a search result (dataclass or dict) to dict with zone metadata."""
    if isinstance(result, dict):
        return _strip_none_context(result)
    fields = getattr(result, "__dataclass_fields__", None)
    if fields is not None:
        d = {f: getattr(result, f) for f in fields}
        # Add computed property
        zone_qp = getattr(result, "zone_qualified_path", None)
        if zone_qp is not None:
            d["zone_qualified_path"] = zone_qp
        return _strip_none_context(d)
    return {"value": result}


async def filter_federated_results(
    results: list[Any],
    subject: tuple[str, str],
    rebac: Any,
) -> list[Any]:
    """Per-result zone-aware permission filter (Issue #3147).

    Groups results by zone_id, then uses rebac_check_batch per zone
    to check "viewer" permission on each result's file path. This is
    a NEW permission-enforcer API — it does NOT modify the existing
    single-zone filter at search.py.

    Used when intra-zone file-level ACLs are enabled (Phase 2+).
    In Phase 1 (zone-level auth only), this function is not called
    by the dispatcher — zone membership is sufficient.

    Args:
        results: Search results with zone_id set (dataclass or dict).
        subject: (subject_type, subject_id) for the requester.
        rebac: ReBACService instance with rebac_check_batch().

    Returns:
        Filtered list containing only results the subject can read.
    """
    if not results:
        return []

    # Group results by zone_id for batched permission checks
    by_zone: dict[str | None, list[tuple[int, Any]]] = {}
    for idx, r in enumerate(results):
        zone_id = r.zone_id if hasattr(r, "zone_id") else r.get("zone_id")
        by_zone.setdefault(zone_id, []).append((idx, r))

    allowed_indices: set[int] = set()

    for zone_id, zone_items in by_zone.items():
        # Build batch check: (subject, "viewer", ("file", path)) per result
        checks = []
        for _idx, r in zone_items:
            path = r.path if hasattr(r, "path") else r.get("path", "")
            checks.append((subject, "viewer", ("file", path)))

        try:
            batch_results = await rebac.rebac_check_batch(
                checks=checks,
                zone_id=zone_id,
            )
            for (idx, _r), allowed in zip(zone_items, batch_results, strict=True):
                if allowed:
                    allowed_indices.add(idx)
        except Exception:
            logger.warning(
                "[FEDERATED] ReBAC batch check failed for zone %s, "
                "allowing all results from this zone (fail-open for availability)",
                zone_id,
            )
            # Fail-open: if ReBAC is unavailable, allow results
            # (zone-level auth already passed in step 1)
            for idx, _r in zone_items:
                allowed_indices.add(idx)

    return [results[i] for i in sorted(allowed_indices)]
