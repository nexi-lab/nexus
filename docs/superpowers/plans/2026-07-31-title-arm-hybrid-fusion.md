# Title Arm in Hybrid Fusion (#4545) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold the skeleton title index (`SearchDaemon.locate()`) into the hybrid search fusion chain as a third keyword-side arm, so title-shaped queries rank their target doc even when its body chunks are weak.

**Architecture:** In `_search_via_backends`' hybrid branch, run `locate()` (in-memory BM25-lite over path tokens + title) after the keyword/dense legs gather, hydrate its `{path, score, title}` hits to page granularity (borrow the best already-fetched leg chunk per path; batched `fetch_ranges` for uncovered paths; chunkless docs emit `chunk_text=""`), and swap the keyword sub-fusion from 2-way `rrf_fusion(chunk_kw, page_kw)` to 3-arm `rrf_multi_fusion([chunk, page, title])`. The final `fuse_results(kw_fused, dense)` stage (#4541 alpha/fusion_method/rrf_k) is untouched. Attribution surfaces as a new `title_score` field.

**Tech Stack:** Python 3.12, dataclasses, pytest + pytest-asyncio, SQLAlchemy async (only via the existing `fetch_ranges`), FastAPI router serializer.

**Spec:** `docs/superpowers/specs/2026-07-31-title-arm-hybrid-fusion-design.md` (approved). Issue: #4545.

## Global Constraints

- Branch: `feat/4545-title-arm-fusion` (already created; spec committed as `30f16d22d`).
- Run tests from the worktree root with the MAIN repo venv (this worktree has no `.venv`; `uv run` fails on native deps):
  `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest <path> -v`
- Config class is `DaemonConfig` (`src/nexus/bricks/search/daemon.py:187`) — the spec's one mention of "SearchDaemonConfig" is a typo, fixed in Task 5.
- Env flag: `NEXUS_SEARCH_TITLE_ARM`, default on, falsy values `("false", "0", "no")` — same parse idiom as `NEXUS_SEARCH_PAGE_AGGREGATION` (`src/nexus/server/lifespan/search.py:176-177`).
- New API field `title_score`: rounded to 4 places, key OMITTED when None (compact pattern like `context`/`macro_text`, NOT the always-null `splade_score` pattern).
- The title arm must never fail a search: hydration is best-effort (debug log on failure).
- Zero schema change, no reindex, no new dependencies.
- Commits: Conventional Commits, imperative, ≤50-char subject, no AI attribution.
- Pre-commit hooks run automatically (ruff, mypy, etc.) — a commit that fails hooks is not done.

---

### Task 1: `title_score` attribution field + serializer + coercion passthrough

**Files:**
- Modify: `src/nexus/bricks/search/results.py` (BaseSearchResult, after `macro_line_end` ~line 47)
- Modify: `src/nexus/bricks/search/daemon.py` (`_coerce_to_search_result` ~line 2182; `_fuse_ranked_results` ~line 2253)
- Modify: `src/nexus/server/api/v2/routers/search.py` (`_serialize_search_result` ~line 156)
- Test: `tests/unit/bricks/search/test_search_result_serialize.py`
- Test: Create `tests/unit/bricks/search/test_daemon_title_arm.py`

**Interfaces:**
- Consumes: existing `BaseSearchResult` dataclass, `SearchDaemon._coerce_to_search_result(raw, *, search_type)` staticmethod, `_serialize_search_result(result)`.
- Produces: `BaseSearchResult.title_score: float | None = None` (inherited by daemon `SearchResult`); serializer emits `"title_score"` (rounded, omit-when-None); coercion preserves `title_score` from both dataclass and dict inputs. Tasks 3–4 rely on the field name being exactly `title_score` (it is `f"{source_name}_score"` for the fusion arm named `"title"`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/bricks/search/test_search_result_serialize.py`:

```python
def test_serialize_omits_title_score_when_absent():
    """Default response unchanged when title_score is absent (#4545)."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    out = _serialize_search_result(r)
    assert "title_score" not in out


def test_serialize_includes_title_score_when_present():
    """title_score emitted rounded to 4 places when the title arm scored the hit."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    r.title_score = 7.00004
    out = _serialize_search_result(r)
    assert out["title_score"] == 7.0
```

Create `tests/unit/bricks/search/test_daemon_title_arm.py`:

```python
"""Skeleton title arm in hybrid fusion (Issue #4545).

locate() — BM25-lite over path tokens + title — joins the keyword
sub-fusion as a third arm. These tests pin attribution plumbing,
hydration, the sub-fusion swap, and non-title-query parity.
"""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest


def test_coerce_preserves_title_score_from_dict() -> None:
    from nexus.bricks.search.daemon import SearchDaemon

    res = SearchDaemon._coerce_to_search_result(
        {"path": "/a.md", "chunk_text": "x", "score": 1.0, "title_score": 2.5},
        search_type="hybrid",
    )
    assert res.title_score == 2.5


def test_coerce_preserves_title_score_from_dataclass() -> None:
    from nexus.bricks.search.daemon import SearchDaemon
    from nexus.bricks.search.results import BaseSearchResult

    base = BaseSearchResult(path="/a.md", chunk_text="x", score=1.0, title_score=2.5)
    res = SearchDaemon._coerce_to_search_result(base, search_type="hybrid")
    assert res.title_score == 2.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_search_result_serialize.py tests/unit/bricks/search/test_daemon_title_arm.py -v`
Expected: `test_serialize_omits_title_score_when_absent` PASSES already (key never emitted); `test_serialize_includes_title_score_when_present` FAILS with KeyError; `..._from_dataclass` FAILS with `TypeError: unexpected keyword argument 'title_score'`; `..._from_dict` FAILS with `AttributeError` on `res.title_score`.

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/results.py` — append to `BaseSearchResult` fields after `macro_line_end: int | None = None`:

```python
    # Issue #4545: skeleton title-arm attribution — locate() score when the
    # title arm voted for this result in hybrid fusion, else None.
    title_score: float | None = None
```

`src/nexus/bricks/search/daemon.py` `_coerce_to_search_result` — in the `BaseSearchResult` branch add after `reranker_score=raw.reranker_score,`:

```
                title_score=raw.title_score,
```

In the `dict` branch add after `reranker_score=raw.get("reranker_score"),`:

```
                title_score=raw.get("title_score"),
```

`src/nexus/bricks/search/daemon.py` `_fuse_ranked_results` — in the `SearchResult(...)` copy add after `reranker_score=result.reranker_score,`:

```
                    title_score=result.title_score,
```

`src/nexus/server/api/v2/routers/search.py` `_serialize_search_result` — after the `reranker` block, before the `context` block:

```python
    title = getattr(result, "title_score", None)
    if title is not None:
        out["title_score"] = round(title, 4)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_search_result_serialize.py tests/unit/bricks/search/test_daemon_title_arm.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/results.py src/nexus/bricks/search/daemon.py \
  src/nexus/server/api/v2/routers/search.py \
  tests/unit/bricks/search/test_search_result_serialize.py \
  tests/unit/bricks/search/test_daemon_title_arm.py
git commit -m "feat(search): add title_score attribution field

Title-arm participation marker for #4545: BaseSearchResult field,
coercion passthrough, serializer emit (omit-when-None).

Refs #4545"
```

---

### Task 2: `title_arm` config flag + env wiring

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (`DaemonConfig`, after `page_bm25_rrf_k: int = 60` ~line 262)
- Modify: `src/nexus/server/lifespan/search.py` (env parse ~line 196; `DaemonConfig(...)` kwargs ~line 222)
- Test: `tests/unit/bricks/search/test_daemon_title_arm.py`

**Interfaces:**
- Consumes: `DaemonConfig` dataclass, lifespan env-parse idiom.
- Produces: `DaemonConfig.title_arm: bool = True`; `NEXUS_SEARCH_TITLE_ARM` env override. Task 4's hybrid block gates on `self.config.title_arm`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/bricks/search/test_daemon_title_arm.py`:

```python
def test_daemon_config_title_arm_defaults_on() -> None:
    from nexus.bricks.search.daemon import DaemonConfig

    assert DaemonConfig().title_arm is True
    assert DaemonConfig(title_arm=False).title_arm is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_title_arm.py::test_daemon_config_title_arm_defaults_on -v`
Expected: FAIL with `AttributeError: 'DaemonConfig' object has no attribute 'title_arm'` (or TypeError on the kwarg).

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/daemon.py` `DaemonConfig` — after `page_bm25_rrf_k: int = 60`:

```python
    # Skeleton title arm in hybrid fusion (Issue #4545). Runs locate() —
    # BM25-lite over path tokens + title (document_skeleton mirror) — as a
    # third arm of the keyword sub-fusion so title-shaped queries rank docs
    # whose body chunks are weak. Rank-based: contributes only where it has
    # hits. Default on; set NEXUS_SEARCH_TITLE_ARM=false to disable.
    title_arm: bool = True
```

`src/nexus/server/lifespan/search.py` — after the `_page_bm25` parse (line ~190), before the `_index_preload_env` block:

```python
        # Skeleton title arm in hybrid fusion (Issue #4545). Default on.
        _title_arm_env = os.environ.get("NEXUS_SEARCH_TITLE_ARM", "true")
        _title_arm = _title_arm_env.strip().lower() not in ("false", "0", "no")
```

And in the `DaemonConfig(...)` call, after `page_bm25_rrf_k=_page_bm25_rrf_k,`:

```
            title_arm=_title_arm,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_title_arm.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py src/nexus/server/lifespan/search.py \
  tests/unit/bricks/search/test_daemon_title_arm.py
git commit -m "feat(search): add title_arm config flag

NEXUS_SEARCH_TITLE_ARM kill-switch (default on), same ablation
pattern as NEXUS_SEARCH_PAGE_AGGREGATION.

Refs #4545"
```

---

### Task 3: hydration helper `_hydrate_title_hits`

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (new method directly after `locate()`, ~line 1547)
- Test: `tests/unit/bricks/search/test_daemon_title_arm.py`

**Interfaces:**
- Consumes: `locate()` output shape `list[{"path": str, "score": float, "title": str | None}]`; leg results as `BaseSearchResult`-like objects (attrs `path`, `score`, `chunk_text`, `chunk_index`, `line_start`, `line_end`); optional `self._vector_backend.fetch_ranges(spans, zone_id) -> list[ChunkRow]` (NeighborFetcher, #4398; `ChunkRow` attrs: `path`, `chunk_index`, `text`, `line_start`, `line_end`).
- Produces: `async def _hydrate_title_hits(self, locate_hits, *, chunk_kw, page_kw, zone_id) -> list[BaseSearchResult]` — hits in locate rank order, `score` = locate score, `zone_id` set, `chunk_index` aligned with the best leg row when the path is covered. Task 4 feeds this list as the `"title"` fusion arm.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/bricks/search/test_daemon_title_arm.py`:

```python
def _hydration_daemon(fetch_rows: list[Any] | None = None, fetch_raises: bool = False):
    """Bare daemon with only what _hydrate_title_hits touches."""
    from nexus.bricks.search.daemon import SearchDaemon

    calls: list[Any] = []

    class FakeVec:
        async def fetch_ranges(self, spans: Any, zone_id: Any) -> list[Any]:
            calls.append((list(spans), zone_id))
            if fetch_raises:
                raise RuntimeError("boom")
            return list(fetch_rows or [])

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._vector_backend = FakeVec()
    return daemon, calls


def _leg(path: str, score: float, chunk_index: int, text: str) -> Any:
    from nexus.bricks.search.results import BaseSearchResult

    return BaseSearchResult(
        path=path,
        chunk_text=text,
        score=score,
        chunk_index=chunk_index,
        line_start=1,
        line_end=2,
    )


@pytest.mark.asyncio
async def test_hydrate_borrows_best_leg_chunk_no_fetch() -> None:
    """Covered path: borrow the best-scored chunk-leg row; fetch_ranges not called."""
    daemon, calls = _hydration_daemon()
    chunk_kw = [_leg("/t.md", 3.0, 4, "weak"), _leg("/t.md", 9.0, 7, "strong")]
    hits = await daemon._hydrate_title_hits(
        [{"path": "/t.md", "score": 6.0, "title": "T"}],
        chunk_kw=chunk_kw,
        page_kw=[],
        zone_id="root",
    )
    assert calls == []
    assert hits[0].chunk_index == 7
    assert hits[0].chunk_text == "strong"
    assert hits[0].score == 6.0
    assert hits[0].zone_id == "root"


@pytest.mark.asyncio
async def test_hydrate_prefers_page_leg_over_chunk_leg() -> None:
    """Page leg is already best-of-page — wins over the chunk pick."""
    daemon, _ = _hydration_daemon()
    hits = await daemon._hydrate_title_hits(
        [{"path": "/t.md", "score": 6.0, "title": "T"}],
        chunk_kw=[_leg("/t.md", 9.0, 7, "chunk-pick")],
        page_kw=[_leg("/t.md", 8.0, 2, "page-pick")],
        zone_id="root",
    )
    assert hits[0].chunk_index == 2
    assert hits[0].chunk_text == "page-pick"


@pytest.mark.asyncio
async def test_hydrate_uncovered_path_one_batched_fetch() -> None:
    """Uncovered paths hydrate via exactly one batched fetch_ranges call."""
    from nexus.bricks.search.macro_chunk import ChunkRow

    row = ChunkRow(
        path="/u.md",
        chunk_index=0,
        text="chunk zero",
        tokens=2,
        line_start=1,
        line_end=3,
        heading_prefix=None,
    )
    daemon, calls = _hydration_daemon(fetch_rows=[row])
    hits = await daemon._hydrate_title_hits(
        [
            {"path": "/u.md", "score": 6.0, "title": "U"},
            {"path": "/v.md", "score": 4.0, "title": "V"},
        ],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert len(calls) == 1
    assert calls[0] == ([("/u.md", 0, 0), ("/v.md", 0, 0)], "root")
    assert hits[0].chunk_text == "chunk zero"
    assert hits[0].line_start == 1
    # /v.md had no chunk rows (chunkless doc) — still retrievable, empty text.
    assert hits[1].path == "/v.md"
    assert hits[1].chunk_text == ""
    assert hits[1].chunk_index == 0


@pytest.mark.asyncio
async def test_hydrate_fetch_failure_degrades_not_fails() -> None:
    """A hydration fetch failure must never fail the search."""
    daemon, _ = _hydration_daemon(fetch_raises=True)
    hits = await daemon._hydrate_title_hits(
        [{"path": "/u.md", "score": 6.0, "title": "U"}],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert hits[0].chunk_text == ""
    assert hits[0].score == 6.0


@pytest.mark.asyncio
async def test_hydrate_no_fetch_ranges_backend() -> None:
    """Vector backend without fetch_ranges (protocol-minimal) degrades gracefully."""
    from nexus.bricks.search.daemon import SearchDaemon

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._vector_backend = object()
    hits = await daemon._hydrate_title_hits(
        [{"path": "/u.md", "score": 6.0, "title": "U"}],
        chunk_kw=[],
        page_kw=[],
        zone_id="root",
    )
    assert hits[0].chunk_text == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_title_arm.py -v -k hydrate`
Expected: all FAIL with `AttributeError: 'SearchDaemon' object has no attribute '_hydrate_title_hits'`.

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/daemon.py` — insert directly after the `locate()` method body (~line 1547):

```python
    async def _hydrate_title_hits(
        self,
        locate_hits: list[dict[str, Any]],
        *,
        chunk_kw: Sequence[Any],
        page_kw: Sequence[Any],
        zone_id: str,
    ) -> list[BaseSearchResult]:
        """Hydrate locate() hits to page granularity for fusion (Issue #4545).

        locate() returns bare ``{path, score, title}`` rows; the hybrid fusion
        dedup key is ``path:chunk_index``, so each hit needs a representative
        chunk. Borrow the best row already fetched by the keyword legs when
        the path is covered (aligns the key so RRF votes accumulate instead
        of splitting), else batch-fetch chunk 0 via the vector backend's
        NeighborFetcher. Chunkless docs stay retrievable with empty text.
        Best-effort: hydration failures degrade to empty text, never raise.
        """
        best_by_path: dict[str, Any] = {}
        for r in chunk_kw:
            cur = best_by_path.get(r.path)
            if cur is None or r.score > cur.score:
                best_by_path[r.path] = r
        for r in page_kw:
            # Page leg rows are already best-of-page — prefer over chunk pick.
            best_by_path[r.path] = r

        uncovered = [h["path"] for h in locate_hits if h["path"] not in best_by_path]
        fetched: dict[str, Any] = {}
        fetch_ranges = getattr(self._vector_backend, "fetch_ranges", None)
        if uncovered and fetch_ranges is not None:
            try:
                rows = await fetch_ranges([(p, 0, 0) for p in uncovered], zone_id)
                for row in rows:
                    fetched.setdefault(row.path, row)
            except Exception as exc:
                logger.debug("[TITLE-ARM] representative-chunk fetch failed: %s", exc)

        hits: list[BaseSearchResult] = []
        for h in locate_hits:
            path = h["path"]
            leg = best_by_path.get(path)
            if leg is not None:
                hits.append(
                    BaseSearchResult(
                        path=path,
                        chunk_text=leg.chunk_text,
                        score=h["score"],
                        chunk_index=leg.chunk_index,
                        line_start=leg.line_start,
                        line_end=leg.line_end,
                        zone_id=zone_id,
                    )
                )
                continue
            row = fetched.get(path)
            hits.append(
                BaseSearchResult(
                    path=path,
                    chunk_text=row.text if row is not None else "",
                    score=h["score"],
                    chunk_index=row.chunk_index if row is not None else 0,
                    line_start=row.line_start if row is not None else None,
                    line_end=row.line_end if row is not None else None,
                    zone_id=zone_id,
                )
            )
        return hits
```

Check imports at top of `daemon.py`: `Sequence` must be imported from `collections.abc` (add to the existing import if absent) and `BaseSearchResult` from `nexus.bricks.search.results` (already imported — verify).

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_title_arm.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_title_arm.py
git commit -m "feat(search): hydrate locate hits to page granularity

Borrow best leg chunk per covered path (aligns path:chunk_index
fusion key); one batched fetch_ranges for uncovered paths;
chunkless docs emit empty text and stay retrievable.

Refs #4545"
```

---

### Task 4: wire the title arm + sub-fusion swap

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (`_empty_backend_timing` ~line 164; `_search_via_backends` hybrid branch ~lines 2042-2156)
- Modify: `tests/unit/bricks/search/test_daemon_fusion_params.py` (`_make_daemon` harness)
- Test: `tests/unit/bricks/search/test_daemon_title_arm.py`

**Interfaces:**
- Consumes: `_hydrate_title_hits` (Task 3), `config.title_arm` (Task 2), `title_score` plumbing (Task 1), existing `rrf_multi_fusion(result_lists, k, limit, id_key)` from `fusion.py`.
- Produces: hybrid results where locate-matched docs carry `title_score`; `title_ms` timing key. No signature changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/bricks/search/test_daemon_title_arm.py`:

```python
def _make_daemon(*, title_arm: bool = True, skeleton: dict[str, dict[str, Any]] | None = None):
    """Bare SearchDaemon with fake backends + skeleton docs.

    Keyword (BM25): /a.md 10 > /b.md 9 > /c.md 8; dense: /d.md .99 > /c.md .9
    > /a.md .5 (mirrors test_daemon_fusion_params). Skeleton adds a doc whose
    title matches "atlas design doc" but which no body leg returns.
    """
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult
    from nexus.bricks.search.macro_chunk import ChunkRow

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

        async def fetch_ranges(self, spans: Any, zone_id: Any) -> list[Any]:
            return [
                ChunkRow(
                    path=p,
                    chunk_index=0,
                    text=f"body of {p}",
                    tokens=3,
                    line_start=1,
                    line_end=2,
                    heading_prefix=None,
                )
                for p, _lo, _hi in spans
            ]

    async def _embed_query(self: Any, query: str) -> list[float]:
        return [0.1, 0.2]

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    daemon._skeleton_docs = dict(skeleton or {})
    daemon.config = DaemonConfig(page_aggregation=False, title_arm=title_arm)
    return daemon


_ATLAS_SKELETON = {
    "/designs/atlas.md": {
        "path_id": "p-atlas",
        "zone_id": "root",
        "title": "Atlas Design Doc",
        "path_tokens": "designs atlas md",
    }
}


async def _hybrid(daemon: Any, query: str, **kwargs: Any) -> list[Any]:
    return await daemon._search_via_backends(
        query, search_type="hybrid", limit=4, path_filter=None, zone_id="root", **kwargs
    )


@pytest.mark.asyncio
async def test_title_match_doc_enters_hybrid_top_n() -> None:
    """Acceptance (#4545): a doc whose title matches the query but whose body
    chunks are weak (absent from every leg) enters the hybrid top-N with
    title_score set. Without the arm it does not surface."""
    on = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON)
    results = await _hybrid(on, "atlas design doc")
    paths = [r.path for r in results]
    assert "/designs/atlas.md" in paths
    atlas = next(r for r in results if r.path == "/designs/atlas.md")
    # locate score: 3 title-token overlaps * 2.0 + 1 path-token overlap ("atlas")
    assert atlas.title_score == pytest.approx(7.0)
    assert atlas.chunk_text == "body of /designs/atlas.md"  # hydrated chunk 0
    assert "title_ms" in on.last_search_timing

    off = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    assert "/designs/atlas.md" not in [r.path for r in await _hybrid(off, "atlas design doc")]


@pytest.mark.asyncio
async def test_non_title_query_parity_arm_on_vs_off() -> None:
    """Non-title queries are byte-identical with the arm on and off: locate
    has no hits, so the title arm contributes nothing."""
    on = _make_daemon(title_arm=True, skeleton=_ATLAS_SKELETON)
    off = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    r_on = await _hybrid(on, "nexus core")
    r_off = await _hybrid(off, "nexus core")
    assert [r.path for r in r_on] == [r.path for r in r_off]
    assert [r.score for r in r_on] == pytest.approx([r.score for r in r_off])
    assert all(r.title_score is None for r in r_on)


@pytest.mark.asyncio
async def test_flag_off_never_calls_locate() -> None:
    daemon = _make_daemon(title_arm=False, skeleton=_ATLAS_SKELETON)
    called: list[str] = []

    async def _locate(self: Any, q: str, **kwargs: Any) -> list[dict[str, Any]]:
        called.append(q)
        return []

    daemon.locate = MethodType(_locate, daemon)
    await _hybrid(daemon, "atlas design doc")
    assert called == []


def test_rrf_multi_two_arms_matches_rrf_fusion() -> None:
    """Guard for the sub-fusion swap: 3-arm rrf_multi_fusion with an empty
    title arm is rank- and score-identical to the old 2-way rrf_fusion."""
    from nexus.bricks.search.fusion import rrf_fusion, rrf_multi_fusion

    kw = [
        {"path": "/a.md", "chunk_index": 0, "score": 10.0},
        {"path": "/b.md", "chunk_index": 0, "score": 9.0},
        {"path": "/c.md", "chunk_index": 0, "score": 8.0},
    ]
    page = [
        {"path": "/b.md", "chunk_index": 2, "score": 5.0},
        {"path": "/e.md", "chunk_index": 1, "score": 4.0},
    ]
    two = rrf_fusion(kw, page, k=60, limit=8, id_key=None)
    multi = rrf_multi_fusion(
        [("chunk", kw), ("page", page), ("title", [])], k=60, limit=8, id_key=None
    )
    assert [r["path"] for r in multi] == [r["path"] for r in two]
    assert [r["score"] for r in multi] == pytest.approx([r["score"] for r in two])
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_title_arm.py -v`
Expected: `test_rrf_multi_two_arms_matches_rrf_fusion` PASSES already (pure fusion-module guard). The three daemon tests FAIL — atlas absent / title_score None (arm not wired yet).

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/daemon.py`:

(a) `_empty_backend_timing()` — add after `"page_keyword_ms": 0.0,`:

```python
        "title_ms": 0.0,
```

(b) In `_search_via_backends`, extend the fusion import and drop `rrf_fusion` if now unused in this method (check `grep -n "rrf_fusion" src/nexus/bricks/search/daemon.py` — keep the import only if other call sites remain):

```python
        from nexus.bricks.search.fusion import (
            FusionConfig,
            FusionMethod,
            fuse_results,
            rrf_multi_fusion,
        )
```

(c) Replace the sub-fusion block (currently the comment + `kw_fused = rrf_fusion(...)` at ~lines 2147-2150) with:

```python
        # Skeleton title arm (Issue #4545): locate() over the in-memory
        # path+title index, hydrated to page granularity so its fusion key
        # (path:chunk_index) lines up with the keyword legs.
        title_hits: list[Any] = []
        if self.config.title_arm:
            title_start = time.perf_counter()
            locate_hits = await self.locate(
                query, zone_id=zone_id, limit=limit * 2, path_prefix=path_filter
            )
            if locate_hits:
                title_hits = await self._hydrate_title_hits(
                    locate_hits, chunk_kw=chunk_kw, page_kw=page_kw, zone_id=zone_id
                )
            timing["title_ms"] = (time.perf_counter() - title_start) * 1000

        # Fuse the keyword-side arms first (chunk + page + title) with plain
        # RRF, then fuse that with dense using the request's method / alpha /
        # k (#4541). The title arm is listed last so leg rows win the
        # first-seen base copy (richer line/offset fields).
        fusion_start = time.perf_counter()
        kw_fused = rrf_multi_fusion(
            [("chunk", chunk_kw), ("page", page_kw), ("title", title_hits)],
            k=rrf_k,
            limit=limit * 2,
            id_key=None,
        )
```

(d) Update the `_search_via_backends` docstring sentence "Hybrid mode fuses two stages: the keyword legs first (3-way on Postgres: chunk-BM25 + page-BM25; 2-way on SQLite …)" to mention the title arm, e.g.:

```
        Hybrid mode fuses two stages: the keyword-side arms first (chunk-BM25
        + page-BM25 + skeleton title arm on Postgres; SQLite has no page leg),
        then keyword × dense. The keyword sub-fusion is always plain RRF; the
        final stage honours the request's ``fusion_method`` / ``alpha`` /
        ``rrf_k`` (Issue #4541). The title arm (Issue #4545) runs locate()
        over the in-memory skeleton index and is gated by config.title_arm.
```

(e) `tests/unit/bricks/search/test_daemon_fusion_params.py` `_make_daemon` — the bare `__new__` harness lacks `_skeleton_docs`; the title arm (default on) would crash it. Add after `daemon._vector_backend = FakeVectorBackend()`:

```python
    # Title arm (#4545) is on by default; bare __new__ skips __init__, so
    # give locate() an empty skeleton index (no hits → fusion unchanged).
    daemon._skeleton_docs = {}
```

- [ ] **Step 4: Run the full search unit suite**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/ -v`
Expected: ALL PASS — including untouched `test_daemon_fusion_params.py` (empty title arm is rank/score-identical: the parity guard proves it) and `test_final_list_page_pooling.py` / `test_page_aggregation.py`.

Note: `test_default_fusion_matches_legacy_two_stage_rrf` computes its expected ordering with `rrf_fusion` — it must still pass unmodified. If it fails, the swap broke parity; fix the swap, do not edit that test.

- [ ] **Step 5: Run integration fusion tests**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_fusion.py tests/integration/bricks/search/test_rrf_bonus.py -v`
Expected: ALL PASS (these exercise `fusion.py` directly; no changes there).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/daemon.py \
  tests/unit/bricks/search/test_daemon_title_arm.py \
  tests/unit/bricks/search/test_daemon_fusion_params.py
git commit -m "feat(search): fold skeleton title arm into hybrid fusion

locate() joins the keyword sub-fusion as a third rrf_multi_fusion
arm; #4541 final-stage knobs untouched. Empty-arm case is rank-
identical to the old 2-way rrf_fusion (guarded by parity test).
Side effect: page-only hits no longer carry a spurious vector_score
from the sub-fusion stage.

Closes #4545"
```

---

### Task 5: surface regen, spec typo fix, full verification

**Files:**
- Modify: `docs/surface-coverage/api-rpc-surface-coverage.yaml` (regenerated)
- Modify: `docs/superpowers/specs/2026-07-31-title-arm-hybrid-fusion-design.md` (name typo)

**Interfaces:**
- Consumes: everything above.
- Produces: green suite + synced surface doc; branch ready for PR.

- [ ] **Step 1: Fix the spec typo**

In `docs/superpowers/specs/2026-07-31-title-arm-hybrid-fusion-design.md`, replace `SearchDaemonConfig.title_arm` with `DaemonConfig.title_arm` (component-changes table).

- [ ] **Step 2: Regenerate surface coverage**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python scripts/gen_api_surface_coverage.py`
Then: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python scripts/validate_api_surface_coverage.py`
Expected: yaml updated (router line-number sync from the `_serialize_search_result` edit); validation clean. If the generator needs flags, check `--help` — mirror the invocation used for commit `dcbf21ff3`.

- [ ] **Step 3: Full test sweep**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/ tests/integration/bricks/search/test_fusion.py tests/integration/bricks/search/test_rrf_bonus.py -q`
Expected: ALL PASS, no skips introduced by this work.

- [ ] **Step 4: Commit**

```bash
git add docs/surface-coverage/api-rpc-surface-coverage.yaml \
  docs/superpowers/specs/2026-07-31-title-arm-hybrid-fusion-design.md
git commit -m "chore(surface): regen coverage after search router edit

title_score field added to search result serialization (#4545);
fix DaemonConfig name typo in the design spec.

Refs #4545"
```

---

## Verification checklist (maps to issue #4545 acceptance)

- [ ] Title-match doc with weak body chunks enters hybrid top-N → `test_title_match_doc_enters_hybrid_top_n`
- [ ] Non-title queries unchanged within tolerance → `test_non_title_query_parity_arm_on_vs_off` + `test_rrf_multi_two_arms_matches_rrf_fusion` + untouched `test_daemon_fusion_params.py` staying green
- [ ] Attribution field marking title-arm participation → `title_score` (serialize tests + acceptance test)
- [ ] Zero schema change, no reindex → no migration files, no indexer edits
