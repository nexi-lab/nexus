# Issue #3773 — RRF Top-Rank Bonus + Path Context Descriptions

**Status:** Approved design
**Date:** 2026-04-16
**Issue:** https://github.com/nexi-lab/nexus/issues/3773
**Inspiration:** [QMD](https://github.com/tobi/qmd) — Tobi Lütke's local hybrid search engine.

## Summary

Two independent improvements to search quality, delivered in a single PR:

1. **RRF top-rank bonus** — small score bump for documents ranked #1–3 in any input list during RRF fusion, so high-confidence matches are not diluted by query expansion or multi-backend fusion.
2. **Path context descriptions** — user-configurable per-path-prefix descriptions attached to search results, giving LLM agents immediate relevance signal without reading each file.

Both are zero-new-deps, zero-LLM-call, opt-out changes.

## 1. RRF Top-Rank Bonus

### Problem

Standard RRF (`score = Σ 1/(k + rank)`) dilutes exact matches. A document ranked #1 in keyword search but absent from vector search scores lower than a mediocre document ranked #3 in both lists. Worsens with query expansion (original match gets diluted across variant queries).

### Solution

After accumulating per-source RRF contributions, add a small bonus based on the document's **best rank across any input list**:

| Best rank in any list | Bonus |
|---|---|
| 1 | +0.05 |
| 2–3 | +0.02 |
| 4+ | 0 |

Preserves high-confidence matches without distorting overall ranking.

### Changes

**File:** `src/nexus/bricks/search/fusion.py`

Module constants:
```python
RRF_TOP1_BONUS = 0.05
RRF_TOP3_BONUS = 0.02
```

`FusionConfig` gains:
```python
top_rank_bonus: bool = True
```

Modify these three fusion functions to apply the bonus (parameter `top_rank_bonus: bool = True` on each):

- `rrf_fusion`
- `rrf_weighted_fusion`
- `rrf_multi_fusion`

**Shared logic** (applied inside each fn, after existing RRF accumulation loop):

```python
# Track best rank per key across all input lists.
best_rank: dict[str, int] = {}
# Populated during the existing enumerate loop(s):
#   best_rank[key] = min(best_rank.get(key, rank), rank)

if top_rank_bonus:
    for key, entry in rrf_scores.items():
        br = best_rank.get(key, 999)
        if br == 1:
            entry["rrf_score"] += RRF_TOP1_BONUS
        elif br <= 3:
            entry["rrf_score"] += RRF_TOP3_BONUS
```

`fuse_results` forwards `config.top_rank_bonus` to each underlying function.

### Tests

New file `tests/bricks/search/test_rrf_bonus.py`:
- Golden: top-1-keyword-only beats mediocre-both (the failure case from the issue).
- Bonus disabled path preserves legacy behaviour.
- `rrf_weighted_fusion`: bonus applied after alpha-weighting.
- `rrf_multi_fusion`: bonus applied across N sources.
- Existing RRF tests: audit and update golden orderings where bonus flips ranking.

## 2. Path Context Descriptions

### Problem

Search results expose `path` but no high-level description of what that file or directory contains. Agent consumers either read each file to judge relevance or guess. Both waste tokens and time.

### Solution

Admin-configured, zone-scoped table mapping path prefixes to human-written descriptions. Search results carry the longest-prefix-matching description in a new `context` field.

### Data model

**New table `path_contexts`:**

```python
sa.Column("id", sa.Integer, primary_key=True, autoincrement=True)
sa.Column("zone_id", sa.String(255), nullable=False, server_default=ROOT_ZONE_ID)
sa.Column("path_prefix", sa.String(1024), nullable=False)
sa.Column("description", sa.Text, nullable=False)
sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False)
sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False)
sa.UniqueConstraint("zone_id", "path_prefix", name="uq_path_contexts_zone_prefix")
sa.Index("ix_path_contexts_zone_updated", "zone_id", "updated_at")
```

- `zone_id` non-null with `ROOT_ZONE_ID` default — matches `src/nexus/lib/db_base.py:74` and `alembic/versions/add_credentials_and_manifests.py:30`.
- `path_prefix` stored canonical: no leading `/`, no trailing `/`. Empty string = zone root (matches every path).
- `(zone_id, updated_at)` index supports the per-search freshness check.

**Migration:** `alembic/versions/add_path_contexts_table.py`. Uses `op.create_table` and `op.batch_alter_table` for SQLite compatibility. Upgrade creates; downgrade drops.

### Canonical prefix rules

Writer normalizes `path_prefix` before persisting:
- Strip leading and trailing `/`.
- Reject `..` traversal segments.
- Max length 1024.

Description max length 4096 characters. Validation enforced in router-layer Pydantic model.

### CRUD store

**New file:** `src/nexus/bricks/search/path_context.py`

```python
@dataclass(frozen=True)
class PathContextRecord:
    zone_id: str
    path_prefix: str
    description: str
    created_at: datetime
    updated_at: datetime

class PathContextStore:
    def __init__(self, async_session_factory): ...

    async def upsert(self, zone_id: str, path_prefix: str, description: str) -> None: ...
    async def delete(self, zone_id: str, path_prefix: str) -> bool: ...
    async def list(self, zone_id: str | None = None) -> list[PathContextRecord]: ...
    async def max_updated_at(self, zone_id: str) -> datetime | None: ...
    async def load_all_for_zone(self, zone_id: str) -> list[PathContextRecord]: ...
```

Implementation follows `src/nexus/bricks/search/chunk_store.py`:
- Raw SQL via `sqlalchemy.text()` with bound params.
- Dialect dispatch for upsert: `INSERT … ON CONFLICT (zone_id, path_prefix) DO UPDATE` on PostgreSQL; `INSERT OR REPLACE` on SQLite.
- Async sessions from injected factory.

### In-memory cache

**Class `PathContextCache`** (same file):

State: `dict[str, tuple[datetime | None, list[PathContextRecord]]]` — keyed by `zone_id`, each entry holds the freshness stamp and the records sorted by `len(path_prefix)` DESC.

API:
```python
async def lookup(self, zone_id: str | None, path: str) -> str | None:
    """Return longest-matching description for path, or None."""

async def refresh_if_stale(self, zone_id: str) -> None:
    """Compare store.max_updated_at to cached stamp; reload if newer."""
```

Per-zone `asyncio.Lock` serializes refreshes. `zone_id is None` coerces to `ROOT_ZONE_ID`.

**Matching semantics:**
- Empty prefix `""` matches any path in the zone.
- Non-empty prefix `p` matches path `x` iff `x == p` OR `x.startswith(p + "/")`. The slash-boundary prevents `src` matching `srcfoo/x.py`.
- First hit in the length-DESC-sorted list wins (longest prefix).

**Freshness:** First `lookup` call in a search awaits `refresh_if_stale`, which issues one `SELECT MAX(updated_at) FROM path_contexts WHERE zone_id = :z`. Reload only when stamp advanced. Subsequent lookups in the same search reuse cached list.

### API router

**New file:** `src/nexus/server/api/v2/routers/path_contexts.py`

Pattern mirrors `src/nexus/server/api/v2/routers/access_manifests.py`:
- `router = APIRouter(prefix="/api/v2/path-contexts", tags=["path-contexts"])`.
- Dependency `_get_path_context_store` pulls from `request.app.state`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `PUT` | `/` | `require_admin` | Upsert `{zone_id?, path_prefix, description}` |
| `GET` | `/` | `require_auth` | List; optional `?zone_id=` filter |
| `DELETE` | `/` | `require_admin` | Delete by `?zone_id=&path_prefix=`; 404 if no matching row |

Pydantic models:
```python
class PathContextIn(BaseModel):
    zone_id: str = Field(default=ROOT_ZONE_ID, max_length=255)
    path_prefix: str = Field(max_length=1024)
    description: str = Field(max_length=4096)

class PathContextOut(BaseModel):
    zone_id: str
    path_prefix: str
    description: str
    created_at: datetime
    updated_at: datetime
```

`zone_id` validated against the existing zone registry before write.

### App wiring

`src/nexus/server/fastapi_server.py`:
- At startup, construct `PathContextStore(async_session_factory)` → `app.state.path_context_store`.
- At startup, construct `PathContextCache(store=…)` → `app.state.path_context_cache`.
- Include `path_contexts.router`.

### Search integration

**Dataclass:** `src/nexus/bricks/search/results.py`
- Add `context: str | None = None` to `BaseSearchResult` after `zone_id`. Backward compatible (default None).

**Daemon:** `src/nexus/bricks/search/daemon.py`
- New optional constructor kwarg `path_context_cache: PathContextCache | None = None`.
- In `search()` after final result list is assembled (around `daemon.py:1186`): if cache is present, for each result call `result.context = await cache.lookup(result.zone_id, result.path)`.
- Federated search (`src/nexus/bricks/search/federated_search.py`): each per-zone daemon attaches its own context, so merged cross-zone results carry correct per-row descriptions.

**Router serializer:** `src/nexus/server/api/v2/routers/search.py`
- `_serialize_search_result` (lines 106–131) emits `context` when non-None; omits key when None to keep responses compact.

**MCP search tool:** no change needed. The tool's response builder converts `BaseSearchResult` to a dict; because `context` is a plain dataclass field, serialization already includes it. Null values are emitted as `null` (or omitted, matching the tool's existing convention for other optional fields like `splade_score`).

### Feature gate

No explicit flag. Empty `path_contexts` table ⇒ every lookup returns None ⇒ search responses unchanged. Operators can fully disable by leaving `app.state.path_context_cache = None`.

## Testing

- `tests/bricks/search/test_rrf_bonus.py` — RRF bonus golden cases (issue scenario), enabled/disabled, all three fusion variants.
- `tests/bricks/search/test_path_context.py` — store CRUD against PG and SQLite, cache freshness/staleness, longest-prefix-match edge cases (empty prefix, slash boundary, no-match, multi-zone isolation).
- `tests/server/api/v2/test_path_contexts_router.py` — admin/auth gate enforcement, validation (prefix normalization, length, zone existence), 404 on missing delete.
- `tests/bricks/search/test_daemon_context_attach.py` — end-to-end: seed contexts, run search, assert `context` populated on `SearchResult` dataclass and in HTTP response JSON.
- Existing RRF tests audited; golden orderings updated where bonus flips results.

## Rollout

- Single PR covers both features.
- `FusionConfig.top_rank_bonus = True` by default — no config change.
- Alembic upgrade/downgrade validated on PG and SQLite (existing CI path).
- No contexts seeded on deploy — zero user-visible search change until an admin populates via API.
- Observability: INFO log on each cache reload (`zone_id`, entry count). No metrics for v1.

## Out of scope

- Query expansion integration — the bonus already compensates for expansion dilution mechanically; no further change.
- Context inheritance UI / admin dashboard — API only.
- Bulk import/export of contexts — single-entry PUT only for v1.
- Per-user contexts — zone-scoped only.
- Non-prefix match (regex, globs) — only exact-or-slash-boundary prefix for v1.
