# Batch Search Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore `POST /api/v2/search/query/batch` to its documented contract: concurrent inner searches, one embedding call per batch of unique query texts, full single-`/query` param + serializer parity, and per-query error surfacing.

**Architecture:** Router-side parsing/validation moves to a new `_search_batch.py` helper (router is at ~1750 of the 2000-line cap). The daemon's `_batch_search_on_current_loop` pre-embeds unique query texts with one `embed_batch` call, threads each vector through a new `SearchRequest.query_vector` field, runs inner searches under `asyncio.gather` bounded by a semaphore, and returns per-query failures positionally as `BatchQueryFailure` instead of silent `[]`.

**Tech Stack:** Python 3.12, FastAPI, pytest (`uv run pytest`), pre-commit (ruff + mypy + file-size caps).

**Spec:** `docs/superpowers/specs/2026-08-04-batch-search-hardening-design.md` (approved).

## Global Constraints

- Worktree: `/Users/tafeng/nexus/.claude/worktrees/batch-search-hardening`, branch `feat/batch-search-hardening`, base `origin/develop`. All commands run from the worktree root.
- Test runner: `uv run pytest <path> -v` (first run resolves the env from `uv.lock`).
- `src/nexus/server/api/v2/routers/search.py` must stay under the repo's 2000-line file cap (currently ~1753 lines) — new batch logic goes in `_search_batch.py`.
- Response changes must be **additive only**; request aliases `query`/`search_type`/`path_filter` must keep working (existing callers: `scripts/test_read_gate_e2e.py`, `tests/integration/services/test_search_zone_set.py::TestBatchReadGate`).
- Numeric validation bounds mirror the single `/query` route exactly: `limit` 1..100, `alpha` 0.0..1.0, `rrf_k` 1..1000, `recency_weight` 0.0..5.0, `recency_half_life_days` >0.0..3650.0. String params (`type`, `fusion`, `expand`, `recency`) pass through unvalidated (single-route parity — the daemon handles unknown values).
- Per-query problems (invalid spec, inner search failure) become per-entry `error` fields; batch-level failures stay whole-request: 400 (no queries), 401/403 (auth/read gate), 503 (daemon initializing).
- Do NOT reference a GitHub issue number in code comments (the upstream issue is filed at PR time, Task 6); describe rationale in prose instead.
- Pre-commit runs on every commit (ruff format, mypy on changed files) — keep code typed.

---

### Task 1: `SearchRequest.query_vector` + daemon plumbing

**Files:**
- Modify: `src/nexus/contracts/search_types.py` (SearchRequest, ~line 64)
- Modify: `src/nexus/bricks/search/daemon.py`:
  - `search()` ~line 2828 (two `_search_on_current_loop` calls)
  - `_search_on_current_loop` signature ~line 2878 and its semantic/hybrid `_search_via_backends` call ~line 3045
  - `_search_via_backends` signature ~line 3157 and its two `self._embed_query(query)` legs ~lines 3212 (semantic) and 3239 (hybrid)
- Test: Create `tests/unit/bricks/search/test_daemon_batch_search.py`

**Interfaces:**
- Produces: `SearchRequest.query_vector: list[float] | None = None` (new frozen-dataclass field); `_search_via_backends(..., query_vector: list[float] | None = None)`; `_search_on_current_loop(..., query_vector: list[float] | None = None)`. Task 2 relies on `SearchRequest(query_vector=...)` skipping the embed step.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/search/test_daemon_batch_search.py`. Copy the module preamble (nexus_runtime stub) and `_make_daemon` harness from `tests/unit/bricks/search/test_daemon_fusion_params.py` verbatim, then add:

```python
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
```

(If the fusion-params harness marks tests with a different async marker — e.g. `pytest.mark.anyio` or plain async supported by config — match whatever that file uses instead of `pytest.mark.asyncio`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_batch_search.py -v`
Expected: FAIL — `TypeError: _search_via_backends() got an unexpected keyword argument 'query_vector'`

- [ ] **Step 3: Implement the plumbing**

In `src/nexus/contracts/search_types.py`, add to the end of `SearchRequest`'s fields:

```python
    # Pre-computed query embedding. When set, the daemon uses this vector
    # for the dense leg instead of embedding the query text itself — the
    # batch endpoint embeds all unique query texts in one embed_batch call
    # and hands each inner search its vector.
    query_vector: list[float] | None = None
```

In `daemon.py` `_search_via_backends` (~3157), add `query_vector: list[float] | None = None,` after `rrf_k: int = 60,` in the signature. Change BOTH embed legs (semantic ~3212, hybrid ~3239) from:

```python
        qvec = await timed_leg("embed_ms", self._embed_query(query))
```

to:

```python
        qvec = (
            query_vector
            if query_vector is not None
            else await timed_leg("embed_ms", self._embed_query(query))
        )
```

In `_search_on_current_loop` (~2878), add `query_vector: list[float] | None = None,` after `rrf_k: int = 60,`. In its semantic/hybrid `_search_via_backends` call (~3045), add `query_vector=query_vector,` after `rrf_k=rrf_k,`. (The keyword-branch call at ~2955 does not embed — leave it unchanged.)

In `search()` (~2828), add `query_vector=req.query_vector,` after `rrf_k=req.rrf_k,` in BOTH `_search_on_current_loop` calls (the `zone_id is None` branch and the `_work()` closure).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_batch_search.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Run neighboring regression tests**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_fusion_params.py tests/unit/bricks/search/test_daemon_recency.py -q`
Expected: PASS (signature additions are default-valued — nothing changes for existing callers)

- [ ] **Step 6: Commit**

```bash
git add src/nexus/contracts/search_types.py src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_batch_search.py
git commit -m "feat(search): thread optional pre-computed query_vector through SearchRequest"
```

---

### Task 2: Concurrent batch execution + embed-once + `BatchQueryFailure`

**Files:**
- Modify: `src/nexus/contracts/search_types.py` (add `BatchQueryFailure` below `SearchRequest`)
- Modify: `src/nexus/bricks/search/daemon.py`:
  - `DaemonConfig` (~line 409, "Performance settings" block ~441): add `batch_search_concurrency: int = 8`
  - `batch_search` (~line 3587) and `_batch_search_on_current_loop` (~line 3599): rewrite
- Modify: `src/nexus/server/lifespan/search.py` (~line 209 `DaemonConfig(...)` construction): parse `NEXUS_SEARCH_BATCH_CONCURRENCY`
- Test: `tests/unit/bricks/search/test_daemon_batch_search.py` (extend)

**Interfaces:**
- Consumes: `SearchRequest.query_vector` (Task 1).
- Produces: `BatchQueryFailure(error: str)` frozen dataclass in `nexus.contracts.search_types`; `batch_search(queries, *, zone_id) -> list[list[Any] | BatchQueryFailure]` where each element maps positionally to the input and is either a result list or a failure marker. Daemon reads spec keys: `query`, `search_type`, `limit`, `path_filter`, `alpha`, `fusion_method`, `rrf_k`, `expand`, `recency`, `recency_weight`, `recency_half_life_days`. Task 4's router relies on exactly these names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/bricks/search/test_daemon_batch_search.py`:

```python
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
async def test_batch_embed_failure_falls_back_to_per_query() -> None:
    daemon = _make_batch_daemon()
    daemon._embedding_client = _FakeEmbeddingClient(fail=True)
    seen_vectors: list[Any] = []

    async def fake_search(self: Any, request: Any) -> list[Any]:
        seen_vectors.append(request.query_vector)
        return []

    daemon.search = MethodType(fake_search, daemon)

    out = await daemon._batch_search_on_current_loop(
        [{"query": "a", "search_type": "hybrid"}], zone_id="root"
    )

    assert out == [[]]  # fail-soft: search still ran
    assert seen_vectors == [None]  # ...and embeds for itself downstream


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
    assert "boom" in out[1].error


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_batch_search.py -v`
Expected: Task-1 tests PASS; new tests FAIL (`ImportError: cannot import name 'BatchQueryFailure'`, `TypeError: DaemonConfig.__init__() got an unexpected keyword argument 'batch_search_concurrency'`, param-forwarding assertions).

- [ ] **Step 3: Implement**

In `src/nexus/contracts/search_types.py`, below `SearchRequest`:

```python
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
```

In `daemon.py` `DaemonConfig` (~441, "Performance settings"):

```python
    # Bounded fan-out width for POST /query/batch inner searches. 1 restores
    # strictly sequential execution (ops fallback).
    batch_search_concurrency: int = 8
```

Update `batch_search` (~3587) return annotations from `list[list[Any]]` to `list[list[Any] | BatchQueryFailure]` (import `BatchQueryFailure` alongside the existing `SearchRequest` import at the top of `daemon.py`).

Rewrite `_batch_search_on_current_loop` (~3599) — keep the initialization guard and the trailing path-context attach block, replace the middle:

```python
    async def _batch_search_on_current_loop(
        self,
        queries: list[dict[str, Any]],
        *,
        zone_id: str | None = None,
    ) -> list[list[Any] | BatchQueryFailure]:
        """Batch search: one shared embed call + bounded-concurrency fan-out.

        Powers ``POST /api/v2/search/query/batch``. All unique query texts
        that need a dense leg (search_type != "keyword") are embedded in ONE
        ``embed_batch`` call; each inner search then runs concurrently under
        ``DaemonConfig.batch_search_concurrency``. A failed inner query is
        returned positionally as :class:`BatchQueryFailure` instead of being
        collapsed to ``[]``, so callers can tell "no matches" from "backend
        fell over".
        """
        if not self._initialized:
            raise RuntimeError("SearchDaemon not initialized. Call startup() first.")

        from nexus.contracts.constants import ROOT_ZONE_ID

        effective_zone_id = zone_id or ROOT_ZONE_ID

        # Pre-embed unique texts once. Fail-soft: on any error fall back to
        # per-query embedding inside each inner search (hybrid degrades to
        # keyword-only on embed failure exactly as a single /query does).
        vector_by_text: dict[str, list[float]] = {}
        if self._embedding_client is not None:
            unique_texts = sorted(
                {
                    str(q.get("query", ""))
                    for q in queries
                    if isinstance(q, dict)
                    and str(q.get("query", ""))
                    and q.get("search_type", "hybrid") != "keyword"
                }
            )
            if unique_texts:
                try:
                    vectors = await self._embedding_client.embed_batch(unique_texts)
                    vector_by_text = dict(zip(unique_texts, vectors, strict=True))
                except Exception as exc:
                    logger.warning(
                        "batch pre-embed failed; falling back to per-query embedding: %s",
                        exc,
                    )

        semaphore = asyncio.Semaphore(max(1, self.config.batch_search_concurrency))

        async def _run_one(q: Any) -> list[Any] | BatchQueryFailure:
            if not isinstance(q, dict):
                return BatchQueryFailure(error="invalid query spec: expected an object")
            query_text = str(q.get("query", ""))
            async with semaphore:
                try:
                    return await self.search(
                        SearchRequest(
                            query=query_text,
                            search_type=q.get("search_type", "hybrid"),
                            limit=int(q.get("limit", 10)),
                            path_filter=q.get("path_filter"),
                            alpha=float(q.get("alpha", 0.5)),
                            fusion_method=q.get("fusion_method", "rrf"),
                            rrf_k=int(q.get("rrf_k", 60)),
                            expand=q.get("expand", "none"),
                            recency=q.get("recency"),
                            recency_weight=q.get("recency_weight"),
                            recency_half_life_days=q.get("recency_half_life_days"),
                            zone_id=effective_zone_id,
                            query_vector=vector_by_text.get(query_text),
                        )
                    )
                except Exception as exc:
                    logger.warning("batch_search inner search failed: %s", exc)
                    return BatchQueryFailure(error=str(exc) or exc.__class__.__name__)

        results: list[list[Any] | BatchQueryFailure] = list(
            await asyncio.gather(*(_run_one(q) for q in queries))
        )
```

In the retained path-context attach block at the end of the method, guard the per-result loop:

```python
                for inner in results:
                    if isinstance(inner, BatchQueryFailure):
                        continue
                    for r in inner:
```

(keep the rest of that block byte-identical) and keep `return results` at the end.

In `src/nexus/server/lifespan/search.py`, next to the other env parses (~195):

```python
        _batch_concurrency_env = os.environ.get("NEXUS_SEARCH_BATCH_CONCURRENCY", "")
        _batch_concurrency = 8
        if _batch_concurrency_env:
            try:
                _batch_concurrency = max(1, int(_batch_concurrency_env))
            except ValueError:
                logger.warning(
                    "Invalid NEXUS_SEARCH_BATCH_CONCURRENCY=%r — falling back to 8",
                    _batch_concurrency_env,
                )
```

and add `batch_search_concurrency=_batch_concurrency,` to the `DaemonConfig(...)` construction (~209).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/bricks/search/test_daemon_batch_search.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/contracts/search_types.py src/nexus/bricks/search/daemon.py src/nexus/server/lifespan/search.py tests/unit/bricks/search/test_daemon_batch_search.py
git commit -m "feat(search): concurrent batch_search with one shared embed call and per-query failure markers"
```

---

### Task 3: Batch spec parser (`_search_batch.py`)

**Files:**
- Create: `src/nexus/server/api/v2/routers/_search_batch.py`
- Test: Create `tests/integration/services/test_search_query_batch.py`

**Interfaces:**
- Produces: `ParsedBatchSpec` (frozen dataclass: `query, search_type, limit, path_filter, alpha, fusion_method, rrf_k, expand, recency, recency_weight, recency_half_life_days`), `parse_batch_query_spec(raw: Any) -> ParsedBatchSpec | str` (str = error message), `spec_query_text(raw: Any) -> str` (best-effort echo for error entries). Task 4's route consumes all three.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/services/test_search_query_batch.py`. Copy the `nexus_runtime` stub preamble and `_MockResult` dataclass from `tests/integration/services/test_search_zone_set.py` verbatim (Task 4 reuses them), then add:

```python
from nexus.server.api.v2.routers._search_batch import (
    ParsedBatchSpec,
    parse_batch_query_spec,
    spec_query_text,
)


class TestParseBatchQuerySpec:
    def test_defaults(self):
        spec = parse_batch_query_spec({"q": "hello"})
        assert spec == ParsedBatchSpec(query="hello")
        assert (spec.search_type, spec.limit, spec.alpha) == ("hybrid", 10, 0.5)
        assert (spec.fusion_method, spec.rrf_k, spec.expand) == ("rrf", 60, "none")
        assert (spec.recency, spec.recency_weight, spec.recency_half_life_days) == (
            None,
            None,
            None,
        )

    def test_public_names_win_over_legacy_aliases(self):
        spec = parse_batch_query_spec(
            {
                "q": "public",
                "query": "legacy",
                "type": "semantic",
                "search_type": "keyword",
                "path": "/a/",
                "path_filter": "/b/",
                "fusion": "weighted",
                "fusion_method": "rrf",
            }
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert spec.query == "public"
        assert spec.search_type == "semantic"
        assert spec.path_filter == "/a/"
        assert spec.fusion_method == "weighted"

    def test_legacy_aliases_still_accepted(self):
        spec = parse_batch_query_spec(
            {"query": "legacy", "search_type": "keyword", "path_filter": "/b/"}
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert (spec.query, spec.search_type, spec.path_filter) == ("legacy", "keyword", "/b/")

    def test_tuning_params_parsed(self):
        spec = parse_batch_query_spec(
            {
                "q": "tuned",
                "alpha": 0.3,
                "fusion": "weighted",
                "rrf_k": 90,
                "expand": "macro",
                "recency": "on",
                "recency_weight": 1.5,
                "recency_half_life_days": 30,
            }
        )
        assert isinstance(spec, ParsedBatchSpec)
        assert (spec.alpha, spec.fusion_method, spec.rrf_k) == (0.3, "weighted", 90)
        assert (spec.expand, spec.recency) == ("macro", "on")
        assert (spec.recency_weight, spec.recency_half_life_days) == (1.5, 30.0)

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ("not a dict", "expected an object"),
            ({}, "query text"),
            ({"q": ""}, "query text"),
            ({"q": "x", "limit": 0}, "limit"),
            ({"q": "x", "limit": 101}, "limit"),
            ({"q": "x", "limit": "ten"}, "limit"),
            ({"q": "x", "alpha": 1.5}, "alpha"),
            ({"q": "x", "alpha": -0.1}, "alpha"),
            ({"q": "x", "rrf_k": 0}, "rrf_k"),
            ({"q": "x", "rrf_k": 1001}, "rrf_k"),
            ({"q": "x", "recency_weight": 5.1}, "recency_weight"),
            ({"q": "x", "recency_half_life_days": 0}, "recency_half_life_days"),
            ({"q": "x", "recency_half_life_days": 3651}, "recency_half_life_days"),
            ({"q": "x", "path": 42}, "path"),
        ],
    )
    def test_invalid_specs_return_error_message(self, raw, fragment):
        err = parse_batch_query_spec(raw)
        assert isinstance(err, str)
        assert fragment in err

    def test_spec_query_text_best_effort(self):
        assert spec_query_text({"q": "a"}) == "a"
        assert spec_query_text({"query": "b"}) == "b"
        assert spec_query_text({"q": "a", "query": "b"}) == "a"
        assert spec_query_text("junk") == ""
        assert spec_query_text({}) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/services/test_search_query_batch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.server.api.v2.routers._search_batch'`

- [ ] **Step 3: Implement the parser**

Create `src/nexus/server/api/v2/routers/_search_batch.py`:

```python
"""Batch search request parsing (`POST /query/batch`).

Extracted from ``routers/search.py`` the same way ``_search_serialize.py``
was (the router sits near the repo's 2000-line file cap). This module owns
the pure per-query spec contract; the route keeps auth, the read gate, and
daemon wiring.

Each spec mirrors the single ``/query`` route's public parameter names and
keeps the legacy batch aliases (``query``/``search_type``/``path_filter``).
The public name wins when both are present. Numeric bounds mirror the
single route's FastAPI ``Query()`` validation exactly; string params pass
through unvalidated (single-route parity — the daemon handles unknown
values). Invalid specs return an error MESSAGE rather than raising: the
route maps them to per-entry ``error`` fields so one bad query cannot fail
a whole batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, kw_only=True)
class ParsedBatchSpec:
    """One validated batch query, normalized to daemon spec key names."""

    query: str
    search_type: str = "hybrid"
    limit: int = 10
    path_filter: str | None = None
    alpha: float = 0.5
    fusion_method: str = "rrf"
    rrf_k: int = 60
    expand: str = "none"
    recency: str | None = None
    recency_weight: float | None = None
    recency_half_life_days: float | None = None


def spec_query_text(raw: Any) -> str:
    """Best-effort query-text echo for error entries (public name wins)."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("q") or raw.get("query") or "")


def _pick(raw: dict[str, Any], public: str, legacy: str) -> Any:
    """Resolve a public-name/legacy-alias pair; public wins when present."""
    if public in raw:
        return raw[public]
    return raw.get(legacy)


def _as_int(value: Any, name: str, lo: int, hi: int) -> int | str:
    try:
        # bool is an int subclass; reject it explicitly so `limit: true`
        # doesn't silently parse as 1.
        if isinstance(value, bool):
            raise ValueError
        out = int(value)
    except (TypeError, ValueError):
        return f"{name} must be an integer"
    if not lo <= out <= hi:
        return f"{name} must be between {lo} and {hi}"
    return out


def _as_float(
    value: Any, name: str, lo: float, hi: float, *, exclusive_lo: bool = False
) -> float | str:
    try:
        if isinstance(value, bool):
            raise ValueError
        out = float(value)
    except (TypeError, ValueError):
        return f"{name} must be a number"
    if exclusive_lo and out <= lo:
        return f"{name} must be greater than {lo} and at most {hi}"
    if not exclusive_lo and not lo <= out <= hi:
        return f"{name} must be between {lo} and {hi}"
    if out > hi:
        return f"{name} must be greater than {lo} and at most {hi}"
    return out


def parse_batch_query_spec(raw: Any) -> ParsedBatchSpec | str:
    """Parse one raw batch query spec.

    Returns a :class:`ParsedBatchSpec` on success or an error message on
    invalid input. Bounds mirror the single ``/query`` route: ``limit``
    1..100, ``alpha`` 0.0..1.0, ``rrf_k`` 1..1000, ``recency_weight``
    0.0..5.0, ``recency_half_life_days`` >0.0..3650.0.
    """
    if not isinstance(raw, dict):
        return "invalid query spec: expected an object"

    query = spec_query_text(raw)
    if not query:
        return "query text required (q)"

    path = _pick(raw, "path", "path_filter")
    if path is not None and not isinstance(path, str):
        return "path must be a string"

    limit = _as_int(raw.get("limit", 10), "limit", 1, 100)
    if isinstance(limit, str):
        return limit
    alpha = _as_float(raw.get("alpha", 0.5), "alpha", 0.0, 1.0)
    if isinstance(alpha, str):
        return alpha
    rrf_k = _as_int(raw.get("rrf_k", 60), "rrf_k", 1, 1000)
    if isinstance(rrf_k, str):
        return rrf_k

    recency_weight: float | None = None
    if raw.get("recency_weight") is not None:
        parsed_weight = _as_float(raw["recency_weight"], "recency_weight", 0.0, 5.0)
        if isinstance(parsed_weight, str):
            return parsed_weight
        recency_weight = parsed_weight

    recency_half_life: float | None = None
    if raw.get("recency_half_life_days") is not None:
        parsed_half_life = _as_float(
            raw["recency_half_life_days"],
            "recency_half_life_days",
            0.0,
            3650.0,
            exclusive_lo=True,
        )
        if isinstance(parsed_half_life, str):
            return parsed_half_life
        recency_half_life = parsed_half_life

    recency = raw.get("recency")

    return ParsedBatchSpec(
        query=query,
        search_type=str(_pick(raw, "type", "search_type") or "hybrid"),
        limit=limit,
        path_filter=path,
        alpha=alpha,
        fusion_method=str(_pick(raw, "fusion", "fusion_method") or "rrf"),
        rrf_k=rrf_k,
        expand=str(raw.get("expand") or "none"),
        recency=str(recency) if recency is not None else None,
        recency_weight=recency_weight,
        recency_half_life_days=recency_half_life,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/services/test_search_query_batch.py -v`
Expected: PASS (all parser tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/api/v2/routers/_search_batch.py tests/integration/services/test_search_query_batch.py
git commit -m "feat(search): batch query spec parser with single-/query param parity"
```

---

### Task 4: Rewrite the `/query/batch` route

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py:798-925` (`search_query_batch`)
- Test: `tests/integration/services/test_search_query_batch.py` (extend)

**Interfaces:**
- Consumes: `parse_batch_query_spec` / `ParsedBatchSpec` / `spec_query_text` (Task 3); `batch_search -> list[list[Any] | BatchQueryFailure]` with daemon spec keys `query/search_type/limit/path_filter/alpha/fusion_method/rrf_k/expand/recency/recency_weight/recency_half_life_days` (Task 2); existing `_serialize_search_result`, `_apply_rebac_filter`, `_compute_rebac_fetch_limit` (already imported in `search.py`).
- Produces: the wire contract from the spec — per-entry `{"query", "results", "total"}` plus additive `"error"`; unchanged top-level fields.

- [ ] **Step 1: Write the failing route tests**

Append to `tests/integration/services/test_search_query_batch.py`:

```python
def _build_batch_app(mock_daemon):
    from fastapi import FastAPI

    from nexus.server.api.v2.routers.search import router

    app = FastAPI()
    app.include_router(router)
    mock_daemon.is_initialized = True
    app.state.search_daemon = mock_daemon
    app.state.search_daemon_enabled = True
    app.state.record_store = MagicMock()
    app.state.async_session_factory = MagicMock()
    app.state.async_read_session_factory = MagicMock()

    from nexus.server.dependencies import require_auth

    app.dependency_overrides[require_auth] = lambda: {
        "authenticated": True,
        "user_id": "u",
        "zone_id": "eng",
        "zone_set": ["eng"],
        "zone_perms": [["eng", "r"]],
    }
    return app


@pytest.mark.skipif(not _HAS_FASTAPI_TESTCLIENT, reason="fastapi test client not available")
class TestBatchRoute:
    def test_params_forwarded_to_daemon(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={
                "queries": [
                    {
                        "q": "tuned",
                        "type": "hybrid",
                        "limit": 7,
                        "path": "/ws/",
                        "alpha": 0.3,
                        "fusion": "weighted",
                        "rrf_k": 90,
                        "expand": "macro",
                        "recency": "on",
                        "recency_weight": 1.5,
                        "recency_half_life_days": 30,
                    }
                ]
            },
        )

        assert resp.status_code == 200, resp.text
        (specs,) = daemon.batch_search.call_args.args
        assert daemon.batch_search.call_args.kwargs == {"zone_id": "eng"}
        spec = specs[0]
        assert spec["query"] == "tuned"
        assert spec["search_type"] == "hybrid"
        assert spec["path_filter"] == "/ws/"
        assert (spec["alpha"], spec["fusion_method"], spec["rrf_k"]) == (0.3, "weighted", 90)
        assert (spec["expand"], spec["recency"]) == ("macro", "on")
        assert (spec["recency_weight"], spec["recency_half_life_days"]) == (1.5, 30.0)
        # No enforcer on this app -> fetch limit == requested limit.
        assert spec["limit"] == 7

    def test_serializer_parity_with_single_query(self):
        from nexus.server.api.v2.routers._search_serialize import _serialize_search_result

        result = _MockResult(
            path="/ws/doc.md",
            chunk_text="hello",
            score=0.9123456,
            chunk_index=3,
            line_start=10,
            line_end=14,
            keyword_score=1.25,
            vector_score=0.75,
        )
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[result]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch", json={"queries": [{"q": "hello"}]}
        )

        assert resp.status_code == 200, resp.text
        entry = resp.json()["queries"][0]
        assert "error" not in entry
        assert entry["total"] == 1
        assert entry["results"][0] == _serialize_search_result(result)
        assert entry["results"][0]["chunk_index"] == 3
        assert entry["results"][0]["line_start"] == 10

    def test_daemon_failure_becomes_error_entry(self):
        from nexus.contracts.search_types import BatchQueryFailure

        daemon = MagicMock()
        daemon.batch_search = AsyncMock(
            return_value=[[], BatchQueryFailure(error="backend fell over")]
        )
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={"queries": [{"q": "ok"}, {"q": "doomed"}]},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "error" not in body["queries"][0]
        assert body["queries"][1] == {
            "query": "doomed",
            "results": [],
            "total": 0,
            "error": "backend fell over",
        }

    def test_invalid_spec_gets_error_entry_and_is_not_sent_to_daemon(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[[]])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch",
            json={"queries": [{"q": "bad-limit", "limit": 0}, {"q": "fine"}]},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total_queries"] == 2
        assert body["queries"][0]["query"] == "bad-limit"
        assert "limit" in body["queries"][0]["error"]
        assert body["queries"][0]["results"] == []
        assert "error" not in body["queries"][1]
        # Only the valid spec reached the daemon.
        (specs,) = daemon.batch_search.call_args.args
        assert [s["query"] for s in specs] == ["fine"]

    def test_all_specs_invalid_skips_daemon_entirely(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post(
            "/api/v2/search/query/batch", json={"queries": [{"q": "x", "limit": 9999}]}
        )

        assert resp.status_code == 200, resp.text
        assert "limit" in resp.json()["queries"][0]["error"]
        daemon.batch_search.assert_not_called()

    def test_empty_queries_still_400(self):
        daemon = MagicMock()
        daemon.batch_search = AsyncMock(return_value=[])
        app = _build_batch_app(daemon)

        resp = TestClient(app).post("/api/v2/search/query/batch", json={"queries": []})

        assert resp.status_code == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/services/test_search_query_batch.py -v`
Expected: parser tests PASS; route tests FAIL (old route drops tuning params, formats entries inline without `chunk_index`, has no `error` entries).

- [ ] **Step 3: Rewrite the route**

In `search.py`, add to the imports near the other router-helper imports:

```python
from nexus.server.api.v2.routers._search_batch import (
    ParsedBatchSpec,
    parse_batch_query_spec,
    spec_query_text,
)
```

Replace the body of `search_query_batch` (KEEP: the decorator/signature, `ROOT_ZONE_ID` import + `zone_id` line, the 400 guard, the #4557 read-gate block, the 503 daemon-init check, and the `permission_enforcer`/`op_context` lines — replace everything from the old over-fetch loop down to the return):

```python
    """Batch search: run N queries through the full hybrid pipeline.

    Body: ``{"queries": [{...}]}`` where each entry mirrors the single
    ``/query`` route's public parameter names (``q``, ``type``, ``limit``,
    ``path``, ``alpha``, ``fusion``, ``rrf_k``, ``expand``, ``recency``,
    ``recency_weight``, ``recency_half_life_days``); the legacy batch
    aliases ``query``/``search_type``/``path_filter`` stay accepted (see
    ``_search_batch.py`` for the exact contract).

    Returns ``{"queries": [{"query", "results", "total"[, "error"]}], ...}``.
    Results use the same serialization as single ``/query``. A failed or
    invalid query yields a per-entry additive ``error`` message instead of
    failing the batch (absence of ``error`` means the result set is
    genuine); batch-level auth/read-gate/daemon-init failures still fail
    the whole request.

    Applies the same ReBAC file-level permission filter as ``/query``
    (Decision #17), over-fetching via ``_compute_rebac_fetch_limit`` and
    trimming to each query's requested ``limit`` after filtering. All
    unique query texts are embedded in ONE ``embed_batch`` call and the
    inner searches run concurrently (``NEXUS_SEARCH_BATCH_CONCURRENCY``,
    default 8).
    """
```

then:

```python
parsed = [parse_batch_query_spec(raw) for raw in raw_queries]
valid: list[tuple[int, ParsedBatchSpec]] = [
    (i, p) for i, p in enumerate(parsed) if isinstance(p, ParsedBatchSpec)
]
fetch_specs = [
    {
        "query": spec.query,
        "search_type": spec.search_type,
        "limit": _compute_rebac_fetch_limit(
            spec.limit, has_enforcer=permission_enforcer is not None
        ),
        "path_filter": spec.path_filter,
        "alpha": spec.alpha,
        "fusion_method": spec.fusion_method,
        "rrf_k": spec.rrf_k,
        "expand": spec.expand,
        "recency": spec.recency,
        "recency_weight": spec.recency_weight,
        "recency_half_life_days": spec.recency_half_life_days,
    }
    for _, spec in valid
]

t0 = time.perf_counter()
raw_results = await search_daemon.batch_search(fetch_specs, zone_id=zone_id) if fetch_specs else []
elapsed_ms = (time.perf_counter() - t0) * 1000

result_by_index: dict[int, Any] = {
    i: result for (i, _), result in zip(valid, raw_results, strict=True)
}

filter_ms_total = 0.0
response_queries: list[dict[str, Any]] = []
for i, p in enumerate(parsed):
    if isinstance(p, str):
        response_queries.append(
            {
                "query": spec_query_text(raw_queries[i]),
                "results": [],
                "total": 0,
                "error": p,
            }
        )
        continue
    inner = result_by_index[i]
    if isinstance(inner, BatchQueryFailure):
        response_queries.append({"query": p.query, "results": [], "total": 0, "error": inner.error})
        continue
    # File-level ReBAC filtering (Decision #17) — same enforcement as /query.
    filtered, filter_ms = _apply_rebac_filter(
        inner,
        permission_enforcer,
        auth_result,
        zone_id,
        operation_context=op_context,
    )
    filter_ms_total += filter_ms
    trimmed = filtered[: p.limit]
    response_queries.append(
        {
            "query": p.query,
            "results": [_serialize_search_result(r) for r in trimmed],
            "total": len(trimmed),
        }
    )

return {
    "queries": response_queries,
    "total_queries": len(raw_queries),
    "latency_ms": round(elapsed_ms, 2),
    "avg_per_query_ms": round(elapsed_ms / max(len(raw_queries), 1), 2),
    "permission_filter_ms": round(filter_ms_total, 2),
}
```

`BatchQueryFailure` import: add `from nexus.contracts.search_types import BatchQueryFailure` inside the function next to the existing `ROOT_ZONE_ID` local import (or at module top if ruff prefers — match the file's existing style: `ROOT_ZONE_ID` is imported locally). The old `overfetch_multiplier`/`requested_limits` loop and the inline `formatted` serialization block are deleted. Confirm `_serialize_search_result` is already imported/re-exported in `search.py` (the `_search_serialize.py` docstring says the router re-exports it); if it's not in scope, import it from `._search_serialize`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/services/test_search_query_batch.py -v`
Expected: PASS (all)

- [ ] **Step 5: Run the batch read-gate + zone-set regressions**

Run: `uv run pytest tests/integration/services/test_search_zone_set.py -v`
Expected: PASS unchanged — the read-gate tests POST `{"queries": [{"q": "alpha"}]}` against a mock `batch_search = AsyncMock(return_value=[[]])`; the rewritten route parses that spec, calls the mock once, serializes `[]`.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py tests/integration/services/test_search_query_batch.py
git commit -m "feat(search): /query/batch param+serializer parity with /query and per-query errors"
```

---

### Task 5: E2E extension (live-server parity)

**Files:**
- Modify: `tests/e2e/self_contained/test_search_surface_live_e2e.py` (batch block, ~line 240)

**Interfaces:**
- Consumes: the deployed route contract from Task 4. Uses the file's existing `_request(live, method, path, ...)` helper and `_assert_endpoint_latency`.

- [ ] **Step 1: Extend the batch block**

Directly after the existing batch assertions (`_assert_endpoint_latency(batch_body)`), add:

```python
    # Hardened batch contract: single-/query serializer parity, tuning
    # params accepted, healthy entries carry no error field.
    parity_response, parity_body = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={
            "queries": [
                {"q": "needle", "type": "keyword", "limit": 5},
                {"q": "needle", "type": "hybrid", "limit": 5, "alpha": 0.3, "fusion": "weighted"},
                {"q": "needle", "type": "semantic", "limit": 5},
            ]
        },
    )
    assert parity_response.status_code == 200
    assert parity_body["total_queries"] == 3
    for entry in parity_body["queries"]:
        assert "error" not in entry, entry
    single_response, single_body = _request(
        live,
        "get",
        "/api/v2/search/query",
        params={"q": "needle", "type": "keyword", "limit": 5},
    )
    assert single_response.status_code == 200
    batch_keyword_results = parity_body["queries"][0]["results"]
    single_results = single_body["results"]
    assert [r["path"] for r in batch_keyword_results] == [r["path"] for r in single_results]
    # Field-shape parity: every key the single route emits appears in the
    # batch entry with the same value (single may add response-envelope
    # keys, so compare per-hit dicts directly).
    for batch_hit, single_hit in zip(batch_keyword_results, single_results, strict=True):
        assert batch_hit == single_hit

    # Per-query error isolation: an invalid spec errors alone.
    mixed_response, mixed_body = _request(
        live,
        "post",
        "/api/v2/search/query/batch",
        json={"queries": [{"q": "needle", "limit": 0}, {"q": "needle", "limit": 2}]},
    )
    assert mixed_response.status_code == 200
    assert "limit" in mixed_body["queries"][0]["error"]
    assert "error" not in mixed_body["queries"][1]
```

- [ ] **Step 2: Run the e2e module**

Run: `uv run pytest tests/e2e/self_contained/test_search_surface_live_e2e.py -v`
Expected: PASS. (This suite boots a self-contained live server; if the module has skip conditions that trip in this environment, run whatever subset executes and note honestly in the PR which legs ran.)

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/self_contained/test_search_surface_live_e2e.py
git commit -m "test(search): live e2e parity + error-isolation coverage for hardened /query/batch"
```

---

### Task 6: Full-suite regression, bench evidence, push + PR

**Files:**
- No new committed files (bench script goes in the session scratchpad, not the repo).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Full search-scoped test sweep**

Run:
```bash
uv run pytest tests/unit/bricks/search/ tests/integration/services/test_search_zone_set.py tests/integration/services/test_search_query_batch.py tests/integration/bricks/search/ -q
```
Expected: PASS (pre-existing failures unrelated to search batch, if any, noted honestly).

- [ ] **Step 2: Pre-commit over the branch diff**

Run: `uv run pre-commit run --from-ref origin/develop --to-ref HEAD`
Expected: all hooks pass (ruff, mypy, file-size caps — `search.py` must still be <2000 lines: `wc -l src/nexus/server/api/v2/routers/search.py`).

- [ ] **Step 3: Bench evidence**

Write a scratch script (session scratchpad, NOT committed) that POSTs to a locally booted server (reuse the self-contained e2e harness boot, or `uv run nexus serve` equivalent used by that suite):
1. One batch of 24 specs sharing one query text (`type: "hybrid"`, distinct `path` filters) — record `latency_ms` from the response.
2. The same 24 as sequential single `/query` GETs — record summed wall-clock.
Repeat 3× on the feature branch, then 3× on `origin/develop` (check out `origin/develop` in a scratch worktree or `git stash`-free switch, since the branch has no uncommitted state). Record medians in the PR body. Also cite the embed-once unit test as the embedding-amortisation evidence (local stacks without an embedding key exercise the keyword fallback, so wall-clock alone understates the win on prod-like deployments).

- [ ] **Step 4: File the upstream issue + open the PR**

```bash
git push -u origin feat/batch-search-hardening
gh issue create --repo nexi-lab/nexus \
  --title "search: /query/batch lost its batched-embedding contract (#3699) — sequential, drops tuning params, silent per-query failures" \
  --body "<summary of the 5 gaps from docs/superpowers/specs/2026-08-04-batch-search-hardening-design.md, noting the endpoint docstring still promises one-call embedding>"
gh pr create --repo nexi-lab/nexus --base develop --head feat/batch-search-hardening \
  --title "search: restore /query/batch contract — concurrent execution, embed-once, /query parity, per-query errors" \
  --body "<spec summary + Fixes #<issue> + test list + bench medians + compat notes (additive response, aliases kept, NEXUS_SEARCH_BATCH_CONCURRENCY=1 ops fallback)>"
```

- [ ] **Step 5: Koodle follow-up note**

Comment on DeepBuildAI/koodle#2050: upstream PR link, adoption remains deferred until the fix is deployed to prod nexus; list what the koodle side will need (SDK `search.queryBatch`, fold of the 3 sub-path queries, coverage-contract mapping of per-query errors → degradable failures).

---

## Verification checklist (post-plan self-review)

- Spec coverage: concurrency (Task 2), embed-once (Tasks 1+2), param parity (Tasks 3+4), serializer parity (Task 4), per-query errors (Tasks 2+4), env knob (Task 2), ReBAC fetch-limit helper (Task 4), e2e (Task 5), bench + PR + koodle note (Task 6). Non-goals untouched.
- Type consistency: `BatchQueryFailure(error: str)` used identically in Tasks 2 and 4; daemon spec keys in Task 2's `_run_one` match Task 4's `fetch_specs` exactly; `query_vector` name identical across SearchRequest/`_search_on_current_loop`/`_search_via_backends`.
