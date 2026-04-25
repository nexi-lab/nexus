# Issue #3871 — Multi-Zone Tokens Phase 2 Cleanup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the four `WHERE APIKeyModel.zone_id = ?` filter sites to junction queries, stop writing the deprecated `zone_id` column, drop the legacy `zone_perms` fallback, and add a tripwire migration that fails loudly if junction backfill is incomplete.

**Architecture:** Junction table `api_key_zones (key_id, zone_id, granted_at, permissions)` shipped in PR #3886 becomes the sole source of truth. `APIKeyModel.zone_id` is left nullable but no longer written; reads route through a new `get_primary_zone(key_id) → str | None` helper that uses `MIN(granted_at)` ordering with a `zone_id ASC` tiebreaker. A diagnostic alembic migration asserts every non-revoked, non-admin key has at least one junction row before the fallback removal lands.

**Tech Stack:** Python 3.13, SQLAlchemy 2.x, Alembic, FastAPI, Pydantic v2, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-04-25-issue-3871-multi-zone-tokens-phase2-cleanup-design.md`

**Hard dependency:** PR #3886 must be merged to `develop` before this plan can run. The plan assumes `api_key_zones`, `APIKeyZoneModel`, `OperationContext.zone_set`/`zone_perms`, and helpers `get_zones_for_key`/`get_zone_perms_for_key`/`add_zone_to_key`/`remove_zone_from_key` already exist on `develop`. **Verify before starting:** see Task 0.

---

## File map

### New files

| Path | Responsibility |
|---|---|
| `alembic/versions/<rev>_assert_api_key_junction_populated_for_3871.py` | Diagnostic migration: raises `RuntimeError` if any non-revoked, non-admin key lacks a junction row. |
| `tests/unit/storage/test_api_key_ops_primary_zone.py` | `get_primary_zone` and `get_primary_zones_for_keys` unit tests. |
| `tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py` | `revoke_key(zone_id=…)` matches multi-zone keys via junction. |
| `tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py` | REST list filter via junction; multi-zone key visible under both zones. |
| `tests/unit/server/rpc/handlers/test_admin_junction_filter.py` | Admin list/revoke 3 sites via junction. |
| `tests/unit/bricks/auth/providers/test_database_key_no_fallback.py` | Junction-empty + non-admin → `zone_perms=()`; admin keys still resolve. |
| `tests/unit/storage/migrations/test_assert_api_key_junction_populated.py` | Tripwire upgrade raises on broken fixture, no-op on healthy. |
| `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py` | End-to-end through `nexus up --build`: create token, assert `zone_id IS NULL`, MCP request both zones. |

### Modified files

| Path | What changes |
|---|---|
| `src/nexus/storage/api_key_ops.py` | Add `get_primary_zone`, `get_primary_zones_for_keys`. `create_api_key` writes `zone_id=None`. |
| `src/nexus/storage/models/auth.py` | `APIKeyModel.zone_id` docstring updated; deprecation note bumped to Phase 3. |
| `src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py` | `revoke_key(zone_id=…)` filter joins `APIKeyZoneModel`. |
| `src/nexus/bricks/auth/providers/database_key.py` | Drop legacy `zone_perms` fallback (the `if not zone_perms_rows: if api_key.zone_id: …` block). `list_keys(zone_id=…)` filter joins `APIKeyZoneModel`. |
| `src/nexus/server/api/v2/routers/auth_keys.py` | List filter joins `APIKeyZoneModel`. Create response `zone` field uses `get_primary_zone`. |
| `src/nexus/server/rpc/handlers/admin.py` | Three list/revoke filters join `APIKeyZoneModel`. Echo `zone_id` field uses `get_primary_zone`. |
| `src/nexus/cli/commands/hub.py` | `token_list` JSON `zone` field + table column use `get_primary_zones_for_keys` (single batch query). |

---

## Task 0: Verify dependencies on `develop`

**Purpose:** Confirm PR #3886 has merged before doing anything else. If it has not, stop and wait.

- [ ] **Step 1: Check the junction table migration is on `develop`**

Run:
```bash
git fetch origin develop
git log origin/develop --oneline -- 'alembic/versions/*api_key_zones*' | head -5
```
Expected: at least one commit referencing `add_api_key_zones_junction_table_for_*.py`. If empty, **stop the plan** — PR #3886 has not merged yet.

- [ ] **Step 2: Check the F4b nullable migration is on `develop`**

Run:
```bash
git log origin/develop --oneline -- 'alembic/versions/*nullable_for_3785*' | head -5
```
Expected: at least one commit (`d41d600929c4_make_api_keys_zone_id_nullable_for_3785.py`). If empty, **stop**.

- [ ] **Step 3: Confirm helper exports**

Run:
```bash
git show origin/develop:src/nexus/storage/api_key_ops.py | grep -E "^def (get_zones_for_key|get_zone_perms_for_key|add_zone_to_key|remove_zone_from_key)"
```
Expected: 4 lines, one per helper. If any missing, **stop**.

- [ ] **Step 4: Confirm `APIKeyZoneModel` exists**

Run:
```bash
git show origin/develop:src/nexus/storage/models/auth.py | grep "^class APIKeyZoneModel"
```
Expected: `class APIKeyZoneModel(Base):`. If missing, **stop**.

- [ ] **Step 5: Pull latest develop into the worktree**

Run:
```bash
git pull origin develop --rebase
```
Expected: clean rebase or fast-forward.

---

## Task 1: Add `get_primary_zone` + `get_primary_zones_for_keys` helpers

**Purpose:** Single source of truth for "primary zone" semantics, replacing the soon-to-be-NULL `APIKeyModel.zone_id` reads.

**Files:**
- Modify: `src/nexus/storage/api_key_ops.py`
- Test: `tests/unit/storage/test_api_key_ops_primary_zone.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/storage/test_api_key_ops_primary_zone.py`:

```python
"""Unit tests for get_primary_zone and get_primary_zones_for_keys (#3871)."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import (
    create_api_key,
    get_primary_zone,
    get_primary_zones_for_keys,
)
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyZoneModel, ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops", "legal"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_get_primary_zone_returns_none_for_zoneless_key(session):
    # `zones=[]` raises ValueError; zoneless = omit both `zones` and `zone_id`.
    key_id, _ = create_api_key(session, user_id="u1", name="admin", is_admin=True)
    assert get_primary_zone(session, key_id) is None


def test_get_primary_zone_returns_only_zone_for_single_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng"])
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zone_returns_min_granted_at(session):
    key_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng"])
    later = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(seconds=10)
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="ops", granted_at=later, permissions="rw"))
    session.commit()
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zone_tiebreaker_is_zone_id_asc(session):
    # Create zoneless then add two junction rows by hand with identical granted_at.
    key_id, _ = create_api_key(session, user_id="u1", name="tied", is_admin=True)
    same = dt.datetime(2026, 4, 25, 12, 0, 0)
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="ops", granted_at=same, permissions="rw"))
    session.add(APIKeyZoneModel(key_id=key_id, zone_id="eng", granted_at=same, permissions="rw"))
    session.commit()
    assert get_primary_zone(session, key_id) == "eng"


def test_get_primary_zones_for_keys_empty_input(session):
    assert get_primary_zones_for_keys(session, []) == {}


def test_get_primary_zones_for_keys_batch(session):
    a, _ = create_api_key(session, user_id="u1", name="a", zones=["eng"])
    b, _ = create_api_key(session, user_id="u1", name="b", zones=["ops"])
    c, _ = create_api_key(session, user_id="u1", name="c", is_admin=True)  # zoneless
    result = get_primary_zones_for_keys(session, [a, b, c])
    assert result == {a: "eng", b: "ops"}  # c absent (zoneless)


def test_get_primary_zones_for_keys_single_query(session):
    from sqlalchemy import event
    a, _ = create_api_key(session, user_id="u1", name="a", zones=["eng"])
    b, _ = create_api_key(session, user_id="u1", name="b", zones=["ops"])
    seen: list[str] = []
    @event.listens_for(session.bind, "before_cursor_execute")
    def _capture(conn, cursor, statement, *_):  # noqa: ARG001
        seen.append(statement)
    get_primary_zones_for_keys(session, [a, b])
    assert sum(1 for s in seen if "api_key_zones" in s) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/storage/test_api_key_ops_primary_zone.py -v`
Expected: 7 FAILs with `ImportError: cannot import name 'get_primary_zone'`.

- [ ] **Step 3: Implement the helpers**

Edit `src/nexus/storage/api_key_ops.py`. Add (placement: after `get_zone_perms_for_key`, before `add_zone_to_key`):

```python
def get_primary_zone(session: "Session", key_id: str) -> str | None:
    """Return the token's primary zone, or None if it has no zones.

    Primary = the row with the smallest granted_at. Ties broken by zone_id ASC
    so the result is deterministic across snapshots and replays.

    Replaces direct reads of the deprecated APIKeyModel.zone_id column (#3871).
    """
    from nexus.storage.models import APIKeyZoneModel
    from sqlalchemy import select

    stmt = (
        select(APIKeyZoneModel.zone_id)
        .where(APIKeyZoneModel.key_id == key_id)
        .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_primary_zones_for_keys(
    session: "Session", key_ids: list[str]
) -> dict[str, str]:
    """Batch variant of get_primary_zone for renderers walking many rows.

    Single round-trip via a window function. Returns {key_id: primary_zone};
    zoneless keys are absent from the dict.
    """
    if not key_ids:
        return {}
    from nexus.storage.models import APIKeyZoneModel
    from sqlalchemy import func, select

    rn = (
        func.row_number()
        .over(
            partition_by=APIKeyZoneModel.key_id,
            order_by=(
                APIKeyZoneModel.granted_at.asc(),
                APIKeyZoneModel.zone_id.asc(),
            ),
        )
        .label("rn")
    )
    inner = (
        select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id, rn)
        .where(APIKeyZoneModel.key_id.in_(key_ids))
        .subquery()
    )
    stmt = select(inner.c.key_id, inner.c.zone_id).where(inner.c.rn == 1)
    return {row.key_id: row.zone_id for row in session.execute(stmt)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/storage/test_api_key_ops_primary_zone.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Lint + types**

Run:
```bash
uv run ruff check src/nexus/storage/api_key_ops.py tests/unit/storage/test_api_key_ops_primary_zone.py
uv run ruff format --check src/nexus/storage/api_key_ops.py tests/unit/storage/test_api_key_ops_primary_zone.py
uv run mypy src/nexus/storage/api_key_ops.py
```
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/storage/api_key_ops.py tests/unit/storage/test_api_key_ops_primary_zone.py
git commit -m "feat(#3871): get_primary_zone helper (junction-backed primary zone)"
```

---

## Task 2: Migrate `database_key.py::list_keys` filter

**Purpose:** First of four filter migrations. `list_keys(zone_id=…)` in the auth provider must match every key that grants the requested zone, not just keys whose primary is that zone.

**Files:**
- Modify: `src/nexus/bricks/auth/providers/database_key.py`
- Test: `tests/unit/bricks/auth/providers/test_database_key_junction_filter.py`

- [ ] **Step 1: Locate the current filter line**

Run:
```bash
rg -n "APIKeyModel\.zone_id == zone_id" src/nexus/bricks/auth/providers/database_key.py
```
Expected: one hit inside `list_keys`. Note the line number for the edit.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/bricks/auth/providers/test_database_key_junction_filter.py`:

```python
"""list_keys zone filter must match every junction row, not just primary (#3871)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_list_keys_zone_filter_matches_every_granted_zone(session):
    multi_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng", "ops"])
    eng_only, _ = create_api_key(session, user_id="u1", name="eng_only", zones=["eng"])

    rows_eng = DatabaseAPIKeyAuth.list_keys(session, zone_id="eng")
    rows_ops = DatabaseAPIKeyAuth.list_keys(session, zone_id="ops")

    assert {r["key_id"] for r in rows_eng} == {multi_id, eng_only}
    assert {r["key_id"] for r in rows_ops} == {multi_id}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/bricks/auth/providers/test_database_key_junction_filter.py -v`
Expected: `test_list_keys_zone_filter_matches_every_granted_zone` FAILs because `multi` was not returned for `ops` (its primary is `eng`, so the current `WHERE zone_id = 'ops'` filter misses it).

- [ ] **Step 4: Apply the junction join**

Edit `src/nexus/bricks/auth/providers/database_key.py`. Locate `list_keys` (search: `def list_keys`). Find the block:

```python
if zone_id is not None:
    stmt = stmt.where(APIKeyModel.zone_id == zone_id)
```

Replace with:

```python
if zone_id is not None:
    from nexus.storage.models import APIKeyZoneModel
    stmt = (
        stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
            .where(APIKeyZoneModel.zone_id == zone_id)
    )
```

(If `APIKeyZoneModel` is already imported at module top, drop the inline import and add it to the module-level import list.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/bricks/auth/providers/test_database_key_junction_filter.py -v`
Expected: PASS.

- [ ] **Step 6: Run the broader auth-provider test file to catch regressions**

Run: `uv run pytest tests/unit/bricks/auth/providers/ -v`
Expected: all green (or only pre-existing flakes documented in PR #3886).

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/nexus/bricks/auth/providers/database_key.py tests/unit/bricks/auth/providers/test_database_key_junction_filter.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/bricks/auth/providers/database_key.py tests/unit/bricks/auth/providers/test_database_key_junction_filter.py
git commit -m "feat(#3871): list_keys zone filter via api_key_zones junction"
```

---

## Task 3: Migrate REST list filter (`auth_keys.py`)

**Purpose:** Second filter migration. The REST `GET /v2/auth/keys?zone_id=…` endpoint must match every key with access to the zone.

**Files:**
- Modify: `src/nexus/server/api/v2/routers/auth_keys.py`
- Test: `tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py`

- [ ] **Step 1: Locate the current filter line**

Run:
```bash
rg -n "APIKeyModel\.zone_id == zone_id" src/nexus/server/api/v2/routers/auth_keys.py
```
Expected: one hit inside the list endpoint handler.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py`:

```python
"""REST list-keys filter routes through the junction (#3871)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.server.api.v2.routers.auth_keys import router
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def app_with_keys(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        multi_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        eng_id, _ = create_api_key(s, user_id="u1", name="eng_only", zones=["eng"])

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)

    # Wire the session dependency to point at our in-memory engine.
    # (Consult the actual router file for the exact dependency name; many
    # routers use `Depends(get_session)`.)
    from nexus.server.dependencies import get_session
    def _override():
        with SessionLocal() as s:
            yield s
    app.dependency_overrides[get_session] = _override

    return app, multi_id, eng_id


def test_rest_list_keys_zone_filter_matches_every_granted_zone(app_with_keys, monkeypatch):
    app, multi_id, eng_id = app_with_keys
    # Bypass auth dependency for the test.
    from nexus.server.dependencies import require_auth
    app.dependency_overrides[require_auth] = lambda: {"is_admin": True}

    client = TestClient(app)
    resp_eng = client.get("/v2/auth/keys", params={"zone_id": "eng"})
    resp_ops = client.get("/v2/auth/keys", params={"zone_id": "ops"})

    assert resp_eng.status_code == 200
    assert resp_ops.status_code == 200
    assert {k["key_id"] for k in resp_eng.json()["keys"]} == {multi_id, eng_id}
    assert {k["key_id"] for k in resp_ops.json()["keys"]} == {multi_id}
```

(If the router uses different dependency names, adjust the override imports — keep the assertions the same.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py -v`
Expected: `test_rest_list_keys_zone_filter_matches_every_granted_zone` FAILs (multi key missing under `ops` filter).

- [ ] **Step 4: Apply the junction join**

Edit `src/nexus/server/api/v2/routers/auth_keys.py`. Locate the line:

```python
if zone_id:
    stmt = stmt.where(APIKeyModel.zone_id == zone_id)
```

Replace with:

```python
if zone_id:
    from nexus.storage.models import APIKeyZoneModel
    stmt = (
        stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
            .where(APIKeyZoneModel.zone_id == zone_id)
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py -v`
Expected: PASS.

- [ ] **Step 6: Run the router's existing test file to catch regressions**

Run: `uv run pytest tests/unit/server/api/v2/routers/ -v -k auth_keys`
Expected: all green.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/nexus/server/api/v2/routers/auth_keys.py tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/server/api/v2/routers/auth_keys.py tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py
git commit -m "feat(#3871): REST list-keys zone filter via junction"
```

---

## Task 4: Migrate admin RPC filters (3 statements)

**Purpose:** Third filter migration. `server/rpc/handlers/admin.py` has three `WHERE APIKeyModel.zone_id == params.zone_id` statements (list, list_by_zone, revoke). All three need the same junction join.

**Files:**
- Modify: `src/nexus/server/rpc/handlers/admin.py`
- Test: `tests/unit/server/rpc/handlers/test_admin_junction_filter.py`

- [ ] **Step 1: Locate the three filter lines**

Run:
```bash
rg -n "APIKeyModel\.zone_id == (params\.)?zone_id" src/nexus/server/rpc/handlers/admin.py
```
Expected: 3 hits. Note the function each one belongs to.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/server/rpc/handlers/test_admin_junction_filter.py`:

```python
"""Admin RPC list/list_by_zone/revoke filters route through junction (#3871)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.server.rpc.handlers import admin
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session_with_keys():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        multi_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        eng_id, _ = create_api_key(s, user_id="u1", name="eng_only", zones=["eng"])
        s.commit()
        yield s, multi_id, eng_id


def test_admin_list_keys_filter_via_junction(session_with_keys):
    session, multi_id, eng_id = session_with_keys
    params = SimpleNamespace(zone_id="ops", user_id=None, key_id=None)
    rows = admin.list_keys(session, params)  # adjust signature to match actual
    assert {r["key_id"] for r in rows} == {multi_id}


def test_admin_list_by_zone_returns_every_member(session_with_keys):
    session, multi_id, eng_id = session_with_keys
    params = SimpleNamespace(zone_id="eng")
    rows = admin.list_keys_by_zone(session, params)  # adjust to actual name
    assert {r["key_id"] for r in rows} == {multi_id, eng_id}
```

(Adjust handler-function names to match the actual exports — the rg above tells you which functions own each filter.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/server/rpc/handlers/test_admin_junction_filter.py -v`
Expected: FAILs (multi key not visible under `ops` filter).

- [ ] **Step 4: Apply the junction join at all three sites**

Edit `src/nexus/server/rpc/handlers/admin.py`. For **each** of the three `if params.zone_id: stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)` blocks, replace with:

```python
if params.zone_id:
    from nexus.storage.models import APIKeyZoneModel
    stmt = (
        stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
            .where(APIKeyZoneModel.zone_id == params.zone_id)
    )
```

Promote the `APIKeyZoneModel` import to the module top once (drop the inline imports after that).

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/server/rpc/handlers/test_admin_junction_filter.py -v`
Expected: PASS.

- [ ] **Step 6: Run the admin handler's existing tests**

Run: `uv run pytest tests/unit/server/rpc/handlers/ -v -k admin`
Expected: all green.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/nexus/server/rpc/handlers/admin.py tests/unit/server/rpc/handlers/test_admin_junction_filter.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/server/rpc/handlers/admin.py tests/unit/server/rpc/handlers/test_admin_junction_filter.py
git commit -m "feat(#3871): admin RPC zone filters via junction (3 sites)"
```

---

## Task 5: Migrate `sqlalchemy_api_key_store::revoke_key` filter

**Purpose:** Fourth and final filter migration. The storage-layer `revoke_key(zone_id=…)` must match multi-zone keys.

**Files:**
- Modify: `src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py`
- Test: `tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py`

- [ ] **Step 1: Locate the filter**

Run:
```bash
rg -n "APIKeyModel\.zone_id == zone_id" src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py
```
Expected: one hit in `revoke_key`.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py`:

```python
"""revoke_key zone filter routes through junction (#3871)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.auth_stores.sqlalchemy_api_key_store import SqlAlchemyApiKeyStore
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyModel, ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_revoke_key_zone_filter_matches_multi_zone_key(session):
    store = SqlAlchemyApiKeyStore(session)
    multi_id, _ = create_api_key(session, user_id="u1", name="multi", zones=["eng", "ops"])

    # Revoke scoped to ops; multi key's primary is eng, so old behavior would miss it.
    revoked = store.revoke_key(multi_id, zone_id="ops")
    assert revoked is True

    refreshed = session.get(APIKeyModel, multi_id)
    assert refreshed.revoked == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py -v`
Expected: FAILs (`revoke_key` returns False because the multi key's `zone_id` column is `eng`, not `ops`).

- [ ] **Step 4: Apply the junction join**

Edit `src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py`. Find:

```python
if zone_id is not None:
    stmt = stmt.where(APIKeyModel.zone_id == zone_id)
```

Replace with:

```python
if zone_id is not None:
    from nexus.storage.models import APIKeyZoneModel
    stmt = (
        stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
            .where(APIKeyZoneModel.zone_id == zone_id)
    )
```

(`UPDATE` statements with joins behave dialect-specifically. SQLAlchemy translates this to a correlated subquery on sqlite; on Postgres it uses `UPDATE … FROM`. Both are correct semantically.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py -v`
Expected: PASS.

- [ ] **Step 6: Run the auth_stores test directory**

Run: `uv run pytest tests/unit/storage/auth_stores/ -v`
Expected: all green.

- [ ] **Step 7: Lint**

Run: `uv run ruff check src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py
git commit -m "feat(#3871): sqlalchemy_api_key_store.revoke_key zone filter via junction"
```

---

## Task 6: Stop writing `APIKeyModel.zone_id`

**Purpose:** Sever the link from `create_api_key` to the deprecated column. New keys persist `zone_id=NULL`; the junction is the only zone record.

**Files:**
- Modify: `src/nexus/storage/api_key_ops.py`
- Modify: `src/nexus/storage/models/auth.py`
- Test: `tests/unit/storage/test_api_key_ops_no_zone_id_write.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_api_key_ops_no_zone_id_write.py`:

```python
"""create_api_key must not write APIKeyModel.zone_id (#3871)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyModel, ZoneModel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        yield s


def test_create_api_key_writes_null_zone_id_for_single_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng"])
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None


def test_create_api_key_writes_null_zone_id_for_multi_zone_key(session):
    key_id, _ = create_api_key(session, user_id="u1", name="alice", zones=["eng", "ops"])
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None


def test_create_api_key_writes_null_zone_id_for_zoneless_admin_key(session):
    # Zoneless = omit both `zones` and `zone_id` (passing `zones=[]` raises ValueError).
    key_id, _ = create_api_key(session, user_id="u1", name="root", is_admin=True)
    row = session.get(APIKeyModel, key_id)
    assert row.zone_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/storage/test_api_key_ops_no_zone_id_write.py -v`
Expected: 2 FAILs (single + multi tests). The zoneless test already passes today since #3886's F4b dropped the default for zoneless keys.

- [ ] **Step 3: Update `create_api_key`**

Edit `src/nexus/storage/api_key_ops.py`. Locate `create_api_key`. Find the line that sets `zone_id` on `APIKeyModel(...)` (search: `zone_id=primary_zone` or `zone_id=zones[0]`). Replace with `zone_id=None`. The function should no longer compute or pass a primary zone to the column.

The junction insert (`INSERT INTO api_key_zones …`) elsewhere in `create_api_key` is unchanged — that is the surviving write path.

- [ ] **Step 4: Update `APIKeyModel.zone_id` docstring**

Edit `src/nexus/storage/models/auth.py`. Locate `class APIKeyModel`. The `zone_id` field docstring (currently mentions "Backfill alias for `api_key_zones.zone_id` first row" per #3785) becomes:

```python
"""DEPRECATED — column scheduled for removal in Phase 3 of #3871.
Always NULL on keys minted on or after Phase 2 (#3871). Source of
truth is `api_key_zones`. Use `get_primary_zone(key_id)` for
"primary zone" semantics or `get_zones_for_key(key_id)` for the
full set."""
```

- [ ] **Step 5: Update the existing #3785 unit tests that asserted the backfill-alias behavior**

Run:
```bash
rg -n "zone_id.*ROOT_ZONE_ID|zone_id.*primary_zone|zone_id.*zones\[0\]" tests/unit/storage/
```
For each test that asserts `api_key.zone_id == <some zone>`, update the assertion to `api_key.zone_id is None`. (Behavior change is intentional and documented in §3.3 of the spec.)

- [ ] **Step 6: Run the new + updated tests**

Run:
```bash
uv run pytest tests/unit/storage/test_api_key_ops_no_zone_id_write.py tests/unit/storage/ -v
```
Expected: new tests PASS; updated tests still PASS.

- [ ] **Step 7: Lint + types**

Run:
```bash
uv run ruff check src/nexus/storage/api_key_ops.py src/nexus/storage/models/auth.py
uv run mypy src/nexus/storage/api_key_ops.py src/nexus/storage/models/auth.py
```
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/storage/api_key_ops.py src/nexus/storage/models/auth.py tests/unit/storage/test_api_key_ops_no_zone_id_write.py tests/unit/storage/
git commit -m "feat(#3871): create_api_key stops writing APIKeyModel.zone_id"
```

---

## Task 7: Tripwire alembic migration

**Purpose:** Diagnostic migration that fails loudly if any non-revoked, non-admin key lacks a junction row. Lands **before** the fallback removal in Task 8 so a broken DB surfaces at upgrade time, not at first denied request.

**Files:**
- Create: `alembic/versions/<rev>_assert_api_key_junction_populated_for_3871.py`
- Test: `tests/unit/storage/migrations/test_assert_api_key_junction_populated.py`

- [ ] **Step 1: Generate the alembic revision file**

Run:
```bash
uv run alembic revision -m "assert_api_key_junction_populated_for_3871"
```
This creates a new file under `alembic/versions/` named `<12hex>_assert_api_key_junction_populated_for_3871.py`. Note the filename. The generator sets `down_revision` to the current head (which on the rebased develop will be `d41d600929c4_make_api_keys_zone_id_nullable_for_3785` or a descendant). Verify by reading the generated file.

- [ ] **Step 2: Replace the migration body**

Open the generated file. Replace its `upgrade()` and `downgrade()` with:

```python
"""assert api_key_zones populated for #3871

Diagnostic migration. Fails loudly if any non-revoked, non-admin api_keys
row lacks a corresponding api_key_zones row. Lands before the legacy
zone_perms fallback is removed (Task 8 of #3871) so data drift surfaces
at upgrade time.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            """
            SELECT k.key_id
            FROM api_keys k
            LEFT JOIN api_key_zones z ON z.key_id = k.key_id
            WHERE k.revoked = 0
              AND k.is_admin = 0
              AND z.key_id IS NULL
            """
        )
    ).fetchall()
    if rows:
        sample = [r[0] for r in rows[:5]]
        raise RuntimeError(
            f"#3871 Phase 2 cleanup blocked: {len(rows)} non-admin live keys lack "
            f"junction rows. Re-run the #3785 backfill before upgrading. "
            f"Sample key_ids: {sample}"
        )


def downgrade() -> None:
    pass  # assertion-only
```

(Leave the `revision = "..."`, `down_revision = "..."`, `branch_labels`, `depends_on` lines that alembic generated.)

- [ ] **Step 3: Write the test**

Create `tests/unit/storage/migrations/test_assert_api_key_junction_populated.py`:

```python
"""Tripwire migration tests (#3871)."""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, text

# Find the migration module dynamically so the test does not bind to the
# alembic revision hash.
def _load_migration_module():
    versions = pathlib.Path("alembic/versions")
    matches = list(versions.glob("*assert_api_key_junction_populated_for_3871*.py"))
    assert len(matches) == 1, f"Expected one migration file, found {matches}"
    import importlib.util
    spec = importlib.util.spec_from_file_location("tripwire", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def engine_with_schema():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE api_keys (
                key_id TEXT PRIMARY KEY,
                revoked INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE api_key_zones (
                key_id TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                PRIMARY KEY (key_id, zone_id)
            )
        """))
    return engine


def test_tripwire_no_op_on_healthy_db(engine_with_schema):
    module = _load_migration_module()
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id) VALUES ('k1')"))
        conn.execute(text("INSERT INTO api_key_zones (key_id, zone_id) VALUES ('k1', 'eng')"))
    from alembic.migration import MigrationContext
    with engine_with_schema.connect() as conn:
        ctx = MigrationContext.configure(conn)
        # The migration uses op.get_bind(); inject via context.
        from alembic import op
        op._proxy = type("P", (), {"get_bind": lambda self: conn})()
        try:
            module.upgrade()  # should not raise
        finally:
            op._proxy = None


def test_tripwire_raises_on_broken_db(engine_with_schema):
    module = _load_migration_module()
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id) VALUES ('orphan')"))  # no junction row
    with engine_with_schema.connect() as conn:
        from alembic import op
        op._proxy = type("P", (), {"get_bind": lambda self: conn})()
        try:
            with pytest.raises(RuntimeError, match="lack junction rows"):
                module.upgrade()
        finally:
            op._proxy = None


def test_tripwire_ignores_admin_keys(engine_with_schema):
    """Admin keys are allowed to have empty junction (zoneless)."""
    module = _load_migration_module()
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id, is_admin) VALUES ('admin', 1)"))
    with engine_with_schema.connect() as conn:
        from alembic import op
        op._proxy = type("P", (), {"get_bind": lambda self: conn})()
        try:
            module.upgrade()  # should not raise
        finally:
            op._proxy = None


def test_tripwire_ignores_revoked_keys(engine_with_schema):
    module = _load_migration_module()
    with engine_with_schema.begin() as conn:
        conn.execute(text("INSERT INTO api_keys (key_id, revoked) VALUES ('dead', 1)"))
    with engine_with_schema.connect() as conn:
        from alembic import op
        op._proxy = type("P", (), {"get_bind": lambda self: conn})()
        try:
            module.upgrade()  # should not raise
        finally:
            op._proxy = None


def test_tripwire_downgrade_is_noop(engine_with_schema):
    module = _load_migration_module()
    module.downgrade()  # must not raise
```

(If the codebase already has an alembic-test fixture pattern under `tests/unit/storage/migrations/`, prefer it over the manual `op._proxy` shim above. Search: `rg -l "MigrationContext.configure" tests/`.)

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/storage/migrations/test_assert_api_key_junction_populated.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Run the migration end-to-end against sqlite**

Run:
```bash
uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: clean (no errors). The healthy in-memory DB has no rows so the tripwire passes.

- [ ] **Step 6: Lint**

Run: `uv run ruff check alembic/versions/*assert_api_key_junction_populated_for_3871*.py tests/unit/storage/migrations/test_assert_api_key_junction_populated.py`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/*assert_api_key_junction_populated_for_3871*.py tests/unit/storage/migrations/test_assert_api_key_junction_populated.py
git commit -m "feat(#3871): tripwire migration — assert api_key_zones populated"
```

---

## Task 8: Remove the legacy `zone_perms` fallback

**Purpose:** With Task 7 in place to catch broken data, the `if not zone_perms_rows: if api_key.zone_id: zone_perms = ((api_key.zone_id, "rw"),)` fallback in `database_key.py` becomes dead code. Removing it is the central behavior change of Phase 2.

**Files:**
- Modify: `src/nexus/bricks/auth/providers/database_key.py`
- Test: `tests/unit/bricks/auth/providers/test_database_key_no_fallback.py`

- [ ] **Step 1: Locate the fallback block**

Run:
```bash
rg -n "legacy fallback|api_key\.zone_id" src/nexus/bricks/auth/providers/database_key.py
```
Expected: a block of ~5-10 lines around line 148-160 (per audit) containing `if not zone_perms_rows:` followed by an `api_key.zone_id` branch.

- [ ] **Step 2: Write the failing test**

Create `tests/unit/bricks/auth/providers/test_database_key_no_fallback.py`:

```python
"""Legacy zone_perms fallback removed in Phase 2 (#3871)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.storage.models._base import Base
from nexus.storage.models.auth import APIKeyModel, ZoneModel
from nexus.storage.api_key_ops import hash_api_key


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ZoneModel(zone_id="eng", name="eng", phase="Active"))
        s.commit()
        yield s


def _insert_legacy_key(session, *, key_id, raw_token, zone_id, is_admin=0):
    """Insert a key in the pre-junction shape (zone_id set, no junction rows)."""
    session.add(APIKeyModel(
        key_id=key_id,
        key_hash=hash_api_key(raw_token),
        user_id="u1",
        name="legacy",
        zone_id=zone_id,
        is_admin=is_admin,
    ))
    session.commit()


def test_non_admin_key_with_empty_junction_returns_no_zone_perms(session):
    _insert_legacy_key(session, key_id="k1", raw_token="nxs_k1_secret", zone_id="eng")
    auth = DatabaseAPIKeyAuth(session_factory=lambda: session)
    result = auth.authenticate("nxs_k1_secret")
    # zone_perms must be empty — no fallback to api_key.zone_id.
    assert result is not None
    assert tuple(result.zone_perms) == ()


def test_admin_key_with_empty_junction_authenticates_zonelessly(session):
    _insert_legacy_key(session, key_id="k2", raw_token="nxs_k2_secret", zone_id=None, is_admin=1)
    auth = DatabaseAPIKeyAuth(session_factory=lambda: session)
    result = auth.authenticate("nxs_k2_secret")
    assert result is not None
    assert result.is_admin is True
    assert tuple(result.zone_perms) == ()
```

(If `DatabaseAPIKeyAuth` constructor / `authenticate` signature differs on develop, adjust — keep the assertions as-is.)

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/bricks/auth/providers/test_database_key_no_fallback.py -v`
Expected: `test_non_admin_key_with_empty_junction_returns_no_zone_perms` FAILs because the current fallback returns `zone_perms = (("eng", "rw"),)`.

- [ ] **Step 4: Remove the fallback block**

Edit `src/nexus/bricks/auth/providers/database_key.py`. Delete the block matching:

```python
if not zone_perms_rows:
    if api_key.zone_id:
        zone_perms = ((api_key.zone_id, "rw"),)
    else:
        zone_perms = ()
```

Replace with:

```python
zone_perms = tuple(zone_perms_rows)
```

(If `zone_perms` was already assigned earlier in a non-fallback path, just delete the fallback block — the earlier assignment is the new sole path.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/bricks/auth/providers/test_database_key_no_fallback.py -v`
Expected: 2 PASS.

- [ ] **Step 6: Run the broader auth-provider tests**

Run: `uv run pytest tests/unit/bricks/auth/providers/ -v`
Expected: all green. If a #3786-era test asserted the fallback (e.g., `test_legacy_single_zone_fallback`), it must now be deleted or updated to assert the new no-fallback behavior. Document the removal in the commit message.

- [ ] **Step 7: Lint + types**

Run: `uv run ruff check src/nexus/bricks/auth/providers/database_key.py tests/unit/bricks/auth/providers/test_database_key_no_fallback.py`
Run: `uv run mypy src/nexus/bricks/auth/providers/database_key.py`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/nexus/bricks/auth/providers/database_key.py tests/unit/bricks/auth/providers/
git commit -m "feat(#3871): remove legacy zone_perms fallback (junction is sole truth)"
```

---

## Task 9: Deprecated `zone` JSON alias → primary

**Purpose:** Three response paths still emit the deprecated singular `zone` field. After Task 6 those are NULL. Re-source them from `get_primary_zone` so single-zone clients see byte-identical output.

**Files:**
- Modify: `src/nexus/cli/commands/hub.py`
- Modify: `src/nexus/server/rpc/handlers/admin.py`
- Modify: `src/nexus/server/api/v2/routers/auth_keys.py`
- Test: `tests/unit/cli/test_hub_token_list_primary_alias.py`
- Test: `tests/unit/server/rpc/handlers/test_admin_primary_alias.py`
- Test: `tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py`

- [ ] **Step 1: Locate the three sites**

Run:
```bash
rg -n 'row\["zone_id"\]|api_key\.zone_id|result\.get\("zone_id"' \
  src/nexus/cli/commands/hub.py \
  src/nexus/server/rpc/handlers/admin.py \
  src/nexus/server/api/v2/routers/auth_keys.py
```
Expected: ≥3 hits across the three files.

- [ ] **Step 2: Write the CLI failing test**

Existing hub-CLI tests in `tests/unit/cli/test_hub.py` use `monkeypatch.setattr` to replace `nexus.cli.commands.hub.create_api_key` and the session factory with stubs, then drive `click.testing.CliRunner`. Mirror that pattern.

Create `tests/unit/cli/test_hub_token_list_primary_alias.py`:

```python
"""token_list deprecated `zone` field uses get_primary_zone (#3871)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nexus.cli.commands.hub import hub  # top-level click group; adjust if renamed
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session_factory_with_keys(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        create_api_key(s, user_id="u1", name="alice", zones=["eng", "ops"])

    # Replace the hub command's session source. The exact attribute name lives
    # at the top of `src/nexus/cli/commands/hub.py` — search for the existing
    # pattern in tests/unit/cli/test_hub.py to copy the matching monkeypatch
    # target. Common pattern: `monkeypatch.setattr("nexus.cli.commands.hub._open_session", lambda: SessionLocal())`.
    from nexus.cli.commands import hub as hub_mod
    monkeypatch.setattr(hub_mod, "_open_session", SessionLocal)
    return SessionLocal


def test_token_list_json_zone_field_equals_primary(session_factory_with_keys):
    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    alice = next(r for r in rows if r["name"] == "alice")
    assert alice["zone"] == "eng"  # primary by granted_at, NOT None


def test_token_list_json_zone_field_is_none_for_zoneless_admin_key(session_factory_with_keys):
    """Zoneless admin keys legitimately have no primary; emit None, not crash."""
    SessionLocal = session_factory_with_keys
    with SessionLocal() as s:
        create_api_key(s, user_id="u1", name="root", is_admin=True)  # zoneless

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    root_row = next(r for r in rows if r["name"] == "root")
    assert root_row["zone"] is None
```

If the actual session-injection attribute on `hub` is not `_open_session`, run `rg -n "session|Session" src/nexus/cli/commands/hub.py | head -20` and copy the pattern from the existing `test_hub.py` monkeypatch calls — the goal is just to make `hub token list` read from the in-memory DB.

- [ ] **Step 3: Write the admin RPC failing test**

Create `tests/unit/server/rpc/handlers/test_admin_primary_alias.py`:

```python
"""admin echo `zone_id` field equals get_primary_zone (#3871)."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nexus.server.rpc.handlers import admin
from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models._base import Base
from nexus.storage.models.auth import ZoneModel


@pytest.fixture
def session_with_key():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for zid in ("eng", "ops"):
            s.add(ZoneModel(zone_id=zid, name=zid, phase="Active"))
        s.commit()
        key_id, _ = create_api_key(s, user_id="u1", name="multi", zones=["eng", "ops"])
        yield s, key_id


def test_admin_get_key_echoes_primary_zone(session_with_key):
    session, key_id = session_with_key
    from types import SimpleNamespace
    response = admin.get_key(session, SimpleNamespace(key_id=key_id))
    assert response["zone_id"] == "eng"  # primary by granted_at
```

(Adjust `admin.get_key` to the actual handler-name that owns line 173 of admin.py.)

- [ ] **Step 4: Write the REST failing test**

Create `tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py`:

```python
"""REST create-key response `zone` field equals get_primary_zone (#3871)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

# Wire app + dependency overrides per the pattern in
# tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py (Task 3).


def test_rest_create_key_response_emits_primary_in_zone_field(app_with_auth):
    client = TestClient(app_with_auth)
    resp = client.post("/v2/auth/keys", json={"name": "alice", "zones": ["eng", "ops"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["zone"] == "eng"  # primary by granted_at, NOT None
```

- [ ] **Step 5: Run all three tests to verify they fail**

Run:
```bash
uv run pytest \
  tests/unit/cli/test_hub_token_list_primary_alias.py \
  tests/unit/server/rpc/handlers/test_admin_primary_alias.py \
  tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py -v
```
Expected: all FAIL — current code emits `None` because `APIKeyModel.zone_id` is now NULL after Task 6.

- [ ] **Step 6: Patch the CLI**

Edit `src/nexus/cli/commands/hub.py`. Locate the `token_list` function. Find the lines that build each row's `zone` field from `row["zone_id"]`. Replace with a single batched lookup:

```python
from nexus.storage.api_key_ops import get_primary_zones_for_keys

# (inside token_list, after fetching all rows but before formatting)
key_ids = [r["key_id"] for r in rows]
primary_map = get_primary_zones_for_keys(session, key_ids)

for r in rows:
    r["zone"] = primary_map.get(r["key_id"])  # None for zoneless admin keys
```

Apply the same `r["zone"] = primary_map.get(r["key_id"])` substitution at both the JSON-output site (line ~288 per audit) and the table-column site (line ~308). Drop the old `r["zone_id"]` reads.

- [ ] **Step 7: Patch the admin RPC handler**

Edit `src/nexus/server/rpc/handlers/admin.py`. Locate the line at ~173 that emits `"zone_id": api_key.zone_id`. Replace with:

```python
from nexus.storage.api_key_ops import get_primary_zone

"zone_id": get_primary_zone(session, api_key.key_id),
```

(If multiple keys are echoed in a list, prefer `get_primary_zones_for_keys` for the batch case — same pattern as Step 6.)

- [ ] **Step 8: Patch the REST router**

Edit `src/nexus/server/api/v2/routers/auth_keys.py`. Locate the create-key response builder (line ~265 per audit) that uses `result.get("zone_id", body.zone_id)`. Replace with:

```python
from nexus.storage.api_key_ops import get_primary_zone

"zone": get_primary_zone(session, result["key_id"]),
```

- [ ] **Step 9: Run the three new tests + their containing dirs**

Run:
```bash
uv run pytest \
  tests/unit/cli/test_hub_token_list_primary_alias.py \
  tests/unit/server/rpc/handlers/test_admin_primary_alias.py \
  tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py \
  tests/unit/cli/ tests/unit/server/rpc/handlers/ tests/unit/server/api/v2/routers/ -v
```
Expected: new 3 PASS; existing tests in those dirs all PASS or only show the 3 pre-existing flakes from #3886's regression baseline.

- [ ] **Step 10: Lint + types**

Run:
```bash
uv run ruff check \
  src/nexus/cli/commands/hub.py \
  src/nexus/server/rpc/handlers/admin.py \
  src/nexus/server/api/v2/routers/auth_keys.py \
  tests/unit/cli/test_hub_token_list_primary_alias.py \
  tests/unit/server/rpc/handlers/test_admin_primary_alias.py \
  tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py
uv run mypy \
  src/nexus/cli/commands/hub.py \
  src/nexus/server/rpc/handlers/admin.py \
  src/nexus/server/api/v2/routers/auth_keys.py
```
Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add \
  src/nexus/cli/commands/hub.py \
  src/nexus/server/rpc/handlers/admin.py \
  src/nexus/server/api/v2/routers/auth_keys.py \
  tests/unit/cli/test_hub_token_list_primary_alias.py \
  tests/unit/server/rpc/handlers/test_admin_primary_alias.py \
  tests/unit/server/api/v2/routers/test_auth_keys_primary_alias.py
git commit -m "feat(#3871): deprecated zone alias re-sourced from get_primary_zone"
```

---

## Task 10: End-to-end test through `nexus up --build`

**Purpose:** Per `feedback_e2e_for_auth_pipeline` memory: unit tests construct dicts directly and miss serializer gaps. Validate the full pipeline once.

**Files:**
- Create: `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py`

- [ ] **Step 1: Inspect the existing hub e2e file for the skip-cleanly pattern**

Run:
```bash
sed -n '1,80p' tests/e2e/self_contained/cli/test_hub_flow.py
```
Expected: shows `_nexus_bin()` helper, `subprocess.run`, and the `pytest.skip(...)` calls keyed off env vars `NEXUS_ADMIN_URL`, `NEXUS_ADMIN_KEY`, `MCP_HTTP_URL`, `NEXUS_DATABASE_URL`. Mirror exactly.

- [ ] **Step 2: Write the e2e test**

Create `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py`:

```python
"""E2E: Phase 2 cleanup of #3871 — multi-zone token through nexus up --build.

Drives `nexus hub token create --zones eng:rw,ops:r` against a real stack
and asserts the deprecated APIKeyModel.zone_id column stays NULL while the
junction is the source of truth. Mirrors the skip pattern from
test_hub_flow.py: skips cleanly if NEXUS_ADMIN_URL / NEXUS_ADMIN_KEY /
NEXUS_DATABASE_URL / MCP_HTTP_URL is unset.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.e2e]


def _nexus_bin() -> str:
    return str(Path(sys.executable).parent / "nexus")


def _required_env() -> dict[str, str]:
    keys = ("NEXUS_ADMIN_URL", "NEXUS_ADMIN_KEY", "NEXUS_DATABASE_URL", "MCP_HTTP_URL")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        pytest.skip(f"missing env: {missing}")
    return {k: os.environ[k] for k in keys}


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_nexus_bin(), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )


def _engine(env: dict[str, str]):
    return create_engine(env["NEXUS_DATABASE_URL"])


def test_phase2_token_create_persists_null_zone_id_and_populates_junction():
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"

    proc = _run(["hub", "token", "create", "--zones", "eng:rw,ops:r", "--name", name, "--json"], env)
    payload = json.loads(proc.stdout)
    key_id = payload["key_id"]

    eng = _engine(env)
    with eng.connect() as conn:
        zone_id_value = conn.execute(
            text("SELECT zone_id FROM api_keys WHERE key_id = :k"), {"k": key_id}
        ).scalar_one()
        junction_zones = {
            r[0]
            for r in conn.execute(
                text("SELECT zone_id FROM api_key_zones WHERE key_id = :k"), {"k": key_id}
            )
        }

    assert zone_id_value is None, "Phase 2: api_keys.zone_id must be NULL"
    assert junction_zones == {"eng", "ops"}


def test_phase2_token_list_emits_primary_in_deprecated_zone_field():
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"
    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:r", "--name", name, "--json"], env
    )
    bob_id = json.loads(create.stdout)["key_id"]

    listed = _run(["hub", "token", "list", "--json"], env)
    rows = json.loads(listed.stdout)
    bob_row = next(r for r in rows if r["key_id"] == bob_id)
    assert bob_row["zone"] == "eng", "deprecated zone alias must equal primary (MIN granted_at)"


def test_phase2_mcp_request_accepts_both_zones():
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"
    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name, "--json"], env
    )
    token = json.loads(create.stdout)["token"]

    # MCP_HTTP_URL points at the hub's MCP endpoint. Issue a search scoped to
    # each zone — both must succeed.
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=10.0) as client:
        eng_resp = client.post(
            f"{env['MCP_HTTP_URL']}/search",
            headers=headers,
            json={"query": "*", "zone_id": "eng"},
        )
        ops_resp = client.post(
            f"{env['MCP_HTTP_URL']}/search",
            headers=headers,
            json={"query": "*", "zone_id": "ops"},
        )
    assert eng_resp.status_code == 200, eng_resp.text
    assert ops_resp.status_code == 200, ops_resp.text


def test_phase2_admin_list_filtered_by_zone_returns_multi_zone_key():
    env = _required_env()
    name = f"e2e-{uuid.uuid4().hex[:8]}"
    create = _run(
        ["hub", "token", "create", "--zones", "eng:rw,ops:rw", "--name", name, "--json"], env
    )
    dave_id = json.loads(create.stdout)["key_id"]

    # Filter by ops — dave's "primary" (MIN granted_at) is eng, so a pre-Phase-2
    # WHERE zone_id='ops' filter would have missed it.
    listed = _run(["hub", "token", "list", "--zone", "ops", "--json"], env)
    ids = [r["key_id"] for r in json.loads(listed.stdout)]
    assert dave_id in ids
```

(If the MCP endpoint shape on `MCP_HTTP_URL` differs from `POST /search` in this codebase, copy the request shape from `tests/e2e/self_contained/mcp/test_mcp_http_audit.py`. The assertion that matters is that both zones return 200.)

- [ ] **Step 3: Run the e2e file (no live stack — should skip cleanly)**

Run: `uv run pytest tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py -v`
Expected: 4 SKIPPED with reason mentioning "no live stack". No errors.

- [ ] **Step 4: Bring up a live stack and rerun**

Run:
```bash
nexus up --build
uv run pytest tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py -v
nexus down
```
Expected: 4 PASS.

- [ ] **Step 5: Lint**

Run: `uv run ruff check tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py
git commit -m "test(#3871): e2e — multi-zone token through nexus up --build"
```

---

## Task 11: Regression sweep + PR

**Purpose:** Run the same scope #3886 used to confirm no regressions, then open the PR.

- [ ] **Step 1: Run the full regression sweep**

Run:
```bash
uv run pytest \
  tests/unit/storage \
  tests/unit/cli \
  tests/unit/auth \
  tests/unit/bricks/auth \
  tests/unit/bricks/mcp \
  tests/unit/server/api/v2/routers \
  tests/unit/contracts \
  -v --tb=short 2>&1 | tail -40
```
Expected: ~1085 tests PASS, with at most the 3 pre-existing environmental flakes documented in PR #3886 (`file_cache` xdist isolation, `mcp_server_tools` mock setup, `operation_log` Rust metastore). Any **new** failure stops the plan — do not proceed to PR.

- [ ] **Step 2: Run the alembic round-trip one more time**

Run: `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: clean.

- [ ] **Step 3: Push the branch**

Run:
```bash
git push -u origin HEAD
```

- [ ] **Step 4: Open the PR**

Run:
```bash
gh pr create --title "feat(#3871): multi-zone tokens phase 2 cleanup" --body "$(cat <<'EOF'
Closes #3871. Phase 2 of multi-zone tokens — cleans up the items PR #3886
explicitly deferred. Spec: `docs/superpowers/specs/2026-04-25-issue-3871-multi-zone-tokens-phase2-cleanup-design.md`. Plan: `docs/superpowers/plans/2026-04-25-issue-3871-multi-zone-tokens-phase2-cleanup.md`.

## Summary
- Migrate the four `WHERE APIKeyModel.zone_id = ?` filter sites to junction joins (`database_key.list_keys`, REST `/v2/auth/keys`, admin RPC list/list_by_zone/revoke, `sqlalchemy_api_key_store.revoke_key`).
- Add `get_primary_zone(key_id)` + batch variant in `api_key_ops` (MIN granted_at, zone_id ASC tiebreaker).
- Stop writing `APIKeyModel.zone_id` in `create_api_key`; column stays nullable.
- Drop the legacy `zone_perms` fallback in `database_key.py`; junction is sole truth.
- Add tripwire alembic migration that fails loudly if any non-revoked, non-admin key lacks a junction row.
- Re-source the deprecated singular `zone` JSON alias (CLI `token list`, admin echo, REST create response) from `get_primary_zone` — single-zone clients see byte-identical output.

## Visible behavior change
Admin "list keys in zone X" now returns every key with X access, not just keys whose primary is X. Documented in the spec §3.2.1. Multi-zone keys appear under multiple filters; renderers that count distinct keys must dedupe by `key_id`.

## Out of scope
- Dropping the `zone_id` column entirely (Phase 3 — separate PR after one release of soak).
- `OperationLogModel.zone_id` (already correct under multi-zone).
- Removing the deprecated `zone` JSON field outright (breaking change; separate PR).

## Test plan
- [ ] Unit: 7 new test files covering helper, 4 filter migrations, fallback removal, primary alias, tripwire migration.
- [ ] Migration: `alembic upgrade → downgrade → upgrade` clean against sqlite. Tripwire raises on synthetic broken row, no-op on healthy.
- [ ] E2E: `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py` against `nexus up --build`.
- [ ] Regression sweep: 1085 tests, no new failures beyond the 3 pre-existing environmental flakes.
EOF
)"
```

- [ ] **Step 5: Verify CI**

Run: `gh pr checks --watch`
Expected: all checks pass (or only the same pre-existing failures #3886 baselined).

---

## Spec coverage checklist (verify before marking plan complete)

- [x] §1 (background) — covered by spec link in plan header + commit messages.
- [x] §2 in-scope item: migrate 4 filter sites — Tasks 2, 3, 4, 5.
- [x] §2 in-scope item: `get_primary_zone` helper + batch variant — Task 1.
- [x] §2 in-scope item: stop writing `zone_id` — Task 6.
- [x] §2 in-scope item: remove fallback — Task 8.
- [x] §2 in-scope item: tripwire migration — Task 7.
- [x] §2 in-scope item: deprecated alias → primary — Task 9.
- [x] §3.1 helper API — Task 1 step 3.
- [x] §3.2 filter pattern — applied uniformly in Tasks 2-5.
- [x] §3.2.1 visible behavior change — documented in Task 11 PR body.
- [x] §3.3 stop writing — Task 6.
- [x] §3.4 fallback removal — Task 8.
- [x] §3.5 tripwire — Task 7.
- [x] §3.6 deprecated alias — Task 9.
- [x] §4.1 unit tests — covered across Tasks 1, 2, 3, 4, 5, 7, 8, 9.
- [x] §4.2 migration tests — Task 7.
- [x] §4.3 e2e — Task 10.
- [x] §4.4 regression sweep — Task 11.
- [x] §6 acceptance criteria — every box maps to a task step.
- [x] §7 Phase 3 (deferred) — explicitly out of scope; not in any task.
