"""Cross-zone search results, failures, and retrieval diagnostics."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ZoneFailure:
    """Metadata about a zone that failed during federated search."""

    zone_id: str
    error: str


@dataclass
class FederatedSearchResponse:
    """Response envelope from a federated search including zone metadata."""

    results: list[dict[str, Any]]
    zones_searched: list[str]
    zones_failed: list[ZoneFailure]
    zones_skipped: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    # Issue #4269 (Codex R6): per-leg backend phase timings (index_load_ms,
    # keyword_ms, vector_ms, fusion_ms, ...) SUMMED across the LOCAL zones that
    # served this query, so a cold federated search exposes the same index-load
    # phase split as a single-zone one.  Remote zones (gRPC peers) do not return
    # per-leg timing through the delegation path, so they don't contribute here.
    search_timing: dict[str, float] = field(default_factory=dict)
    # #3778 marker (#4541 review round 9): True when any zone served this
    # query with a degraded dense leg — captured BEFORE ReBAC filtering so
    # the signal survives empty and fully-filtered responses.
    semantic_degraded: bool = False
