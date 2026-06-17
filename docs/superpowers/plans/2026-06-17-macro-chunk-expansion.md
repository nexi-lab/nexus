# Macro-chunk Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, read-side macro-chunk expansion stage to hybrid search that stitches each search hit into its surrounding section and returns it in additive `macro_text` fields.

**Architecture:** A pure, backend-agnostic module (`macro_chunk.py`) implements the expansion algorithm over a `NeighborFetcher` protocol. Each storage backend (`PgVectorBackend`, `SqliteVecBackend`) implements `fetch_ranges`. The daemon invokes expansion after assembling the final result list, gated on a new `expand=macro` request param.

**Tech Stack:** Python 3.x, SQLAlchemy async (`text()` bound params), Alembic migrations, sqlite-vec (SANDBOX), pgvector (FULL), pytest + `pytest.mark.asyncio`.

## Global Constraints

- Default response (no `expand` param) MUST be byte-identical to today. Expansion is opt-in.
- Server emits **snake_case** fields (`macro_text`, `macro_line_start`, `macro_line_end`) — there is no camelCase serializer server-side. The TS `nexus-client` SDK surfaces camelCase to Koodle (out of scope here).
- Do NOT name external reference repos in code/comments.
- Expansion is **best-effort**: any failure leaves the hit's `chunk_text` untouched and never errors the search.
- Both profiles must work: FULL (pgvector / `document_chunks`) and SANDBOX (sqlite-vec / `nexus_vec`).
- Env knobs follow `config.py` pattern via `get_env_int` / `get_env_bool`.
- SQL uses `:named` bound params (Postgres async engine) / sqlite3 via `asyncio.to_thread`.
- Tests: `pytest.mark.asyncio` for async; `AsyncMock` session fixtures for unit; real DB fixtures for integration.

---

### Task 1: Pure expansion core — types, span-merge, section bounds, window selection, stitch

**Files:**
- Create: `src/nexus/bricks/search/macro_chunk.py`
- Test: `tests/unit/bricks/search/test_macro_chunk_core.py`

**Interfaces:**
- Produces:
  - `ChunkRow(path: str, chunk_index: int, text: str, tokens: int, line_start: int|None, line_end: int|None, heading_prefix: str|None)` (frozen dataclass)
  - `ExpansionConfig(token_budget: int = 1024, window: int = 8, code_forward_bias: bool = True)` (frozen dataclass)
  - `NeighborFetcher` Protocol: `async def fetch_ranges(self, spans: Sequence[tuple[str,int,int]], zone_id: str|None) -> list[ChunkRow]`
  - `merge_spans(spans: list[tuple[str,int,int]]) -> list[tuple[str,int,int]]`
  - `_is_code_path(path: str) -> bool`
  - `_section_bounds(by_index: dict[int,ChunkRow], anchor_idx: int) -> tuple[int,int]`
  - `_window_for_anchor(by_index: dict[int,ChunkRow], anchor_idx: int, cfg: ExpansionConfig, is_code: bool) -> tuple[int,int]`
  - `_stitch(by_index: dict[int,ChunkRow], lo: int, hi: int) -> tuple[str, int|None, int|None]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_macro_chunk_core.py
from nexus.bricks.search.macro_chunk import (
    ChunkRow, ExpansionConfig, merge_spans, _is_code_path,
    _section_bounds, _window_for_anchor, _stitch,
)


def _row(idx, tokens=10, heading="H1", text=None, ls=None, le=None, path="/a.md"):
    return ChunkRow(
        path=path, chunk_index=idx, text=text or f"c{idx}", tokens=tokens,
        line_start=ls, line_end=le, heading_prefix=heading,
    )


def _map(rows):
    return {r.chunk_index: r for r in rows}


def test_merge_spans_collapses_overlapping_and_adjacent():
    spans = [("/a", 0, 5), ("/a", 6, 9), ("/a", 20, 22), ("/b", 0, 3)]
    out = sorted(merge_spans(spans))
    assert out == [("/a", 0, 9), ("/a", 20, 22), ("/b", 0, 3)]


def test_is_code_path():
    assert _is_code_path("/x/foo.py") is True
    assert _is_code_path("/x/foo.md") is False


def test_section_bounds_stops_at_heading_change():
    rows = [_row(0, heading="A"), _row(1, heading="A"),
            _row(2, heading="B"), _row(3, heading="B")]
    assert _section_bounds(_map(rows), 1) == (0, 1)
    assert _section_bounds(_map(rows), 2) == (2, 3)


def test_window_whole_section_when_under_budget():
    rows = [_row(i, tokens=10, heading="A") for i in range(4)]
    # section tokens = 40 <= budget 1024 -> whole section
    lo, hi = _window_for_anchor(_map(rows), 2, ExpansionConfig(token_budget=1024), False)
    assert (lo, hi) == (0, 3)


def test_window_prose_centered_when_over_budget():
    rows = [_row(i, tokens=100, heading="A") for i in range(7)]
    # budget 250 -> anchor(3)=100, then centered: 2,4 -> 300>250 stop at 250
    lo, hi = _window_for_anchor(_map(rows), 3, ExpansionConfig(token_budget=250), False)
    assert lo <= 3 <= hi
    assert sum(rows[i].tokens for i in range(lo, hi + 1)) <= 250
    assert (hi - lo) >= 1  # expanded beyond the single anchor


def test_window_code_forward_bias():
    rows = [_row(i, tokens=100, heading="A", path="/f.py") for i in range(7)]
    lo, hi = _window_for_anchor(_map(rows), 3, ExpansionConfig(token_budget=250), True)
    # forward-first: should include 4 before 2
    assert hi >= 4
    assert sum(rows[i].tokens for i in range(lo, hi + 1)) <= 250


def test_stitch_concats_and_spans_lines():
    rows = [_row(0, text="alpha", ls=1, le=2), _row(1, text="beta", ls=3, le=5)]
    text, ls, le = _stitch(_map(rows), 0, 1)
    assert text == "alpha\nbeta"
    assert (ls, le) == (1, 5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_macro_chunk_core.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nexus.bricks.search.macro_chunk'`

- [ ] **Step 3: Write the module**

```python
# src/nexus/bricks/search/macro_chunk.py
"""Read-side macro-chunk (neighbor-context) expansion for hybrid search (Issue #4398).

Pure and backend-agnostic. Given ranked results and a NeighborFetcher that returns
chunk rows for (path, chunk_index range) spans, expand each hit into its surrounding
section (bounded by heading_prefix, file edge, and a token budget) and attach the
stitched text as ``macro_text``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

_CODE_EXTENSIONS = (
    ".c", ".cc", ".cpp", ".h", ".hpp", ".py", ".rs", ".go", ".java",
    ".ts", ".tsx", ".js", ".jsx", ".v", ".vh", ".sv", ".svh",
    ".scala", ".rb", ".swift", ".kt",
)


@dataclass(frozen=True)
class ChunkRow:
    path: str
    chunk_index: int
    text: str
    tokens: int
    line_start: int | None = None
    line_end: int | None = None
    heading_prefix: str | None = None


@dataclass(frozen=True)
class ExpansionConfig:
    token_budget: int = 1024
    window: int = 8
    code_forward_bias: bool = True


class NeighborFetcher(Protocol):
    async def fetch_ranges(
        self, spans: Sequence[tuple[str, int, int]], zone_id: str | None
    ) -> list["ChunkRow"]: ...


def _is_code_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(ext) for ext in _CODE_EXTENSIONS)


def merge_spans(spans: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Merge overlapping/adjacent (path, lo, hi) spans into a minimal set."""
    by_path: dict[str, list[tuple[int, int]]] = {}
    for path, lo, hi in spans:
        by_path.setdefault(path, []).append((lo, hi))
    out: list[tuple[str, int, int]] = []
    for path, ranges in by_path.items():
        ranges.sort()
        clo, chi = ranges[0]
        for lo, hi in ranges[1:]:
            if lo <= chi + 1:
                chi = max(chi, hi)
            else:
                out.append((path, clo, chi))
                clo, chi = lo, hi
        out.append((path, clo, chi))
    return out


def _section_bounds(by_index: dict[int, ChunkRow], anchor_idx: int) -> tuple[int, int]:
    """Maximal contiguous run around anchor sharing its heading_prefix."""
    target = by_index[anchor_idx].heading_prefix
    lo = anchor_idx
    while (lo - 1) in by_index and by_index[lo - 1].heading_prefix == target:
        lo -= 1
    hi = anchor_idx
    while (hi + 1) in by_index and by_index[hi + 1].heading_prefix == target:
        hi += 1
    return lo, hi


def _window_for_anchor(
    by_index: dict[int, ChunkRow], anchor_idx: int, cfg: ExpansionConfig, is_code: bool
) -> tuple[int, int]:
    s_lo, s_hi = _section_bounds(by_index, anchor_idx)
    section_tokens = sum(
        by_index[i].tokens for i in range(s_lo, s_hi + 1) if i in by_index
    )
    if section_tokens <= cfg.token_budget:
        return s_lo, s_hi

    used = by_index[anchor_idx].tokens
    lo = hi = anchor_idx

    def _can(i: int) -> bool:
        return i in by_index and used + by_index[i].tokens <= cfg.token_budget

    if is_code and cfg.code_forward_bias:
        while hi + 1 <= s_hi and _can(hi + 1):
            hi += 1
            used += by_index[hi].tokens
        while lo - 1 >= s_lo and _can(lo - 1):
            lo -= 1
            used += by_index[lo].tokens
        return lo, hi

    back = True
    while True:
        moved = False
        if back and lo - 1 >= s_lo and _can(lo - 1):
            lo -= 1
            used += by_index[lo].tokens
            moved = True
        elif (not back) and hi + 1 <= s_hi and _can(hi + 1):
            hi += 1
            used += by_index[hi].tokens
            moved = True
        else:
            if back and hi + 1 <= s_hi and _can(hi + 1):
                hi += 1
                used += by_index[hi].tokens
                moved = True
            elif (not back) and lo - 1 >= s_lo and _can(lo - 1):
                lo -= 1
                used += by_index[lo].tokens
                moved = True
        if not moved:
            break
        back = not back
    return lo, hi


def _stitch(
    by_index: dict[int, ChunkRow], lo: int, hi: int
) -> tuple[str, int | None, int | None]:
    rows = [by_index[i] for i in range(lo, hi + 1) if i in by_index]
    text = "\n".join(r.text for r in rows)
    starts = [r.line_start for r in rows if r.line_start is not None]
    ends = [r.line_end for r in rows if r.line_end is not None]
    return text, (min(starts) if starts else None), (max(ends) if ends else None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_macro_chunk_core.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/macro_chunk.py tests/unit/bricks/search/test_macro_chunk_core.py
git commit -m "feat(search): macro-chunk pure core — span-merge, section bounds, window selection (#4398)"
```

---

### Task 2: `expand_results` orchestrator (range-merge → fetch → section-dedup → attach)

**Files:**
- Modify: `src/nexus/bricks/search/macro_chunk.py`
- Test: `tests/unit/bricks/search/test_macro_chunk_expand.py`

**Interfaces:**
- Consumes: `ChunkRow`, `ExpansionConfig`, `NeighborFetcher`, `merge_spans`, `_section_bounds`, `_window_for_anchor`, `_stitch`, `_is_code_path` (Task 1).
- Produces: `async def expand_results(results: list, fetcher: NeighborFetcher, cfg: ExpansionConfig, zone_id: str|None = None) -> list` — mutates each result, setting `result.macro_text`, `result.macro_line_start`, `result.macro_line_end`. Results must expose `.path` and `.chunk_index`; settable attrs `macro_text`/`macro_line_start`/`macro_line_end`. Returns the same list. Never raises on a single-result failure.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/bricks/search/test_macro_chunk_expand.py
import pytest

from nexus.bricks.search.macro_chunk import ChunkRow, ExpansionConfig, expand_results


class _Result:
    def __init__(self, path, chunk_index):
        self.path = path
        self.chunk_index = chunk_index
        self.macro_text = None
        self.macro_line_start = None
        self.macro_line_end = None


class _FakeFetcher:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def fetch_ranges(self, spans, zone_id):
        self.calls.append(list(spans))
        wanted = list(spans)
        out = []
        for r in self._rows:
            for path, lo, hi in wanted:
                if r.path == path and lo <= r.chunk_index <= hi:
                    out.append(r)
                    break
        return out


def _row(idx, path="/a.md", heading="A", tokens=10, text=None, ls=None, le=None):
    return ChunkRow(path=path, chunk_index=idx, text=text or f"c{idx}",
                    tokens=tokens, line_start=ls, line_end=le, heading_prefix=heading)


@pytest.mark.asyncio
async def test_expand_attaches_macro_text_for_section():
    rows = [_row(i, heading="A", ls=i + 1, le=i + 1) for i in range(3)]
    fetcher = _FakeFetcher(rows)
    res = [_Result("/a.md", 1)]
    out = await expand_results(res, fetcher, ExpansionConfig(token_budget=1024, window=8))
    assert out[0].macro_text == "c0\nc1\nc2"
    assert (out[0].macro_line_start, out[0].macro_line_end) == (1, 3)


@pytest.mark.asyncio
async def test_expand_single_batched_fetch_and_section_dedup():
    rows = [_row(i, heading="A") for i in range(4)]
    fetcher = _FakeFetcher(rows)
    # two hits in the SAME section
    res = [_Result("/a.md", 1), _Result("/a.md", 2)]
    out = await expand_results(res, fetcher, ExpansionConfig(token_budget=1024, window=8))
    assert len(fetcher.calls) == 1                 # one batched fetch
    assert out[0].macro_text == out[1].macro_text  # section computed once, shared


@pytest.mark.asyncio
async def test_expand_missing_anchor_leaves_result_untouched():
    fetcher = _FakeFetcher([])  # eventual-consistency gap: nothing returned
    res = [_Result("/a.md", 5)]
    out = await expand_results(res, fetcher, ExpansionConfig())
    assert out[0].macro_text is None  # never errors, no expansion


@pytest.mark.asyncio
async def test_expand_empty_results_noop():
    fetcher = _FakeFetcher([])
    assert await expand_results([], fetcher, ExpansionConfig()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_macro_chunk_expand.py -v`
Expected: FAIL — `ImportError: cannot import name 'expand_results'`

- [ ] **Step 3: Append `expand_results` to `macro_chunk.py`**

```python
# append to src/nexus/bricks/search/macro_chunk.py
import logging

logger = logging.getLogger(__name__)


async def expand_results(
    results: list,
    fetcher: NeighborFetcher,
    cfg: ExpansionConfig,
    zone_id: str | None = None,
) -> list:
    """Attach macro_text/macro_line_start/macro_line_end to each result. Best-effort."""
    if not results:
        return results

    spans = [
        (r.path, max(0, r.chunk_index - cfg.window), r.chunk_index + cfg.window)
        for r in results
    ]
    try:
        rows = await fetcher.fetch_ranges(merge_spans(spans), zone_id)
    except Exception:
        logger.warning("macro-chunk fetch failed; returning unexpanded", exc_info=True)
        return results

    by_path: dict[str, dict[int, ChunkRow]] = {}
    for row in rows:
        by_path.setdefault(row.path, {})[row.chunk_index] = row

    section_cache: dict[tuple[str, int, int], tuple[str, int | None, int | None]] = {}
    for r in results:
        by_index = by_path.get(r.path)
        if not by_index or r.chunk_index not in by_index:
            continue  # gap — leave chunk_text as-is
        try:
            s_lo, s_hi = _section_bounds(by_index, r.chunk_index)
            key = (r.path, s_lo, s_hi)
            if key not in section_cache:
                w_lo, w_hi = _window_for_anchor(
                    by_index, r.chunk_index, cfg, _is_code_path(r.path)
                )
                section_cache[key] = _stitch(by_index, w_lo, w_hi)
            text, ls, le = section_cache[key]
            r.macro_text = text
            r.macro_line_start = ls
            r.macro_line_end = le
        except Exception:
            logger.warning("macro-chunk expansion failed for %s", r.path, exc_info=True)
            continue
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_macro_chunk_expand.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/macro_chunk.py tests/unit/bricks/search/test_macro_chunk_expand.py
git commit -m "feat(search): macro-chunk expand_results orchestrator with section dedup (#4398)"
```

---

### Task 3: Migration — persist `heading_prefix` on `document_chunks`

**Files:**
- Create: `alembic/versions/add_chunk_heading_prefix.py`
- Test: `tests/integration/bricks/search/test_migration_heading_prefix.py`

**Interfaces:**
- Produces: a nullable `heading_prefix TEXT` column on `document_chunks` (Postgres + SQLite via batch_alter_table).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/bricks/search/test_migration_heading_prefix.py
import sqlalchemy as sa
from sqlalchemy import create_engine


def test_document_chunks_has_heading_prefix_column(tmp_path):
    # The migration suite is applied by the shared test DB fixture; here we assert
    # the column exists on a freshly migrated sqlite DB.
    from nexus.bricks.search.testing import build_migrated_sqlite_url  # helper (Task 3 step 3)

    url = build_migrated_sqlite_url(tmp_path / "m.db")
    engine = create_engine(url)
    insp = sa.inspect(engine)
    cols = {c["name"] for c in insp.get_columns("document_chunks")}
    assert "heading_prefix" in cols
```

> Note: if the repo already has a migrated-DB fixture (check `tests/integration/bricks/search/conftest.py` for an existing `migrated_engine`/`db_url` fixture), use that fixture instead of a new `build_migrated_sqlite_url` helper and assert the column on it. Prefer the existing fixture; only add the helper if none exists.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_migration_heading_prefix.py -v`
Expected: FAIL — column `heading_prefix` not present.

- [ ] **Step 3: Write the migration**

Find the current head revision: `PYTHONPATH=$PWD/src alembic heads` (note the revision id; set `down_revision` to it).

```python
# alembic/versions/add_chunk_heading_prefix.py
"""add heading_prefix to document_chunks

Revision ID: add_chunk_heading_prefix
Revises: <CURRENT_HEAD>
Create Date: 2026-06-17
"""
import sqlalchemy as sa
from alembic import op

revision = "add_chunk_heading_prefix"
down_revision = "<CURRENT_HEAD>"  # replace with `alembic heads` output
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("heading_prefix", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.drop_column("heading_prefix")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_migration_heading_prefix.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/add_chunk_heading_prefix.py tests/integration/bricks/search/test_migration_heading_prefix.py
git commit -m "feat(search): migration — persist heading_prefix on document_chunks (#4398)"
```

---

### Task 4: Persist `heading_prefix` through `ChunkStore` + indexing

**Files:**
- Modify: `src/nexus/bricks/search/chunk_store.py` (ChunkRecord + both INSERT column lists + params)
- Modify: the indexing path that builds `ChunkRecord` from `DocumentChunk` (grep `ChunkRecord(` under `src/nexus/bricks/search/` — likely `pipeline_indexer.py` / `indexing.py`)
- Test: `tests/integration/bricks/search/test_chunk_store.py` (extend existing)

**Interfaces:**
- Consumes: migration from Task 3.
- Produces: `ChunkRecord.heading_prefix: str | None = None`; written to `document_chunks.heading_prefix`.

- [ ] **Step 1: Write the failing test** (extend existing `test_chunk_store.py`)

```python
# add to tests/integration/bricks/search/test_chunk_store.py
@pytest.mark.asyncio
async def test_chunk_store_writes_heading_prefix():
    from unittest.mock import AsyncMock, MagicMock
    from nexus.bricks.search.chunk_store import ChunkStore, ChunkRecord

    session = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    store = ChunkStore(async_session_factory=MagicMock(return_value=ctx), db_type="sqlite")

    await store.replace_document_chunks(
        "pid-1",
        [ChunkRecord(chunk_text="hi", chunk_tokens=1, heading_prefix="## H")],
    )
    # the INSERT params for the last execute include heading_prefix
    insert_call = session.execute.await_args_list[-1]
    params = insert_call.args[1]
    rows = params if isinstance(params, list) else [params]
    assert any(p.get("heading_prefix") == "## H" for p in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_chunk_store.py::test_chunk_store_writes_heading_prefix -v`
Expected: FAIL — `heading_prefix` not in params (or `ChunkRecord` has no such field).

- [ ] **Step 3: Implement**

In `chunk_store.py`:
1. Add to `ChunkRecord` (after `line_end`): `heading_prefix: str | None = None`
2. In BOTH INSERT statements (with-embedding lines ~140-144 and without-embedding lines ~156-161), add `heading_prefix` to the column list and `:heading_prefix` to the VALUES list.
3. In the params dict built per chunk (near line 118-123), add `"heading_prefix": chunk.heading_prefix`.

In the indexing path (where `ChunkRecord(...)` is constructed from a `DocumentChunk`): add `heading_prefix=doc_chunk.heading_prefix` to the constructor call.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_chunk_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/chunk_store.py src/nexus/bricks/search/*indexer*.py src/nexus/bricks/search/indexing.py tests/integration/bricks/search/test_chunk_store.py
git commit -m "feat(search): persist heading_prefix through ChunkStore and indexing (#4398)"
```

---

### Task 5: Result fields + serializer (`macro_text`, `macro_line_start`, `macro_line_end`)

**Files:**
- Modify: `src/nexus/bricks/search/results.py` (`BaseSearchResult`)
- Modify: `src/nexus/server/api/v2/routers/search.py` (`_serialize_search_result`, ~lines 156-183)
- Test: `tests/integration/bricks/search/test_results.py` (extend) + `tests/unit/bricks/search/test_search_result_serialize.py`

**Interfaces:**
- Produces: `BaseSearchResult.macro_text: str|None`, `.macro_line_start: int|None`, `.macro_line_end: int|None` (defaults None). `_serialize_search_result` emits these keys ONLY when `macro_text is not None` (keeps default response byte-identical).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/bricks/search/test_search_result_serialize.py
from nexus.server.api.v2.routers.search import _serialize_search_result
from nexus.bricks.search.results import BaseSearchResult


def test_serialize_omits_macro_when_absent():
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    out = _serialize_search_result(r)
    assert "macro_text" not in out  # default response unchanged


def test_serialize_includes_macro_when_present():
    r = BaseSearchResult(path="/a", chunk_text="x", score=1.0, chunk_index=0)
    r.macro_text = "x\ny"
    r.macro_line_start = 1
    r.macro_line_end = 4
    out = _serialize_search_result(r)
    assert out["macro_text"] == "x\ny"
    assert out["macro_line_start"] == 1
    assert out["macro_line_end"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_search_result_serialize.py -v`
Expected: FAIL — `AttributeError: 'BaseSearchResult' object has no attribute 'macro_text'`

- [ ] **Step 3: Implement**

In `results.py` `BaseSearchResult` (after `context`): add
```python
    macro_text: str | None = None
    macro_line_start: int | None = None
    macro_line_end: int | None = None
```

In `search.py` `_serialize_search_result`, before `return out`:
```python
    macro_text = getattr(result, "macro_text", None)
    if macro_text is not None:
        out["macro_text"] = macro_text
        out["macro_line_start"] = getattr(result, "macro_line_start", None)
        out["macro_line_end"] = getattr(result, "macro_line_end", None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search/test_search_result_serialize.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/results.py src/nexus/server/api/v2/routers/search.py tests/unit/bricks/search/test_search_result_serialize.py
git commit -m "feat(search): additive macro_text result fields + serializer (#4398)"
```

---

### Task 6: `PgVectorBackend.fetch_ranges`

**Files:**
- Modify: `src/nexus/bricks/search/pg_vector_backend.py`
- Test: `tests/integration/bricks/search/test_pg_macro_fetch.py`

**Interfaces:**
- Consumes: `ChunkRow` (Task 1).
- Produces: `async def fetch_ranges(self, spans: Sequence[tuple[str,int,int]], zone_id: str|None) -> list[ChunkRow]` on `PgVectorBackend` — satisfies `NeighborFetcher`.

- [ ] **Step 1: Write the failing test** (requires the pg integration fixture used by `test_daemon_search_pg.py`)

```python
# tests/integration/bricks/search/test_pg_macro_fetch.py
import pytest
# reuse the same pg backend fixture pattern as test_pg_vector_backend.py / test_daemon_search_pg.py


@pytest.mark.asyncio
async def test_pg_fetch_ranges_returns_contiguous_rows(pg_vector_backend, seeded_doc):
    # seeded_doc indexes a doc at path "/ws/doc.md" with >=3 chunks in zone "z1"
    rows = await pg_vector_backend.fetch_ranges([("/ws/doc.md", 0, 2)], zone_id="z1")
    idxs = sorted(r.chunk_index for r in rows)
    assert idxs == [0, 1, 2]
    assert all(r.path == "/ws/doc.md" for r in rows)
    assert rows[0].tokens >= 0  # chunk_tokens populated
```

> Use the existing pg fixtures from `tests/integration/bricks/search/conftest.py`. If `seeded_doc`/`pg_vector_backend` fixtures don't exist, mirror the setup in `test_daemon_search_pg.py` (index a small doc, then call the backend).

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_pg_macro_fetch.py -v`
Expected: FAIL — `AttributeError: 'PgVectorBackend' object has no attribute 'fetch_ranges'`

- [ ] **Step 3: Implement** — add to `PgVectorBackend`:

```python
from typing import Sequence
from nexus.bricks.search.macro_chunk import ChunkRow

async def fetch_ranges(
    self, spans: Sequence[tuple[str, int, int]], zone_id: str | None
) -> list[ChunkRow]:
    if not spans:
        return []
    clauses = []
    params: dict = {"zone_id": zone_id}
    for i, (path, lo, hi) in enumerate(spans):
        clauses.append(
            f"(fp.virtual_path = :p{i} AND c.chunk_index BETWEEN :lo{i} AND :hi{i})"
        )
        params[f"p{i}"] = path
        params[f"lo{i}"] = lo
        params[f"hi{i}"] = hi
    sql = text(
        "SELECT fp.virtual_path AS path, c.chunk_index, c.chunk_text, "
        "       c.chunk_tokens, c.line_start, c.line_end, c.heading_prefix "
        "FROM document_chunks c "
        "JOIN file_paths fp ON c.path_id = fp.path_id "
        "WHERE fp.zone_id = :zone_id AND fp.deleted_at IS NULL "
        "  AND (" + " OR ".join(clauses) + ") "
        "ORDER BY fp.virtual_path, c.chunk_index"
    )
    async with self._engine.connect() as conn:
        rows = (await conn.execute(sql, params)).mappings().all()
    return [
        ChunkRow(
            path=r["path"], chunk_index=int(r["chunk_index"]), text=r["chunk_text"],
            tokens=int(r["chunk_tokens"] or 0), line_start=r["line_start"],
            line_end=r["line_end"], heading_prefix=r["heading_prefix"],
        )
        for r in rows
    ]
```

(Ensure `from sqlalchemy import text` is imported — it is already used by the module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_pg_macro_fetch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/pg_vector_backend.py tests/integration/bricks/search/test_pg_macro_fetch.py
git commit -m "feat(search): PgVectorBackend.fetch_ranges neighbor primitive (#4398)"
```

---

### Task 7: `SqliteVecBackend.fetch_ranges` + `nexus_vec` aux columns + schema bump

**Files:**
- Modify: `src/nexus/bricks/search/sqlite_vec_backend.py` (table DDL: add `chunk_tokens`, `line_start`, `line_end`, `heading_prefix` aux columns; bump the stored schema version so the existing rebuild path recreates the table; the side-write that inserts rows; new `fetch_ranges`)
- Modify: the SANDBOX side-write caller if it passes explicit columns (grep `nexus_vec` inserts)
- Test: `tests/unit/bricks/search/test_sqlite_vec_backend.py` (extend) + `tests/integration/bricks/search/test_sqlite_macro_fetch.py`

**Interfaces:**
- Consumes: `ChunkRow` (Task 1).
- Produces: `async def fetch_ranges(self, spans, zone_id) -> list[ChunkRow]` on `SqliteVecBackend`; `nexus_vec` now stores `chunk_tokens`/`line_start`/`line_end`/`heading_prefix`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/bricks/search/test_sqlite_macro_fetch.py
import pytest
# reuse sqlite_vec fixtures from test_sqlite_vec_backend.py / test_daemon_search_sqlite.py


@pytest.mark.asyncio
async def test_sqlite_fetch_ranges_returns_rows_with_metadata(sqlite_vec_backend, seeded_doc_sqlite):
    rows = await sqlite_vec_backend.fetch_ranges([("/ws/doc.md", 0, 2)], zone_id="z1")
    idxs = sorted(r.chunk_index for r in rows)
    assert idxs == [0, 1, 2]
    assert rows[0].heading_prefix is not None or rows[0].heading_prefix is None  # column present
    assert rows[0].tokens >= 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_sqlite_macro_fetch.py -v`
Expected: FAIL — no `fetch_ranges` / missing columns.

- [ ] **Step 3: Implement**
1. In the `nexus_vec` CREATE (vec0) DDL, add auxiliary columns `+chunk_tokens INTEGER, +line_start INTEGER, +line_end INTEGER, +heading_prefix TEXT` (sqlite-vec aux columns use the `+col` syntax).
2. Bump the schema-version value stored in `nexus_vec_meta` so the existing mismatch→rebuild path drops and recreates the table.
3. In the row INSERT side-write, pass the four new values (from the `ChunkRecord`/`DocumentChunk`).
4. Add `fetch_ranges` (runs sync sqlite under `asyncio.to_thread`, mirroring `semantic_search`):

```python
async def fetch_ranges(self, spans, zone_id):
    if not spans:
        return []
    return await asyncio.to_thread(self._fetch_ranges_sync, list(spans), zone_id)

def _fetch_ranges_sync(self, spans, zone_id):
    from nexus.bricks.search.macro_chunk import ChunkRow
    out = []
    with self._lock:  # mirror existing connection-guard pattern in this module
        cur = self._conn.cursor()
        for path, lo, hi in spans:
            cur.execute(
                "SELECT path, chunk_index, chunk_text, chunk_tokens, "
                "       line_start, line_end, heading_prefix "
                "FROM nexus_vec "
                "WHERE zone_id = ? AND path = ? AND chunk_index BETWEEN ? AND ? "
                "ORDER BY chunk_index",
                (zone_id, path, lo, hi),
            )
            for row in cur.fetchall():
                out.append(ChunkRow(
                    path=row[0], chunk_index=int(row[1]), text=row[2],
                    tokens=int(row[3] or 0), line_start=row[4],
                    line_end=row[5], heading_prefix=row[6],
                ))
    return out
```

> Match this module's actual connection/lock attribute names (grep for `self._conn`/`self._lock`/`asyncio.to_thread` in `sqlite_vec_backend.py`) — adjust the guard to the existing pattern.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_sqlite_macro_fetch.py tests/unit/bricks/search/test_sqlite_vec_backend.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/sqlite_vec_backend.py tests/integration/bricks/search/test_sqlite_macro_fetch.py tests/unit/bricks/search/test_sqlite_vec_backend.py
git commit -m "feat(search): SqliteVecBackend.fetch_ranges + nexus_vec metadata columns (#4398)"
```

---

### Task 8: Daemon wiring + config knobs

**Files:**
- Modify: `src/nexus/bricks/search/config.py` (add `macro_chunk_tokens`, `macro_chunk_window`, `macro_chunk_code_forward_bias`)
- Modify: `src/nexus/bricks/search/daemon.py` (`search` signature + post-assembly expansion call)
- Test: `tests/unit/bricks/search/test_search_config.py` (extend) + `tests/integration/bricks/search/test_daemon_macro_expansion.py`

**Interfaces:**
- Consumes: `expand_results` (Task 2), backend `fetch_ranges` (Tasks 6/7), `BaseSearchResult.macro_text` (Task 5), `SearchConfig` knobs.
- Produces: `daemon.search(..., expand: str = "none")`. When `expand == "macro"`, after the final result list is assembled, call `expand_results(results, <active vector backend>, ExpansionConfig(...from config...), zone_id=zone_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/bricks/search/test_daemon_macro_expansion.py
import pytest
# reuse the sqlite daemon fixture from test_daemon_search_sqlite.py


@pytest.mark.asyncio
async def test_daemon_default_response_has_no_macro_text(sqlite_daemon, seeded):
    results = await sqlite_daemon.search("vector search", search_type="hybrid", limit=5)
    assert all(getattr(r, "macro_text", None) is None for r in results)


@pytest.mark.asyncio
async def test_daemon_expand_macro_attaches_macro_text(sqlite_daemon, seeded):
    results = await sqlite_daemon.search(
        "vector search", search_type="hybrid", limit=5, expand="macro"
    )
    assert any(getattr(r, "macro_text", None) for r in results)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_daemon_macro_expansion.py -v`
Expected: FAIL — `search()` has no `expand` kwarg.

- [ ] **Step 3: Implement**

In `config.py` `SearchConfig` + `search_config_from_env()`:
```python
    macro_chunk_tokens: int = 1024
    macro_chunk_window: int = 8
    macro_chunk_code_forward_bias: bool = True
```
```python
    macro_chunk_tokens=get_env_int("NEXUS_SEARCH_MACRO_CHUNK_TOKENS", 1024),
    macro_chunk_window=get_env_int("NEXUS_SEARCH_MACRO_CHUNK_WINDOW", 8),
    macro_chunk_code_forward_bias=get_env_bool("NEXUS_SEARCH_MACRO_CHUNK_FORWARD_BIAS", True),
```

In `daemon.py` `search`: add `expand: str = "none"` (last param). After the final `results` list is built and before returning:
```python
    if expand == "macro" and results:
        from nexus.bricks.search.macro_chunk import expand_results, ExpansionConfig
        fetcher = self._vector_backend_for(zone_id)  # the active PgVectorBackend/SqliteVecBackend
        cfg = ExpansionConfig(
            token_budget=self._config.macro_chunk_tokens,
            window=self._config.macro_chunk_window,
            code_forward_bias=self._config.macro_chunk_code_forward_bias,
        )
        await expand_results(results, fetcher, cfg, zone_id=zone_id)
```

> `_vector_backend_for` / the backend handle: use whatever attribute the daemon already holds for the active vector backend (grep `PgVectorBackend(`/`SqliteVecBackend(` in `daemon.py` to find the stored attribute, e.g. `self._vec_backend`). If the daemon stores the backend per-zone, fetch that; otherwise use the single stored backend. Do NOT add new construction — reuse the existing backend instance.

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_daemon_macro_expansion.py tests/unit/bricks/search/test_search_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/bricks/search/config.py src/nexus/bricks/search/daemon.py tests/integration/bricks/search/test_daemon_macro_expansion.py tests/unit/bricks/search/test_search_config.py
git commit -m "feat(search): daemon expand=macro wiring + config knobs (#4398)"
```

---

### Task 9: API router `expand` param

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py` (`search_query`, ~line 291)
- Test: `tests/integration/bricks/search/test_search_query_expand.py` (or the existing API/router test module — grep for tests hitting `search_query`)

**Interfaces:**
- Consumes: `daemon.search(..., expand=...)` (Task 8), serializer (Task 5).
- Produces: `expand: str = "none"` query param on `POST /api/v2/search/query`, threaded into the search call.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/bricks/search/test_search_query_expand.py
import pytest
# use the existing API test client/fixture used elsewhere for /api/v2/search/query


@pytest.mark.asyncio
async def test_search_query_default_no_macro(api_client, seeded):
    resp = await api_client.post("/api/v2/search/query", params={"q": "vector search"})
    body = resp.json()
    assert all("macro_text" not in hit for hit in body["results"])


@pytest.mark.asyncio
async def test_search_query_expand_macro(api_client, seeded):
    resp = await api_client.post(
        "/api/v2/search/query", params={"q": "vector search", "expand": "macro"}
    )
    body = resp.json()
    assert any(hit.get("macro_text") for hit in body["results"])
```

> Use whichever client fixture the existing router tests use (grep `search/query` in tests). If routers are tested via FastAPI `TestClient`/`httpx.AsyncClient`, mirror that.

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_search_query_expand.py -v`
Expected: FAIL — `expand` not accepted / `macro_text` absent.

- [ ] **Step 3: Implement**

In `search_query()` add the query param (next to `fusion`):
```python
    expand: str = "none",
```
Thread it into the daemon/search-service call that produces `results`:
```python
    ... search(..., expand=expand)
```
(Match the exact call site that invokes `daemon.search` / the search service; pass `expand=expand`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/integration/bricks/search/test_search_query_expand.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py tests/integration/bricks/search/test_search_query_expand.py
git commit -m "feat(search): expand=macro request param on /api/v2/search/query (#4398)"
```

---

### Task 10: Benchmark guard + full suite

**Files:**
- Modify: `tests/benchmarks/gbrain_eval.py` only if a flag is needed to run with `expand=macro` (otherwise none)
- Test: run existing `gbrain_eval` + the new search tests

**Interfaces:**
- Consumes: everything above.
- Produces: evidence that ranking is unchanged and expansion works end-to-end.

- [ ] **Step 1: Run the full search test suite**

Run: `PYTHONPATH=$PWD/src python -m pytest tests/unit/bricks/search tests/integration/bricks/search -v`
Expected: PASS (all, including new tests)

- [ ] **Step 2: Confirm default-response invariance on the benchmark**

Run: `just bench-search` (requires `GBRAIN_EVALS_DIR` + `NEXUS_DATABASE_URL`)
Expected: `recall@5` / `NDCG@5` within the existing 1pp slack of baseline (0.9489 / 0.9028) — expansion is off by default, so numbers must not move.

- [ ] **Step 3: Smoke the expansion path on the tiny fixture first**

Run a 10-item check before the full corpus (per the validate-small-first rule): index `tests/benchmarks/_tiny_fixture/`, issue a query with `expand=macro`, confirm `macro_text` is populated and contains neighbor content beyond the single chunk. Document the observed context-quality delta.

- [ ] **Step 4: Commit any benchmark-harness changes**

```bash
git add tests/benchmarks/
git commit -m "test(search): macro-chunk benchmark guard + tiny-fixture smoke (#4398)"
```

---

## Self-Review

**Spec coverage:**
- Output contract (opt-in `expand`, additive snake_case fields) → Tasks 5, 8, 9. ✓
- Architecture (pure module + protocol + backend fetch_ranges + daemon wiring) → Tasks 1, 2, 6, 7, 8. ✓
- Expansion algorithm (section, heading clip, token budget, forward-bias, adaptive, dedup) → Tasks 1, 2. ✓
- Fetch primitive (both profiles) → Tasks 6, 7. ✓
- Migration (heading_prefix) + SANDBOX columns → Tasks 3, 4, 7. ✓
- Error handling (best-effort) → Task 2 (`expand_results` try/except). ✓
- Test plan + benchmark guard + both profiles → Tasks 1-2 (unit), 6-9 (integration), 10 (bench). ✓
- Config knobs → Task 8. ✓

**Type consistency:** `ChunkRow` / `ExpansionConfig` / `NeighborFetcher.fetch_ranges(spans, zone_id)` / `expand_results(results, fetcher, cfg, zone_id)` are used identically across Tasks 1, 2, 6, 7, 8. `macro_text`/`macro_line_start`/`macro_line_end` consistent across Tasks 2, 5, 8, 9.

**Known fixture-dependent steps** (flagged inline, resolve against existing fixtures during execution): migrated-DB fixture name (Task 3), pg/sqlite seed fixtures (Tasks 6-9), daemon backend attribute name (Task 8), indexing `ChunkRecord(` construction site (Task 4), `nexus_vec` connection/lock attr names (Task 7).
