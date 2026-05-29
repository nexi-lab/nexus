"""Tests for Issue #4269: search phase timings bound to structlog request_completed."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import structlog

from nexus.server.api.v2.routers.search import _BACKEND_LEG_TIMING_KEYS, _add_backend_leg_timings

# ---------------------------------------------------------------------------
# _BACKEND_LEG_TIMING_KEYS includes index_load_ms
# ---------------------------------------------------------------------------


def test_backend_leg_timing_keys_includes_index_load_ms() -> None:
    assert "index_load_ms" in _BACKEND_LEG_TIMING_KEYS


# ---------------------------------------------------------------------------
# _add_backend_leg_timings propagates index_load_ms
# ---------------------------------------------------------------------------


def test_add_backend_leg_timings_propagates_index_load_ms() -> None:
    breakdown: dict[str, float] = {"total_ms": 100.0}
    daemon_timing = {"backend_ms": 80.0, "keyword_ms": 50.0, "index_load_ms": 12.5}

    _add_backend_leg_timings(breakdown, daemon_timing)

    assert breakdown["index_load_ms"] == 12.5


def test_add_backend_leg_timings_skips_missing_index_load_ms() -> None:
    breakdown: dict[str, float] = {"total_ms": 100.0}
    daemon_timing = {"backend_ms": 80.0, "keyword_ms": 50.0}

    _add_backend_leg_timings(breakdown, daemon_timing)

    assert "index_load_ms" not in breakdown


# ---------------------------------------------------------------------------
# Search phase timings are bound to structlog context in _handle_standard_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_phase_timings_bound_to_structlog_context() -> None:
    """_handle_single_zone_search binds search_backend_ms etc. to structlog context."""
    import time

    from nexus.bricks.search.daemon import SearchDaemon, SearchResult, SearchResultList

    # --- Build a minimal daemon stub ---
    timing_snapshot = {
        "backend_ms": 42.0,
        "embed_ms": 5.0,
        "keyword_ms": 20.0,
        "page_keyword_ms": 8.0,
        "vector_ms": 15.0,
        "fusion_ms": 2.0,
        "rerank_ms": 0.0,
        "index_load_ms": 7.5,
    }
    result = SearchResult(
        path="/doc.md",
        chunk_text="hello",
        score=1.0,
        chunk_index=0,
        search_type="keyword",
    )
    results = SearchResultList([result], search_timing=timing_snapshot)

    daemon = MagicMock(spec=SearchDaemon)
    daemon.search = MagicMock(return_value=results)

    # Make search an async mock that returns the results
    async def _async_search(*args: Any, **kwargs: Any) -> SearchResultList:
        return results

    daemon.search = _async_search
    daemon.last_search_timing = timing_snapshot
    daemon.config = MagicMock()
    daemon.config.txtai_graph = False

    # --- Build a minimal request stub ---
    app_state = MagicMock()
    app_state.permission_enforcer = None
    app_state.search_daemon = daemon

    request = MagicMock()
    request.app.state = app_state

    auth_result: dict[str, Any] = {
        "authenticated": True,
        "subject_id": "user:alice",
        "user_id": "alice",
        "zone_id": "root",
        "is_admin": True,
        "allow_admin_bypass": True,
    }

    # Clear structlog context before the call
    structlog.contextvars.clear_contextvars()

    from nexus.server.api.v2.routers.search import _handle_single_zone_search

    await _handle_single_zone_search(
        request=request,
        q="revenue",
        search_type="keyword",
        limit=10,
        path_filter=None,
        alpha=0.5,
        fusion_method="rrf",
        graph_mode="none",
        auth_result=auth_result,
        search_daemon=daemon,
        async_session_factory=MagicMock(),
        record_store=MagicMock(),
        zone_id="root",
        start_time=time.perf_counter(),
    )

    ctx = structlog.contextvars.get_contextvars()
    assert "search_backend_ms" in ctx
    assert "search_keyword_ms" in ctx
    assert ctx["search_backend_ms"] >= 0.0


@pytest.mark.asyncio
async def test_federated_search_binds_total_ms_to_structlog(monkeypatch: Any) -> None:
    """Cross-zone (federated) searches must still bind search_total_ms so their
    request_completed logs aren't an observability blind spot (Codex R2 #2)."""
    from types import SimpleNamespace

    import nexus.server.api.v2.routers.search as search_mod

    # Stub the dispatcher so we don't need a live ReBAC fan-out.
    fed_response = SimpleNamespace(
        results=[],
        latency_ms=12.3,
        zones_searched=["root", "z1"],
        zones_failed=[],
        zones_skipped=[],
        cached=False,
        # Per-leg timing aggregated across local zones (Codex R6).
        search_timing={"index_load_ms": 4.0, "keyword_ms": 6.0},
    )

    class _StubDispatcher:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def search(self, *a: Any, **k: Any) -> Any:
            return fed_response

    # _handle_federated_search imports these names locally from federated_search.
    import nexus.bricks.search.federated_search as fed_mod

    monkeypatch.setattr(fed_mod, "FederatedSearchDispatcher", _StubDispatcher)
    monkeypatch.setattr(fed_mod, "is_all_peers_failed", lambda _r: False)

    app_state = MagicMock()
    app_state.rebac_service = MagicMock()
    app_state.deployment_profile = "shared"  # not sandbox → skip BM25S branch
    request = MagicMock()
    request.app.state = app_state

    auth_result: dict[str, Any] = {
        "authenticated": True,
        "user_id": "alice",
        "subject_id": "user:alice",
        "subject_type": "user",
    }

    structlog.contextvars.clear_contextvars()

    resp = await search_mod._handle_federated_search(
        q="revenue",
        search_type="hybrid",
        limit=10,
        path_filter=None,
        alpha=0.5,
        fusion_method="rrf",
        auth_result=auth_result,
        search_daemon=MagicMock(),
        request=request,
    )

    ctx = structlog.contextvars.get_contextvars()
    assert "search_total_ms" in ctx
    assert resp["federated"] is True
    assert resp["latency_breakdown"]["total_ms"] == 12.3
    # Per-leg timings aggregated across local zones surface in the breakdown and
    # the structlog context (Codex R6) — the federated path is no longer a blind
    # spot for the index-load phase split.
    assert resp["latency_breakdown"]["index_load_ms"] == 4.0
    assert ctx.get("search_index_load_ms") == 4.0
    assert ctx.get("search_keyword_ms") == 6.0


@pytest.mark.asyncio
async def test_federated_sandbox_fallback_latency_included_in_total(monkeypatch: Any) -> None:
    """The SANDBOX all-peers-failed BM25S fallback runs after the dispatcher
    returns; its time must be folded into total_ms + a fallback_ms leg, not
    omitted (Codex R3)."""
    import asyncio
    from types import SimpleNamespace

    import nexus.bricks.search.federated_search as fed_mod
    import nexus.server.api.v2.routers.search as search_mod

    fed_response = SimpleNamespace(
        results=[],
        latency_ms=5.0,  # dispatcher-only latency
        zones_searched=["root"],
        zones_failed=[],
        zones_skipped=[],
        cached=False,
        search_timing={},
    )

    class _StubDispatcher:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def search(self, *a: Any, **k: Any) -> Any:
            return fed_response

    monkeypatch.setattr(fed_mod, "FederatedSearchDispatcher", _StubDispatcher)
    monkeypatch.setattr(fed_mod, "is_all_peers_failed", lambda _r: True)  # all peers down

    # SANDBOX search_service whose BM25S fallback takes a measurable ~20ms.
    class _SearchService:
        async def semantic_search(self, **kwargs: Any) -> list[Any]:
            await asyncio.sleep(0.02)
            return []

    nexus_fs = MagicMock()
    nexus_fs.service.return_value = _SearchService()

    app_state = MagicMock()
    app_state.rebac_service = MagicMock()
    app_state.deployment_profile = "sandbox"
    app_state.nexus_fs = nexus_fs
    request = MagicMock()
    request.app.state = app_state

    auth_result: dict[str, Any] = {
        "authenticated": True,
        "user_id": "alice",
        "subject_id": "user:alice",
        "subject_type": "user",
    }

    structlog.contextvars.clear_contextvars()

    resp = await search_mod._handle_federated_search(
        q="revenue",
        search_type="semantic",
        limit=10,
        path_filter=None,
        alpha=0.5,
        fusion_method="rrf",
        auth_result=auth_result,
        search_daemon=MagicMock(),
        request=request,
    )

    lb = resp["latency_breakdown"]
    # total_ms = dispatcher 5.0 + ~20ms fallback → clearly > 20, and a distinct
    # fallback_ms leg is present. Without the fix total_ms would stay 5.0.
    assert lb["total_ms"] > 20.0
    assert lb.get("fallback_ms", 0.0) > 15.0
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("search_total_ms", 0.0) > 20.0
    assert ctx.get("search_fallback_ms", 0.0) > 15.0


def test_federated_cache_hit_does_not_replay_backend_timings() -> None:
    """A federated cache hit executes no backend work, so its search_timing must
    be empty — not the original cold query's per-leg timings replayed as if the
    work happened again (Codex R8)."""
    import time as _time

    from nexus.bricks.search.federated_search import (
        FederatedSearchConfig,
        FederatedSearchDispatcher,
        FederatedSearchResponse,
    )

    dispatcher: Any = FederatedSearchDispatcher.__new__(FederatedSearchDispatcher)
    dispatcher._config = FederatedSearchConfig(result_cache_enabled=True)
    original = FederatedSearchResponse(
        results=[],
        zones_searched=["root"],
        zones_failed=[],
        latency_ms=999.0,
        search_timing={"index_load_ms": 50.0, "keyword_ms": 80.0},
    )
    dispatcher._result_cache = {"k": (original, _time.monotonic() + 999.0)}

    hit = dispatcher._get_cached_result("k", start=_time.perf_counter())

    assert hit is not None
    assert hit.cached is True
    assert hit.search_timing == {}  # no stale backend legs replayed
    # latency reflects the fresh cache lookup, NOT the original miss's 999ms
    # wall time (Codex R9).
    assert hit.latency_ms < 100.0
