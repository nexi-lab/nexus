# Issue #3773 — RRF Top-Rank Bonus + Path Context Descriptions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two search-quality improvements inspired by QMD: a top-rank bonus in RRF fusion (prevents dilution of perfect matches), and admin-managed path-context descriptions attached to search results (gives LLM agents relevance signal without reading files).

**Architecture:** RRF changes are a small in-place modification of three fusion functions in `src/nexus/bricks/search/fusion.py`. Path contexts add a new Alembic-migrated `path_contexts` table, a zone-scoped async store + in-memory longest-prefix cache in a new `src/nexus/bricks/search/path_context.py`, a new `/api/v2/path-contexts` router, a daemon attach step after final result assembly, and a `context` field on `BaseSearchResult`.

**Tech Stack:** Python 3.12+, SQLAlchemy async, Alembic, FastAPI, Pydantic v2, pytest.

**Spec:** `docs/superpowers/specs/2026-04-16-issue-3773-rrf-bonus-path-contexts-design.md`

---

## File Structure

**Create:**
- `src/nexus/bricks/search/path_context.py` — `PathContextRecord`, `PathContextStore`, `PathContextCache`.
- `src/nexus/server/api/v2/routers/path_contexts.py` — FastAPI router.
- `alembic/versions/add_path_contexts_table.py` — table migration.
- `tests/integration/bricks/search/test_rrf_bonus.py` — RRF top-rank bonus tests.
- `tests/integration/bricks/search/test_path_context.py` — store + cache tests.
- `tests/integration/server/api/v2/routers/test_path_contexts_router.py` — router tests.
- `tests/integration/bricks/search/test_daemon_context_attach.py` — daemon + serializer E2E.

**Modify:**
- `src/nexus/bricks/search/fusion.py` — add bonus constants, modify three RRF functions, extend `FusionConfig`.
- `src/nexus/bricks/search/results.py` — add `context: str | None = None` to `BaseSearchResult`.
- `src/nexus/bricks/search/daemon.py` — inject cache, attach context after result assembly.
- `src/nexus/server/api/v2/routers/search.py` — emit `context` in `_serialize_search_result`.
- `src/nexus/server/api/v2/versioning.py` — register new router.
- `src/nexus/server/lifespan/search.py` — construct store + cache on startup, inject into daemon.

---

## Task 1: RRF bonus — constants, `FusionConfig` flag, apply to `rrf_fusion`

**Files:**
- Modify: `src/nexus/bricks/search/fusion.py` (constants, `FusionConfig`, `rrf_fusion`)
- Create: `tests/integration/bricks/search/test_rrf_bonus.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/bricks/search/test_rrf_bonus.py`:

```python
"""Tests for RRF top-rank bonus (Issue #3773).

Verifies that documents ranked #1 in any input list get a +0.05 bonus
and those ranked #2-3 get +0.02, preventing dilution of perfect matches
across multi-source / query-expanded fusion.
"""

from nexus.bricks.search.fusion import (
    RRF_TOP1_BONUS,
    RRF_TOP3_BONUS,
    FusionConfig,
    FusionMethod,
    fuse_results,
    rrf_fusion,
    rrf_multi_fusion,
    rrf_weighted_fusion,
)


class TestRrfTop1Bonus:
    def test_top1_keyword_only_beats_mediocre_both(self) -> None:
        """Issue #3773 scenario: #1 in keyword but absent from vector
        beats #3 in both without the bonus."""
        kw = [
            {"path": "perfect.txt", "chunk_index": 0, "score": 10.0},  # rank 1
            {"path": "x.txt", "chunk_index": 0, "score": 1.0},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},  # rank 3
        ]
        vec = [
            {"path": "y.txt", "chunk_index": 0, "score": 0.9},
            {"path": "z.txt", "chunk_index": 0, "score": 0.8},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},  # rank 3
        ]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        ranked_paths = [r["path"] for r in results]
        assert ranked_paths.index("perfect.txt") < ranked_paths.index("mediocre.txt")

    def test_bonus_disabled_preserves_legacy_behavior(self) -> None:
        kw = [
            {"path": "perfect.txt", "chunk_index": 0, "score": 10.0},
            {"path": "x.txt", "chunk_index": 0, "score": 1.0},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},
        ]
        vec = [
            {"path": "y.txt", "chunk_index": 0, "score": 0.9},
            {"path": "z.txt", "chunk_index": 0, "score": 0.8},
            {"path": "mediocre.txt", "chunk_index": 0, "score": 0.5},
        ]
        results = rrf_fusion(
            kw, vec, k=60, limit=10, id_key=None, top_rank_bonus=False
        )
        ranked_paths = [r["path"] for r in results]
        # Without bonus, mediocre (in both) outranks perfect (single list).
        assert ranked_paths.index("mediocre.txt") < ranked_paths.index("perfect.txt")

    def test_rank1_receives_top1_bonus(self) -> None:
        """Single-doc fusion: score == 2 * 1/(k+1) + RRF_TOP1_BONUS."""
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        assert len(results) == 1
        expected = (1.0 / 61) + (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_rank3_receives_top3_bonus(self) -> None:
        """Doc at rank 3 in keyword only: 1/(k+3) + RRF_TOP3_BONUS."""
        kw = [
            {"path": "a.txt", "chunk_index": 0, "score": 1.0},
            {"path": "b.txt", "chunk_index": 0, "score": 1.0},
            {"path": "c.txt", "chunk_index": 0, "score": 1.0},
        ]
        vec: list[dict] = []
        results = rrf_fusion(kw, vec, k=60, limit=10, id_key=None)
        c_result = next(r for r in results if r["path"] == "c.txt")
        expected = (1.0 / 63) + RRF_TOP3_BONUS
        assert abs(c_result["score"] - expected) < 1e-9

    def test_rank4_receives_no_bonus(self) -> None:
        kw = [
            {"path": f"r{i}.txt", "chunk_index": 0, "score": 1.0} for i in range(5)
        ]
        results = rrf_fusion(kw, [], k=60, limit=10, id_key=None)
        r4 = next(r for r in results if r["path"] == "r3.txt")  # zero-indexed -> rank 4
        expected = 1.0 / 64
        assert abs(r4["score"] - expected) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py -v`
Expected: FAIL with `ImportError: cannot import name 'RRF_TOP1_BONUS' from 'nexus.bricks.search.fusion'`.

- [ ] **Step 3: Add constants + `FusionConfig` flag + modify `rrf_fusion`**

Edit `src/nexus/bricks/search/fusion.py`.

After the existing imports (after line 22), add module constants:

```python
# Issue #3773: Top-rank bonus preserves high-confidence matches
# against dilution from query expansion and multi-source fusion.
RRF_TOP1_BONUS = 0.05
RRF_TOP3_BONUS = 0.02
```

In `FusionConfig` dataclass (around line 44), add a new field:

```python
top_rank_bonus: bool = True  # Issue #3773: boost docs ranked #1-3 in any list
```

So the final `FusionConfig` reads:

```python
@dataclass
class FusionConfig:
    method: FusionMethod = FusionMethod.RRF
    alpha: float = 0.5
    rrf_k: int = 60
    normalize_scores: bool = True
    over_fetch_factor: float = 3.0
    top_rank_bonus: bool = True  # Issue #3773
```

Replace `rrf_fusion` (currently lines 112-169) with:

```python
def rrf_fusion(
    keyword_results: Sequence[dict[str, Any] | Any],
    vector_results: Sequence[dict[str, Any] | Any],
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
    top_rank_bonus: bool = True,
) -> list[dict[str, Any]]:
    """Combine results using Reciprocal Rank Fusion.

    RRF score = sum(1 / (k + rank)) for each result list, plus an optional
    top-rank bonus (Issue #3773): +0.05 for docs ranked #1 in any list and
    +0.02 for docs ranked #2-3. The bonus preserves high-confidence matches
    against dilution from query expansion and multi-source fusion.

    Args:
        keyword_results: Results from keyword search (ranked by BM25)
        vector_results: Results from vector search (ranked by similarity)
        k: RRF constant (default: 60, per original paper)
        limit: Maximum results to return
        id_key: Key for identifying unique results, or None for path:chunk_index
        top_rank_bonus: Apply top-rank bonus (Issue #3773). Default True.

    Returns:
        Combined results ranked by RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}

    # Add keyword results
    for rank, raw_result in enumerate(keyword_results, start=1):
        result = _to_dict(raw_result)
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
        rrf_scores[key]["result"]["keyword_score"] = result.get("score", 0.0)
        best_rank[key] = min(best_rank.get(key, rank), rank)

    # Add vector results
    for rank, raw_result in enumerate(vector_results, start=1):
        result = _to_dict(raw_result)
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
        rrf_scores[key]["result"]["vector_score"] = result.get("score", 0.0)
        best_rank[key] = min(best_rank.get(key, rank), rank)

    # Issue #3773: apply top-rank bonus before final sort
    if top_rank_bonus:
        for key, entry in rrf_scores.items():
            br = best_rank.get(key, 999)
            if br == 1:
                entry["rrf_score"] += RRF_TOP1_BONUS
            elif br <= 3:
                entry["rrf_score"] += RRF_TOP3_BONUS

    # Sort by RRF score
    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    # Update final scores
    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py -v -k "not weighted and not multi and not fuse_results"`
Expected: all tests in `TestRrfTop1Bonus` PASS.

Also run the existing fusion test suite to check for golden-ordering regressions:

Run: `pytest tests/integration/bricks/search/test_fusion.py -v`
Expected: any failures are golden-ordering shifts caused by the bonus. If a test fails purely because rank ordering flipped (not a logic error), update the expected order inline and re-run. If a test fails with an arithmetic comparison (e.g. asserting an exact float score), update the expected value to `old + RRF_TOPN_BONUS` and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/fusion.py \
        tests/integration/bricks/search/test_rrf_bonus.py \
        tests/integration/bricks/search/test_fusion.py
git commit -m "feat(#3773): rrf_fusion top-rank bonus (+0.05 / +0.02)"
```

---

## Task 2: RRF bonus — apply to `rrf_weighted_fusion`

**Files:**
- Modify: `src/nexus/bricks/search/fusion.py` (`rrf_weighted_fusion`)
- Modify: `tests/integration/bricks/search/test_rrf_bonus.py` (add weighted tests)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/bricks/search/test_rrf_bonus.py`:

```python
class TestRrfWeightedBonus:
    def test_weighted_top1_gets_bonus(self) -> None:
        """rrf_weighted_fusion also applies the top-rank bonus."""
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(kw, vec, alpha=0.5, k=60, limit=10, id_key=None)
        # alpha=0.5, rank=1 in both: 0.5*(1/61) + 0.5*(1/61) + RRF_TOP1_BONUS
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_weighted_bonus_disabled(self) -> None:
        kw = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "only.txt", "chunk_index": 0, "score": 1.0}]
        results = rrf_weighted_fusion(
            kw, vec, alpha=0.5, k=60, limit=10, id_key=None, top_rank_bonus=False
        )
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py::TestRrfWeightedBonus -v`
Expected: FAIL — `rrf_weighted_fusion` has no `top_rank_bonus` parameter and does not apply the bonus, so the first test asserts a score higher than produced.

- [ ] **Step 3: Modify `rrf_weighted_fusion`**

Replace `rrf_weighted_fusion` (currently lines 253-310 in `src/nexus/bricks/search/fusion.py`) with:

```python
def rrf_weighted_fusion(
    keyword_results: Sequence[dict[str, Any] | Any],
    vector_results: Sequence[dict[str, Any] | Any],
    alpha: float = 0.5,
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
    top_rank_bonus: bool = True,
) -> list[dict[str, Any]]:
    """Combine results using RRF with alpha weighting.

    RRF score = (1 - alpha) * (1/(k+keyword_rank)) + alpha * (1/(k+vector_rank))
                + top-rank bonus (Issue #3773)

    Args:
        keyword_results: Results from keyword search
        vector_results: Results from vector search
        alpha: Weight for vector contribution (0.0 = all BM25, 1.0 = all vector)
        k: RRF constant
        limit: Maximum results to return
        id_key: Key for identifying unique results
        top_rank_bonus: Apply top-rank bonus (Issue #3773). Default True.

    Returns:
        Combined results ranked by weighted RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}

    # Add keyword results with (1 - alpha) weight
    for rank, raw_result in enumerate(keyword_results, start=1):
        result = _to_dict(raw_result)
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += (1 - alpha) * (1.0 / (k + rank))
        rrf_scores[key]["result"]["keyword_score"] = result.get("score", 0.0)
        best_rank[key] = min(best_rank.get(key, rank), rank)

    # Add vector results with alpha weight
    for rank, raw_result in enumerate(vector_results, start=1):
        result = _to_dict(raw_result)
        key = _get_result_key(result, id_key)
        if key not in rrf_scores:
            rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
        rrf_scores[key]["rrf_score"] += alpha * (1.0 / (k + rank))
        rrf_scores[key]["result"]["vector_score"] = result.get("score", 0.0)
        best_rank[key] = min(best_rank.get(key, rank), rank)

    if top_rank_bonus:
        for key, entry in rrf_scores.items():
            br = best_rank.get(key, 999)
            if br == 1:
                entry["rrf_score"] += RRF_TOP1_BONUS
            elif br <= 3:
                entry["rrf_score"] += RRF_TOP3_BONUS

    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py::TestRrfWeightedBonus -v`
Expected: PASS.

Also re-run the legacy fusion test suite:

Run: `pytest tests/integration/bricks/search/test_fusion.py -v`
Expected: PASS. Update any golden orderings that flipped (see Task 1 Step 4 note).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/fusion.py \
        tests/integration/bricks/search/test_rrf_bonus.py \
        tests/integration/bricks/search/test_fusion.py
git commit -m "feat(#3773): rrf_weighted_fusion top-rank bonus"
```

---

## Task 3: RRF bonus — apply to `rrf_multi_fusion`

**Files:**
- Modify: `src/nexus/bricks/search/fusion.py` (`rrf_multi_fusion`)
- Modify: `tests/integration/bricks/search/test_rrf_bonus.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/bricks/search/test_rrf_bonus.py`:

```python
class TestRrfMultiBonus:
    def test_multi_top1_gets_bonus_from_any_source(self) -> None:
        """rrf_multi_fusion: best rank across all sources drives bonus."""
        lists = [
            (
                "keyword",
                [
                    {"path": "perfect.txt", "chunk_index": 0, "score": 1.0},
                    {"path": "other.txt", "chunk_index": 0, "score": 1.0},
                ],
            ),
            (
                "vector",
                [
                    {"path": "unrelated.txt", "chunk_index": 0, "score": 1.0},
                    {"path": "other.txt", "chunk_index": 0, "score": 1.0},
                ],
            ),
            (
                "splade",
                [{"path": "perfect.txt", "chunk_index": 0, "score": 1.0}],
            ),
        ]
        results = rrf_multi_fusion(lists, k=60, limit=10, id_key=None)
        # "perfect.txt" is rank 1 in keyword + splade -> gets TOP1 bonus.
        perfect = next(r for r in results if r["path"] == "perfect.txt")
        expected = (1.0 / 61) + (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(perfect["score"] - expected) < 1e-9

    def test_multi_bonus_disabled(self) -> None:
        lists = [
            ("keyword", [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]),
            ("vector", [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]),
        ]
        results = rrf_multi_fusion(
            lists, k=60, limit=10, id_key=None, top_rank_bonus=False
        )
        expected = 2 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py::TestRrfMultiBonus -v`
Expected: FAIL — `rrf_multi_fusion` has no `top_rank_bonus` parameter.

- [ ] **Step 3: Modify `rrf_multi_fusion`**

Replace `rrf_multi_fusion` (currently lines 313-357 in `src/nexus/bricks/search/fusion.py`) with:

```python
def rrf_multi_fusion(
    result_lists: Sequence[tuple[str, Sequence[Any]]],
    k: int = 60,
    limit: int = 10,
    id_key: str | None = "chunk_id",
    top_rank_bonus: bool = True,
) -> list[dict[str, Any]]:
    """N-way Reciprocal Rank Fusion for combining 3+ retrieval sources.

    Generalizes RRF from 2-way to N-way for pipelines that combine
    keyword + dense + SPLADE (or any number of retrievers). Applies the
    top-rank bonus (Issue #3773) using the best rank across all sources.

    Args:
        result_lists: List of (source_name, results) tuples.
            source_name is used to set '{source_name}_score' on each result.
        k: RRF constant (default: 60)
        limit: Maximum results to return
        id_key: Key for identifying unique results, or None for path:chunk_index
        top_rank_bonus: Apply top-rank bonus (Issue #3773). Default True.

    Returns:
        Combined results ranked by RRF score
    """
    rrf_scores: dict[str, dict[str, Any]] = {}
    best_rank: dict[str, int] = {}

    for source_name, results in result_lists:
        score_key = f"{source_name}_score"
        for rank, raw_result in enumerate(results, start=1):
            result = _to_dict(raw_result)
            key = _get_result_key(result, id_key)
            if key not in rrf_scores:
                rrf_scores[key] = {"result": result.copy(), "rrf_score": 0.0}
            rrf_scores[key]["rrf_score"] += 1.0 / (k + rank)
            rrf_scores[key]["result"][score_key] = result.get("score", 0.0)
            best_rank[key] = min(best_rank.get(key, rank), rank)

    if top_rank_bonus:
        for key, entry in rrf_scores.items():
            br = best_rank.get(key, 999)
            if br == 1:
                entry["rrf_score"] += RRF_TOP1_BONUS
            elif br <= 3:
                entry["rrf_score"] += RRF_TOP3_BONUS

    sorted_results = sorted(
        rrf_scores.values(),
        key=lambda x: x["rrf_score"],
        reverse=True,
    )[:limit]

    for item in sorted_results:
        item["result"]["score"] = item["rrf_score"]

    return [item["result"] for item in sorted_results]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py::TestRrfMultiBonus -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/fusion.py tests/integration/bricks/search/test_rrf_bonus.py
git commit -m "feat(#3773): rrf_multi_fusion top-rank bonus"
```

---

## Task 4: RRF bonus — wire `FusionConfig.top_rank_bonus` through `fuse_results`

**Files:**
- Modify: `src/nexus/bricks/search/fusion.py` (`fuse_results`)
- Modify: `tests/integration/bricks/search/test_rrf_bonus.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/bricks/search/test_rrf_bonus.py`:

```python
class TestFuseResultsConfigFlag:
    def test_fuse_results_passes_top_rank_bonus_false(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        config = FusionConfig(method=FusionMethod.RRF, top_rank_bonus=False)
        results = fuse_results(kw, vec, config=config, limit=10, id_key=None)
        expected = 2 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_fuse_results_default_applies_bonus(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        results = fuse_results(kw, vec, config=None, limit=10, id_key=None)
        expected = 2 * (1.0 / 61) + RRF_TOP1_BONUS
        assert abs(results[0]["score"] - expected) < 1e-9

    def test_fuse_results_weighted_respects_flag(self) -> None:
        kw = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        vec = [{"path": "a.txt", "chunk_index": 0, "score": 1.0}]
        config = FusionConfig(method=FusionMethod.RRF_WEIGHTED, top_rank_bonus=False)
        results = fuse_results(kw, vec, config=config, limit=10, id_key=None)
        expected = 0.5 * (1.0 / 61) + 0.5 * (1.0 / 61)
        assert abs(results[0]["score"] - expected) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py::TestFuseResultsConfigFlag -v`
Expected: FAIL — `fuse_results` does not yet forward `top_rank_bonus` to the underlying RRF functions.

- [ ] **Step 3: Modify `fuse_results`**

Replace `fuse_results` (currently lines 360-422 in `src/nexus/bricks/search/fusion.py`) with:

```python
def fuse_results(
    keyword_results: Sequence[dict[str, Any] | Any],
    vector_results: Sequence[dict[str, Any] | Any],
    config: FusionConfig | None = None,
    limit: int = 10,
    id_key: str | None = "chunk_id",
) -> list[dict[str, Any]]:
    """Fuse keyword and vector search results using configured method.

    This is the main entry point for hybrid search fusion. It dispatches
    to the appropriate fusion algorithm based on the configuration.

    Accepts both dict results and BaseSearchResult dataclass instances
    (Issue #1520).

    Args:
        keyword_results: Results from keyword/BM25 search
        vector_results: Results from vector/semantic search
        config: Fusion configuration (defaults to RRF with k=60)
        limit: Maximum results to return
        id_key: Key for identifying unique results

    Returns:
        Combined results ranked by fusion score

    Raises:
        ValueError: If an unknown fusion method is specified
    """
    if config is None:
        config = FusionConfig()

    if config.method == FusionMethod.RRF:
        return rrf_fusion(
            keyword_results,
            vector_results,
            k=config.rrf_k,
            limit=limit,
            id_key=id_key,
            top_rank_bonus=config.top_rank_bonus,
        )
    elif config.method == FusionMethod.WEIGHTED:
        return weighted_fusion(
            keyword_results,
            vector_results,
            alpha=config.alpha,
            normalize=config.normalize_scores,
            limit=limit,
            id_key=id_key,
        )
    elif config.method == FusionMethod.RRF_WEIGHTED:
        return rrf_weighted_fusion(
            keyword_results,
            vector_results,
            alpha=config.alpha,
            k=config.rrf_k,
            limit=limit,
            id_key=id_key,
            top_rank_bonus=config.top_rank_bonus,
        )
    else:
        raise ValueError(f"Unknown fusion method: {config.method}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_rrf_bonus.py -v`
Expected: all tests PASS.

Run full fusion test suite: `pytest tests/integration/bricks/search/test_fusion.py tests/integration/bricks/search/test_rrf_bonus.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/fusion.py tests/integration/bricks/search/test_rrf_bonus.py
git commit -m "feat(#3773): wire FusionConfig.top_rank_bonus through fuse_results"
```

---

## Task 5: Path contexts — Alembic migration (`path_contexts` table)

**Files:**
- Create: `alembic/versions/add_path_contexts_table.py`

- [ ] **Step 1: Discover the current Alembic head revision**

Run: `cd <repo-root> && alembic heads`

The repo has an `alembic.ini` at the root. If the command works, note the revision ID printed (e.g. `abc123def456`). If the command fails with "No script_location", use the helper: `python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; cfg = Config('alembic.ini'); print([h for h in ScriptDirectory.from_config(cfg).get_heads()])"`. Record the head.

Call the discovered head revision `<CURRENT_HEAD>`. If there are multiple heads, merge them first using `alembic merge heads -m "merge before path contexts"` before proceeding.

- [ ] **Step 2: Create the migration file**

Create `alembic/versions/add_path_contexts_table.py`:

```python
"""Add path_contexts table (Issue #3773).

Creates the path_contexts table used to attach admin-configured, zone-scoped
human-readable descriptions to search result paths via longest-prefix match.

Revision ID: add_path_contexts_table
Revises: <CURRENT_HEAD>
Create Date: 2026-04-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_path_contexts_table"
down_revision: Union[str, Sequence[str], None] = "<CURRENT_HEAD>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create path_contexts table."""
    op.create_table(
        "path_contexts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "zone_id",
            sa.String(255),
            nullable=False,
            server_default="root",
        ),
        sa.Column("path_prefix", sa.String(1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "zone_id",
            "path_prefix",
            name="uq_path_contexts_zone_prefix",
        ),
    )
    op.create_index(
        "ix_path_contexts_zone_updated",
        "path_contexts",
        ["zone_id", "updated_at"],
    )


def downgrade() -> None:
    """Remove path_contexts table."""
    op.drop_index("ix_path_contexts_zone_updated", table_name="path_contexts")
    op.drop_table("path_contexts")
```

Replace the literal string `<CURRENT_HEAD>` in both `down_revision` and the docstring with the revision ID recorded in Step 1.

- [ ] **Step 3: Run migration upgrade against SQLite (local)**

Run: `cd <repo-root> && alembic upgrade head`
Expected: output includes `Running upgrade <CURRENT_HEAD> -> add_path_contexts_table, Add path_contexts table`.

- [ ] **Step 4: Verify the table exists and downgrade works**

Run: `cd <repo-root> && sqlite3 <repo-root>/<dev.db> ".schema path_contexts"` (use the dev DB the local env uses; if unsure, `echo $NEXUS_DATABASE_URL`).
Expected: prints `CREATE TABLE path_contexts (...)` with the columns above.

Run: `cd <repo-root> && alembic downgrade -1 && alembic upgrade head`
Expected: the table drops and re-creates without errors.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/add_path_contexts_table.py
git commit -m "feat(#3773): alembic migration — path_contexts table"
```

---

## Task 6: Path contexts — `PathContextRecord` + `PathContextStore` (CRUD + freshness)

**Files:**
- Create: `src/nexus/bricks/search/path_context.py`
- Create: `tests/integration/bricks/search/test_path_context.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/bricks/search/test_path_context.py`:

```python
"""Tests for path_contexts store and cache (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import (
    PathContextCache,
    PathContextRecord,
    PathContextStore,
)

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def async_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def store(async_session_factory):
    return PathContextStore(async_session_factory=async_session_factory, db_type="sqlite")


class TestPathContextStoreUpsert:
    @pytest.mark.asyncio
    async def test_insert_then_read(self, store: PathContextStore) -> None:
        await store.upsert("root", "src/nexus/bricks/search", "Hybrid search brick")
        records = await store.list("root")
        assert len(records) == 1
        assert records[0].zone_id == "root"
        assert records[0].path_prefix == "src/nexus/bricks/search"
        assert records[0].description == "Hybrid search brick"

    @pytest.mark.asyncio
    async def test_upsert_replaces_description(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        await store.upsert("root", "src", "second")
        records = await store.list("root")
        assert len(records) == 1
        assert records[0].description == "second"

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_removed(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        assert await store.delete("root", "src") is True
        assert await store.list("root") == []

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, store: PathContextStore) -> None:
        assert await store.delete("root", "nonexistent") is False

    @pytest.mark.asyncio
    async def test_zones_are_isolated(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root desc")
        await store.upsert("other", "src", "other desc")
        root = await store.list("root")
        other = await store.list("other")
        assert len(root) == 1 and root[0].description == "root desc"
        assert len(other) == 1 and other[0].description == "other desc"

    @pytest.mark.asyncio
    async def test_list_all_zones(self, store: PathContextStore) -> None:
        await store.upsert("root", "a", "a")
        await store.upsert("other", "b", "b")
        records = await store.list(zone_id=None)
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_max_updated_at_tracks_writes(self, store: PathContextStore) -> None:
        assert await store.max_updated_at("root") is None
        await store.upsert("root", "src", "first")
        stamp1 = await store.max_updated_at("root")
        assert stamp1 is not None
        await store.upsert("root", "src", "second")
        stamp2 = await store.max_updated_at("root")
        assert stamp2 is not None
        assert stamp2 >= stamp1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_path_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.bricks.search.path_context'`.

- [ ] **Step 3: Create `src/nexus/bricks/search/path_context.py` (store only — cache lives in Task 7)**

```python
"""Path context descriptions (Issue #3773).

Stores admin-configured, zone-scoped mappings from path prefix to human-readable
description. Used by the search daemon to attach a ``context`` field to each
search result via longest-prefix match. In-memory cache is in this module too.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text

from nexus.contracts.constants import ROOT_ZONE_ID


@dataclass(frozen=True)
class PathContextRecord:
    """One row in the path_contexts table."""

    zone_id: str
    path_prefix: str
    description: str
    created_at: datetime
    updated_at: datetime


class PathContextStore:
    """Async CRUD for the path_contexts table.

    Follows the raw-SQL pattern used by ChunkStore (src/nexus/bricks/search/chunk_store.py).
    """

    def __init__(self, *, async_session_factory: Any, db_type: str = "sqlite") -> None:
        self._async_session_factory = async_session_factory
        self._db_type = db_type

    async def upsert(
        self, zone_id: str, path_prefix: str, description: str
    ) -> None:
        """Insert or replace a context row. updated_at refreshed on replace."""
        now = datetime.utcnow()
        async with self._async_session_factory() as session:
            if self._db_type == "postgresql":
                await session.execute(
                    text(
                        """
                        INSERT INTO path_contexts
                            (zone_id, path_prefix, description, created_at, updated_at)
                        VALUES
                            (:zone_id, :path_prefix, :description, :now, :now)
                        ON CONFLICT (zone_id, path_prefix) DO UPDATE
                        SET description = EXCLUDED.description,
                            updated_at  = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "zone_id": zone_id,
                        "path_prefix": path_prefix,
                        "description": description,
                        "now": now,
                    },
                )
            else:
                # SQLite: INSERT OR REPLACE (loses created_at on replace, acceptable).
                await session.execute(
                    text(
                        """
                        INSERT OR REPLACE INTO path_contexts
                            (zone_id, path_prefix, description, created_at, updated_at)
                        VALUES
                            (:zone_id, :path_prefix, :description,
                             COALESCE(
                                (SELECT created_at FROM path_contexts
                                 WHERE zone_id = :zone_id AND path_prefix = :path_prefix),
                                :now),
                             :now)
                        """
                    ),
                    {
                        "zone_id": zone_id,
                        "path_prefix": path_prefix,
                        "description": description,
                        "now": now,
                    },
                )
            await session.commit()

    async def delete(self, zone_id: str, path_prefix: str) -> bool:
        """Delete one row. Returns True if a row was removed."""
        async with self._async_session_factory() as session:
            result = await session.execute(
                text(
                    "DELETE FROM path_contexts "
                    "WHERE zone_id = :zone_id AND path_prefix = :path_prefix"
                ),
                {"zone_id": zone_id, "path_prefix": path_prefix},
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def list(self, zone_id: str | None = None) -> list[PathContextRecord]:
        """List contexts. When zone_id is None, returns rows for all zones."""
        query = (
            "SELECT zone_id, path_prefix, description, created_at, updated_at "
            "FROM path_contexts"
        )
        params: dict[str, Any] = {}
        if zone_id is not None:
            query += " WHERE zone_id = :zone_id"
            params["zone_id"] = zone_id
        query += " ORDER BY zone_id, path_prefix"
        async with self._async_session_factory() as session:
            rows = (await session.execute(text(query), params)).all()
        return [
            PathContextRecord(
                zone_id=row[0],
                path_prefix=row[1],
                description=row[2],
                created_at=row[3],
                updated_at=row[4],
            )
            for row in rows
        ]

    async def max_updated_at(self, zone_id: str) -> datetime | None:
        """Return the max updated_at for a zone, or None if empty."""
        async with self._async_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT MAX(updated_at) FROM path_contexts "
                        "WHERE zone_id = :zone_id"
                    ),
                    {"zone_id": zone_id},
                )
            ).scalar()
        return row

    async def load_all_for_zone(self, zone_id: str) -> list[PathContextRecord]:
        """Load every context row for one zone."""
        return await self.list(zone_id=zone_id)


# Issue #3773: PathContextCache is defined in Task 7.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_path_context.py::TestPathContextStoreUpsert -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/path_context.py \
        tests/integration/bricks/search/test_path_context.py
git commit -m "feat(#3773): PathContextStore CRUD + max_updated_at"
```

---

## Task 7: Path contexts — `PathContextCache` (longest-prefix match + freshness)

**Files:**
- Modify: `src/nexus/bricks/search/path_context.py` (add `PathContextCache`)
- Modify: `tests/integration/bricks/search/test_path_context.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/bricks/search/test_path_context.py`:

```python
class TestPathContextCacheLookup:
    @pytest.mark.asyncio
    async def test_longest_prefix_wins(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "top-level src")
        await store.upsert("root", "src/nexus/bricks/search", "search brick")
        cache = PathContextCache(store=store)
        desc = await cache.lookup("root", "src/nexus/bricks/search/fusion.py")
        assert desc == "search brick"

    @pytest.mark.asyncio
    async def test_empty_prefix_matches_any(self, store: PathContextStore) -> None:
        await store.upsert("root", "", "zone root fallback")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "anything/x.py") == "zone root fallback"

    @pytest.mark.asyncio
    async def test_slash_boundary_enforced(self, store: PathContextStore) -> None:
        """'src' must NOT match 'srcfoo/x.py' — only slash-bounded match."""
        await store.upsert("root", "src", "src only")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "srcfoo/x.py") is None
        assert await cache.lookup("root", "src/x.py") == "src only"
        assert await cache.lookup("root", "src") == "src only"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, store: PathContextStore) -> None:
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "anything/x.py") is None

    @pytest.mark.asyncio
    async def test_zone_none_coerces_to_root(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root src")
        cache = PathContextCache(store=store)
        assert await cache.lookup(None, "src/x.py") == "root src"

    @pytest.mark.asyncio
    async def test_zones_isolated_in_cache(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root src")
        await store.upsert("other", "src", "other src")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") == "root src"
        assert await cache.lookup("other", "src/x.py") == "other src"

    @pytest.mark.asyncio
    async def test_refresh_after_write(self, store: PathContextStore) -> None:
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") is None
        await store.upsert("root", "src", "first desc")
        assert await cache.lookup("root", "src/x.py") == "first desc"
        await store.upsert("root", "src", "second desc")
        assert await cache.lookup("root", "src/x.py") == "second desc"

    @pytest.mark.asyncio
    async def test_refresh_after_delete(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") == "first"
        await store.delete("root", "src")
        assert await cache.lookup("root", "src/x.py") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_path_context.py::TestPathContextCacheLookup -v`
Expected: FAIL with `ImportError: cannot import name 'PathContextCache'`.

- [ ] **Step 3: Add `PathContextCache` to `src/nexus/bricks/search/path_context.py`**

Replace the trailing comment `# Issue #3773: PathContextCache is defined in Task 7.` with:

```python
class PathContextCache:
    """In-memory cache of path contexts keyed by zone, with longest-prefix lookup.

    - Per-zone ``asyncio.Lock`` serializes refreshes.
    - Each lookup cheaply checks ``store.max_updated_at(zone_id)`` and reloads
      when the cached stamp is stale.
    - Records are kept sorted by ``len(path_prefix)`` DESC so the first
      slash-boundary match is the longest prefix.
    """

    def __init__(self, *, store: PathContextStore) -> None:
        self._store = store
        self._entries: dict[str, tuple[datetime | None, list[PathContextRecord]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, zone_id: str) -> asyncio.Lock:
        lock = self._locks.get(zone_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[zone_id] = lock
        return lock

    async def refresh_if_stale(self, zone_id: str) -> None:
        db_stamp = await self._store.max_updated_at(zone_id)
        cached = self._entries.get(zone_id)
        if cached is not None and cached[0] == db_stamp:
            return
        async with self._lock_for(zone_id):
            # Re-check after lock acquisition — another task may have refreshed.
            db_stamp = await self._store.max_updated_at(zone_id)
            cached = self._entries.get(zone_id)
            if cached is not None and cached[0] == db_stamp:
                return
            records = await self._store.load_all_for_zone(zone_id)
            records.sort(key=lambda r: len(r.path_prefix), reverse=True)
            self._entries[zone_id] = (db_stamp, records)

    async def lookup(self, zone_id: str | None, path: str) -> str | None:
        """Return the longest-matching description for ``path`` in ``zone_id``,
        or None when no prefix matches.
        """
        effective_zone = zone_id or ROOT_ZONE_ID
        await self.refresh_if_stale(effective_zone)
        cached = self._entries.get(effective_zone)
        if cached is None:
            return None
        _, records = cached
        for record in records:
            prefix = record.path_prefix
            if prefix == "":
                return record.description
            if path == prefix or path.startswith(prefix + "/"):
                return record.description
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_path_context.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/path_context.py \
        tests/integration/bricks/search/test_path_context.py
git commit -m "feat(#3773): PathContextCache longest-prefix lookup + freshness"
```

---

## Task 8: Path contexts — API router (`/api/v2/path-contexts`)

**Files:**
- Create: `src/nexus/server/api/v2/routers/path_contexts.py`
- Create: `tests/integration/server/api/v2/routers/test_path_contexts_router.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/server/api/v2/routers/test_path_contexts_router.py`:

```python
"""Tests for /api/v2/path-contexts router (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import PathContextStore
from nexus.server.api.v2.routers.path_contexts import router as path_contexts_router

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def test_app():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = PathContextStore(async_session_factory=factory, db_type="sqlite")

    app = FastAPI()
    app.state.path_context_store = store
    app.include_router(path_contexts_router)

    # Override auth deps with canned admin/auth identities for the test.
    from nexus.server.dependencies import require_admin, require_auth

    app.dependency_overrides[require_auth] = lambda: {
        "subject_id": "tester",
        "zone_id": "root",
        "is_admin": False,
    }
    app.dependency_overrides[require_admin] = lambda: {
        "subject_id": "admin",
        "zone_id": "root",
        "is_admin": True,
    }
    yield app
    await engine.dispose()


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


class TestPathContextRouter:
    def test_put_upsert_then_list(self, client: TestClient) -> None:
        r = client.put(
            "/api/v2/path-contexts/",
            json={
                "zone_id": "root",
                "path_prefix": "src/nexus/bricks/search",
                "description": "Search brick",
            },
        )
        assert r.status_code == 200, r.text
        r = client.get("/api/v2/path-contexts/", params={"zone_id": "root"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["contexts"]) == 1
        assert body["contexts"][0]["description"] == "Search brick"

    def test_put_replaces(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "first"},
        )
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "second"},
        )
        body = client.get("/api/v2/path-contexts/").json()
        assert len(body["contexts"]) == 1
        assert body["contexts"][0]["description"] == "second"

    def test_delete_removes(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src", "description": "x"},
        )
        r = client.delete(
            "/api/v2/path-contexts/",
            params={"zone_id": "root", "path_prefix": "src"},
        )
        assert r.status_code == 200
        body = client.get("/api/v2/path-contexts/").json()
        assert body["contexts"] == []

    def test_delete_missing_returns_404(self, client: TestClient) -> None:
        r = client.delete(
            "/api/v2/path-contexts/",
            params={"zone_id": "root", "path_prefix": "nonexistent"},
        )
        assert r.status_code == 404

    def test_put_normalizes_prefix(self, client: TestClient) -> None:
        client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "/src/", "description": "x"},
        )
        body = client.get("/api/v2/path-contexts/").json()
        assert body["contexts"][0]["path_prefix"] == "src"

    def test_put_rejects_traversal(self, client: TestClient) -> None:
        r = client.put(
            "/api/v2/path-contexts/",
            json={"zone_id": "root", "path_prefix": "src/../etc", "description": "x"},
        )
        assert r.status_code == 422 or r.status_code == 400

    def test_non_admin_cannot_write(self, test_app: FastAPI) -> None:
        # Force require_admin to raise 403 as if caller were non-admin.
        from fastapi import HTTPException

        from nexus.server.dependencies import require_admin

        def _reject() -> None:
            raise HTTPException(status_code=403, detail="admin required")

        test_app.dependency_overrides[require_admin] = _reject
        with TestClient(test_app) as c:
            r = c.put(
                "/api/v2/path-contexts/",
                json={"zone_id": "root", "path_prefix": "src", "description": "x"},
            )
            assert r.status_code == 403
            r = c.delete(
                "/api/v2/path-contexts/",
                params={"zone_id": "root", "path_prefix": "src"},
            )
            assert r.status_code == 403

    def test_list_requires_auth_only(self, client: TestClient) -> None:
        r = client.get("/api/v2/path-contexts/")
        assert r.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/server/api/v2/routers/test_path_contexts_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.server.api.v2.routers.path_contexts'`.

- [ ] **Step 3: Create `src/nexus/server/api/v2/routers/path_contexts.py`**

```python
"""Path Contexts API v2 router (Issue #3773).

Admin-managed per-zone path-prefix -> description mappings. Search results
carry the longest-prefix-matching description in their ``context`` field.

Endpoints:
- PUT    /api/v2/path-contexts/       Upsert (admin)
- GET    /api/v2/path-contexts/       List contexts (auth)
- DELETE /api/v2/path-contexts/       Delete one (admin)

Pattern mirrors access_manifests.py.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from nexus.bricks.search.path_context import PathContextStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.dependencies import require_admin, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/path-contexts", tags=["path_contexts"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


def _normalize_prefix(raw: str) -> str:
    """Canonical form: no leading/trailing slashes, no '..' traversal.

    Raises ValueError on traversal attempts.
    """
    value = raw.strip()
    while value.startswith("/"):
        value = value[1:]
    while value.endswith("/"):
        value = value[:-1]
    parts = value.split("/") if value else []
    for segment in parts:
        if segment == ".." or segment == ".":
            raise ValueError(
                f"path_prefix must not contain '.' or '..' segments (got {raw!r})"
            )
    return value


class PathContextIn(BaseModel):
    zone_id: str = Field(default=ROOT_ZONE_ID, max_length=255)
    path_prefix: str = Field(max_length=1024)
    description: str = Field(max_length=4096, min_length=1)

    @field_validator("path_prefix")
    @classmethod
    def _validate_prefix(cls, v: str) -> str:
        return _normalize_prefix(v)


class PathContextOut(BaseModel):
    zone_id: str
    path_prefix: str
    description: str
    created_at: Any
    updated_at: Any


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> PathContextStore:
    store = getattr(request.app.state, "path_context_store", None)
    if store is None:
        raise HTTPException(
            status_code=503, detail="path context store not configured"
        )
    return store


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.put("/")
async def upsert_context(
    body: PathContextIn,
    _admin: dict[str, Any] = Depends(require_admin),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """Upsert a path context (admin only)."""
    await store.upsert(body.zone_id, body.path_prefix, body.description)
    return {
        "zone_id": body.zone_id,
        "path_prefix": body.path_prefix,
        "description": body.description,
    }


@router.get("/")
async def list_contexts(
    zone_id: str | None = Query(default=None),
    _auth: dict[str, Any] = Depends(require_auth),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """List path contexts (any authenticated caller). Optional ?zone_id filter."""
    records = await store.list(zone_id)
    return {
        "contexts": [
            {
                "zone_id": r.zone_id,
                "path_prefix": r.path_prefix,
                "description": r.description,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in records
        ]
    }


@router.delete("/")
async def delete_context(
    zone_id: str = Query(...),
    path_prefix: str = Query(...),
    _admin: dict[str, Any] = Depends(require_admin),
    store: PathContextStore = Depends(_get_store),
) -> dict[str, Any]:
    """Delete a path context (admin only)."""
    try:
        normalized = _normalize_prefix(path_prefix)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    removed = await store.delete(zone_id, normalized)
    if not removed:
        raise HTTPException(status_code=404, detail="path context not found")
    return {"zone_id": zone_id, "path_prefix": normalized, "status": "deleted"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/server/api/v2/routers/test_path_contexts_router.py -v`
Expected: all tests PASS.

If `test_put_rejects_traversal` fails, `pydantic.ValidationError` from a `field_validator` raises 422 by default in FastAPI — the test accepts 422 or 400. If it fails with something else, read the assertion message and fix the validator.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/api/v2/routers/path_contexts.py \
        tests/integration/server/api/v2/routers/test_path_contexts_router.py
git commit -m "feat(#3773): /api/v2/path-contexts router (admin-gated CRUD)"
```

---

## Task 9: Path contexts — app wiring (`lifespan/search.py` + `versioning.py`)

**Files:**
- Modify: `src/nexus/server/lifespan/search.py`
- Modify: `src/nexus/server/api/v2/versioning.py`

- [ ] **Step 1: Construct store + cache during search startup**

Edit `src/nexus/server/lifespan/search.py`. Locate the block that creates `SearchDaemon` (around line 115) and insert the following immediately **before** `app.state.search_daemon = SearchDaemon(...)`:

```python
        # Issue #3773: path context store + cache
        path_context_store = None
        path_context_cache = None
        if _async_sf is not None:
            try:
                from nexus.bricks.search.path_context import (
                    PathContextCache,
                    PathContextStore,
                )

                _db_type = "postgresql" if (svc.database_url or "").startswith(
                    ("postgres", "postgresql")
                ) else "sqlite"
                path_context_store = PathContextStore(
                    async_session_factory=_async_sf,
                    db_type=_db_type,
                )
                path_context_cache = PathContextCache(store=path_context_store)
            except Exception:  # pragma: no cover — non-fatal wiring failure
                logger.exception("Failed to initialize path context store/cache")
        app.state.path_context_store = path_context_store
        app.state.path_context_cache = path_context_cache
```

Then modify the `SearchDaemon(...)` construction call (currently at `app.state.search_daemon = SearchDaemon(config, async_session_factory=_async_sf, zoekt_client=_zoekt_client, cache_brick=_cache_brick, settings_store=_settings_store,)`) to add a new kwarg:

```python
        app.state.search_daemon = SearchDaemon(
            config,
            async_session_factory=_async_sf,
            zoekt_client=_zoekt_client,
            cache_brick=_cache_brick,
            settings_store=_settings_store,
            path_context_cache=path_context_cache,  # Issue #3773
        )
```

(The `SearchDaemon` constructor is extended to accept this kwarg in Task 10.)

- [ ] **Step 2: Register the router in `versioning.py`**

Edit `src/nexus/server/api/v2/versioning.py`. After the `# ---- Access Manifests router` block (around lines 335-345), add:

```python
    # ---- Path Contexts router (Issue #3773) ----
    try:
        from nexus.server.api.v2.routers.path_contexts import (
            router as path_contexts_router,
        )

        registry.add(
            RouterEntry(router=path_contexts_router, name="path_contexts", endpoint_count=3)
        )
    except ImportError as e:
        logger.warning("Failed to import Path Contexts routes: %s", e)
```

- [ ] **Step 3: Smoke-check the app starts**

Run: `python -c "from nexus.server.api.v2.versioning import *; print('ok')"`
Expected: prints `ok`.

Run: `python -c "from nexus.server.lifespan.search import startup_search; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add src/nexus/server/lifespan/search.py src/nexus/server/api/v2/versioning.py
git commit -m "feat(#3773): wire PathContextStore + PathContextCache into app lifespan"
```

---

## Task 10: Path contexts — `BaseSearchResult.context` + daemon attach

**Files:**
- Modify: `src/nexus/bricks/search/results.py`
- Modify: `src/nexus/bricks/search/daemon.py`
- Create: `tests/integration/bricks/search/test_daemon_context_attach.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/bricks/search/test_daemon_context_attach.py`:

```python
"""End-to-end: path contexts are attached to SearchResult instances (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import PathContextCache, PathContextStore
from nexus.bricks.search.results import BaseSearchResult

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def cache():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = PathContextStore(async_session_factory=factory, db_type="sqlite")
    await store.upsert("root", "src/nexus/bricks/search", "Hybrid search brick")
    await store.upsert("root", "docs", "Project documentation")
    yield PathContextCache(store=store)
    await engine.dispose()


class TestBaseSearchResultContextField:
    def test_default_context_is_none(self) -> None:
        r = BaseSearchResult(path="x", chunk_text="y", score=0.5)
        assert r.context is None


class TestAttachContextToResults:
    @pytest.mark.asyncio
    async def test_attach_via_cache(self, cache: PathContextCache) -> None:
        results = [
            BaseSearchResult(
                path="src/nexus/bricks/search/fusion.py",
                chunk_text="",
                score=0.9,
                zone_id="root",
            ),
            BaseSearchResult(
                path="docs/README.md",
                chunk_text="",
                score=0.8,
                zone_id="root",
            ),
            BaseSearchResult(
                path="scripts/noop.py",
                chunk_text="",
                score=0.7,
                zone_id="root",
            ),
        ]
        for r in results:
            r.context = await cache.lookup(r.zone_id, r.path)
        assert results[0].context == "Hybrid search brick"
        assert results[1].context == "Project documentation"
        assert results[2].context is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_daemon_context_attach.py::TestBaseSearchResultContextField -v`
Expected: FAIL with `AttributeError: 'BaseSearchResult' object has no attribute 'context'`.

- [ ] **Step 3: Add `context` field to `BaseSearchResult`**

Edit `src/nexus/bricks/search/results.py`. In the `BaseSearchResult` dataclass (lines 14-49), add the new field **after** the `zone_id` line:

```python
    # Issue #3773: admin-configured path description for LLM consumers
    context: str | None = None
```

Final dataclass segment around zone_id should read:

```python
    # Issue #3147: Federated search — zone provenance
    zone_id: str | None = None  # Source zone for cross-zone federated results
    # Issue #3773: admin-configured path description for LLM consumers
    context: str | None = None

    @property
    def zone_qualified_path(self) -> str | None:
```

- [ ] **Step 4: Extend daemon to accept + use the cache**

Edit `src/nexus/bricks/search/daemon.py`.

**4a.** Locate the `SearchDaemon` class constructor (`__init__`). Add a new kwarg `path_context_cache: PathContextCache | None = None`, save it to `self._path_context_cache`. If the file uses `TYPE_CHECKING` import blocks, add the import there; otherwise import lazily inside the attach helper to avoid circular imports. Preferred form at top of file:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.search.path_context import PathContextCache
```

Constructor signature change (illustrative — preserve all existing kwargs):

```python
def __init__(
    self,
    config: DaemonConfig,
    *,
    async_session_factory: Any = None,
    zoekt_client: Any = None,
    cache_brick: Any = None,
    settings_store: Any = None,
    path_context_cache: "PathContextCache | None" = None,  # Issue #3773
) -> None:
    ...
    self._path_context_cache = path_context_cache
```

**4b.** Locate the end of the SearchResult construction block (currently `daemon.py:1169-1186`, ending just before `latency_ms = (time.perf_counter() - start) * 1000`). Insert a new helper call **after** the list comprehension that builds `results` and **before** the `latency_ms` line:

```python
                    if self._path_context_cache is not None and results:
                        for r in results:
                            r.context = await self._path_context_cache.lookup(
                                r.zone_id, r.path
                            )
```

**4c.** Apply the same attach pattern to any other path that returns `SearchResult` instances in `daemon.py` (legacy fallback branch around lines 1193+). Grep for `return results` within the `search(` method body:

```
rg -n "return results" src/nexus/bricks/search/daemon.py
```

For each `return results` in the `search(...)` method, add the same `if self._path_context_cache is not None and results:` attach block immediately before it.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_daemon_context_attach.py -v`
Expected: all tests PASS.

Also run the full search daemon test suite to catch regressions:

Run: `pytest tests/integration/bricks/search/ -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/search/results.py \
        src/nexus/bricks/search/daemon.py \
        tests/integration/bricks/search/test_daemon_context_attach.py
git commit -m "feat(#3773): attach path context to SearchResult via cache"
```

---

## Task 11: Search router — emit `context` in HTTP response

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py` (`_serialize_search_result`)
- Modify: `tests/integration/bricks/search/test_daemon_context_attach.py` (add serializer test)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/bricks/search/test_daemon_context_attach.py`:

```python
class TestSerializerEmitsContext:
    def test_context_field_emitted_when_set(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        r = BaseSearchResult(
            path="src/nexus/bricks/search/fusion.py",
            chunk_text="body",
            score=0.9,
            zone_id="root",
        )
        r.context = "Hybrid search brick"
        out = _serialize_search_result(r)
        assert out.get("context") == "Hybrid search brick"

    def test_context_field_omitted_when_none(self) -> None:
        from nexus.server.api.v2.routers.search import _serialize_search_result

        r = BaseSearchResult(path="x", chunk_text="y", score=0.5)
        out = _serialize_search_result(r)
        assert "context" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/bricks/search/test_daemon_context_attach.py::TestSerializerEmitsContext -v`
Expected: FAIL — first test fails because `context` is absent; second may incidentally pass.

- [ ] **Step 3: Modify `_serialize_search_result`**

Edit `src/nexus/server/api/v2/routers/search.py`. Replace `_serialize_search_result` (lines 106-131) with:

```python
def _serialize_search_result(result: Any) -> dict[str, Any]:
    """Serialize a single search result into the canonical response dict.

    Collapses the 25-line dict comprehension previously duplicated across
    the graph and non-graph branches of ``search_query``. Preserves the
    pre-refactor field ordering, rounding, and None semantics.

    Issue #3773: emits ``context`` when the result carries a non-None value
    (omits the key otherwise to keep responses compact).
    """
    out: dict[str, Any] = {
        "path": result.path,
        "chunk_text": result.chunk_text,
        "score": round(result.score, 4),
        "chunk_index": result.chunk_index,
        "line_start": result.line_start,
        "line_end": result.line_end,
        "keyword_score": (round(result.keyword_score, 4) if result.keyword_score else None),
        "vector_score": (round(result.vector_score, 4) if result.vector_score else None),
    }
    splade = getattr(result, "splade_score", None)
    out["splade_score"] = round(splade, 4) if splade is not None else None
    reranker = getattr(result, "reranker_score", None)
    out["reranker_score"] = round(reranker, 4) if reranker is not None else None
    context = getattr(result, "context", None)
    if context is not None:
        out["context"] = context
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/bricks/search/test_daemon_context_attach.py::TestSerializerEmitsContext -v`
Expected: both tests PASS.

Also re-run any existing search router tests:

Run: `pytest tests/integration/server/api/v2/routers/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py \
        tests/integration/bricks/search/test_daemon_context_attach.py
git commit -m "feat(#3773): emit context field in /api/v2/search responses"
```

---

## Task 12: Final verification

**Files:** none — verification only.

- [ ] **Step 1: Run the full relevant test suite**

Run:
```
pytest tests/integration/bricks/search/test_rrf_bonus.py \
       tests/integration/bricks/search/test_fusion.py \
       tests/integration/bricks/search/test_path_context.py \
       tests/integration/bricks/search/test_daemon_context_attach.py \
       tests/integration/server/api/v2/routers/test_path_contexts_router.py \
       -v
```
Expected: all PASS.

- [ ] **Step 2: Type check**

Run: `mypy src/nexus/bricks/search/fusion.py src/nexus/bricks/search/path_context.py src/nexus/bricks/search/results.py src/nexus/bricks/search/daemon.py src/nexus/server/api/v2/routers/path_contexts.py src/nexus/server/api/v2/routers/search.py src/nexus/server/lifespan/search.py`
Expected: no new errors introduced (pre-existing errors unrelated to these files are acceptable if they existed on `develop`).

- [ ] **Step 3: Lint**

Run: `ruff check src/nexus/bricks/search/ src/nexus/server/api/v2/routers/path_contexts.py src/nexus/server/lifespan/search.py`
Expected: no new errors.

- [ ] **Step 4: Confirm migration round-trips**

Run: `alembic downgrade -1 && alembic upgrade head`
Expected: clean downgrade followed by clean upgrade.

- [ ] **Step 5: Smoke test against a local server (optional but recommended)**

Start the server with default config, then:
```
curl -s -X PUT http://localhost:PORT/api/v2/path-contexts/ \
     -H 'Content-Type: application/json' \
     -H 'Authorization: Bearer <admin_token>' \
     -d '{"zone_id":"root","path_prefix":"src/nexus/bricks/search","description":"Hybrid search"}'

curl -s "http://localhost:PORT/api/v2/search?q=fusion&limit=3" \
     -H 'Authorization: Bearer <auth_token>' | jq '.results[0]'
```
Expected: the first result for a matching path carries a `context` field.

- [ ] **Step 6: No additional commit**

All prior tasks committed their own changes. Verify `git status` is clean.

Run: `git status`
Expected: `nothing to commit, working tree clean`.

---

## Self-review notes

- **Spec coverage:** all sections of `2026-04-16-issue-3773-rrf-bonus-path-contexts-design.md` map to tasks:
  - RRF bonus → Tasks 1–4.
  - Schema/migration → Task 5.
  - Store + cache → Tasks 6–7.
  - Router → Task 8.
  - App wiring → Task 9.
  - Dataclass field + daemon attach → Task 10.
  - Serializer → Task 11.
  - Feature gate (empty table = no-op) → enforced implicitly by cache returning None.
- **Placeholders:** none. `<CURRENT_HEAD>` in Task 5 is a discoverable value, with explicit steps to find it.
- **Type consistency:** `PathContextStore`, `PathContextCache`, `PathContextRecord` names reused verbatim in Tasks 6–11. `top_rank_bonus` parameter and `RRF_TOP1_BONUS`/`RRF_TOP3_BONUS` constants reused verbatim in Tasks 1–4. `BaseSearchResult.context: str | None = None` reused in Tasks 10–11.
- **Observability** (INFO log on cache reload) is deliberately omitted from tasks — a trivial log line can be added during code review if desired.
