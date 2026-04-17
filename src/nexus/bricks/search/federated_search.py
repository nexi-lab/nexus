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
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nexus.bricks.search.fusion import rrf_multi_fusion

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
    ) -> list[Any]:
        """Search a single zone with capability-aware routing."""
        effective_type, alpha_override = self._get_effective_search_type(zone_id, search_type)
        effective_alpha = alpha_override if alpha_override is not None else alpha

        # Phase 2: Check if this zone has a remote transport in the registry.
        # If so, search via gRPC with a SearchDelegation credential.
        if self._registry is not None and self._registry.is_remote(zone_id):
            return await self._search_remote_zone(
                zone_id=zone_id,
                query=query,
                search_type=effective_type,
                limit=limit,
                path_filter=path_filter,
                alpha=effective_alpha,
                fusion_method=fusion_method,
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
            fusion_method=fusion_method,
            zone_id=zone_id,
        )

        # Tag results with zone provenance
        for r in results:
            r.zone_id = zone_id

        return list(results)

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

        # Convert remote response to result dicts with zone tagging
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
    ) -> str:
        """Phase 3: Create a cache key for result caching."""
        raw = f"{subject[0]}:{subject[1]}|{query}|{search_type}|{limit}|{path_filter}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _get_cached_result(self, cache_key: str) -> FederatedSearchResponse | None:
        """Phase 3: Check result cache."""
        if not self._config.result_cache_enabled:
            return None
        cached = self._result_cache.get(cache_key)
        if cached is None:
            return None
        response, expiry = cached
        if time.monotonic() > expiry:
            del self._result_cache[cache_key]
            return None
        return FederatedSearchResponse(
            results=response.results,
            zones_searched=response.zones_searched,
            zones_failed=response.zones_failed,
            zones_skipped=response.zones_skipped,
            latency_ms=response.latency_ms,
            cached=True,
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

        Returns:
            FederatedSearchResponse with fused results and zone metadata.
        """
        start = time.perf_counter()

        # Phase 3: Check result cache
        cache_key = self._make_cache_key(query, subject, search_type, limit, path_filter)
        cached = self._get_cached_result(cache_key)
        if cached is not None:
            return cached

        # 1. Zone discovery (decision 2A: zone-level auth is sufficient)
        accessible_zones = await self._get_accessible_zones(subject)

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

        # Single zone: skip fusion overhead
        if len(searchable_zones) == 1:
            zone_id = searchable_zones[0]
            try:
                results = await asyncio.wait_for(
                    self._search_zone(
                        zone_id,
                        query,
                        search_type,
                        limit,
                        path_filter,
                        alpha,
                        fusion_method,
                        subject=subject,
                    ),
                    timeout=self._config.zone_timeout_seconds,
                )
                # Per-file ReBAC post-filter (Phase 2+)
                if self._enable_per_file_rebac:
                    results = await filter_federated_results(
                        results,
                        subject=subject,
                        rebac=self._rebac,
                    )
                result_dicts = [_result_to_dict(r) for r in results[:limit]]
                resp = FederatedSearchResponse(
                    results=result_dicts,
                    zones_searched=[zone_id],
                    zones_failed=[],
                    zones_skipped=zones_skipped,
                    latency_ms=(time.perf_counter() - start) * 1000,
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

        # 2. Multi-zone fan-out with concurrency bound (decision 16A)
        per_zone_limit = limit * self._config.over_fetch_factor
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
                            per_zone_limit,
                            path_filter,
                            alpha,
                            fusion_method,
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

        for zone_id, outcome in zone_outcomes:
            if isinstance(outcome, BaseException):
                logger.warning("[FEDERATED] Zone %s failed: %s", zone_id, outcome)
                zones_failed.append(ZoneFailure(zone_id=zone_id, error=str(outcome)))
            else:
                zones_searched.append(zone_id)
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
            _zone_id, results = zone_result_lists[0]
            fused_results = [_result_to_dict(r) for r in results[:limit]]
        elif self._config.fusion_strategy == FederatedFusionStrategy.RRF:
            # RRF: for heterogeneous zones with different scoring functions
            fused_results = rrf_multi_fusion(
                result_lists=zone_result_lists,
                k=60,
                limit=limit,
                id_key="zone_qualified_path",
            )
            # Issue #3773 (Round-6 review): rrf_multi_fusion emits dicts built
            # from __dataclass_fields__ verbatim, so ``context: None`` leaks
            # into the wire. Normalize here so every federated code path
            # matches the non-federated router's omit-when-None contract.
            fused_results = [_strip_none_context(d) for d in fused_results]
        else:
            # Raw score merge-sort: for homogeneous zones (default)
            fused_results = _merge_by_raw_score(zone_result_lists, limit)

        resp = FederatedSearchResponse(
            results=fused_results,
            zones_searched=zones_searched,
            zones_failed=zones_failed,
            zones_skipped=zones_skipped,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        self._cache_result(cache_key, resp)
        return resp

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
        for r in results:
            all_results.append(_result_to_dict(r))

    # Dedup by zone_qualified_path, keeping highest score
    seen: dict[str, dict[str, Any]] = {}
    for r in all_results:
        key = r.get(
            "zone_qualified_path",
            f"{r.get('zone_id', '')}:{r.get('path', '')}:{r.get('chunk_index', 0)}",
        )
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
    ``context``. Issue #3773 review (Rounds 5-6): every federated emission
    path must route through this to avoid ``context: null`` leaking onto
    the wire and creating a shape-drift between fusion strategies."""
    if d.get("context") is None:
        d.pop("context", None)
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
