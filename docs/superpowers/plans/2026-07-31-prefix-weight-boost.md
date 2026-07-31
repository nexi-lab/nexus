# Per-Prefix Ranking Weight (Source-Tier Boost) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins seed a per-prefix `weight` on `path_contexts` rows that multiplies search-result scores (with `tier_boost` attribution), so noisy prefixes rank below curated ones. Issue #4544.

**Architecture:** Additive `weight` column on `path_contexts` → carried through `PathContextRecord`/cache → applied in `SearchDaemon._attach_path_contexts` (multiply + stamp + stable re-sort), with a conditional daemon-side over-fetch in `_search_on_current_loop` so boosts can promote below-cutoff hits. Zone with no weights ≠ 1.0 → code path byte-identical to today. Spec: `docs/superpowers/specs/2026-07-31-prefix-weight-boost-design.md`.

**Tech Stack:** Python 3.12+ (repo), SQLAlchemy async + raw SQL, Alembic, FastAPI, Click, pytest + pytest-asyncio.

## Global Constraints

- Weight validation range at API/CLI: **0.1 ≤ weight ≤ 10.0**; DB `NULL` ≡ 1.0.
- Floor-ratio gate: applies to **uplifts only** (`w > 1.0`); default ratio **0.25**; `0` disables. Demotions always apply.
- Over-fetch factor default **3**; env `NEXUS_SEARCH_TIER_BOOST_OVERFETCH`. Floor env: `NEXUS_SEARCH_TIER_BOOST_FLOOR_RATIO`.
- Byte-identity: with no weight ≠ 1.0 configured, search behavior (results, order, scores, timing keys) must be unchanged.
- Run tests from the worktree root as: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest <path> -x -q`
  (do NOT `uv run` — native deps break in worktrees; do NOT create a bare `.venv` here).
- Commit style: conventional commits (`feat(search): ...`), reference `#4544`. Do not push or open a PR in this plan.
- All new code must pass `pre-commit` (ruff). If pre-commit fails on unrelated files, use `git commit --no-verify` only for docs-only commits.

---

### Task 1: `weight` column — migration, model, record, store

**Files:**
- Create: `alembic/versions/add_path_context_weight.py`
- Modify: `src/nexus/storage/models/path_context.py`
- Modify: `src/nexus/bricks/search/path_context.py` (PathContextRecord, upsert, list)
- Modify (fixtures — schema drift): `tests/integration/bricks/search/test_daemon_context_attach.py`, `tests/integration/bricks/search/test_path_context.py`, `tests/integration/server/api/v2/routers/test_path_contexts_router.py`
- Test: `tests/integration/bricks/search/test_path_context.py`

**Interfaces:**
- Consumes: existing `PathContextStore` / `PathContextRecord` in `src/nexus/bricks/search/path_context.py`.
- Produces: `PathContextRecord.weight: float | None` (default `None`); `PathContextStore.upsert(zone_id: str, path_prefix: str, description: str, weight: float | None = None) -> None`; `list()` rows carry `weight`. Later tasks rely on these exact names.

- [ ] **Step 1: Write the failing store test**

Each raw `CREATE TABLE path_contexts` fixture in the three test files above must gain a `weight FLOAT` line (nullable, no default) after `description`:

```sql
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    weight FLOAT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
```

Add to `tests/integration/bricks/search/test_path_context.py` (mirror its existing fixture/test style):

```python
class TestWeightColumn:
    """Issue #4544: nullable per-prefix ranking weight."""

    @pytest.mark.asyncio
    async def test_upsert_and_list_weight_roundtrip(self, store) -> None:
        await store.upsert("root", "chat/logs", "Chat transcripts", weight=0.5)
        records = await store.list("root")
        rec = next(r for r in records if r.path_prefix == "chat/logs")
        assert rec.weight == 0.5

    @pytest.mark.asyncio
    async def test_weight_defaults_to_none(self, store) -> None:
        await store.upsert("root", "docs/curated", "Curated docs")
        records = await store.list("root")
        rec = next(r for r in records if r.path_prefix == "docs/curated")
        assert rec.weight is None

    @pytest.mark.asyncio
    async def test_upsert_without_weight_resets_existing_weight(self, store) -> None:
        # PUT-replace semantics: an upsert that omits weight clears it.
        await store.upsert("root", "chat/logs", "Chat transcripts", weight=0.5)
        await store.upsert("root", "chat/logs", "Chat transcripts v2")
        records = await store.list("root")
        rec = next(r for r in records if r.path_prefix == "chat/logs")
        assert rec.weight is None
```

(Reuse the file's existing `store` fixture name — if it differs, adapt to the actual fixture, but do not create a new engine pattern.)

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_path_context.py -q -k Weight`
Expected: FAIL — `upsert() got an unexpected keyword argument 'weight'` (or `AttributeError: weight`).

- [ ] **Step 3: Implement record + store changes**

In `src/nexus/bricks/search/path_context.py`:

`PathContextRecord` gains a field (after `description`):

```python
@dataclass(frozen=True)
class PathContextRecord:
    """One row in the path_contexts table."""

    zone_id: str
    path_prefix: str
    description: str
    created_at: datetime
    updated_at: datetime
    # Issue #4544: per-prefix ranking weight; None ≡ 1.0 (no boost).
    weight: float | None = None
```

`upsert` — new signature `async def upsert(self, zone_id: str, path_prefix: str, description: str, weight: float | None = None) -> None:` and both SQL branches gain the column. Postgres branch:

```sql
INSERT INTO path_contexts
    (zone_id, path_prefix, description, weight, created_at, updated_at)
VALUES
    (:zone_id, :path_prefix, :description, :weight, :now, :now)
ON CONFLICT (zone_id, path_prefix) DO UPDATE
SET description = EXCLUDED.description,
    weight      = EXCLUDED.weight,
    updated_at  = EXCLUDED.updated_at
```

SQLite branch: same shape with lowercase `excluded.description` / `excluded.weight` / `excluded.updated_at` (match the existing SQLite branch). Both param dicts gain `"weight": weight`. Note: an upsert without `weight` deliberately resets it to NULL — PUT-replace semantics (documented in the docstring).

`list()` — SELECT gains the column and positional indices shift:

```python
query = (
    "SELECT zone_id, path_prefix, description, weight, created_at, updated_at "
    "FROM path_contexts"
)
...
return [
    PathContextRecord(
        zone_id=row[0],
        path_prefix=row[1],
        description=row[2],
        weight=row[3],
        created_at=_coerce_datetime(row[4]),
        updated_at=_coerce_datetime(row[5]),
    )
    for row in rows
]
```

In `src/nexus/storage/models/path_context.py` add after `description` (import `Float` from sqlalchemy):

```python
    # Issue #4544: per-prefix ranking weight; NULL ≡ 1.0 (no boost).
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
```

- [ ] **Step 4: Write the alembic migration**

First confirm the head: run `alembic -c alembic/alembic.ini heads` — expected single head `align_graph_zone_columns (knowledge_platform)`. If it changed since plan-writing, use the actual head.

Create `alembic/versions/add_path_context_weight.py`:

```python
"""Add weight column to path_contexts (Issue #4544).

Nullable, no server default: NULL ≡ 1.0 in application code, so existing
rows and weightless new rows rank exactly as before the migration.

Revision ID: add_path_context_weight
Revises: align_graph_zone_columns
Create Date: 2026-07-31
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_path_context_weight"
down_revision: Union[str, Sequence[str], None] = "align_graph_zone_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable weight column."""
    op.add_column("path_contexts", sa.Column("weight", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop weight column."""
    op.drop_column("path_contexts", "weight")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_path_context.py tests/integration/bricks/search/test_daemon_context_attach.py -q`
Expected: PASS (including all pre-existing tests — the fixture schema change must not break them).

- [ ] **Step 6: Verify migration is a single head**

Run: `alembic -c alembic/alembic.ini heads`
Expected: exactly one head: `add_path_context_weight`.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/add_path_context_weight.py src/nexus/storage/models/path_context.py src/nexus/bricks/search/path_context.py tests/integration/bricks/search/test_path_context.py tests/integration/bricks/search/test_daemon_context_attach.py tests/integration/server/api/v2/routers/test_path_contexts_router.py
git commit -m "feat(search): add nullable weight column to path_contexts (#4544)"
```

---

### Task 2: record lookup helper, `tier_boost` field, weight-apply helper, config knobs

**Files:**
- Modify: `src/nexus/bricks/search/path_context.py` (`lookup_record_in_records`, rewire `lookup_in_records`)
- Modify: `src/nexus/bricks/search/results.py` (`tier_boost` field)
- Modify: `src/nexus/bricks/search/daemon.py` (DaemonConfig knobs ~line 278 area; module-level `_apply_tier_weight`; import `get_env_float`)
- Test: `tests/unit/bricks/search/test_tier_boost.py` (new)

**Interfaces:**
- Consumes: `PathContextRecord.weight` from Task 1.
- Produces (later tasks call these exactly):
  - `lookup_record_in_records(records: list[PathContextRecord], path: str) -> PathContextRecord | None` in `nexus.bricks.search.path_context`
  - `BaseSearchResult.tier_boost: float | None = None` in `nexus.bricks.search.results`
  - `_apply_tier_weight(result: Any, weight: float | None, top_score: float, floor_ratio: float) -> bool` module-level in `nexus.bricks.search.daemon`
  - `DaemonConfig.tier_boost_overfetch_factor: int` (default 3), `DaemonConfig.tier_boost_floor_ratio: float` (default 0.25)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bricks/search/test_tier_boost.py`:

```python
"""Per-prefix ranking weight primitives (Issue #4544).

Covers the record-grain prefix lookup, the score-multiply helper with the
uplift-only floor-ratio gate, and idempotency (a result already stamped
with tier_boost is never boosted twice — batch_search re-attaches).
"""

from __future__ import annotations

from datetime import datetime

from nexus.bricks.search.daemon import _apply_tier_weight
from nexus.bricks.search.path_context import (
    PathContextRecord,
    lookup_in_records,
    lookup_record_in_records,
)
from nexus.bricks.search.results import BaseSearchResult


def _rec(prefix: str, weight: float | None = None) -> PathContextRecord:
    now = datetime(2026, 7, 31)
    return PathContextRecord(
        zone_id="root",
        path_prefix=prefix,
        description=f"desc:{prefix or 'root'}",
        created_at=now,
        updated_at=now,
        weight=weight,
    )


def _result(path: str = "chat/logs/a.md", score: float = 1.0) -> BaseSearchResult:
    return BaseSearchResult(path=path, chunk_text="", score=score)


class TestLookupRecord:
    def test_returns_longest_prefix_record(self) -> None:
        records = sorted(
            [_rec("chat", weight=0.9), _rec("chat/logs", weight=0.5)],
            key=lambda r: len(r.path_prefix),
            reverse=True,
        )
        rec = lookup_record_in_records(records, "chat/logs/a.md")
        assert rec is not None and rec.weight == 0.5

    def test_no_match_returns_none(self) -> None:
        assert lookup_record_in_records([_rec("docs")], "src/a.py") is None

    def test_description_wrapper_unchanged(self) -> None:
        # lookup_in_records keeps its historical contract.
        assert lookup_in_records([_rec("docs")], "docs/a.md") == "desc:docs"
        assert lookup_in_records([_rec("docs")], "src/a.py") is None


class TestApplyTierWeight:
    def test_none_and_one_are_noops(self) -> None:
        r = _result(score=1.0)
        assert _apply_tier_weight(r, None, top_score=1.0, floor_ratio=0.25) is False
        assert _apply_tier_weight(r, 1.0, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 1.0 and r.tier_boost is None

    def test_demotion_applies_and_stamps(self) -> None:
        r = _result(score=1.0)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 0.5 and r.tier_boost == 0.5

    def test_demotion_ignores_floor_gate(self) -> None:
        # Far-below-top result still gets demoted.
        r = _result(score=0.01)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 0.005

    def test_uplift_blocked_below_floor(self) -> None:
        r = _result(score=0.1)  # 0.1 < 0.25 * 1.0
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 0.1 and r.tier_boost is None

    def test_uplift_applies_above_floor(self) -> None:
        r = _result(score=0.5)
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.25) is True
        assert r.score == 1.0 and r.tier_boost == 2.0

    def test_zero_ratio_disables_gate(self) -> None:
        r = _result(score=0.001)
        assert _apply_tier_weight(r, 2.0, top_score=1.0, floor_ratio=0.0) is True

    def test_idempotent_on_stamped_result(self) -> None:
        # batch_search re-runs attach on already-boosted results: no w².
        r = _result(score=1.0)
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is True
        assert _apply_tier_weight(r, 0.5, top_score=1.0, floor_ratio=0.25) is False
        assert r.score == 0.5


class TestConfigKnobs:
    def test_defaults(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        cfg = DaemonConfig()
        assert cfg.tier_boost_overfetch_factor == 3
        assert cfg.tier_boost_floor_ratio == 0.25

    def test_env_overrides(self, monkeypatch) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        monkeypatch.setenv("NEXUS_SEARCH_TIER_BOOST_OVERFETCH", "5")
        monkeypatch.setenv("NEXUS_SEARCH_TIER_BOOST_FLOOR_RATIO", "0.5")
        cfg = DaemonConfig()
        assert cfg.tier_boost_overfetch_factor == 5
        assert cfg.tier_boost_floor_ratio == 0.5
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_tier_boost.py -q`
Expected: FAIL — `ImportError: cannot import name '_apply_tier_weight'` / `lookup_record_in_records`.

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/results.py` — add after `macro_line_end`:

```python
    # Issue #4544: per-prefix source-tier weight applied to score (None = unboosted)
    tier_boost: float | None = None
```

`src/nexus/bricks/search/path_context.py` — replace `lookup_in_records` body with record-grain core + wrapper:

```python
def lookup_record_in_records(
    records: builtins.list[PathContextRecord], path: str
) -> PathContextRecord | None:
    """Longest-prefix lookup returning the full record (Issue #4544).

    Records must be sorted by ``len(path_prefix)`` DESC so the first
    slash-boundary match is the longest prefix.
    """
    for record in records:
        prefix = record.path_prefix
        if prefix == "":
            return record
        if path == prefix or path.startswith(prefix + "/"):
            return record
    return None


def lookup_in_records(records: builtins.list[PathContextRecord], path: str) -> str | None:
    """Longest-prefix lookup returning only the description (Issue #3773 contract)."""
    record = lookup_record_in_records(records, path)
    return record.description if record is not None else None
```

(Keep the original docstring notes about sort order on the record-grain function.)

`src/nexus/bricks/search/daemon.py`:

1. Extend the config import (line ~50):

```python
from nexus.bricks.search.config import get_env_bool as _get_env_bool
from nexus.bricks.search.config import get_env_float as _get_env_float
from nexus.bricks.search.config import get_env_int as _get_env_int
```

2. DaemonConfig — add after the `macro_chunk_*` block, same `field(default_factory=...)` env pattern:

```python
    # Per-prefix ranking weight (Issue #4544). When the effective zone has any
    # path_contexts row with weight != 1.0, the daemon widens its candidate
    # fetch by ``tier_boost_overfetch_factor`` so a boost can promote a
    # below-cutoff hit, then trims back to the requested limit after the
    # weights are applied. ``tier_boost_floor_ratio`` gates uplifts only:
    # a result scoring below ratio*top cannot be boosted past strong matches
    # (0 disables the gate). Demotions always apply.
    tier_boost_overfetch_factor: int = field(
        default_factory=lambda: _get_env_int("NEXUS_SEARCH_TIER_BOOST_OVERFETCH", 3)
    )
    tier_boost_floor_ratio: float = field(
        default_factory=lambda: _get_env_float("NEXUS_SEARCH_TIER_BOOST_FLOOR_RATIO", 0.25)
    )
```

3. Module-level helper (place near `_merge_backend_timing`, before the SearchDaemon class):

```python
def _apply_tier_weight(
    result: Any,
    weight: float | None,
    top_score: float,
    floor_ratio: float,
) -> bool:
    """Multiply ``result.score`` by a path-prefix weight (Issue #4544).

    Returns True when the weight was applied. Skips: no/neutral weight,
    already-stamped results (``batch_search`` re-runs attach on results whose
    inner search already applied weights — without this guard the score would
    compound to weight²), and uplifts on results scoring below
    ``floor_ratio * top_score`` (metadata may reorder near-peers but must not
    lift weak matches past strong ones). Demotions always apply.
    """
    if weight is None or weight == 1.0:
        return False
    if getattr(result, "tier_boost", None) is not None:
        return False
    if weight > 1.0 and floor_ratio > 0.0 and result.score < floor_ratio * top_score:
        return False
    result.score *= weight
    result.tier_boost = weight
    return True
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_tier_boost.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/results.py src/nexus/bricks/search/path_context.py src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_tier_boost.py
git commit -m "feat(search): tier-weight primitives — record lookup, tier_boost field, apply helper, config knobs (#4544)"
```

---

### Task 3: boost inside `_attach_path_contexts` (+ batch block context rewire)

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` — `_attach_path_contexts` (~1647–1755) and the `batch_search` attach block (~2375–2390)
- Modify: `docs/superpowers/specs/2026-07-31-prefix-weight-boost-design.md` (documented deviation, see Step 3)
- Test: `tests/integration/bricks/search/test_daemon_context_attach.py`

**Interfaces:**
- Consumes: `lookup_record_in_records`, `_apply_tier_weight`, `DaemonConfig.tier_boost_floor_ratio` (Task 2).
- Produces: `_attach_path_contexts` now multiplies scores, stamps `tier_boost`, and stable-re-sorts the list in place when ≥1 weight applied. Signature unchanged: `async def _attach_path_contexts(self, results: list[SearchResult], *, zone_id: str | None = None) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/bricks/search/test_daemon_context_attach.py` (uses the file's existing `cache` fixture and the `SearchDaemon.__new__` harness pattern from `TestAttachUsesCallerZone.test_real_daemon_attach_falls_back_to_caller_zone`):

```python
def _bare_daemon(cache) -> "SearchDaemon":
    """Attach-only daemon harness (no startup())."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon

    daemon = SearchDaemon.__new__(SearchDaemon)
    daemon.config = DaemonConfig()
    daemon._path_context_cache = cache
    daemon._path_context_cache_by_loop = {}
    daemon._path_context_engines_by_loop = {}

    class _Stats:
        path_context_attach_failures = 0
        path_context_resolve_failures = 0

    daemon.stats = _Stats()
    return daemon


class TestTierWeightAttach:
    """Issue #4544: _attach_path_contexts applies per-prefix weights."""

    @pytest.mark.asyncio
    async def test_demotion_reorders_below_equal_relevance_peer(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _bare_daemon(cache)
        noisy = SearchResult(path="chat/a.md", chunk_text="", score=1.0)
        curated = SearchResult(path="docs/a.md", chunk_text="", score=0.9)
        results = [noisy, curated]
        await daemon._attach_path_contexts(results, zone_id="root")
        assert noisy.score == 0.5 and noisy.tier_boost == 0.5
        assert curated.score == 0.9 and curated.tier_boost is None
        assert results == [curated, noisy]  # re-sorted in place

    @pytest.mark.asyncio
    async def test_no_weights_leaves_list_untouched(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult

        # Fixture rows have no weight → byte-identity: same objects, order, scores.
        daemon = _bare_daemon(cache)
        r1 = SearchResult(path="docs/a.md", chunk_text="", score=0.4)
        r2 = SearchResult(path="docs/b.md", chunk_text="", score=0.9)
        results = [r1, r2]  # deliberately unsorted
        await daemon._attach_path_contexts(results, zone_id="root")
        assert results == [r1, r2]
        assert r1.score == 0.4 and r2.score == 0.9
        assert r1.tier_boost is None and r2.tier_boost is None
        assert r1.context == "Project documentation"  # context still attached

    @pytest.mark.asyncio
    async def test_uplift_gated_by_floor_ratio(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult

        await cache._store.upsert("root", "docs", "Project documentation", weight=2.0)
        daemon = _bare_daemon(cache)
        strong = SearchResult(path="src/x.py", chunk_text="", score=1.0)
        weak_boosted = SearchResult(path="docs/a.md", chunk_text="", score=0.1)
        near_peer_boosted = SearchResult(path="docs/b.md", chunk_text="", score=0.6)
        results = [strong, near_peer_boosted, weak_boosted]
        await daemon._attach_path_contexts(results, zone_id="root")
        assert weak_boosted.score == 0.1 and weak_boosted.tier_boost is None
        assert near_peer_boosted.score == 1.2 and near_peer_boosted.tier_boost == 2.0
        assert results[0] is near_peer_boosted  # promoted past strong

    @pytest.mark.asyncio
    async def test_double_attach_is_idempotent(self, cache) -> None:
        from nexus.bricks.search.daemon import SearchResult

        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _bare_daemon(cache)
        r = SearchResult(path="chat/a.md", chunk_text="", score=1.0)
        await daemon._attach_path_contexts([r], zone_id="root")
        await daemon._attach_path_contexts([r], zone_id="root")
        assert r.score == 0.5  # not 0.25
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_daemon_context_attach.py -q -k TierWeight`
Expected: FAIL — scores unmultiplied / `tier_boost` stays None.

- [ ] **Step 3: Implement**

In `_attach_path_contexts`, replace the final lookup loop (currently `for r in results: ... r.context = lookup_in_records(records, r.path)`) with:

```python
        from nexus.bricks.search.path_context import lookup_record_in_records

        # Issue #4544: apply per-prefix ranking weights while attaching
        # context. top_score is the pre-boost max of this batch — the floor
        # gate must compare against the unweighted ranking.
        floor_ratio = self.config.tier_boost_floor_ratio
        top_score = max((r.score for r in results), default=0.0)
        boosted_any = False
        for r in results:
            zone = _zone_for(r)
            records = snapshots.get(zone)
            if records is None:
                continue
            try:
                record = lookup_record_in_records(records, r.path)
                r.context = record.description if record is not None else None
                if record is not None and _apply_tier_weight(
                    r, record.weight, top_score, floor_ratio
                ):
                    boosted_any = True
            except Exception as exc:
                self.stats.path_context_attach_failures += 1
                logger.warning(
                    "path context lookup failed for path=%r (total=%d): %s",
                    r.path,
                    self.stats.path_context_attach_failures,
                    exc,
                )
        if boosted_any:
            # Stable sort: equal scores keep their pre-boost relative order.
            results.sort(key=lambda r: r.score, reverse=True)
```

(Delete the now-unused `from nexus.bricks.search.path_context import lookup_in_records` import in this method.)

In the `batch_search` attach block (~2375): swap `lookup_in_records` for the record-grain call but attach **context only** — do NOT apply weights there:

```python
            if records is not None:
                from nexus.bricks.search.path_context import lookup_record_in_records

                # Issue #4544 note: weights are NOT applied here. Each inner
                # self.search() already ran _attach_path_contexts (multiply +
                # tier_boost stamp); re-applying against post-boost scores
                # would re-evaluate the floor gate against a shifted top and
                # boost previously-gated results. This block only backfills
                # ``context`` for legacy/mocked daemons.
                for inner in results:
                    for r in inner:
                        try:
                            record = lookup_record_in_records(records, r.path)
                            r.context = record.description if record is not None else None
                        except Exception as exc:
                            self.stats.path_context_attach_failures += 1
                            logger.warning(
                                "path context lookup failed for path=%r (total=%d): %s",
                                r.path,
                                self.stats.path_context_attach_failures,
                                exc,
                            )
```

**Spec deviation (record it):** spec §2 says the batch block gets "the same boost logic"; implementation keeps it context-only for the reason in the comment above. Update the spec's §2 bullet to match (one sentence: "The `batch_search` inline attach block stays context-only — inner `search()` calls already applied weights; re-applying against post-boost scores would re-evaluate the floor gate against a shifted top.").

- [ ] **Step 4: Run to verify pass (plus no regressions in the file)**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_daemon_context_attach.py tests/unit/bricks/search/test_tier_boost.py -q`
Expected: PASS — all pre-existing attach tests still green (they exercise context-only rows, which take the `w is None` no-op path).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/integration/bricks/search/test_daemon_context_attach.py docs/superpowers/specs/2026-07-31-prefix-weight-boost-design.md
git commit -m "feat(search): apply per-prefix tier weights in _attach_path_contexts (#4544)"
```

---

### Task 4: conditional over-fetch + trim in `_search_on_current_loop`

**Files:**
- Modify: `src/nexus/bricks/search/daemon.py` — `_search_on_current_loop` (~1826–2018), new method `_zone_has_tier_weights`
- Test: `tests/unit/bricks/search/test_daemon_tier_boost_overfetch.py` (new)

**Interfaces:**
- Consumes: `_resolve_path_context_cache` / `PathContextCache.refresh_if_stale` / `snapshot_zone` (existing), `PathContextRecord.weight` (Task 1), boosted `_attach_path_contexts` (Task 3), `DaemonConfig.tier_boost_overfetch_factor` (Task 2).
- Produces: `async def _zone_has_tier_weights(self, zone_id: str) -> bool` on `SearchDaemon`. Public `search()` / `_search_on_current_loop` signatures unchanged.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/bricks/search/test_daemon_tier_boost_overfetch.py`. Harness mirrors `tests/unit/bricks/search/test_daemon_fusion_params.py::_make_daemon` (bare `SearchDaemon.__new__`, fake fts/vector backends) plus the sqlite path-context cache from `tests/integration/bricks/search/test_daemon_context_attach.py`:

```python
"""Conditional over-fetch for tier weights (Issue #4544).

When the effective zone has a path_contexts weight != 1.0 the daemon must
widen its backend fetch (limit × tier_boost_overfetch_factor), apply the
weights, re-sort, and trim back to the requested limit — so a boost can
promote a hit that the un-widened fusion would have cut. Zones with no
weights must hit the byte-identical legacy path (no widened fetch).
"""

from __future__ import annotations

from types import MethodType
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.bricks.search.path_context import PathContextCache, PathContextStore

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    weight FLOAT,
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
    yield PathContextCache(store=store)
    await engine.dispose()


def _make_daemon(cache: PathContextCache) -> Any:
    """Keyword-only daemon: N corpus docs, keyword_search honours ``limit``
    and records the limits it was asked for."""
    from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, SearchResult

    corpus = [
        SearchResult(path="chat/a.md", chunk_text="", score=10.0, search_type="keyword"),
        SearchResult(path="chat/b.md", chunk_text="", score=9.0, search_type="keyword"),
        SearchResult(path="docs/x.md", chunk_text="", score=8.0, search_type="keyword"),
    ]

    class FakeFtsBackend:
        def __init__(self) -> None:
            self.requested_limits: list[int] = []

        async def keyword_search(
            self,
            query: str,
            path: str,
            limit: int,
            zone_id: str,
            *,
            timing: dict[str, float] | None = None,
        ) -> list[Any]:
            self.requested_limits.append(limit)
            # Fresh copies: score mutation must not leak between searches.
            return [
                SearchResult(
                    path=r.path, chunk_text=r.chunk_text, score=r.score,
                    search_type=r.search_type,
                )
                for r in corpus[:limit]
            ]

    class FakeVectorBackend:
        async def semantic_search(
            self, qvec: list[float], path: str, limit: int, zone_id: str
        ) -> list[Any]:
            return []

    class _Stats:
        path_context_attach_failures = 0
        path_context_resolve_failures = 0

    daemon: Any = SearchDaemon.__new__(SearchDaemon)
    daemon.last_search_timing = {}
    daemon._initialized = True
    daemon._fts_backend = FakeFtsBackend()
    daemon._vector_backend = FakeVectorBackend()
    daemon._path_context_cache = cache
    daemon._path_context_cache_by_loop = {}
    daemon._path_context_engines_by_loop = {}
    daemon.stats = _Stats()
    daemon._track_latency = MethodType(lambda self, ms: None, daemon)
    daemon.config = DaemonConfig(page_aggregation=False)
    return daemon


class TestOverfetchAndTrim:
    @pytest.mark.asyncio
    async def test_demotion_promotes_below_cutoff_hit_into_returned_set(
        self, cache
    ) -> None:
        # limit=2 without weights returns the two chat docs. Demoting chat
        # must let docs/x.md (rank 3, beyond the un-widened fetch) into the
        # top 2 — this is exactly the promotion the over-fetch exists for.
        await cache._store.upsert("root", "chat", "Chat transcripts", weight=0.5)
        daemon = _make_daemon(cache)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert len(results) == 2  # trimmed back to the requested limit
        assert daemon._fts_backend.requested_limits == [2 * 3]  # widened fetch
        paths = [r.path for r in results]
        assert "docs/x.md" in paths
        assert results[0].path == "docs/x.md"  # 8.0 beats 10.0*0.5

    @pytest.mark.asyncio
    async def test_no_weights_keeps_legacy_fetch_size(self, cache) -> None:
        await cache._store.upsert("root", "chat", "Chat transcripts")  # no weight
        daemon = _make_daemon(cache)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]  # byte-identical path
        assert [r.path for r in results] == ["chat/a.md", "chat/b.md"]
        assert all(r.tier_boost is None for r in results)

    @pytest.mark.asyncio
    async def test_weight_one_rows_do_not_trigger_overfetch(self, cache) -> None:
        await cache._store.upsert("root", "chat", "Chat transcripts", weight=1.0)
        daemon = _make_daemon(cache)
        await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert daemon._fts_backend.requested_limits == [2]

    @pytest.mark.asyncio
    async def test_probe_failure_fails_soft(self, cache) -> None:
        daemon = _make_daemon(cache)

        async def _boom(self: Any) -> Any:
            raise RuntimeError("cache resolution exploded")

        daemon._resolve_path_context_cache = MethodType(_boom, daemon)
        results = await daemon._search_on_current_loop(
            "q", search_type="keyword", limit=2, zone_id="root"
        )
        assert [r.path for r in results] == ["chat/a.md", "chat/b.md"]
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_tier_boost_overfetch.py -q`
Expected: FAIL — `requested_limits == [2]` where `[6]` expected; `docs/x.md` absent.

- [ ] **Step 3: Implement**

Add method to `SearchDaemon` (place directly after `_resolve_path_context_cache`):

```python
    async def _zone_has_tier_weights(self, zone_id: str) -> bool:
        """True when the zone has any path-context weight != 1.0 (Issue #4544).

        Decides whether _search_on_current_loop widens its candidate fetch.
        Fail-soft: any error means "no over-fetch" — the search itself must
        never break on a weight probe, and _attach_path_contexts still runs
        its own fail-soft pass later in the same request. The refresh here is
        the same fingerprint check attach pays, so the steady-state cost is
        one cache hit.
        """
        try:
            cache = await self._resolve_path_context_cache()
            if cache is None:
                return False
            await cache.refresh_if_stale(zone_id)
            snapshot = cache.snapshot_zone(zone_id)
        except Exception as exc:
            logger.debug("tier-weight probe failed for zone=%r: %s", zone_id, exc)
            return False
        if not snapshot:
            return False
        return any(rec.weight is not None and rec.weight != 1.0 for rec in snapshot)
```

In `_search_on_current_loop`, right after `effective_zone_id = zone_id or ROOT_ZONE_ID`:

```python
        # Issue #4544: when the zone carries tier weights, widen every
        # candidate fetch so a boost can promote a below-cutoff hit, then trim
        # back to ``limit`` after _attach_path_contexts applies the weights.
        # Zones without weights keep internal_limit == limit and take the
        # byte-identical legacy path.
        has_tier_weights = await self._zone_has_tier_weights(effective_zone_id)
        internal_limit = (
            limit * self.config.tier_boost_overfetch_factor if has_tier_weights else limit
        )
```

Then replace fetch sizes (leave `alpha`/`fusion_method`/`rrf_k` untouched):

| Site (current line refs) | Change |
|---|---|
| keyword branch `_search_via_backends(..., limit=limit, ...)` (~1871) | `limit=internal_limit` |
| keyword fallback `_keyword_search(query, limit, path_filter, ...)` (~1895) | `_keyword_search(query, internal_limit, path_filter, ...)` |
| legacy hybrid prefetch `_keyword_search(query, limit * 3, ...)` (~1933) | `internal_limit * 3` |
| hybrid `_search_via_backends(..., limit=limit, ...)` (~1947) | `limit=internal_limit` |
| `_fuse_ranked_results(hybrid_keyword_results, results, limit)` (~1962) | pass `internal_limit` |
| `_semantic_search(query, limit, path_filter, ...)` (~1980) | `internal_limit` |
| `_hybrid_search(query, limit, path_filter, alpha, fusion_method, ...)` (~1982) | `internal_limit` |

And at each of the four attach+return sites (keyword backend ~1883, keyword fallback ~1923, hybrid backend ~1970, legacy fallback ~2013), trim after attach:

```python
                        await self._attach_path_contexts(backend_results, zone_id=effective_zone_id)
                        if internal_limit != limit:
                            backend_results = backend_results[:limit]
                        return self._with_search_timing(backend_results)
```

(Same three-line pattern with the local variable name at each site: `keyword_results`, `results`, `results`.)

- [ ] **Step 4: Run to verify pass + guard the neighbors**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/unit/bricks/search/test_daemon_tier_boost_overfetch.py tests/unit/bricks/search/test_daemon_fusion_params.py tests/unit/bricks/search/test_daemon_backend_routing.py tests/unit/bricks/search/test_final_list_page_pooling.py -q`
Expected: PASS — fusion-params byte-identity pins and backend routing untouched.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/daemon.py tests/unit/bricks/search/test_daemon_tier_boost_overfetch.py
git commit -m "feat(search): conditional over-fetch + trim so tier boosts can promote hits (#4544)"
```

---

### Task 5: API weight field, `tier_boost` serialization, CLI `--weight`, surface regen

**Files:**
- Modify: `src/nexus/server/api/v2/routers/path_contexts.py` (PathContextIn/Out, upsert, list)
- Modify: `src/nexus/server/api/v2/routers/search.py` (`_serialize_search_result`, ~156–188)
- Modify: `src/nexus/cli/commands/path_context.py` (set `--weight`, list rendering)
- Modify (regenerated): `docs/surface-coverage/api-rpc-surface-coverage.yaml`
- Test: `tests/integration/server/api/v2/routers/test_path_contexts_router.py`, `tests/unit/cli/test_path_context_cli.py`

**Interfaces:**
- Consumes: `PathContextStore.upsert(..., weight=...)` (Task 1); `SearchResult.tier_boost` (Task 2).
- Produces: HTTP surface — PUT/GET `/api/v2/path-contexts/` carry `weight`; search responses carry `tier_boost` when set. CLI: `nexus path-context set PREFIX DESC --weight 0.5`.

- [ ] **Step 1: Write the failing router tests**

Append to `tests/integration/server/api/v2/routers/test_path_contexts_router.py`, mirroring its existing client/auth fixtures:

```python
class TestWeightField:
    """Issue #4544: weight on the path-contexts CRUD surface."""

    def test_put_persists_and_echoes_weight(self, admin_client) -> None:
        resp = admin_client.put(
            "/api/v2/path-contexts/",
            json={"path_prefix": "chat/logs", "description": "Chat", "weight": 0.5},
        )
        assert resp.status_code == 200
        assert resp.json()["weight"] == 0.5
        listed = admin_client.get("/api/v2/path-contexts/").json()["contexts"]
        entry = next(c for c in listed if c["path_prefix"] == "chat/logs")
        assert entry["weight"] == 0.5

    def test_list_emits_null_weight_for_unset(self, admin_client) -> None:
        admin_client.put(
            "/api/v2/path-contexts/",
            json={"path_prefix": "docs", "description": "Docs"},
        )
        listed = admin_client.get("/api/v2/path-contexts/").json()["contexts"]
        entry = next(c for c in listed if c["path_prefix"] == "docs")
        assert entry["weight"] is None

    def test_weight_bounds_rejected(self, admin_client) -> None:
        for bad in (0.05, 10.5, 0.0, -1.0):
            resp = admin_client.put(
                "/api/v2/path-contexts/",
                json={"path_prefix": "x", "description": "d", "weight": bad},
            )
            assert resp.status_code == 422, f"weight={bad} must be rejected"
```

(Adapt the fixture name to the file's actual admin-auth client fixture.)

And the serializer test — append to the serializer test class in `tests/integration/bricks/search/test_daemon_context_attach.py` (near `test_context_field_emitted_when_set`, which pins `_serialize_search_result`):

```python
    def test_tier_boost_emitted_when_set_and_omitted_when_none(self) -> None:
        from nexus.bricks.search.daemon import SearchResult
        from nexus.server.api.v2.routers.search import _serialize_search_result

        boosted = SearchResult(path="a", chunk_text="", score=0.5, tier_boost=0.5)
        plain = SearchResult(path="b", chunk_text="", score=0.5)
        assert _serialize_search_result(boosted)["tier_boost"] == 0.5
        assert "tier_boost" not in _serialize_search_result(plain)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/server/api/v2/routers/test_path_contexts_router.py -q -k Weight`
Expected: FAIL — `weight` key absent / 200 where 422 expected.

- [ ] **Step 3: Implement router + serializer**

`src/nexus/server/api/v2/routers/path_contexts.py`:

```python
class PathContextIn(BaseModel):
    zone_id: str = Field(default=ROOT_ZONE_ID, max_length=255)
    path_prefix: str = Field(max_length=1024)
    description: str = Field(max_length=4096, min_length=1)
    # Issue #4544: per-prefix ranking weight; None ≡ 1.0. PUT-replace
    # semantics — omitting weight on an update clears any stored value.
    weight: float | None = Field(default=None, ge=0.1, le=10.0)
```

`PathContextOut` gains `weight: float | None = None`. Upsert endpoint:

```python
    await store.upsert(body.zone_id, body.path_prefix, body.description, weight=body.weight)
    return {
        "zone_id": body.zone_id,
        "path_prefix": body.path_prefix,
        "description": body.description,
        "weight": body.weight,
    }
```

List endpoint dict gains `"weight": r.weight,` after `"description"`.

`src/nexus/server/api/v2/routers/search.py` `_serialize_search_result` — after the `context` block:

```python
    tier_boost = getattr(result, "tier_boost", None)
    if tier_boost is not None:
        out["tier_boost"] = round(tier_boost, 4)
```

- [ ] **Step 4: Implement CLI**

`src/nexus/cli/commands/path_context.py` — `set` command gains:

```python
@click.option(
    "--weight",
    type=float,
    default=None,
    help="Ranking weight 0.1-10.0 (<1 demotes, >1 boosts; omit to clear).",
)
```

Thread `weight: float | None` through `path_context_set`'s signature and body:

```python
    result = client.put(
        "/api/v2/path-contexts/",
        {
            "zone_id": zone_id,
            "path_prefix": path_prefix,
            "description": description,
            "weight": weight,
        },
    )
```

Success line appends the weight when set:

```python
    weight_note = f" [weight={result.get('weight')}]" if result.get("weight") is not None else ""
    console.print(
        f"[nexus.success]set[/nexus.success] "
        f"{result.get('zone_id')}:{result.get('path_prefix')} = "
        f"{result.get('description')!r}{weight_note}"
    )
```

`list` rendering appends the same `[weight=...]` suffix per entry when `entry.get("weight") is not None`.

**Fix the existing CLI payload pin:** `tests/unit/cli/test_path_context_cli.py::TestPathContextSet::test_put_sends_expected_payload` asserts the exact PUT dict — it must gain `"weight": None`:

```python
        fake.put.assert_called_once_with(
            "/api/v2/path-contexts/",
            {"zone_id": "root", "path_prefix": "src", "description": "x", "weight": None},
        )
```

Then add to the same class (same `CliRunner`/`_patched_client` harness):

```python
    def test_weight_option_sent_in_payload(self) -> None:
        runner = CliRunner(env=_ENV)
        fake = _patched_client(
            put={
                "zone_id": "root",
                "path_prefix": "chat",
                "description": "x",
                "weight": 0.5,
            }
        )
        with patch("nexus.cli.api_client.get_api_client_from_options", return_value=fake):
            result = runner.invoke(
                path_context,
                ["set", "chat", "x", "--weight", "0.5", "--remote-url", MOCK_URL],
            )
        assert result.exit_code == 0, result.output
        assert fake.put.call_args.args[1]["weight"] == 0.5
        assert "[weight=0.5]" in result.output
```

- [ ] **Step 5: Run router + CLI + serializer tests**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/server/api/v2/routers/test_path_contexts_router.py tests/unit/cli/test_path_context_cli.py tests/integration/bricks/search/test_daemon_context_attach.py -q`
Expected: PASS.

- [ ] **Step 6: Regenerate the surface-coverage YAML**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python scripts/gen_api_surface_coverage.py`
Then: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/surface_coverage -q`
Expected: YAML diff limited to path-contexts/search entries; surface tests PASS. (#4541 lesson: router edits without regen fail CI.)

- [ ] **Step 7: Commit**

```bash
git add src/nexus/server/api/v2/routers/path_contexts.py src/nexus/server/api/v2/routers/search.py src/nexus/cli/commands/path_context.py docs/surface-coverage/api-rpc-surface-coverage.yaml tests/integration/server/api/v2/routers/test_path_contexts_router.py tests/unit/cli/test_path_context_cli.py tests/integration/bricks/search/test_daemon_context_attach.py
git commit -m "feat(search): expose path-context weight via API/CLI, emit tier_boost attribution (#4544)"
```

---

### Task 6: federated wire hygiene + owning-zone weight test

**Files:**
- Modify: `src/nexus/bricks/search/federated_search.py` (`_strip_none_context`, ~865)
- Test: `tests/integration/bricks/search/test_federated_search.py`

**Interfaces:**
- Consumes: `tier_boost` field (Task 2); daemon-side boosting (Tasks 3–4).
- Produces: federated result dicts never carry `tier_boost: null`; carry the value when set. No signature changes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/bricks/search/test_federated_search.py`, following its existing dispatcher/mock-daemon patterns:

```python
class TestTierBoostFederated:
    """Issue #4544: owning-zone weights flow through the federated merge."""

    def test_strip_removes_null_tier_boost_and_keeps_value(self) -> None:
        from nexus.bricks.search.federated_search import _strip_none_context

        assert "tier_boost" not in _strip_none_context(
            {"path": "a", "score": 1.0, "tier_boost": None}
        )
        assert _strip_none_context({"path": "a", "score": 1.0, "tier_boost": 0.5})[
            "tier_boost"
        ] == 0.5

    def test_result_to_dict_omits_unset_tier_boost(self) -> None:
        from nexus.bricks.search.federated_search import _result_to_dict
        from nexus.bricks.search.results import BaseSearchResult

        plain = BaseSearchResult(path="a", chunk_text="", score=1.0, zone_id="z1")
        boosted = BaseSearchResult(
            path="b", chunk_text="", score=0.5, zone_id="z1", tier_boost=0.5
        )
        assert "tier_boost" not in _result_to_dict(plain)
        assert _result_to_dict(boosted)["tier_boost"] == 0.5

    def test_merge_by_raw_score_orders_on_boosted_scores(self) -> None:
        from nexus.bricks.search.federated_search import _merge_by_raw_score
        from nexus.bricks.search.results import BaseSearchResult

        # zone-a demoted its chat hit (1.0 -> 0.4) before returning; zone-b's
        # unweighted 0.6 must now outrank it in the merged list.
        za = BaseSearchResult(
            path="chat/a.md", chunk_text="", score=0.4, zone_id="za", tier_boost=0.4
        )
        zb = BaseSearchResult(path="docs/b.md", chunk_text="", score=0.6, zone_id="zb")
        merged = _merge_by_raw_score([("za", [za]), ("zb", [zb])], limit=2)
        assert [m["path"] for m in merged] == ["docs/b.md", "chat/a.md"]
        assert merged[1]["tier_boost"] == 0.4
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_federated_search.py -q -k TierBoost`
Expected: FAIL — `tier_boost: None` present in stripped dict.

- [ ] **Step 3: Implement**

`src/nexus/bricks/search/federated_search.py` — extend `_strip_none_context` (name kept; 4 call sites unchanged):

```python
def _strip_none_context(d: dict[str, Any]) -> dict[str, Any]:
    """Match the non-federated router's omit-when-None contract for
    ``context`` (Issue #3773, review Rounds 5-6) and ``tier_boost``
    (Issue #4544): every federated emission path must route through this so
    ``null`` never leaks onto the wire and the fusion strategies stay
    shape-consistent."""
    for key in ("context", "tier_boost"):
        if d.get(key) is None:
            d.pop(key, None)
    return d
```

Also verify (read, no code change expected) that the remote-zone RPC search response path serializes result dicts from the daemon's dataclasses — `tier_boost` then flows automatically; if the remote handler whitelists fields, add `tier_boost` there and note it in the commit message. Start from `FederatedSearchDispatcher._search_remote_zone` (`transport.call_rpc("search", ...)`) and grep the servicer: `grep -rn "\"search\"" src/nexus/server --include="*.py"`.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/integration/bricks/search/test_federated_search.py -q`
Expected: PASS (whole file — no regressions in existing federated tests).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/federated_search.py tests/integration/bricks/search/test_federated_search.py
git commit -m "feat(search): keep tier_boost off the federated wire when unset (#4544)"
```

---

### Task 7: full regression sweep

**Files:**
- No new files — verification only (fix anything it surfaces).

**Interfaces:**
- Consumes: everything above.
- Produces: green suite; branch ready for review/PR flow.

- [ ] **Step 1: Run the focused search + storage + router suites**

Run:
```bash
PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest \
  tests/unit/bricks/search tests/integration/bricks/search \
  tests/integration/server/api/v2/routers/test_path_contexts_router.py \
  tests/unit/cli/test_path_context_cli.py \
  tests/surface_coverage -q
```
Expected: PASS. These suites contain the byte-identity pins (`test_daemon_fusion_params.py` legacy-fusion regression, page-pooling tests, all #3773 attach tests) — any failure here is a real regression, not noise.

- [ ] **Step 2: Run the migration-adjacent tests**

Run: `PYTHONPATH=$PWD/src /Users/tafeng/nexus/.venv/bin/python -m pytest tests/migrations tests/unit/storage -q`
Expected: PASS.

- [ ] **Step 3: Lint**

Run: `pre-commit run --files $(git diff --name-only develop...HEAD | tr '\n' ' ')`
Expected: PASS (ruff/format clean). Fix and amend if not.

- [ ] **Step 4: Final commit (only if fixes were needed)**

```bash
git add -A && git commit -m "test(search): regression fixes for tier-weight sweep (#4544)"
```

---

## Self-review notes (already applied)

- Spec §2 batch-block deviation is intentional and documented in Task 3 (double-gate wobble on post-boost scores); the spec file is amended in the same commit.
- Spec coverage check: schema/store (§1→Task 1), attach boost + floor gate + idempotency (§2→Tasks 2–3), over-fetch/trim (§3→Task 4), API/CLI/serializer/surface (§4→Task 5), federated (§5→Task 6), config knobs (§6→Task 2), testing incl. byte-identity (§7→all + Task 7).
- Type consistency: `weight: float | None` everywhere; `tier_boost: float | None`; `_apply_tier_weight(result, weight, top_score, floor_ratio) -> bool`; `_zone_has_tier_weights(zone_id: str) -> bool`.
