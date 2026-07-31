# Search Recency Decay Implementation Plan (Issue #4543)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Query-conditional post-fusion recency boost for search ranking — newer documents outrank stale near-duplicates when recency is requested, with zero change to default responses.

**Architecture:** A single chokepoint in `SearchDaemon.search()` (next to the #4398 `_apply_macro_expansion` post-hook): when recency is active, one batch hydration query fetches `file_paths.updated_at` for the result paths (index-only via `idx_file_paths_zone_path_covering`), then a pure function multiplies scores by `1 + w·H/(H+age_days)`, stamps `recency_boost` attribution, and re-sorts. No SQL `ORDER BY` changes anywhere. Spec: `docs/superpowers/specs/2026-07-31-search-recency-decay-design.md`.

**Tech Stack:** Python 3.12, FastAPI (flat `Query` params), SQLAlchemy async, pytest + pytest-asyncio, fake-backend daemon harness (`SearchDaemon.__new__`).

## Global Constraints

- Default-off requests must stay **byte-identical** (no `recency_boost` key, no re-sort, no extra SQL query).
- Recency decay must never appear in SQL `ORDER BY` — RRF rank positions and the HNSW pure-distance scan must stay untouched.
- Fail-soft: any hydration/boost error → log warning + `DaemonStats.recency_attach_failures` increment + unboosted results. Search never 500s.
- Remote federated zones do NOT receive the new params (older nodes reject unknown params — the #4541 rrf_k precedent).
- This is a git worktree: run tests as `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest <path> -v` (bare `uv run` fails on native deps in worktrees; do NOT create a `.venv` here).
- Branch: `feat/4543-search-recency-decay` (already created; spec committed). Repo requires linear history — rebase, never merge.
- Pre-commit hooks run on `git commit` (ruff, ruff format, mypy). Fix failures; don't `--no-verify`.

---

### Task 1: Pure recency core — `RECENCY_WORDS` + `recency.py`

**Files:**
- Modify: `src/nexus/contracts/search_types.py` (after `TEMPORAL_WORDS`, ~line 103)
- Create: `src/nexus/bricks/search/recency.py`
- Test: `tests/unit/bricks/search/test_recency.py` (new)

**Interfaces:**
- Consumes: `nexus.contracts.search_types.RECENCY_WORDS` (new frozenset).
- Produces (used by Task 3):
  - `has_recency_intent(query: str) -> bool`
  - `apply_recency_boost(results: list[Any], mtimes: dict[str, datetime], *, weight: float, half_life_days: float, now: datetime | None = None) -> int` — mutates `result.score` / `result.recency_boost` in place, in-place re-sorts only when ≥1 boost fired, returns count of boosted results. Works on any object with `.path`, `.score`, `.recency_boost` attributes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_recency.py
"""Pure recency-boost core (Issue #4543).

Covers the decay math, the intent detector, missing-mtime and
non-positive-score skips, and the byte-identity guarantee that a
zero-boost call performs no re-sort.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nexus.bricks.search.recency import apply_recency_boost, has_recency_intent
from nexus.bricks.search.results import BaseSearchResult

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def _r(path: str, score: float) -> BaseSearchResult:
    return BaseSearchResult(path=path, chunk_text=path, score=score, chunk_index=0)


class TestDecayMath:
    def test_fresh_doc_gets_full_boost(self) -> None:
        """age=0 → score × (1 + w)."""
        r = _r("/fresh.md", 2.0)
        n = apply_recency_boost([r], {"/fresh.md": NOW}, weight=0.3, half_life_days=30.0, now=NOW)
        assert n == 1
        assert r.score == pytest.approx(2.0 * 1.3)
        assert r.recency_boost == pytest.approx(1.3)

    def test_half_life_old_doc_gets_half_boost(self) -> None:
        """age=H → score × (1 + w/2)."""
        r = _r("/mid.md", 2.0)
        mtimes = {"/mid.md": NOW - timedelta(days=30)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(2.0 * 1.15)

    def test_ancient_doc_boost_approaches_one(self) -> None:
        r = _r("/old.md", 2.0)
        mtimes = {"/old.md": NOW - timedelta(days=36500)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert 2.0 < r.score < 2.002

    def test_future_mtime_clamped_to_max_boost(self) -> None:
        """Clock skew must never demote: age clamps to 0."""
        r = _r("/skew.md", 1.0)
        mtimes = {"/skew.md": NOW + timedelta(days=5)}
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(1.3)

    def test_naive_mtime_treated_as_utc(self) -> None:
        """file_paths.updated_at is a naive UTC column."""
        r = _r("/naive.md", 1.0)
        mtimes = {"/naive.md": datetime(2026, 7, 31, 12, 0, 0)}  # naive == NOW in UTC
        apply_recency_boost([r], mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == pytest.approx(1.3)


class TestSkips:
    def test_missing_mtime_left_untouched(self) -> None:
        r = _r("/unknown.md", 2.0)
        n = apply_recency_boost([r], {}, weight=0.3, half_life_days=30.0, now=NOW)
        assert n == 0
        assert r.score == 2.0
        assert r.recency_boost is None

    def test_non_positive_score_skipped(self) -> None:
        """Multiplicative boost on score<=0 would demote — skip defensively."""
        r = _r("/zero.md", 0.0)
        apply_recency_boost([r], {"/zero.md": NOW}, weight=0.3, half_life_days=30.0, now=NOW)
        assert r.score == 0.0
        assert r.recency_boost is None

    def test_zero_weight_is_noop(self) -> None:
        r = _r("/a.md", 2.0)
        n = apply_recency_boost([r], {"/a.md": NOW}, weight=0.0, half_life_days=30.0, now=NOW)
        assert n == 0
        assert r.score == 2.0


class TestReordering:
    def test_newer_near_duplicate_overtakes_older(self) -> None:
        """Acceptance fixture: near-tie text scores, different mtimes —
        the newer doc must rank first once the boost fires."""
        older = _r("/old-dup.md", 1.00)
        newer = _r("/new-dup.md", 0.99)
        results = [older, newer]
        mtimes = {
            "/old-dup.md": NOW - timedelta(days=1095),
            "/new-dup.md": NOW - timedelta(days=1),
        }
        apply_recency_boost(results, mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert [r.path for r in results] == ["/new-dup.md", "/old-dup.md"]

    def test_no_boost_means_no_resort(self) -> None:
        """Byte-identity guard: when nothing fires, order is untouched even
        if the input was not score-sorted."""
        a, b = _r("/a.md", 0.5), _r("/b.md", 0.9)
        results = [a, b]  # deliberately unsorted
        apply_recency_boost(results, {}, weight=0.3, half_life_days=30.0, now=NOW)
        assert results == [a, b]

    def test_resort_preserves_list_subclass(self) -> None:
        """SearchResultList (list subclass carrying search_timing) must
        survive: boost sorts in place, never rebinds."""
        from nexus.bricks.search.daemon import SearchResult, SearchResultList

        rl = SearchResultList(
            [
                SearchResult(path="/old.md", chunk_text="x", score=1.0, chunk_index=0),
                SearchResult(path="/new.md", chunk_text="x", score=0.99, chunk_index=0),
            ],
            search_timing={"backend_ms": 1.0},
        )
        mtimes = {"/old.md": NOW - timedelta(days=365), "/new.md": NOW}
        apply_recency_boost(rl, mtimes, weight=0.3, half_life_days=30.0, now=NOW)
        assert isinstance(rl, SearchResultList)
        assert rl.search_timing == {"backend_ms": 1.0}
        assert rl[0].path == "/new.md"


class TestIntent:
    @pytest.mark.parametrize(
        "query",
        ["latest deploy notes", "recent incidents", "what changed today", "Newest API docs"],
    )
    def test_recency_words_fire(self, query: str) -> None:
        assert has_recency_intent(query)

    @pytest.mark.parametrize(
        "query",
        [
            "authentication middleware",
            "history of the auth module",  # TEMPORAL word, not a recency word
            "before the migration",
        ],
    )
    def test_neutral_and_temporal_queries_do_not_fire(self, query: str) -> None:
        assert not has_recency_intent(query)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_recency.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.search.recency'`

- [ ] **Step 3: Add `RECENCY_WORDS` to contracts**

In `src/nexus/contracts/search_types.py`, directly after the `TEMPORAL_WORDS` block (line ~102):

```python
# Recency-intent words (Issue #4543). Deliberately distinct from
# TEMPORAL_WORDS: that set signals temporal *complexity* for query routing
# ("history", "before", "until" often want OLD documents), while these words
# signal the caller wants NEW material — used to gate the recency=auto boost.
RECENCY_WORDS: frozenset[str] = frozenset(
    {
        "latest",
        "newest",
        "recent",
        "recently",
        "current",
        "currently",
        "today",
        "yesterday",
        "now",
        "new",
    }
)
```

- [ ] **Step 4: Create `src/nexus/bricks/search/recency.py`**

```python
"""Post-fusion recency boost (Issue #4543).

Pure functions only — DB hydration and mode resolution live on
``SearchDaemon`` (``_apply_recency_boost`` / ``_fetch_recency_mtimes``).

The boost is multiplicative hyperbolic decay::

    score *= 1 + weight * H / (H + age_days)

so a fresh document gets ``×(1 + weight)``, a half-life-old one
``×(1 + weight/2)``, and the multiplier decays toward ``×1`` — it strictly
promotes newer material and can never demote. It is applied AFTER fusion on
final scores; rank positions consumed by RRF and the HNSW pure-distance scan
are never touched (hard rule from the issue).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from nexus.contracts.search_types import RECENCY_WORDS

_SECONDS_PER_DAY = 86400.0


def has_recency_intent(query: str) -> bool:
    """True when the query contains a recency-intent word (recency=auto gate)."""
    return bool(set(query.lower().split()) & RECENCY_WORDS)


def apply_recency_boost(
    results: list[Any],
    mtimes: dict[str, datetime],
    *,
    weight: float,
    half_life_days: float,
    now: datetime | None = None,
) -> int:
    """Boost ``results`` in place by mtime recency; return the boosted count.

    Per result with a known mtime and a positive score:
    ``score *= 1 + weight * H / (H + age_days)`` and ``recency_boost`` is set
    to the multiplier (attribution — stays ``None`` when the boost did not
    fire). ``age_days`` is fractional and clamped >= 0 so clock-skewed future
    mtimes get the max boost, never a penalty. Naive datetimes are treated as
    UTC (``file_paths.updated_at`` is a naive-UTC column).

    Results lacking an mtime row (deleted mid-flight, remote-zone results,
    legacy sources) are left untouched. Non-positive scores are skipped —
    a multiplicative boost on a negative score would demote.

    Sorts in place (never rebinds) so ``SearchResultList`` and its
    ``search_timing`` survive, and only when at least one boost fired so a
    no-op call leaves ordering byte-identical.
    """
    if weight <= 0 or half_life_days <= 0 or not results:
        return 0
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    boosted = 0
    for result in results:
        mtime = mtimes.get(result.path)
        if mtime is None or result.score <= 0:
            continue
        if mtime.tzinfo is None:
            mtime = mtime.replace(tzinfo=UTC)
        age_days = max(0.0, (now - mtime).total_seconds() / _SECONDS_PER_DAY)
        boost = 1.0 + weight * half_life_days / (half_life_days + age_days)
        result.score *= boost
        result.recency_boost = boost
        boosted += 1

    if boosted:
        results.sort(key=lambda r: r.score, reverse=True)
    return boosted
```

- [ ] **Step 5: Add `recency_boost` field to `BaseSearchResult`**

In `src/nexus/bricks/search/results.py`, after the macro-chunk fields (line ~47):

```python
    # Issue #4543: recency-decay attribution — the multiplier applied to
    # ``score`` when the post-fusion recency boost fired; None otherwise.
    recency_boost: float | None = None
```

(Needed now: `apply_recency_boost` writes `result.recency_boost`, and
`BaseSearchResult` is a plain dataclass — assigning an undeclared attribute
would work at runtime but the field must exist for serialization, federated
`_result_to_dict`, and mypy.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_recency.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/nexus/contracts/search_types.py src/nexus/bricks/search/recency.py src/nexus/bricks/search/results.py tests/unit/bricks/search/test_recency.py
git commit -m "feat(search): recency boost core — RECENCY_WORDS + hyperbolic decay (#4543)"
```

---

### Task 2: `DaemonConfig` knobs + `DaemonStats` counter

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (imports ~line 50, `DaemonStats` ~line 138, `DaemonConfig` ~line 305)
- Test: `tests/unit/bricks/search/test_recency.py` (extend)

**Interfaces:**
- Consumes: `nexus.bricks.search.config.get_env_float` (exists, `config.py:37`).
- Produces (used by Task 3):
  - `DaemonConfig.recency_mode: str` (default `"off"`, env `NEXUS_SEARCH_RECENCY`)
  - `DaemonConfig.recency_weight: float` (default `0.3`, env `NEXUS_SEARCH_RECENCY_WEIGHT`)
  - `DaemonConfig.recency_half_life_days: float` (default `30.0`, env `NEXUS_SEARCH_RECENCY_HALF_LIFE_DAYS`)
  - `DaemonStats.recency_attach_failures: int` (default 0)

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/bricks/search/test_recency.py`)

```python
class TestConfig:
    def test_daemon_config_recency_defaults(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        cfg = DaemonConfig()
        assert cfg.recency_mode == "off"
        assert cfg.recency_weight == pytest.approx(0.3)
        assert cfg.recency_half_life_days == pytest.approx(30.0)

    def test_daemon_config_recency_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        monkeypatch.setenv("NEXUS_SEARCH_RECENCY", "AUTO")
        monkeypatch.setenv("NEXUS_SEARCH_RECENCY_WEIGHT", "0.5")
        monkeypatch.setenv("NEXUS_SEARCH_RECENCY_HALF_LIFE_DAYS", "7")
        cfg = DaemonConfig()
        assert cfg.recency_mode == "auto"  # normalized lowercase
        assert cfg.recency_weight == pytest.approx(0.5)
        assert cfg.recency_half_life_days == pytest.approx(7.0)

    def test_daemon_stats_has_recency_failure_counter(self) -> None:
        from nexus.bricks.search.daemon import DaemonStats

        assert DaemonStats().recency_attach_failures == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_recency.py::TestConfig -v`
Expected: FAIL — `AttributeError`/`TypeError` (fields don't exist)

- [ ] **Step 3: Implement**

In `src/nexus/bricks/search/daemon.py`:

(a) Extend the existing config-helper import block at ~line 50:

```python
from nexus.bricks.search.config import get_env_bool as _get_env_bool
from nexus.bricks.search.config import get_env_float as _get_env_float
from nexus.bricks.search.config import get_env_int as _get_env_int
```

(b) In `DaemonStats` (directly after `path_context_resolve_failures: int = 0`, ~line 138):

```python
    # Fail-soft counter for the recency boost (Issue #4543): hydration or
    # boost errors must never fail a search, but persistent failures should
    # be visible via /search/stats rather than only in log lines.
    recency_attach_failures: int = 0
```

(c) In `DaemonConfig`, after the `macro_chunk_*` block (~line 305):

```python
    # Recency decay (Issue #4543). Post-fusion multiplicative boost
    # ``score *= 1 + w * H / (H + age_days)`` applied at the search()
    # chokepoint from a batch mtime hydration query — never in SQL ORDER BY.
    # Modes: "off" (default), "on" (always), "auto" (only for queries with a
    # RECENCY_WORDS intent word). Unrecognized env values behave as "off"
    # (fail closed). Request params override these per call.
    recency_mode: str = field(
        default_factory=lambda: os.environ.get("NEXUS_SEARCH_RECENCY", "off").strip().lower()
        or "off"
    )
    recency_weight: float = field(
        default_factory=lambda: _get_env_float("NEXUS_SEARCH_RECENCY_WEIGHT", 0.3)
    )
    recency_half_life_days: float = field(
        default_factory=lambda: _get_env_float("NEXUS_SEARCH_RECENCY_HALF_LIFE_DAYS", 30.0)
    )
```

(`os` is already imported at the top of daemon.py, line 37 — no new import needed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_recency.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_recency.py
git commit -m "feat(search): DaemonConfig recency knobs + fail-soft stats counter (#4543)"
```

---

### Task 3: Daemon wiring — hydration + boost chokepoint in `search()`

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` (`search()` ~line 1784, new methods next to `_apply_macro_expansion` ~line 1756)
- Test: `tests/unit/bricks/search/test_daemon_recency.py` (new)

**Interfaces:**
- Consumes: Task 1 (`apply_recency_boost`, `has_recency_intent`), Task 2 (config fields, stats counter).
- Produces (used by Tasks 4/5):
  - `SearchDaemon.search(..., recency: str | None = None, recency_weight: float | None = None, recency_half_life_days: float | None = None)`
  - `SearchDaemon._fetch_recency_mtimes(paths: Sequence[str], *, zone_id: str) -> dict[str, datetime]` — monkeypatchable test seam.
  - `SearchDaemon._apply_recency_boost_post_search(results, *, query, recency, recency_weight, recency_half_life_days, zone_id) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_daemon_recency.py
"""Recency boost wiring through SearchDaemon.search() (Issue #4543).

Mirrors the #4541 fake-backend harness (test_daemon_fusion_params.py):
keyword and dense orderings disagree so any score change is visible.
The hydration seam ``_fetch_recency_mtimes`` is monkeypatched — no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MethodType
from typing import Any

import pytest

# The daemon boosts against real datetime.now(UTC) (no injectable clock on the
# request path), so fixtures must be built relative to the real now — a fixed
# constant would silently age and change the expected multipliers.


def _make_daemon(mtimes: dict[str, datetime] | None = None, **config_kwargs: Any) -> Any:
    """Bare SearchDaemon: fake fts/vector backends + fake mtime hydration.

    Corpus: keyword /a.md > /b.md > /c.md; dense /d.md > /c.md > /a.md
    (same fixture as test_daemon_fusion_params.py).
    """
    from nexus.bricks.search.daemon import (
        DaemonConfig,
        DaemonStats,
        SearchDaemon,
        SearchResult,
    )

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

    async def _attach_path_contexts(self: Any, results: Any, *, zone_id: str) -> None:
        return None

    def _track_latency(self: Any, latency_ms: float) -> None:
        return None

    fetch_calls: list[tuple[tuple[str, ...], str]] = []

    async def _fetch_recency_mtimes(
        self: Any, paths: Any, *, zone_id: str
    ) -> dict[str, datetime]:
        fetch_calls.append((tuple(paths), zone_id))
        return dict(mtimes or {})

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon._initialized = True
    daemon.last_search_timing = {}
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._embed_query = MethodType(_embed_query, daemon)
    daemon._attach_path_contexts = MethodType(_attach_path_contexts, daemon)
    daemon._track_latency = MethodType(_track_latency, daemon)
    daemon._fetch_recency_mtimes = MethodType(_fetch_recency_mtimes, daemon)
    daemon.config = DaemonConfig(page_aggregation=False, **config_kwargs)
    daemon.stats = DaemonStats()
    daemon._recency_fetch_calls = fetch_calls  # test-side spy
    return daemon


def _old_new_mtimes() -> dict[str, datetime]:
    """/b.md is fresh, everything else is years old — with default fusion
    ordering /a.md > /d.md > /c.md > /b.md, a strong boost lifts /b.md.

    RRF-score arithmetic for this fixture (k=60, top-rank bonuses 0.05/0.02):
    a≈.0823, d≈.0664, c≈.0520, b≈.0361. At weight=0.3 the fresh ×1.3 on b
    (.0470) still trails c's stale .0523 — so ordering tests use weight=1.0
    (b → .0722, ancient ×≈1.02 elsewhere), giving a > b > d > c."""
    now = datetime.now(UTC)
    old = now - timedelta(days=1500)
    return {"/a.md": old, "/b.md": now, "/c.md": old, "/d.md": old}


@pytest.mark.asyncio
async def test_default_request_untouched_and_no_hydration() -> None:
    """Byte-identity: with recency off (default config, no request params)
    ordering matches #4541's pinned default and NO hydration query runs."""
    daemon = _make_daemon(_old_new_mtimes())
    results = await daemon.search("nexus core", search_type="hybrid", limit=4)

    assert [r.path for r in results] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    assert all(r.recency_boost is None for r in results)
    assert daemon._recency_fetch_calls == []


@pytest.mark.asyncio
async def test_recency_on_boosts_and_reorders() -> None:
    """recency='on' (weight=1.0) hydrates once and lifts the fresh /b.md
    from last place to second; results carry recency_boost attribution.
    See _old_new_mtimes docstring for the RRF arithmetic."""
    daemon = _make_daemon(_old_new_mtimes())
    results = await daemon.search(
        "nexus core", search_type="hybrid", limit=4, recency="on", recency_weight=1.0
    )

    assert len(daemon._recency_fetch_calls) == 1
    assert [r.path for r in results] == ["/a.md", "/b.md", "/d.md", "/c.md"]
    b = results[1]
    assert b.recency_boost == pytest.approx(2.0, rel=0.01)  # fresh: 1 + 1.0*H/(H+~0)
    assert all(r.recency_boost is not None for r in results)  # every path hydrated
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_recency_auto_fires_only_on_intent_queries() -> None:
    daemon = _make_daemon(_old_new_mtimes(), recency_mode="auto")

    neutral = await daemon.search("nexus core", search_type="hybrid", limit=4)
    assert daemon._recency_fetch_calls == []
    assert all(r.recency_boost is None for r in neutral)

    boosted = await daemon.search("latest nexus core", search_type="hybrid", limit=4)
    assert len(daemon._recency_fetch_calls) == 1
    assert any(r.recency_boost is not None for r in boosted)


@pytest.mark.asyncio
async def test_request_params_override_config() -> None:
    """Explicit request knobs beat config: recency='off' suppresses a
    config-on daemon; explicit weight reaches the boost math."""
    daemon = _make_daemon(_old_new_mtimes(), recency_mode="on")
    off = await daemon.search("nexus core", search_type="hybrid", limit=4, recency="off")
    assert all(r.recency_boost is None for r in off)
    assert daemon._recency_fetch_calls == []

    daemon2 = _make_daemon(_old_new_mtimes())
    on = await daemon2.search(
        "nexus core", search_type="hybrid", limit=4, recency="on", recency_weight=1.0
    )
    b = next(r for r in on if r.path == "/b.md")
    assert b.recency_boost == pytest.approx(2.0, rel=0.01)  # 1 + 1.0 * H/(H+~0)


@pytest.mark.asyncio
async def test_hydration_failure_is_fail_soft() -> None:
    """A hydration exception must not 500 the search: results come back
    unboosted and the stats counter increments."""
    daemon = _make_daemon()

    async def _boom(self: Any, paths: Any, *, zone_id: str) -> dict[str, datetime]:
        raise RuntimeError("db down")

    daemon._fetch_recency_mtimes = MethodType(_boom, daemon)
    results = await daemon.search("nexus core", search_type="hybrid", limit=4, recency="on")

    assert [r.path for r in results] == ["/a.md", "/d.md", "/c.md", "/b.md"]
    assert all(r.recency_boost is None for r in results)
    assert daemon.stats.recency_attach_failures == 1


@pytest.mark.asyncio
async def test_keyword_and_semantic_paths_also_boost() -> None:
    """The chokepoint lives in search(), so non-hybrid types get the boost
    too (hydration-approach coverage win over per-SELECT carrying)."""
    daemon = _make_daemon(_old_new_mtimes())
    kw = await daemon.search("nexus core", search_type="keyword", limit=3, recency="on")
    assert any(r.recency_boost is not None for r in kw)

    daemon2 = _make_daemon(_old_new_mtimes())
    sem = await daemon2.search("nexus core", search_type="semantic", limit=3, recency="on")
    assert any(r.recency_boost is not None for r in sem)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_recency.py -v`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'recency'` (and the default test may pass; that's fine)

- [ ] **Step 3: Implement in `src/nexus/bricks/search/daemon.py`**

(a) Extend `search()` signature (~line 1784) — add after `expand: str = "none"`:

```python
        recency: str | None = None,
        recency_weight: float | None = None,
        recency_half_life_days: float | None = None,
```

(b) In `search()`'s body, insert the boost call after `results` is assigned
(after the `if zone_id is None: ... else: ...` block, ~line 1822) and BEFORE
`await self._apply_macro_expansion(...)`:

```python
        await self._apply_recency_boost_post_search(
            results,
            query=query,
            recency=recency,
            recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days,
            zone_id=zone_id or ROOT_ZONE_ID,
        )
```

`ROOT_ZONE_ID`: add `from nexus.contracts.constants import ROOT_ZONE_ID` as a
local import at the top of `search()` (mirroring `_search_on_current_loop`
line ~1854) or reuse if already imported at module level.

(c) Add the two methods directly before `search()` (next to
`_apply_macro_expansion`, ~line 1783):

```python
    async def _apply_recency_boost_post_search(
        self,
        results: list[SearchResult],
        *,
        query: str,
        recency: str | None,
        recency_weight: float | None,
        recency_half_life_days: float | None,
        zone_id: str,
    ) -> None:
        """Post-fusion recency decay at the search() chokepoint (Issue #4543).

        Runs AFTER fusion/coercion on typed results (the #4398 macro_text
        pattern) so every source is covered — all three search types, both DB
        backends including sqlite_vec dense-only rows, and the legacy
        fallback stack — and no backend SELECT or SQL ORDER BY changes.
        Request params override DaemonConfig; ``None`` defers. Fail-soft:
        errors log + count, never fail the search.
        """
        from nexus.bricks.search.recency import apply_recency_boost, has_recency_intent

        mode = recency if recency is not None else self.config.recency_mode
        weight = recency_weight if recency_weight is not None else self.config.recency_weight
        half_life = (
            recency_half_life_days
            if recency_half_life_days is not None
            else self.config.recency_half_life_days
        )
        if not results or weight <= 0 or half_life <= 0:
            return
        if mode == "auto":
            if not has_recency_intent(query):
                return
        elif mode != "on":
            return  # "off" and unrecognized modes fail closed

        try:
            mtimes = await self._fetch_recency_mtimes(
                [r.path for r in results], zone_id=zone_id
            )
            apply_recency_boost(results, mtimes, weight=weight, half_life_days=half_life)
        except Exception as exc:
            self.stats.recency_attach_failures += 1
            logger.warning("Recency boost failed (fail-soft, results unboosted): %s", exc)

    async def _fetch_recency_mtimes(
        self,
        paths: Sequence[str],
        *,
        zone_id: str,
    ) -> dict[str, datetime]:
        """Batch-hydrate ``file_paths.updated_at`` for ``paths`` (Issue #4543).

        One SELECT served index-only by ``idx_file_paths_zone_path_covering``
        (zone_id, virtual_path → INCLUDE updated_at). Returns {} when no
        session factory is wired (legacy embedded deployments) — the boost
        then no-ops rather than failing.
        """
        if self._async_session is None or not paths:
            return {}
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        unique_paths = list({p for p in paths if p})
        stmt = select(FilePathModel.virtual_path, FilePathModel.updated_at).where(
            FilePathModel.zone_id == zone_id,
            FilePathModel.virtual_path.in_(unique_paths),
            FilePathModel.deleted_at.is_(None),
        )
        async with self._async_session() as session:
            rows = (await session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows if row[1] is not None}
```

Imports: `datetime` is already imported at daemon.py line 42
(`from datetime import UTC, datetime`). `Sequence` is NOT yet imported — extend
line 39 to `from collections.abc import Awaitable, Callable, Iterable, Sequence`.
NOTE: keep the `self._async_session` read inside `_fetch_recency_mtimes` (not
in `search()`), so `__new__`-built test harnesses that monkeypatch the fetch
seam never need the attribute.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_recency.py tests/unit/bricks/search/test_recency.py tests/unit/bricks/search/test_daemon_fusion_params.py -v`
Expected: ALL PASS (fusion-params suite proves no default-path regression)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_recency.py
git commit -m "feat(search): recency boost chokepoint + mtime hydration in daemon.search (#4543)"
```

---

### Task 4: Router params, validation, forwarding, serialization

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py` (`search_query` ~line 295, `_handle_single_zone_search` ~line 402, `_serialize_search_result` ~line 156; federated forwarding is Task 5)
- Test: `tests/unit/server/api/v2/test_search_recency_param.py` (new)
- Test: `tests/unit/bricks/search/test_search_result_serialize.py` (extend)

**Interfaces:**
- Consumes: Task 3's `daemon.search(..., recency=, recency_weight=, recency_half_life_days=)`.
- Produces: wire params `recency` / `recency_weight` / `recency_half_life_days` on `GET /api/v2/search/query`; per-result `recency_boost` (rounded 4dp, omit-when-None) in responses.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/server/api/v2/test_search_recency_param.py
"""Unit tests for the recency request params on GET /api/v2/search/query (#4543).

Covers:
(a) recency=on|auto accepted and threaded to daemon.search
(b) recency=bogus -> 400 before touching the daemon
(c) params omitted -> daemon receives None (defer-to-config sentinel)
(d) weight/half-life bounds -> 422 from FastAPI validation
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "r"]],
    "is_admin": False,
}


class _RecordingRunner:
    async def call(self, work: Any) -> Any:
        return await work()


class _RecordingRegistry:
    def runner_for(self, zone_id: str) -> _RecordingRunner:
        return _RecordingRunner()


def _build_app(daemon: Any) -> "FastAPI":
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.record_store = object()
    app.state.async_read_session_factory = object()
    app.state.permission_enforcer = None
    app.state.zone_registry = _RecordingRegistry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


def _make_daemon() -> MagicMock:
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.config = MagicMock()
    daemon.config.txtai_graph = False

    async def fake_search(**kwargs: Any) -> list[Any]:
        return []

    daemon.search = AsyncMock(side_effect=fake_search)
    return daemon


def test_recency_bogus_returns_400() -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x", "recency": "bogus"})
    assert response.status_code == 400, response.text
    assert "recency" in response.json().get("detail", "").lower()
    daemon.search.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "on", "auto"])
def test_recency_mode_accepted_and_threaded(mode: str) -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get(
            "/api/v2/search/query",
            params={
                "q": "x",
                "recency": mode,
                "recency_weight": 0.5,
                "recency_half_life_days": 7,
            },
        )
    assert response.status_code not in (400, 422), response.text
    kwargs = daemon.search.call_args.kwargs
    assert kwargs.get("recency") == mode
    assert kwargs.get("recency_weight") == 0.5
    assert kwargs.get("recency_half_life_days") == 7.0


def test_recency_omitted_forwards_none() -> None:
    """None is the defer-to-DaemonConfig sentinel — must survive the router."""
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x"})
    assert response.status_code not in (400, 422), response.text
    kwargs = daemon.search.call_args.kwargs
    assert kwargs.get("recency") is None
    assert kwargs.get("recency_weight") is None
    assert kwargs.get("recency_half_life_days") is None


@pytest.mark.parametrize(
    "params",
    [
        {"recency_weight": -0.1},
        {"recency_weight": 5.1},
        {"recency_half_life_days": 0},
        {"recency_half_life_days": 3651},
    ],
)
def test_out_of_bounds_knobs_return_422(params: dict[str, Any]) -> None:
    daemon = _make_daemon()
    with TestClient(_build_app(daemon)) as client:
        response = client.get("/api/v2/search/query", params={"q": "x", **params})
    assert response.status_code == 422, response.text
    daemon.search.assert_not_called()
```

And append to `tests/unit/bricks/search/test_search_result_serialize.py`:

```python
def test_serialize_omits_recency_boost_when_absent():
    """Default response unchanged when the boost did not fire (#4543)."""
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    out = _serialize_search_result(r)
    assert "recency_boost" not in out


def test_serialize_includes_recency_boost_when_present():
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    r.recency_boost = 1.234567
    out = _serialize_search_result(r)
    assert out["recency_boost"] == 1.2346  # rounded to 4dp like other scores
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/server/api/v2/test_search_recency_param.py tests/unit/bricks/search/test_search_result_serialize.py -v`
Expected: recency-param tests FAIL (bogus value passes through as no param exists → daemon called / no 400); serialize include test FAILS (`recency_boost` key missing)

- [ ] **Step 3: Implement in `src/nexus/server/api/v2/routers/search.py`**

(a) `search_query` params — after `expand: str = Query(...)` (~line 305):

```python
    recency: str | None = Query(
        None, description="Recency boost mode: off, on, or auto (default: server config, #4543)"
    ),
    recency_weight: float | None = Query(
        None,
        description="Recency boost weight w in score*=1+w*H/(H+age_days) (default: server config)",
        ge=0.0,
        le=5.0,
    ),
    recency_half_life_days: float | None = Query(
        None,
        description="Recency half-life H in days (default: server config)",
        gt=0.0,
        le=3650.0,
    ),
```

(b) Validation — after the `expand` check (~line 352), same pattern:

```python
    if recency is not None and recency not in ("off", "on", "auto"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid recency: {recency}. Must be 'off', 'on', or 'auto'",
        )
```

(c) Forward in `_work()`'s single-zone branch (~line 380-397): add to the
`_handle_single_zone_search(...)` call:

```python
            recency=recency,
            recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days,
```

(Federated branch forwarding is Task 5 — leave `_handle_federated_search`
untouched in this task so it stays green.)

(d) `_handle_single_zone_search` — add keyword-only params after `rrf_k: int`
(~line 411):

```python
    recency: str | None = None,
    recency_weight: float | None = None,
    recency_half_life_days: float | None = None,
```

and add to the `search_daemon.search(...)` call (~line 525):

```python
            recency=recency,
            recency_weight=recency_weight,
            recency_half_life_days=recency_half_life_days,
```

(The graph branch `graph_enhanced_search` does not take the knobs — same
pre-existing gap as alpha/fusion_method, out of scope per spec.)

(e) `_serialize_search_result` — after the `macro_text` block (~line 187):

```python
    recency_boost = getattr(result, "recency_boost", None)
    if recency_boost is not None:
        out["recency_boost"] = round(recency_boost, 4)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/server/api/v2/test_search_recency_param.py tests/unit/server/api/v2/test_search_expand_param.py tests/unit/bricks/search/test_search_result_serialize.py -v`
Expected: ALL PASS (expand suite proves no router regression)

- [ ] **Step 5: Regenerate the API surface coverage YAML**

The `/query` decorator line shifted, so the freshness gate's `source:` line
drifts (memory: #4541 needed the same regen).

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python scripts/gen_api_surface_coverage.py`
Then: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/surface_coverage/ -v`
Expected: PASS; `git diff docs/surface-coverage/` shows only line-number/param drift for `search.query`.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py docs/surface-coverage/ tests/unit/server/api/v2/test_search_recency_param.py tests/unit/bricks/search/test_search_result_serialize.py
git commit -m "feat(search): recency request params + recency_boost response field (#4543)"
```

---

### Task 5: Federated dispatcher — threading, cache key, None-strip

**Files:**
- Modify: `src/nexus/bricks/search/federated_search.py` (`_make_cache_key` ~line 430, `search` ~line 500, `_search_impl` ~line 555, `_search_zone` ~line 290, single-zone shortcut ~line 634, fan-out ~line 690, `_strip_none_context` ~line 865)
- Modify: `src/nexus/server/api/v2/routers/search.py` (`_handle_federated_search` ~line 587, federated branch of `_work()` ~line 366)
- Test: `tests/integration/bricks/search/test_federated_search.py` (extend)

**Interfaces:**
- Consumes: Task 3's `daemon.search` recency kwargs; Task 4's router params.
- Produces: `FederatedSearchDispatcher.search(..., recency: str | None = None, recency_weight: float | None = None, recency_half_life_days: float | None = None)`; cache keys that segregate recency variants; federated wire dicts without `recency_boost: null`.

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/bricks/search/test_federated_search.py`, following its existing dispatcher-fixture style — reuse the file's existing dispatcher/daemon fakes where present)

```python
class TestRecencyKnobs:
    """Issue #4543: recency knobs must segregate cache entries and reach
    local zones' daemon.search; remote RPC params stay untouched."""

    def _dispatcher(self) -> Any:
        from nexus.bricks.search.federated_search import FederatedSearchDispatcher

        return FederatedSearchDispatcher(daemon=MagicMock(), rebac=MagicMock())

    def test_cache_key_includes_recency_knobs(self) -> None:
        d = self._dispatcher()
        base = d._make_cache_key("q", ("user", "u"), "hybrid", 10, None, 0.5, "rrf", 60)
        with_mode = d._make_cache_key(
            "q", ("user", "u"), "hybrid", 10, None, 0.5, "rrf", 60, recency="on"
        )
        with_weight = d._make_cache_key(
            "q", ("user", "u"), "hybrid", 10, None, 0.5, "rrf", 60,
            recency="on", recency_weight=1.0,
        )
        assert base != with_mode
        assert with_mode != with_weight

    @pytest.mark.asyncio
    async def test_local_zone_receives_recency_knobs(self) -> None:
        """_search_zone must forward the knobs to daemon.search for local
        zones (remote zones are exercised by existing rrf_k-omission tests)."""
        d = self._dispatcher()
        daemon = MagicMock()
        daemon.search = AsyncMock(return_value=[])
        d._get_daemon_for_zone = MagicMock(return_value=daemon)

        await d._search_zone(
            "zone-a", "q", "hybrid", 10, None, 0.5, "rrf",
            rrf_k=60, recency="auto", recency_weight=0.4, recency_half_life_days=14.0,
        )
        kwargs = daemon.search.call_args.kwargs
        assert kwargs["recency"] == "auto"
        assert kwargs["recency_weight"] == 0.4
        assert kwargs["recency_half_life_days"] == 14.0

    def test_strip_none_recency_boost_from_federated_dicts(self) -> None:
        from nexus.bricks.search.federated_search import _result_to_dict
        from nexus.bricks.search.results import BaseSearchResult

        r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
        assert "recency_boost" not in _result_to_dict(r)

        r.recency_boost = 1.2
        assert _result_to_dict(r)["recency_boost"] == 1.2
```

(Match the file's existing import style for `MagicMock` / `AsyncMock` /
`pytest`; add them to the module imports if not already present.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_federated_search.py -k Recency -v`
Expected: FAIL — `_make_cache_key() got an unexpected keyword argument 'recency'` etc.

- [ ] **Step 3: Implement in `src/nexus/bricks/search/federated_search.py`**

(a) `_make_cache_key` (~line 430) — add params + fold into `raw`:

```python
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
        recency: str | None = None,
        recency_weight: float | None = None,
        recency_half_life_days: float | None = None,
    ) -> str:
        """Phase 3: Create a cache key for result caching.

        Fusion knobs are part of the key: they change result ordering now
        that the daemon honours them (#4541), so requests differing only in
        alpha / fusion_method / rrf_k must not share a cache entry. The
        recency knobs (#4543) are included for the same reason.
        """
        raw = (
            f"{subject[0]}:{subject[1]}|{query}|{search_type}|{limit}|{path_filter}"
            f"|{alpha}|{fusion_method}|{rrf_k}"
            f"|{recency}|{recency_weight}|{recency_half_life_days}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
```

(b) `search()` (~line 500): add the three `= None` params after `rrf_k` and
forward them in the `self._search_impl(...)` call.

(c) `_search_impl()` (~line 555): add the three `= None` params after
`rrf_k`; extend the docstring Args with:

```
            recency: Recency boost mode override (#4543). Like rrf_k,
                applied to LOCAL zones only — older remote nodes reject
                unknown search params, so remote zones stay unboosted until
                the RPC surface is versioned.
            recency_weight: Recency boost weight override (local zones only).
            recency_half_life_days: Recency half-life override (local zones
                only).
```

Pass them to `self._make_cache_key(...)` (~line 589) and to BOTH
`self._search_zone(...)` call sites (single-zone shortcut ~line 634 and
`_bounded_search` fan-out ~line 690) as keyword args.

(d) `_search_zone()` (~line 290): add after `rrf_k: int = 60`:

```python
        recency: str | None = None,
        recency_weight: float | None = None,
        recency_half_life_days: float | None = None,
```

Forward them in the LOCAL `daemon.search(...)` call (~line 326). Do NOT add
them to `_search_remote_zone` or its `params` dict — extend the existing
rrf_k NOTE comment (~line 308) to mention the recency knobs.

(e) `_strip_none_context` (~line 865) — extend to also strip the new field:

```python
def _strip_none_context(d: dict[str, Any]) -> dict[str, Any]:
    """Match the non-federated router's omit-when-None contract for
    ``context``. Issue #3773 review (Rounds 5-6): every federated emission
    path must route through this to avoid ``context: null`` leaking onto
    the wire and creating a shape-drift between fusion strategies.
    Also strips ``recency_boost`` (#4543), which has the same
    omit-when-None wire contract."""
    if d.get("context") is None:
        d.pop("context", None)
    if d.get("recency_boost") is None:
        d.pop("recency_boost", None)
    return d
```

(f) In `src/nexus/server/api/v2/routers/search.py`:
`_handle_federated_search` (~line 587) — add the three `= None` keyword
params after `rrf_k: int` and forward them in `dispatcher.search(...)`
(~line 646). Then in `_work()`'s federated branch (~line 366) pass
`recency=recency, recency_weight=recency_weight,
recency_half_life_days=recency_half_life_days`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_federated_search.py -v`
Expected: ALL PASS (whole file — existing federated suites prove no regression)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/federated_search.py src/nexus/server/api/v2/routers/search.py tests/integration/bricks/search/test_federated_search.py
git commit -m "feat(search): thread recency knobs through federated dispatcher, local-only (#4543)"
```

---

### Task 6: Protocol conformance + full-suite verification

**Files:**
- Modify: `src/nexus/contracts/protocols/search.py:48-59`
- Modify: `tests/integration/bricks/search/test_brick_protocol.py` (`MockSearchBrick.search` ~line 76, `test_search_with_all_kwargs` ~line 172)

**Interfaces:**
- Consumes: everything above.
- Produces: `SearchBrickProtocol.search` declaring the recency params.

- [ ] **Step 1: Update the protocol**

In `src/nexus/contracts/protocols/search.py`, add to the `search` signature
after `rrf_k: int = 60,`:

```python
        recency: str | None = None,
        recency_weight: float | None = None,
        recency_half_life_days: float | None = None,
```

- [ ] **Step 2: Extend the protocol test**

In `tests/integration/bricks/search/test_brick_protocol.py`, add the same
three `= None` params to `MockSearchBrick.search` (~line 84, after
`adaptive_k: bool = False`), and extend `test_search_with_all_kwargs`
(~line 181) with:

```python
            recency="auto",
            recency_weight=0.3,
            recency_half_life_days=30.0,
```

- [ ] **Step 3: Run the targeted suites**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_brick_protocol.py tests/unit/bricks/search/ tests/unit/server/api/v2/ -v`
Expected: ALL PASS

- [ ] **Step 4: Run the broad regression net**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/ tests/integration/services/test_search_router.py tests/surface_coverage/ -q`
Expected: PASS (or only failures already present on `develop` — verify any
failure against `git stash`-free `develop` behavior before touching it)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/contracts/protocols/search.py tests/integration/bricks/search/test_brick_protocol.py
git commit -m "feat(search): declare recency knobs on SearchBrickProtocol (#4543)"
```

---

### Task 7: PR

- [ ] **Step 1: Final verification** — rerun everything Task 6 ran, plus
`PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_fusion_params.py tests/unit/bricks/search/test_final_list_page_pooling.py -v` (adjacent-feature regression pins).

- [ ] **Step 2: Push and open PR**

```bash
git push -u origin feat/4543-search-recency-decay
gh pr create --repo nexi-lab/nexus --base develop \
  --title "feat(search): query-conditional post-fusion recency decay (#4543)" \
  --body "$(cat <<'EOF'
Closes #4543.

Post-fusion multiplicative recency boost `score *= 1 + w·H/(H+age_days)` at a
single chokepoint in `SearchDaemon.search()`, hydrating `file_paths.updated_at`
with one index-only batch query (covering index already carries it). Default
off; `recency=on|auto` per request or `NEXUS_SEARCH_RECENCY` per deployment —
`auto` fires only on RECENCY_WORDS-intent queries. Per-result `recency_boost`
attribution, omit-when-None. Federated: knobs in the cache key, local zones
only (rrf_k precedent). No SQL ORDER BY changes; HNSW stays pure-distance.

Design: docs/superpowers/specs/2026-07-31-search-recency-decay-design.md
EOF
)"
```

(Repo gotchas: linear history — rebase on develop if it moved; CI includes the
surface-coverage freshness gate, covered by Task 4 Step 5.)
