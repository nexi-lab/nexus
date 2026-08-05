"""Batch search plumbing: pre-computed query_vector threads through the daemon.

SearchRequest.query_vector allows the batch endpoint to embed all unique queries
once and hand each inner search its vector, bypassing redundant per-search embeds.
Tests pin the thread and the embed-skip logic.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest


def _make_daemon() -> Any:
    """Bare SearchDaemon with fake (non-PG) fts + vector backends.

    Fixture corpus is built so keyword and dense orderings disagree:
      keyword (BM25):  /a.md (10.0) > /b.md (9.0) > /c.md (8.0)
      dense  (cosine): /d.md (0.99) > /c.md (0.9) > /a.md (0.5)
    so any change of fusion method / alpha / k is visible in the ordering.
    """
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    def _kw(path: str, score: float) -> SearchResult:
        return SearchResult(
            path=path, chunk_text=path, score=score, chunk_index=0, search_type="keyword"
        )

    def _dense(path: str, score: float) -> SearchResult:
        return SearchResult(
            path=path, chunk_text=path, score=score, chunk_index=0, search_type="semantic"
        )

    class FakeFtsBackend:
        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            return [_kw("/a.md", 10.0), _kw("/b.md", 9.0), _kw("/c.md", 8.0)]

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return [_dense("/d.md", 0.99), _dense("/c.md", 0.9), _dense("/a.md", 0.5)]

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    # Title arm (#4545) is on by default; bare __new__ skips __init__, so
    # give locate() an empty skeleton index (no hits → fusion unchanged).
    daemon._skeleton_docs = {}
    daemon._skeleton_title_index = {}
    daemon._skeleton_path_index = {}
    daemon._skeleton_exact_title_index = {}
    daemon._embed_query = MethodType(_embed_query, daemon)
    # Disable #4542 final-list page pooling: these tests assert chunk-grain
    # fusion ordering, which pooling would re-group.
    daemon.config = DaemonConfig(page_aggregation=False)
    return daemon


class _CountingEmbed:
    """Records embed calls made through daemon._embed_query."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, daemon: Any, query: str) -> list[float]:
        self.calls.append(query)
        return [0.1, 0.2]


@pytest.mark.asyncio
async def test_query_vector_override_skips_embed() -> None:
    daemon = _make_daemon()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    results = await daemon._search_via_backends(
        "hello",
        search_type="hybrid",
        limit=5,
        path_filter=None,
        zone_id="root",
        query_vector=[0.3, 0.4],
    )

    assert counter.calls == []
    assert results  # fused hybrid results still produced


@pytest.mark.asyncio
async def test_no_query_vector_still_embeds() -> None:
    daemon = _make_daemon()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    await daemon._search_via_backends(
        "hello",
        search_type="hybrid",
        limit=5,
        path_filter=None,
        zone_id="root",
    )

    assert counter.calls == ["hello"]


def _make_batch_daemon(concurrency: int = 8) -> Any:
    """Bare daemon for _batch_search_on_current_loop: search() is stubbed
    per-test; only batch orchestration fields are real."""
    from unittest.mock import AsyncMock

    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon._embedding_client = None
    daemon.config = DaemonConfig(batch_search_concurrency=concurrency)
    # Path-context attach is exercised elsewhere; resolve to None (= skip).
    daemon._resolve_path_context_cache = AsyncMock(return_value=None)
    return daemon


class _FakeEmbeddingClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def embed_batch(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embed down")
        return [[0.1, 0.2] for _ in texts]


@pytest.mark.asyncio
async def test_batch_pre_embeds_unique_texts_once() -> None:
    daemon = _make_batch_daemon()
    client = _FakeEmbeddingClient()
    daemon._embedding_client = client
    seen_vectors: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        seen_vectors.append(request.query_vector)
        return []

    daemon.search = MethodType(fake_search, daemon)
    specs = [{"query": "same text", "search_type": "hybrid", "limit": 5} for _ in range(24)]

    out = await daemon._batch_search_on_current_loop(specs, zone_id="root")

    assert client.calls == [["same text"]]  # ONE embed_batch call, unique texts only
    assert seen_vectors == [[0.1, 0.2]] * 24
    assert out == [[] for _ in range(24)]


@pytest.mark.asyncio
async def test_batch_keyword_only_never_embeds() -> None:
    daemon = _make_batch_daemon()
    client = _FakeEmbeddingClient()
    daemon._embedding_client = client

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [{"query": "kw", "search_type": "keyword"}], zone_id="root"
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_batch_embed_failure_degrades_hybrid_without_per_query_retries() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    daemon._embedding_client = _FakeEmbeddingClient(fail=True)
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [
            {"query": "a", "search_type": "hybrid"},
            {"query": "a", "search_type": "semantic"},
        ],
        zone_id="root",
    )

    # Hybrid still runs, but flagged so the dense leg skips its own embed
    # retry (no N-way retry storm against a degraded provider).
    assert out[0] == []
    assert len(captured) == 1
    assert captured[0].search_type == "hybrid"
    assert captured[0].embedding_unavailable is True
    assert captured[0].query_vector is None
    # Semantic-only cannot be served without an embedding: typed failure,
    # and search() is never invoked for it.
    assert isinstance(out[1], BatchQueryFailure)
    assert out[1].error == "Query embedding unavailable"


@pytest.mark.asyncio
async def test_batch_sets_propagate_failures() -> None:
    daemon = _make_batch_daemon()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop([{"query": "q"}], zone_id="root")

    assert captured[0].propagate_failures is True
    assert captured[0].embedding_unavailable is False


@pytest.mark.asyncio
async def test_batch_timeout_becomes_sanitized_failure() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        raise TimeoutError

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop([{"query": "slow"}], zone_id="root")

    assert isinstance(out[0], BatchQueryFailure)
    assert out[0].error == "Search timed out"


@pytest.mark.asyncio
async def test_batch_failure_isolated_per_query() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        if request.query == "poison":
            raise RuntimeError("boom")
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [{"query": "ok1"}, {"query": "poison"}, {"query": "ok2"}], zone_id="root"
    )

    assert out[0] == [] and out[2] == []
    assert isinstance(out[1], BatchQueryFailure)
    # Raw exception text must never cross the API boundary (it can carry
    # SQL/hosts/provider internals) — the wire message is the stable
    # sanitized constant; full detail goes to the server log.
    assert "boom" not in out[1].error
    assert out[1].error == "Search query failed"


@pytest.mark.asyncio
async def test_batch_respects_concurrency_bound() -> None:
    import asyncio

    daemon = _make_batch_daemon(concurrency=2)
    peak = 0
    active = 0

    async def fake_search(self: Any, request: Any) -> list[Any]:
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [{"query": f"q{i}"} for i in range(10)], zone_id="root"
    )

    assert peak <= 2


@pytest.mark.asyncio
async def test_batch_forwards_tuning_params() -> None:
    daemon = _make_batch_daemon()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [
            {
                "query": "tuned",
                "search_type": "hybrid",
                "limit": 7,
                "path_filter": "/ws/",
                "alpha": 0.3,
                "fusion_method": "weighted",
                "rrf_k": 90,
                "expand": "macro",
                "recency": "on",
                "recency_weight": 1.5,
                "recency_half_life_days": 30.0,
            }
        ],
        zone_id="root",
    )

    req = captured[0]
    assert (req.alpha, req.fusion_method, req.rrf_k) == (0.3, "weighted", 90)
    assert (req.expand, req.recency, req.recency_weight) == ("macro", "on", 1.5)
    assert req.recency_half_life_days == 30.0
    assert (req.limit, req.path_filter, req.zone_id) == (7, "/ws/", "root")


@pytest.mark.asyncio
async def test_batch_non_dict_spec_becomes_failure() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(["not a dict"], zone_id="root")

    assert isinstance(out[0], BatchQueryFailure)


@pytest.mark.asyncio
async def test_semantic_backend_leg_propagates_missing_embedding() -> None:
    from nexus.bricks.search.daemon import EmbeddingUnavailableError

    daemon = _make_daemon()

    async def _no_embed(self: Any, query: str) -> None:
        return None

    daemon._embed_query = MethodType(_no_embed, daemon)

    # Interactive contract: degrade to empty.
    out = await daemon._search_via_backends(
        "q", search_type="semantic", limit=5, path_filter=None, zone_id="root"
    )
    assert out == []

    # Batch contract: a semantic search served nothing because the embedding
    # is missing — that is a failure, not a healthy empty.
    with pytest.raises(EmbeddingUnavailableError):
        await daemon._search_via_backends(
            "q",
            search_type="semantic",
            limit=5,
            path_filter=None,
            zone_id="root",
            propagate_failures=True,
        )


@pytest.mark.asyncio
async def test_backend_legs_skip_embed_when_unavailable_flagged() -> None:
    daemon = _make_daemon()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    # Hybrid with embedding_unavailable: no embed attempt, keyword-only result.
    out = await daemon._search_via_backends(
        "hello",
        search_type="hybrid",
        limit=5,
        path_filter=None,
        zone_id="root",
        embedding_unavailable=True,
    )
    assert counter.calls == []
    assert out  # keyword fallback still serves results


@pytest.mark.asyncio
async def test_legacy_semantic_search_uses_query_vector_and_propagates() -> None:
    from nexus.bricks.search.daemon import EmbeddingUnavailableError, SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    seen_vectors: list[Any] = []

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            seen_vectors.append(qvec)
            return []

    daemon._vector_backend = FakeVectorBackend()
    counter = _CountingEmbed()
    daemon._embed_query = MethodType(counter.__call__, daemon)

    # Pre-computed vector: the legacy fallback must not re-embed (the batch
    # empty-result fallthrough runs this path on every miss).
    out = await daemon._semantic_search("q", 5, None, zone_id="root", query_vector=[0.9, 0.8])
    assert out == []
    assert counter.calls == []
    assert seen_vectors == [[0.9, 0.8]]

    # No vector + no embedding available + batch mode -> typed failure.
    async def _no_embed(self: Any, query: str) -> None:
        return None

    daemon._embed_query = MethodType(_no_embed, daemon)
    with pytest.raises(EmbeddingUnavailableError):
        await daemon._semantic_search("q", 5, None, zone_id="root", propagate_failures=True)

    # embedding_unavailable (hybrid degrade support): no embed attempt, empty.
    daemon._embed_query = MethodType(counter.__call__, daemon)
    out = await daemon._semantic_search("q", 5, None, zone_id="root", embedding_unavailable=True)
    assert out == []
    assert counter.calls == []


@pytest.mark.asyncio
async def test_hung_inner_search_times_out_without_blocking_siblings() -> None:
    import asyncio

    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(batch_search_concurrency=8, query_timeout_seconds=0.05)

    async def fake_search(self: Any, request: Any) -> list[Any]:
        if request.query == "hung":
            await asyncio.sleep(30)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await asyncio.wait_for(
        daemon._batch_search_on_current_loop(
            [{"query": "hung"}, {"query": "healthy"}], zone_id="root"
        ),
        timeout=5,
    )

    # The hung query is bounded and converted; the healthy sibling's result
    # is not withheld behind it.
    assert isinstance(out[0], BatchQueryFailure)
    assert out[0].error == "Search timed out"
    assert out[1] == []


@pytest.mark.asyncio
async def test_hung_pre_embed_degrades_instead_of_blocking_batch() -> None:
    import asyncio

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(batch_search_concurrency=8, query_timeout_seconds=0.05)

    class HangingEmbeddingClient:
        async def embed_batch(self, texts: Any) -> list[list[float]]:
            await asyncio.sleep(30)
            return []

    daemon._embedding_client = HangingEmbeddingClient()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await asyncio.wait_for(
        daemon._batch_search_on_current_loop(
            [{"query": "a", "search_type": "hybrid"}], zone_id="root"
        ),
        timeout=5,
    )

    # Shared pre-embed timeout takes the same degradation path as any other
    # pre-embed failure: hybrid runs keyword-only, no per-query embed retry.
    assert out == [[]]
    assert captured[0].embedding_unavailable is True


@pytest.mark.asyncio
async def test_batch_deadline_bounds_queued_hung_queries() -> None:
    import asyncio
    import time

    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    # Per-query timeout generous; the ABSOLUTE batch deadline is the bound
    # under test: with concurrency 1, three hung queries would otherwise
    # consume three consecutive per-query windows.
    daemon.config = type(daemon.config)(
        batch_search_concurrency=1,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=0.1,
    )

    async def fake_search(self: Any, request: Any) -> list[Any]:
        if request.query.startswith("hung"):
            await asyncio.sleep(60)
        return []

    daemon.search = MethodType(fake_search, daemon)

    started = time.monotonic()
    out = await daemon._batch_search_on_current_loop(
        [{"query": "ok"}, {"query": "hung-1"}, {"query": "hung-2"}], zone_id="root"
    )
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"batch deadline did not bound the request ({elapsed:.1f}s)"
    # Concurrency 1 runs the healthy query first; the queued hung queries are
    # cancelled at the deadline and settled positionally as timeouts.
    assert out[0] == []
    assert isinstance(out[1], BatchQueryFailure)
    assert out[1].error == "Search timed out"
    assert isinstance(out[2], BatchQueryFailure)
    assert out[2].error == "Search timed out"


@pytest.mark.asyncio
async def test_hybrid_dense_backend_failure_fails_batch_query() -> None:
    daemon = _make_daemon()

    class RaisingVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            raise RuntimeError("vector backend down")

    daemon._vector_backend = RaisingVectorBackend()

    # Interactive contract: fail-soft to keyword-only.
    out = await daemon._search_via_backends(
        "q", search_type="hybrid", limit=5, path_filter=None, zone_id="root"
    )
    assert out  # keyword hits still served

    # Batch contract: a dense INFRASTRUCTURE failure is a per-query failure,
    # not a silently keyword-only "success".
    with pytest.raises(RuntimeError, match="vector backend down"):
        await daemon._search_via_backends(
            "q",
            search_type="hybrid",
            limit=5,
            path_filter=None,
            zone_id="root",
            propagate_failures=True,
        )


@pytest.mark.asyncio
async def test_legacy_hybrid_missing_embedding_still_degrades_under_batch() -> None:
    from nexus.bricks.search.daemon import SearchDaemon, SearchResult

    daemon: Any = SearchDaemon.__new__(SearchDaemon)

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [
            SearchResult(
                path="/kw.md", chunk_text="kw", score=1.0, chunk_index=0, search_type="keyword"
            )
        ]

    daemon._keyword_search = MethodType(_keyword_search, daemon)

    class NoEmbedVectorBackend:
        async def semantic_search(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise AssertionError("dense leg must not run without an embedding")

    daemon._vector_backend = NoEmbedVectorBackend()

    async def _no_embed(self: Any, query: str) -> None:
        return None

    daemon._embed_query = MethodType(_no_embed, daemon)

    # Missing embedding is legitimate hybrid degradation even in batch mode:
    # the EmbeddingUnavailableError carve-out keeps the keyword-only result.
    out = await daemon._hybrid_search(
        "q", 4, None, 0.5, "rrf", zone_id="root", propagate_failures=True
    )
    assert [r.path for r in out] == ["/kw.md"]


@pytest.mark.asyncio
async def test_batch_deadline_covers_pre_embed_phase() -> None:
    import asyncio
    import time

    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=8,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=0.2,
    )

    class SlowEmbeddingClient:
        async def embed_batch(self, texts: Any) -> list[list[float]]:
            await asyncio.sleep(0.3)
            return [[0.1, 0.2] for _ in texts]

    daemon._embedding_client = SlowEmbeddingClient()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        await asyncio.sleep(5)
        return []

    daemon.search = MethodType(fake_search, daemon)

    started = time.monotonic()
    out = await daemon._batch_search_on_current_loop([{"query": "a"}], zone_id="root")
    elapsed = time.monotonic() - started

    # The ONE deadline spans pre-embed + fan-out: a pre-embed that eats the
    # whole budget must not hand the fan-out a fresh window.
    assert elapsed < 2.0, f"pre-embed escaped the batch deadline ({elapsed:.2f}s)"
    assert isinstance(out[0], BatchQueryFailure)


@pytest.mark.asyncio
async def test_task_finishing_during_cancel_still_settles_as_timeout() -> None:
    import asyncio
    import contextlib

    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=8,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=0.05,
    )

    async def fake_search(self: Any, request: Any) -> list[Any]:
        # Suppress the cancellation and complete anyway — the task was still
        # unfinished AT THE DEADLINE, so it must settle as a timeout.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.5)
        return [{"path": "/late.md"}]

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop([{"query": "late"}], zone_id="root")

    assert isinstance(out[0], BatchQueryFailure)
    assert out[0].error == "Search timed out"


@pytest.mark.asyncio
async def test_missing_vector_backend_is_distinct_from_missing_embedding() -> None:
    from nexus.bricks.search.daemon import (
        SearchDaemon,
        SearchResult,
        VectorBackendUnavailableError,
    )

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._vector_backend = None

    async def _keyword_search(self: Any, *args: Any, **kwargs: Any) -> list[Any]:
        return [
            SearchResult(
                path="/kw.md", chunk_text="kw", score=1.0, chunk_index=0, search_type="keyword"
            )
        ]

    daemon._keyword_search = MethodType(_keyword_search, daemon)

    # Interactive contract: absent dense backend degrades to empty/keyword.
    assert await daemon._semantic_search("q", 5, None, zone_id="root") == []

    # Batch contract: an ABSENT backend is infrastructure, not a missing
    # embedding — even with a valid precomputed vector in hand.
    with pytest.raises(VectorBackendUnavailableError):
        await daemon._semantic_search(
            "q", 5, None, zone_id="root", query_vector=[0.1], propagate_failures=True
        )

    # ...and the hybrid gather helper must NOT suppress it (that carve-out is
    # only for a genuinely missing embedding), so incomplete dense coverage
    # cannot masquerade as a keyword-only success.
    with pytest.raises(VectorBackendUnavailableError):
        await daemon._hybrid_search(
            "q", 4, None, 0.5, "rrf", zone_id="root", propagate_failures=True
        )


@pytest.mark.asyncio
async def test_single_bad_embedding_input_does_not_poison_siblings() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    class PoisonRejectingClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed_batch(self, texts: Any) -> list[list[float]]:
            chunk = list(texts)
            self.calls.append(chunk)
            if any(t == "poison" for t in chunk):
                raise ValueError("input rejected: poison")
            return [[0.1, 0.2] for _ in chunk]

    client = PoisonRejectingClient()
    daemon._embedding_client = client
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [
            {"query": "alpha", "search_type": "semantic"},
            {"query": "poison", "search_type": "semantic"},
            {"query": "beta", "search_type": "semantic"},
        ],
        zone_id="root",
    )

    # Only the offending text fails; healthy siblings still get vectors and
    # run their searches.
    assert isinstance(out[1], BatchQueryFailure)
    assert out[1].error == "Query embedding unavailable"
    assert out[0] == [] and out[2] == []
    ran = {r.query: r for r in captured}
    assert set(ran) == {"alpha", "beta"}
    assert all(r.query_vector == [0.1, 0.2] for r in ran.values())
    # Isolation is bounded (bisection), not one probe per text.
    assert len(client.calls) <= 6


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_does_not_strand() -> None:
    import asyncio
    import contextlib

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=8,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=30.0,
    )

    async def fake_search(self: Any, request: Any) -> list[Any]:
        # Cancellation-resistant backend.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)
        await asyncio.sleep(30)
        return []

    daemon.search = MethodType(fake_search, daemon)

    batch = asyncio.ensure_future(
        daemon._batch_search_on_current_loop([{"query": "a"}, {"query": "b"}], zone_id="root")
    )
    await asyncio.sleep(0.05)
    batch.cancel()

    # Caller cancellation must propagate promptly (bounded cleanup), never be
    # converted into per-query failures or left pending forever.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(batch, timeout=10)


@pytest.mark.asyncio
async def test_provider_outage_is_bounded_and_degrades_hybrid() -> None:
    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=8,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=30.0,
    )

    class DeadEmbeddingClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed_batch(self, texts: Any) -> list[list[float]]:
            self.calls.append(list(texts))
            raise RuntimeError("provider down")

    client = DeadEmbeddingClient()
    daemon._embedding_client = client
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    specs = [{"query": f"q{i}", "search_type": "hybrid"} for i in range(64)]
    out = await daemon._batch_search_on_current_loop(specs, zone_id="root")

    # A total outage is classified after the first split, not by descending
    # through every text — and hybrid queries still RUN (keyword-degraded)
    # rather than being timed out by an exhausted deadline.
    # Bounded by the no-success outage signal (initial probe + first split +
    # a couple more), never a descent through all 64 texts.
    assert len(client.calls) <= 6, client.calls
    assert len(captured) == 64
    assert all(r.embedding_unavailable is True for r in captured)
    assert out == [[] for _ in range(64)]


@pytest.mark.asyncio
async def test_abandoned_batch_tasks_keep_their_permits(monkeypatch) -> None:
    import asyncio

    from nexus.bricks.search import daemon as daemon_module

    # Keep the drain short so the test does not sit on the production grace
    # window; the behavior under test is what happens AFTER the drain gives up.
    monkeypatch.setattr(daemon_module, "_BATCH_CANCEL_DRAIN_SECONDS", 0.05)

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=1,
        query_timeout_seconds=30.0,
        batch_search_timeout_seconds=0.05,
    )
    release = asyncio.Event()
    started = 0

    async def fake_search(self: Any, request: Any) -> list[Any]:
        nonlocal started
        started += 1
        # Cancellation-resistant backend: swallows the cancel and clears the
        # request (uncancel) so it genuinely keeps running, the way a driver
        # that shields its cleanup would.
        inner = asyncio.ensure_future(release.wait())
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop([{"query": "a"}], zone_id="root")

    # The abandoned search is tracked and still holds the daemon-scoped
    # permit, so a follow-up batch cannot start fresh work behind its back.
    assert daemon._abandoned_batch_tasks
    assert daemon._batch_semaphore().locked()
    started_before = started
    await daemon._batch_search_on_current_loop([{"query": "b"}], zone_id="root")
    assert started == started_before

    release.set()
    await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_multiple_bad_inputs_across_halves_are_isolated_individually() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()

    class MultiPoisonClient:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed_batch(self, texts: Any) -> list[list[float]]:
            chunk = list(texts)
            self.calls.append(chunk)
            if any(t in {"bad1", "bad2"} for t in chunk):
                raise ValueError("input rejected")
            return [[0.1, 0.2] for _ in chunk]

    daemon._embedding_client = MultiPoisonClient()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    # sorted() puts one bad text in each bisection half.
    specs = [
        {"query": t, "search_type": "semantic"}
        for t in ("aaa", "bad1", "ccc", "bad2", "eee", "fff")
    ]
    out = await daemon._batch_search_on_current_loop(specs, zone_id="root")

    failures = {specs[i]["query"] for i, r in enumerate(out) if isinstance(r, BatchQueryFailure)}
    # Independent bad inputs in DIFFERENT halves must not collapse into a
    # false "provider down" that fails every healthy sibling.
    assert failures == {"bad1", "bad2"}, out
    assert {r.query for r in captured} == {"aaa", "ccc", "eee", "fff"}
    assert all(r.query_vector == [0.1, 0.2] for r in captured)


@pytest.mark.asyncio
async def test_cancellation_resistant_pre_embed_cannot_outlive_deadline(monkeypatch) -> None:
    import asyncio
    import time

    from nexus.bricks.search import daemon as daemon_module
    from nexus.contracts.search_types import BatchQueryFailure

    monkeypatch.setattr(daemon_module, "_BATCH_CANCEL_DRAIN_SECONDS", 0.05)

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=4,
        query_timeout_seconds=0.05,
        batch_search_timeout_seconds=0.3,
    )
    release = asyncio.Event()

    class ResistantEmbeddingClient:
        async def embed_batch(self, texts: Any) -> list[list[float]]:
            inner = asyncio.ensure_future(release.wait())
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
            return [[0.1, 0.2] for _ in texts]

    daemon._embedding_client = ResistantEmbeddingClient()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    started = time.monotonic()
    out = await daemon._batch_search_on_current_loop(
        [{"query": "a", "search_type": "semantic"}], zone_id="root"
    )
    elapsed = time.monotonic() - started

    # A provider that swallows cancellation must not suspend the batch: the
    # pre-embed is cancelled, charged to the daemon-scoped pool, and the
    # request settles within its own deadline.
    assert elapsed < 3.0, f"pre-embed outlived the batch deadline ({elapsed:.2f}s)"
    assert isinstance(out[0], BatchQueryFailure)
    assert daemon._abandoned_batch_tasks

    release.set()
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_bad_inputs_contaminating_every_early_partition_stay_isolated() -> None:
    from nexus.contracts.search_types import BatchQueryFailure

    daemon = _make_batch_daemon()
    bad = {"bad1", "bad2", "bad3", "bad4"}

    class SpreadPoisonClient:
        async def embed_batch(self, texts: Any) -> list[list[float]]:
            chunk = list(texts)
            if any(t in bad for t in chunk):
                raise ValueError("input rejected")
            return [[0.1, 0.2] for _ in chunk]

    daemon._embedding_client = SpreadPoisonClient()
    captured: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        captured.append(request)
        return []

    daemon.search = MethodType(fake_search, daemon)

    # Interleaved so a bad text lands in the initial batch, both halves, and
    # the early quarters — the shape that used to read as a provider outage.
    texts = ["bad1", "aaa", "bad2", "bbb", "bad3", "ccc", "bad4", "ddd"]
    out = await daemon._batch_search_on_current_loop(
        [{"query": t, "search_type": "semantic"} for t in texts], zone_id="root"
    )

    failed = {texts[i] for i, r in enumerate(out) if isinstance(r, BatchQueryFailure)}
    # The control probe proves the provider is healthy, so only the offending
    # texts fail — healthy siblings still run with their vectors.
    assert failed == bad, out
    assert {r.query for r in captured} == {"aaa", "bbb", "ccc", "ddd"}


@pytest.mark.asyncio
async def test_exhausted_permit_pool_cannot_wedge_a_later_batch(monkeypatch) -> None:
    import asyncio
    import time

    from nexus.bricks.search import daemon as daemon_module
    from nexus.contracts.search_types import BatchQueryFailure

    monkeypatch.setattr(daemon_module, "_BATCH_CANCEL_DRAIN_SECONDS", 0.05)

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=1,
        query_timeout_seconds=0.05,
        batch_search_timeout_seconds=0.2,
    )
    release = asyncio.Event()

    async def resistant(self: Any, request: Any) -> list[Any]:
        inner = asyncio.ensure_future(release.wait())
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        return []

    daemon.search = MethodType(resistant, daemon)
    await daemon._batch_search_on_current_loop([{"query": "a"}], zone_id="root")
    assert daemon._batch_semaphore().locked()

    # A follow-up batch must settle on its OWN deadline rather than parking
    # forever on the permit the abandoned work still holds.
    started = time.monotonic()
    out = await daemon._batch_search_on_current_loop([{"query": "b"}], zone_id="root")
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"permit wait escaped the batch deadline ({elapsed:.2f}s)"
    assert isinstance(out[0], BatchQueryFailure)

    release.set()
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_healthy_queued_waves_get_a_full_execution_budget() -> None:
    import asyncio

    daemon = _make_batch_daemon()
    # Concurrency 1 forces three waves; each search takes longer than the
    # queue wait it sits through.
    daemon.config = type(daemon.config)(
        batch_search_concurrency=1,
        query_timeout_seconds=0.1,
        batch_search_timeout_seconds=5.0,
    )

    async def fake_search(self: Any, request: Any) -> list[Any]:
        await asyncio.sleep(0.06)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [{"query": "a"}, {"query": "b"}, {"query": "c"}], zone_id="root"
    )

    # Queue time is charged to the batch deadline, not to each query's own
    # execution budget — every healthy wave must complete.
    assert out == [[], [], []], out


@pytest.mark.asyncio
async def test_timed_out_pre_embed_probe_does_not_drain_the_permit_pool(monkeypatch) -> None:
    import asyncio

    from nexus.bricks.search import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "_BATCH_CANCEL_DRAIN_SECONDS", 0.05)

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=8,
        query_timeout_seconds=0.05,
        batch_search_timeout_seconds=1.0,
    )
    release = asyncio.Event()

    class ResistantEmbeddingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def embed_batch(self, texts: Any) -> list[list[float]]:
            self.calls += 1
            inner = asyncio.ensure_future(release.wait())
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()
            return [[0.1, 0.2] for _ in texts]

    client = ResistantEmbeddingClient()
    daemon._embedding_client = client

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    await daemon._batch_search_on_current_loop(
        [{"query": f"q{i}", "search_type": "hybrid"} for i in range(8)], zone_id="root"
    )

    # A timed-out probe stops isolation: exactly one provider task is left
    # holding a permit, so later batches still have capacity.
    assert client.calls == 1, client.calls
    assert len(daemon._abandoned_batch_tasks) == 1

    release.set()
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_resistant_path_context_refresh_cannot_hold_the_response(monkeypatch) -> None:
    import asyncio
    import time
    from unittest.mock import MagicMock

    from nexus.bricks.search import daemon as daemon_module

    monkeypatch.setattr(daemon_module, "_BATCH_CANCEL_DRAIN_SECONDS", 0.05)

    daemon = _make_batch_daemon()
    daemon.config = type(daemon.config)(
        batch_search_concurrency=4,
        query_timeout_seconds=0.05,
        batch_search_timeout_seconds=0.3,
    )
    release = asyncio.Event()

    class ResistantCache:
        async def refresh_if_stale(self, zone_id: str) -> None:
            inner = asyncio.ensure_future(release.wait())
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None:
                        current.uncancel()

        def snapshot_zone(self, zone_id: str) -> None:
            return None

    daemon._resolve_path_context_cache = MagicMock(
        return_value=asyncio.sleep(0, result=ResistantCache())
    )
    # The attach block's fail-soft counters live on the daemon's stats object,
    # which a bare __new__ daemon does not have.
    from nexus.bricks.search.daemon import DaemonStats

    daemon.stats = DaemonStats()

    async def fake_search(self: Any, request: Any) -> list[Any]:
        return []

    daemon.search = MethodType(fake_search, daemon)

    started = time.monotonic()
    out = await daemon._batch_search_on_current_loop([{"query": "a"}], zone_id="root")
    elapsed = time.monotonic() - started

    # The fail-soft refresh must not hold the response open past the deadline.
    assert elapsed < 3.0, f"path-context refresh escaped the deadline ({elapsed:.2f}s)"
    assert out == [[]]

    release.set()
    await asyncio.sleep(0.1)
