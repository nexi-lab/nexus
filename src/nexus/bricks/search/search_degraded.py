"""Response DTOs + helpers for the "search is degraded" signal path.

The SANDBOX profile wraps the search plugin call in a guarded degrade
path: when the plugin can't reach any peer (or the response envelope
otherwise reports zero reachable peers), the server falls back to
local BM25S and stamps the response with ``semantic_degraded=True`` so
downstream consumers know they got a lossy answer (Issue #3778).

These types previously lived in ``nexus.bricks.search.federated_search``,
which was itself misnamed for the Rust-plugin era.  Isolating the
degrade-guard surface here keeps the semantics explicit: this module
carries the "search is degraded, degrade-guard fired" DTOs and the
single helper the guard uses to inspect a response envelope.
"""

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
    cached: bool = False
    # Issue #4269 (Codex R6): per-leg backend phase timings (index_load_ms,
    # keyword_ms, vector_ms, fusion_ms, ...) SUMMED across the LOCAL zones that
    # served this query, so a cold federated search exposes the same index-load
    # phase split as a single-zone one.  Remote zones (gRPC peers) do not return
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
    return bool(
        not response.results and len(response.zones_failed) >= len(response.zones_searched)
    )
