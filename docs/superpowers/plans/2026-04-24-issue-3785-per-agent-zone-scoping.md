# Per-agent zone scoping (Issue #3785) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve P3-1 single-zone bearer tokens into multi-zone tokens — `nexus hub token create --zones eng,ops` mints a credential whose zone allow-list flows from auth → `OperationContext` → ReBAC and federated search.

**Architecture:** Junction table `api_key_zones` is the source of truth for a token's zone set. `ResolvedIdentity` and `OperationContext` carry both `zone_id` (primary, single-zone defaults) and `zone_set` (allow-list, fan-out). Routers gate explicit zone references via `assert_zone_allowed()` (admin bypass) and fan out across `zone_set` when no zone is given. Single-zone tokens (entire P3-1 install base) hit the same code path with zero overhead.

**Tech Stack:** Python 3.x, SQLAlchemy ORM, Alembic migrations, Click CLI, FastAPI routers, pytest.

**Spec:** `docs/superpowers/specs/2026-04-24-issue-3785-per-agent-zone-scoping-design.md`

---

## Conventions for all tasks

- **Test runner:** `uv run pytest …` (the project uses uv with an editable install of `nexus-ai-fs` from the worktree). Bare `pytest` resolves against a stale site-packages copy and will produce false positives/negatives.
- **Base import:** `from nexus.storage.models._base import Base` (the module is `_base`, not `base`).
- **`ZoneModel` requires `name=`** in test fixtures (`name` is `nullable=False`). Use `ZoneModel(zone_id="eng", name="eng", phase="Active")`.
- **`pytest.raises`:** prefer specific exception classes (e.g. `pytest.raises(IntegrityError)`) over bare `Exception` to satisfy ruff B017.

---

## File Structure

**New files:**
- `src/nexus/storage/models/api_key_zones.py` — `APIKeyZoneModel` (or extend `models/auth.py`; we extend `auth.py` since `APIKeyModel` already lives there).
- `alembic/versions/<rev>_add_api_key_zones.py` — junction table + backfill.
- `tests/unit/bricks/mcp/test_assert_zone_allowed.py` — allow-list helper.
- `tests/integration/server/test_search_zone_scoping.py` — AC #2 + #3.
- `tests/integration/server/test_file_read_zone_scoping.py` — AC #4.
- `tests/integration/server/test_token_expiry_zone_scoping.py` — AC #5 in zone-scoping context.
- `tests/migrations/test_api_key_zones_backfill.py` — migration backfill.

**Modified files:**
- `src/nexus/storage/models/auth.py` — add `APIKeyZoneModel`.
- `src/nexus/storage/api_key_ops.py` — extend `create_api_key()` to accept zone list; add `add_zone_to_key()`, `remove_zone_from_key()`, `list_zones_for_key()`, `get_zones_for_key()`.
- `src/nexus/contracts/types.py:78-138` — `OperationContext.zone_set: tuple[str, ...]`; `__post_init__` defaults to `(zone_id,)`. Add `assert_zone_allowed()` module-level helper.
- `src/nexus/bricks/auth/providers/database_key.py` — `DatabaseAPIKeyAuth.authenticate()` loads zone set; populates `ResolvedIdentity.zone_set`.
- `src/nexus/bricks/mcp/auth_bridge.py` — `ResolvedIdentity.zone_set: tuple[str, ...]`; `op_context_to_auth_dict()` adds `"zone_set"`; `resolve_mcp_operation_context()` propagates.
- `src/nexus/cli/commands/hub.py` — `--zones` (CSV) on `token create`; `--zone` hidden alias; `zones` column in `token list`; new `nexus hub token zones {add,remove,show}` group.
- `src/nexus/server/api/v2/routers/search.py` — three-branch logic (explicit zone → assert; no zone single-set → unchanged; no zone multi-set → federated fan-out).
- `src/nexus/server/api/v2/routers/async_files.py` — `assert_zone_allowed()` at file-op entrypoints (read/write/delete/list).
- `tests/unit/cli/test_hub.py` — multi-zone token creation, zones add/remove tests.
- `tests/unit/auth/test_database_key.py` — zone_set load + legacy fallback.
- `tests/unit/bricks/mcp/test_auth_bridge_cache.py` — zone_set in cached identity.

**Conventions:** Tests live mirror-pathed under `tests/unit/...` or `tests/integration/...`. Imports use absolute `nexus.*` paths. Migration revisions use a descriptive filename plus a fresh revision id (let `alembic revision -m` produce it).

---

## Task 1: `APIKeyZoneModel` (junction table SQLAlchemy model)

**Files:**
- Modify: `src/nexus/storage/models/auth.py`
- Test: `tests/unit/storage/models/test_api_key_zone_model.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/models/test_api_key_zone_model.py`:

```python
"""APIKeyZoneModel — junction table for token → zone allow-list (#3785)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel
from nexus.storage.models.base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_junction_row_inserts_and_loads(session):
    session.add(ZoneModel(zone_id="eng", phase="Active"))
    session.add(ZoneModel(zone_id="ops", phase="Active"))
    session.add(
        APIKeyModel(
            key_id="kid_1",
            key_hash="hash_1",
            user_id="alice",
            name="alice",
            zone_id="eng",
        )
    )
    session.commit()

    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="ops"))
    session.commit()

    rows = (
        session.execute(
            select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == "kid_1")
        )
        .scalars()
        .all()
    )
    zones = sorted(r.zone_id for r in rows)
    assert zones == ["eng", "ops"]
    assert all(isinstance(r.granted_at, datetime) for r in rows)


def test_composite_pk_prevents_duplicate(session):
    session.add(ZoneModel(zone_id="eng", phase="Active"))
    session.add(
        APIKeyModel(
            key_id="kid_1",
            key_hash="hash_1",
            user_id="alice",
            name="alice",
            zone_id="eng",
        )
    )
    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    session.commit()

    session.add(APIKeyZoneModel(key_id="kid_1", zone_id="eng"))
    with pytest.raises(Exception):  # IntegrityError on duplicate composite PK
        session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/storage/models/test_api_key_zone_model.py -v`
Expected: FAIL — `ImportError: cannot import name 'APIKeyZoneModel'`.

- [ ] **Step 3: Add the model**

In `src/nexus/storage/models/auth.py`, after the `APIKeyModel` class (~line 170), add:

```python
class APIKeyZoneModel(Base):
    """Junction: token → zone allow-list (#3785). Composite PK (key_id, zone_id)."""

    __tablename__ = "api_key_zones"

    key_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_keys.key_id", ondelete="CASCADE"),
        primary_key=True,
    )
    zone_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("zones.zone_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_api_key_zones_key", "key_id"),
        Index("idx_api_key_zones_zone", "zone_id"),
    )
```

In `src/nexus/storage/models/__init__.py`, export `APIKeyZoneModel` alongside `APIKeyModel`:

```python
from nexus.storage.models.auth import (
    APIKeyModel,
    APIKeyZoneModel,
    OAuthAPIKeyModel,
    UserModel,
    UserOAuthAccountModel,
)
```

(Add `APIKeyZoneModel` to the existing import block; preserve other names.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/storage/models/test_api_key_zone_model.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/storage/models/auth.py src/nexus/storage/models/__init__.py tests/unit/storage/models/test_api_key_zone_model.py
git commit -m "feat(#3785): add APIKeyZoneModel junction table"
```

---

## Task 2: Alembic migration with backfill

**Files:**
- Create: `alembic/versions/<rev>_add_api_key_zones.py` (filename produced by `alembic revision`)
- Test: `tests/migrations/test_api_key_zones_backfill.py`

- [ ] **Step 1: Generate migration revision**

Run from repo root:

```bash
alembic revision -m "add api_key_zones junction table for #3785"
```

Note the produced revision file path. Open it.

- [ ] **Step 2: Write the migration upgrade/downgrade**

Replace the generated file's body with:

```python
"""add api_key_zones junction table for #3785

Revision ID: <leave as generated>
Revises: <leave as generated — alembic resolves down_revision>
Create Date: <leave as generated>
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic. (leave alembic-generated values intact)
revision = "<as generated>"
down_revision = "<as generated>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_key_zones",
        sa.Column("key_id", sa.String(length=36), nullable=False),
        sa.Column("zone_id", sa.String(length=255), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["key_id"], ["api_keys.key_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.zone_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("key_id", "zone_id"),
    )
    op.create_index("idx_api_key_zones_key", "api_key_zones", ["key_id"])
    op.create_index("idx_api_key_zones_zone", "api_key_zones", ["zone_id"])

    # Backfill: every live token gets one junction row matching its current
    # primary zone_id. Idempotent set-based insert.
    op.execute(
        """
        INSERT INTO api_key_zones (key_id, zone_id, granted_at)
        SELECT key_id, zone_id, created_at FROM api_keys WHERE revoked = 0
        """
    )


def downgrade() -> None:
    op.drop_index("idx_api_key_zones_zone", table_name="api_key_zones")
    op.drop_index("idx_api_key_zones_key", table_name="api_key_zones")
    op.drop_table("api_key_zones")
```

Leave `revision`, `down_revision`, and `Create Date` as alembic generated them — do not hand-edit.

- [ ] **Step 3: Write the backfill test**

Create `tests/migrations/test_api_key_zones_backfill.py`:

```python
"""Verifies the api_key_zones backfill mirrors api_keys.zone_id (#3785)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from nexus.storage.models import APIKeyModel, ZoneModel
from nexus.storage.models.base import Base


def test_backfill_creates_one_junction_row_per_live_token(tmp_path):
    """Pre-migration: api_keys with single zone_id. Post-migration: matching junction row."""
    db_path = tmp_path / "backfill.db"
    engine = create_engine(f"sqlite:///{db_path}")

    # Build pre-migration shape: api_keys + zones, no junction yet.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE zones (
                zone_id VARCHAR(255) PRIMARY KEY,
                phase VARCHAR(50)
            )
        """))
        conn.execute(text("""
            CREATE TABLE api_keys (
                key_id VARCHAR(36) PRIMARY KEY,
                key_hash VARCHAR(64) NOT NULL,
                user_id VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                zone_id VARCHAR(255) NOT NULL,
                revoked INTEGER DEFAULT 0,
                created_at DATETIME
            )
        """))
        conn.execute(text(
            "INSERT INTO zones (zone_id, phase) VALUES ('eng', 'Active')"
        ))
        conn.execute(text("""
            INSERT INTO api_keys
              (key_id, key_hash, user_id, name, zone_id, revoked, created_at)
            VALUES
              ('kid_live', 'h1', 'alice', 'alice', 'eng', 0, '2026-04-01'),
              ('kid_dead', 'h2', 'bob',   'bob',   'eng', 1, '2026-04-01')
        """))

    # Apply the upgrade body inline (DDL + backfill) — same SQL the migration runs.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE api_key_zones (
                key_id VARCHAR(36) NOT NULL,
                zone_id VARCHAR(255) NOT NULL,
                granted_at DATETIME NOT NULL,
                PRIMARY KEY (key_id, zone_id),
                FOREIGN KEY (key_id) REFERENCES api_keys (key_id) ON DELETE CASCADE,
                FOREIGN KEY (zone_id) REFERENCES zones (zone_id) ON DELETE RESTRICT
            )
        """))
        conn.execute(text("""
            INSERT INTO api_key_zones (key_id, zone_id, granted_at)
            SELECT key_id, zone_id, created_at FROM api_keys WHERE revoked = 0
        """))

    # Assert: live token has one junction row, revoked token has none.
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT key_id, zone_id FROM api_key_zones ORDER BY key_id"
        )).all()
    assert rows == [("kid_live", "eng")]
```

- [ ] **Step 4: Run the test**

Run: `pytest tests/migrations/test_api_key_zones_backfill.py -v`
Expected: PASS.

- [ ] **Step 5: Verify alembic upgrade runs cleanly**

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Expected: each command exits 0; no errors. (If `alembic.ini` requires a specific DB URL, set `NEXUS_DATABASE_URL=sqlite:///./tmp_alembic.db` first.)

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/*api_key_zones*.py tests/migrations/test_api_key_zones_backfill.py
git commit -m "feat(#3785): alembic migration for api_key_zones junction"
```

---

## Task 3: `OperationContext.zone_set` field + `assert_zone_allowed()` helper

**Files:**
- Modify: `src/nexus/contracts/types.py:78-138`
- Test: `tests/unit/contracts/test_operation_context_zone_set.py`
- Test: `tests/unit/contracts/test_assert_zone_allowed.py`

- [ ] **Step 1: Write the failing test for `zone_set`**

Create `tests/unit/contracts/test_operation_context_zone_set.py`:

```python
"""OperationContext gains zone_set: tuple[str, ...] — allow-list (#3785)."""

from __future__ import annotations

from nexus.contracts.types import OperationContext


def test_zone_set_defaults_to_zone_id_singleton():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng")
    assert ctx.zone_set == ("eng",)


def test_zone_set_explicit_overrides_default():
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_id="eng",
        zone_set=("eng", "ops"),
    )
    assert ctx.zone_set == ("eng", "ops")


def test_zone_set_empty_when_zone_id_is_none():
    ctx = OperationContext(user_id="alice", groups=[], zone_id=None)
    assert ctx.zone_set == ()


def test_zone_set_is_tuple_for_hashability():
    ctx = OperationContext(
        user_id="alice", groups=[], zone_id="eng", zone_set=("eng", "ops")
    )
    assert isinstance(ctx.zone_set, tuple)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_operation_context_zone_set.py -v`
Expected: FAIL — unexpected keyword argument `zone_set`.

- [ ] **Step 3: Add the field and post-init default**

In `src/nexus/contracts/types.py`, inside `OperationContext`:

After the existing `zone_id: str | None = None` line (~line 111), add:

```python
    zone_set: tuple[str, ...] = ()
```

In the existing `__post_init__()` (~line 135), after the existing `if self.subject_id is None:` block, add:

```python
        # zone_set defaults to (zone_id,) when zone is set; () otherwise.
        # Multi-zone tokens populate zone_set explicitly via auth_bridge.
        if not self.zone_set and self.zone_id is not None:
            object.__setattr__(self, "zone_set", (self.zone_id,))
```

(Use `object.__setattr__` only if the dataclass is frozen. The current class is not frozen — plain assignment `self.zone_set = (self.zone_id,)` is fine. Verify by looking at `@dataclass` decorator at line 77.)

- [ ] **Step 4: Run tests for `zone_set`**

Run: `pytest tests/unit/contracts/test_operation_context_zone_set.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for `assert_zone_allowed`**

Create `tests/unit/contracts/test_assert_zone_allowed.py`:

```python
"""assert_zone_allowed — gate explicit zone references against token allow-list (#3785)."""

from __future__ import annotations

import pytest

from nexus.contracts.types import OperationContext, assert_zone_allowed


def test_in_set_passes():
    ctx = OperationContext(
        user_id="alice", groups=[], zone_id="eng", zone_set=("eng", "ops")
    )
    assert_zone_allowed(ctx, "ops")  # no raise


def test_out_of_set_raises():
    ctx = OperationContext(
        user_id="alice", groups=[], zone_id="eng", zone_set=("eng",)
    )
    with pytest.raises(PermissionError) as exc:
        assert_zone_allowed(ctx, "legal")
    assert "legal" in str(exc.value)
    assert "('eng',)" in str(exc.value) or "['eng']" in str(exc.value)


def test_admin_bypasses_set():
    ctx = OperationContext(
        user_id="root", groups=[], zone_id="eng", is_admin=True, zone_set=("eng",)
    )
    assert_zone_allowed(ctx, "legal")  # no raise
```

- [ ] **Step 6: Run to verify failure**

Run: `pytest tests/unit/contracts/test_assert_zone_allowed.py -v`
Expected: FAIL — `cannot import name 'assert_zone_allowed'`.

- [ ] **Step 7: Add the helper**

In `src/nexus/contracts/types.py`, at module level (e.g. immediately after the `OperationContext` class), add:

```python
def assert_zone_allowed(ctx: OperationContext, requested: str) -> None:
    """Raise PermissionError if `requested` is not in the token's zone allow-list.

    Admins (ctx.is_admin) bypass the check — mirrors existing ReBAC admin shortcut.
    """
    if ctx.is_admin or requested in ctx.zone_set:
        return
    raise PermissionError(
        f"zone {requested!r} not in token's allow-list {ctx.zone_set}"
    )
```

- [ ] **Step 8: Run tests**

Run: `pytest tests/unit/contracts/test_assert_zone_allowed.py tests/unit/contracts/test_operation_context_zone_set.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 9: Commit**

```bash
git add src/nexus/contracts/types.py tests/unit/contracts/test_operation_context_zone_set.py tests/unit/contracts/test_assert_zone_allowed.py
git commit -m "feat(#3785): OperationContext.zone_set + assert_zone_allowed helper"
```

---

## Task 4: `api_key_ops` — extend `create_api_key()` for zone list

**Files:**
- Modify: `src/nexus/storage/api_key_ops.py:73-125`
- Test: `tests/unit/storage/test_api_key_ops_zones.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_api_key_ops_zones.py`:

```python
"""create_api_key accepts a zone list and writes junction rows (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import create_api_key
from nexus.storage.models import APIKeyModel, APIKeyZoneModel, ZoneModel
from nexus.storage.models.base import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(ZoneModel(zone_id="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", phase="Active"))
        s.commit()
        yield s


def test_single_zone_creates_one_junction_row(session, monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zones=["eng"],
    )
    session.commit()

    junction = (
        session.execute(
            select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == key_id)
        )
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng"]
    primary = session.get(APIKeyModel, key_id)
    assert primary.zone_id == "eng"


def test_multi_zone_creates_one_junction_row_per_zone(session, monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zones=["eng", "ops"],
    )
    session.commit()

    junction = (
        session.execute(
            select(APIKeyZoneModel)
            .where(APIKeyZoneModel.key_id == key_id)
            .order_by(APIKeyZoneModel.zone_id)
        )
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng", "ops"]
    primary = session.get(APIKeyModel, key_id)
    assert primary.zone_id == "eng"  # first in zones list


def test_zone_id_legacy_kwarg_still_works(session, monkeypatch):
    """Backward-compat for callers that still pass single zone_id."""
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    key_id, _ = create_api_key(
        session,
        user_id="alice",
        name="alice",
        zone_id="eng",
    )
    session.commit()

    junction = (
        session.execute(
            select(APIKeyZoneModel).where(APIKeyZoneModel.key_id == key_id)
        )
        .scalars()
        .all()
    )
    assert [r.zone_id for r in junction] == ["eng"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/storage/test_api_key_ops_zones.py -v`
Expected: FAIL — `create_api_key()` doesn't accept `zones` kwarg or doesn't write junction rows.

- [ ] **Step 3: Modify `create_api_key()`**

In `src/nexus/storage/api_key_ops.py`, change the signature and body of `create_api_key` (line 73). Replace:

```python
def create_api_key(
    session,
    *,
    user_id: str,
    name: str,
    zone_id: str | None = None,
    is_admin: bool = False,
    expires_at=None,
    subject_type: str = "user",
    subject_id: str | None = None,
) -> tuple[str, str]:
    # ... existing body ...
```

with:

```python
def create_api_key(
    session,
    *,
    user_id: str,
    name: str,
    zones: list[str] | None = None,
    zone_id: str | None = None,
    is_admin: bool = False,
    expires_at=None,
    subject_type: str = "user",
    subject_id: str | None = None,
) -> tuple[str, str]:
    """Create an API key with one or more zones in its allow-list.

    Either `zones` (list) or `zone_id` (single, legacy) may be passed; if both
    are present, `zones` wins. Primary zone (APIKeyModel.zone_id) = first item.
    A junction row is inserted per zone in `api_key_zones` (#3785).
    """
    from nexus.storage.models import APIKeyZoneModel  # local import to avoid cycle

    if zones is None:
        zones = [zone_id] if zone_id else []
    if not zones:
        raise ValueError("create_api_key requires at least one zone")

    primary_zone = zones[0]

    # ... existing body up through the APIKeyModel insert, but pass primary_zone:
    # zone_prefix = f"{primary_zone[:8]}_" if primary_zone else ""
    # ... build raw_key, key_hash, etc., as today ...
    # api_key = APIKeyModel(..., zone_id=primary_zone, ...)
    # session.add(api_key)
    # session.flush()  # ensure key_id is assigned before junction inserts

    for z in zones:
        session.add(APIKeyZoneModel(key_id=key_id, zone_id=z))

    return key_id, raw_key
```

(Keep all existing code that hashes the key, builds `raw_key`, inserts the `APIKeyModel` row. Only changes: accept `zones`, derive `primary_zone`, swap `zone_id` → `primary_zone` in the prefix and model insert, and add the junction inserts after `session.flush()`.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/storage/test_api_key_ops_zones.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run regression on existing tests**

Run: `pytest tests/unit/storage/ -v 2>&1 | tail -30`
Expected: no new failures (existing single-`zone_id` callers covered by Step 3 backward-compat branch).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/storage/api_key_ops.py tests/unit/storage/test_api_key_ops_zones.py
git commit -m "feat(#3785): create_api_key accepts zones list, writes junction rows"
```

---

## Task 5: `api_key_ops` — zone CRUD helpers

**Files:**
- Modify: `src/nexus/storage/api_key_ops.py`
- Test: `tests/unit/storage/test_api_key_ops_zone_crud.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/storage/test_api_key_ops_zone_crud.py`:

```python
"""Zone-list CRUD helpers for tokens (#3785)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nexus.storage.api_key_ops import (
    add_zone_to_key,
    create_api_key,
    get_zones_for_key,
    remove_zone_from_key,
)
from nexus.storage.models import APIKeyZoneModel, ZoneModel
from nexus.storage.models.base import Base


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for z in ("eng", "ops", "legal"):
            s.add(ZoneModel(zone_id=z, phase="Active"))
        s.commit()
        yield s


def test_get_zones_for_key_returns_set(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()
    assert sorted(get_zones_for_key(session, key_id)) == ["eng", "ops"]


def test_add_zone_inserts_junction_row(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    added = add_zone_to_key(session, key_id, "ops")
    session.commit()
    assert added is True
    assert sorted(get_zones_for_key(session, key_id)) == ["eng", "ops"]


def test_add_zone_idempotent(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    added = add_zone_to_key(session, key_id, "eng")
    session.commit()
    assert added is False  # already present


def test_remove_zone_deletes_junction_row(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()

    removed = remove_zone_from_key(session, key_id, "ops")
    session.commit()
    assert removed is True
    assert get_zones_for_key(session, key_id) == ["eng"]


def test_remove_zone_refuses_last_zone(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng"])
    session.commit()

    with pytest.raises(ValueError, match="last zone"):
        remove_zone_from_key(session, key_id, "eng")


def test_remove_unknown_zone_returns_false(session):
    key_id, _ = create_api_key(session, user_id="a", name="a", zones=["eng", "ops"])
    session.commit()
    assert remove_zone_from_key(session, key_id, "legal") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/storage/test_api_key_ops_zone_crud.py -v`
Expected: FAIL — `cannot import name 'add_zone_to_key'`.

- [ ] **Step 3: Add the helpers**

At the bottom of `src/nexus/storage/api_key_ops.py`, add:

```python
def get_zones_for_key(session, key_id: str) -> list[str]:
    """Return the full zone allow-list for a token (#3785)."""
    from nexus.storage.models import APIKeyZoneModel

    rows = (
        session.execute(
            sa.select(APIKeyZoneModel.zone_id).where(APIKeyZoneModel.key_id == key_id)
        )
        .scalars()
        .all()
    )
    return list(rows)


def add_zone_to_key(session, key_id: str, zone_id: str) -> bool:
    """Add a zone to a token's allow-list. Idempotent — returns False if already present."""
    from nexus.storage.models import APIKeyZoneModel

    existing = session.get(APIKeyZoneModel, (key_id, zone_id))
    if existing is not None:
        return False
    session.add(APIKeyZoneModel(key_id=key_id, zone_id=zone_id))
    return True


def remove_zone_from_key(session, key_id: str, zone_id: str) -> bool:
    """Remove a zone. Refuses to leave a token with zero zones (raises ValueError)."""
    from nexus.storage.models import APIKeyZoneModel

    current = get_zones_for_key(session, key_id)
    if zone_id not in current:
        return False
    if len(current) == 1:
        raise ValueError(
            f"refusing to remove last zone {zone_id!r} from key {key_id!r}; "
            "revoke the token instead"
        )
    row = session.get(APIKeyZoneModel, (key_id, zone_id))
    session.delete(row)
    return True
```

(Add `import sqlalchemy as sa` at the top of the file if not already present. Check the existing imports.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/storage/test_api_key_ops_zone_crud.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/nexus/storage/api_key_ops.py tests/unit/storage/test_api_key_ops_zone_crud.py
git commit -m "feat(#3785): api_key_ops zone CRUD helpers"
```

---

## Task 6: `ResolvedIdentity.zone_set` + `DatabaseAPIKeyAuth` loads it

**Files:**
- Modify: `src/nexus/bricks/auth/providers/database_key.py`
- Modify: `src/nexus/bricks/mcp/auth_bridge.py` (add `zone_set` to `ResolvedIdentity`)
- Test: `tests/unit/auth/test_database_key.py` (extend)

- [ ] **Step 1: Locate `ResolvedIdentity` and add `zone_set`**

Open `src/nexus/bricks/mcp/auth_bridge.py`. Find the `ResolvedIdentity` dataclass (typically near the top of the file, before line 22). Add `zone_set: tuple[str, ...] = ()` field. If the dataclass is `@dataclass(frozen=True)`, the field default is fine.

- [ ] **Step 2: Write the failing test for zone_set load**

In `tests/unit/auth/test_database_key.py`, add:

```python
def test_authenticate_loads_zone_set_from_junction(monkeypatch):
    """DatabaseAPIKeyAuth.authenticate populates zone_set from api_key_zones (#3785)."""
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.storage.api_key_ops import create_api_key
    from nexus.storage.models import ZoneModel
    from nexus.storage.models.base import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        s.add(ZoneModel(zone_id="eng", phase="Active"))
        s.add(ZoneModel(zone_id="ops", phase="Active"))
        s.commit()
        _, raw_key = create_api_key(
            s, user_id="alice", name="alice", zones=["eng", "ops"]
        )
        s.commit()

    auth = DatabaseAPIKeyAuth(session_factory=Session)
    result = auth.authenticate_sync(raw_key)  # or however the sync path is exposed

    assert result.authenticated is True
    assert result.zone_id == "eng"
    assert sorted(result.zone_set) == ["eng", "ops"]


def test_authenticate_legacy_token_falls_back_to_zone_id(monkeypatch):
    """Legacy token with no junction rows → zone_set = (zone_id,)."""
    from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
    from nexus.storage.api_key_ops import hash_api_key
    from nexus.storage.models import APIKeyModel
    from nexus.storage.models.base import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    monkeypatch.setenv("NEXUS_API_KEY_HMAC_SECRET", "test-secret")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    raw_key = "nxs_legacy_test_abc"
    with Session() as s:
        s.add(APIKeyModel(
            key_id="kid_legacy", key_hash=hash_api_key(raw_key),
            user_id="legacy", name="legacy", zone_id="eng",
        ))
        s.commit()

    auth = DatabaseAPIKeyAuth(session_factory=Session)
    result = auth.authenticate_sync(raw_key)

    assert result.authenticated is True
    assert result.zone_set == ("eng",)
```

(If `authenticate_sync` is not the method name, find the actual entrypoint by reading the existing tests' first lines. Adapt method name only.)

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/unit/auth/test_database_key.py::test_authenticate_loads_zone_set_from_junction -v`
Expected: FAIL — `result.zone_set` does not exist or is empty.

- [ ] **Step 4: Modify `DatabaseAPIKeyAuth.authenticate()`**

In `src/nexus/bricks/auth/providers/database_key.py` (lines 27-100), inside the existing authenticate method, after the APIKeyModel is loaded and verified, add a junction-table query:

```python
from nexus.storage.models import APIKeyZoneModel  # add to imports

# ... existing code through the APIKeyModel load + revoke/expiry checks ...

zone_set_rows = (
    session.execute(
        sa.select(APIKeyZoneModel.zone_id).where(
            APIKeyZoneModel.key_id == api_key_row.key_id
        )
    )
    .scalars()
    .all()
)
zone_set: tuple[str, ...] = tuple(zone_set_rows) or (api_key_row.zone_id,)
```

Pass `zone_set=zone_set` into the `ResolvedIdentity` (or auth result) constructor.

If the fallback branch fires (legacy token, no junction rows), log once per key_id at WARN:

```python
import functools

@functools.lru_cache(maxsize=4096)
def _warn_legacy_token_once(key_id: str) -> None:
    logger.warning("legacy api_key %s has no api_key_zones rows; backfill missing", key_id)

# ... in the auth path, when zone_set_rows is empty:
if not zone_set_rows:
    _warn_legacy_token_once(api_key_row.key_id)
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/auth/test_database_key.py -v`
Expected: PASS (existing + 2 new tests).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/auth/providers/database_key.py src/nexus/bricks/mcp/auth_bridge.py tests/unit/auth/test_database_key.py
git commit -m "feat(#3785): DatabaseAPIKeyAuth loads zone_set; ResolvedIdentity.zone_set"
```

---

## Task 7: `auth_bridge` propagation — `op_context_to_auth_dict` + `resolve_mcp_operation_context`

**Files:**
- Modify: `src/nexus/bricks/mcp/auth_bridge.py` (lines 22-43, 132-259)
- Test: `tests/unit/bricks/mcp/test_auth_bridge_zone_set.py`
- Test: `tests/unit/bricks/mcp/test_auth_bridge_cache.py` (extend)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/bricks/mcp/test_auth_bridge_zone_set.py`:

```python
"""auth_bridge propagates zone_set through to OperationContext (#3785)."""

from __future__ import annotations

from nexus.bricks.mcp.auth_bridge import op_context_to_auth_dict
from nexus.contracts.types import OperationContext


def test_op_context_to_auth_dict_includes_zone_set():
    ctx = OperationContext(
        user_id="alice",
        groups=[],
        zone_id="eng",
        zone_set=("eng", "ops"),
        is_admin=False,
    )
    auth = op_context_to_auth_dict(ctx)
    assert auth["zone_id"] == "eng"
    assert auth["zone_set"] == ["eng", "ops"]
    assert auth["is_admin"] is False


def test_auth_dict_zone_set_defaults_to_zone_id_singleton():
    ctx = OperationContext(user_id="alice", groups=[], zone_id="eng")
    auth = op_context_to_auth_dict(ctx)
    assert auth["zone_set"] == ["eng"]
```

In `tests/unit/bricks/mcp/test_auth_bridge_cache.py`, add:

```python
def test_zone_set_cached_with_identity():
    """Cached identity preserves zone_set tuple (#3785)."""
    # Call authenticate_api_key twice with the same key; assert zone_set is identical
    # and that the underlying provider was invoked exactly once.
    # Build an auth result via _mk_auth_result + a custom field carrying zone_set:
    from nexus.bricks.mcp import auth_bridge

    result = _mk_auth_result(subject_id="alice", zone_id="eng")
    result.zone_set = ("eng", "ops")  # provider returns full set

    provider_calls = []

    def fake_provider(key: str):
        provider_calls.append(key)
        return result

    cached_1 = auth_bridge.authenticate_api_key("nxs_alice_x", auth_provider=fake_provider)
    cached_2 = auth_bridge.authenticate_api_key("nxs_alice_x", auth_provider=fake_provider)

    assert cached_1.zone_set == ("eng", "ops")
    assert cached_2.zone_set == ("eng", "ops")
    assert len(provider_calls) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/bricks/mcp/test_auth_bridge_zone_set.py tests/unit/bricks/mcp/test_auth_bridge_cache.py::test_zone_set_cached_with_identity -v`
Expected: FAIL — `KeyError: 'zone_set'` on the auth dict.

- [ ] **Step 3: Modify `op_context_to_auth_dict()` (lines 22-43)**

In `src/nexus/bricks/mcp/auth_bridge.py`, replace the body of `op_context_to_auth_dict()`:

```python
def op_context_to_auth_dict(ctx: OperationContext) -> dict[str, Any]:
    """Convert OperationContext to auth_result dict for ReBAC filtering."""
    return {
        "subject_id": ctx.subject_id,
        "subject_type": ctx.subject_type,
        "zone_id": ctx.zone_id or ROOT_ZONE_ID,
        "zone_set": list(ctx.zone_set) if ctx.zone_set else [ctx.zone_id or ROOT_ZONE_ID],
        "is_admin": ctx.is_admin,
        "user_id": ctx.user_id,
    }
```

(Preserve any other fields the existing dict carries — e.g. `agent_generation`, `inherit_permissions`. Read the existing dict literal first; only ADD `"zone_set"`.)

- [ ] **Step 4: Modify `resolve_mcp_operation_context()` (lines 132-259)**

Find the per-request API key branch where `zone_id` is extracted from auth_result (~line 203). Right after, also extract `zone_set`:

```python
zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
zone_set_raw = auth_result.get("zone_set")
zone_set = tuple(zone_set_raw) if zone_set_raw else (zone_id,)
```

Then construct OperationContext with `zone_set=zone_set`:

```python
return OperationContext(
    user_id=...,
    groups=[],
    zone_id=zone_id,
    zone_set=zone_set,
    is_admin=...,
    # ... existing fields ...
)
```

For the other resolution branches (kernel cred, default ctx, whoami) — leave them unchanged. They construct OperationContext with `zone_id=...` only; the `__post_init__` default (Task 3) populates `zone_set=(zone_id,)`.

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/bricks/mcp/test_auth_bridge_zone_set.py tests/unit/bricks/mcp/test_auth_bridge_cache.py -v`
Expected: PASS (3 new + existing cache tests).

- [ ] **Step 6: Commit**

```bash
git add src/nexus/bricks/mcp/auth_bridge.py tests/unit/bricks/mcp/test_auth_bridge_zone_set.py tests/unit/bricks/mcp/test_auth_bridge_cache.py
git commit -m "feat(#3785): auth_bridge propagates zone_set into OperationContext"
```

---

## Task 8: CLI — `nexus hub token create --zones` (CSV)

**Files:**
- Modify: `src/nexus/cli/commands/hub.py:36-128`
- Test: `tests/unit/cli/test_hub.py` (extend)

- [ ] **Step 1: Write the failing tests**

In `tests/unit/cli/test_hub.py`, add:

```python
def test_token_create_zones_csv(monkeypatch):
    """--zones eng,ops creates a token bound to both zones (#3785)."""
    captured = {}

    def fake_create_api_key(session, **kwargs):
        captured.update(kwargs)
        return ("kid_xyz", "sk-eng_alice_xx_yy")

    # Mock zone validation to accept both zones as Active.
    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,            # no existing token by name
        active_zone,     # zone "eng" Active
        active_zone,     # zone "ops" Active
    ]
    session.execute.return_value.scalars.return_value.all.return_value = []

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub,
        ["token", "create", "--name", "alice", "--zones", "eng,ops"],
    )
    assert result.exit_code == 0, result.output
    assert captured["zones"] == ["eng", "ops"]


def test_token_create_zone_alias_still_works(monkeypatch):
    """Backward-compat: --zone single still mints a token (#3785)."""
    captured = {}

    def fake_create_api_key(session, **kwargs):
        captured.update(kwargs)
        return ("kid_x", "sk-x")

    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None, active_zone,
    ]

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "create", "--name", "svc", "--zone", "eng"]
    )
    assert result.exit_code == 0, result.output
    assert captured["zones"] == ["eng"]


def test_token_create_rejects_empty_zones(monkeypatch):
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(MagicMock()),
    )
    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "create", "--name", "alice", "--zones", ""]
    )
    assert result.exit_code != 0
    assert "zone" in result.output.lower()


def test_token_create_rejects_inactive_zone_in_list(monkeypatch):
    """If any zone in --zones is not Active, the whole mint fails."""
    session = MagicMock()
    active_zone = MagicMock()
    active_zone.zone_id = "eng"
    # First lookup: token name doesn't exist. Then: eng Active, ops not found.
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,            # no existing
        active_zone,     # eng Active
        None,            # ops not found
    ]
    # `any_zone` lookup returns a zone (so bootstrap escape doesn't fire):
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None, active_zone, None,
    ]
    # Active zones list (for error message):
    session.execute.return_value.scalars.return_value.all.return_value = [active_zone]

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )
    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "create", "--name", "alice", "--zones", "eng,ops"]
    )
    assert result.exit_code != 0
    assert "ops" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/cli/test_hub.py -k zones -v`
Expected: FAIL — `--zones` option doesn't exist.

- [ ] **Step 3: Replace `--zone` with `--zones`**

In `src/nexus/cli/commands/hub.py`, replace the `token_create` decorator and signature (lines 36-53):

```python
@token.command("create")
@click.option("--name", required=True, help="Human-readable token name (unique).")
@click.option(
    "--zones",
    "zones_csv",
    default=None,
    help="Comma-separated zones the token can access (e.g. eng,ops).",
)
@click.option(
    "--zone",
    "zone_alias",
    default=None,
    hidden=True,
    help="Deprecated alias for --zones (single zone).",
)
@click.option("--admin", "is_admin", is_flag=True, help="Grant admin privileges.")
@click.option(
    "--expires",
    "expires",
    default=None,
    help="Expiry duration (e.g. 90d, 24h, 30m).",
)
@click.option("--user-id", default=None, help="Owner user_id. Defaults to --name.")
def token_create(
    name: str,
    zones_csv: str | None,
    zone_alias: str | None,
    is_admin: bool,
    expires: str | None,
    user_id: str | None,
) -> None:
    """Create a new bearer token. Prints the raw key once; not retrievable after."""
    if zones_csv is None and zone_alias is None:
        raise click.ClickException("Either --zones or --zone is required.")
    raw = zones_csv if zones_csv is not None else zone_alias
    zones = [z.strip() for z in raw.split(",") if z.strip()]
    if not zones:
        raise click.ClickException("--zones must contain at least one non-empty zone.")
    # ... rest of body, but loop validation per zone in `zones`, and pass `zones=zones`
    #     to create_api_key instead of `zone_id=zone_id`.
```

Then in the body, replace the existing single-zone validation (lines 81-114) with a loop:

```python
    factory = get_session_factory()
    expires_at: datetime | None = None
    if expires:
        try:
            expires_at = datetime.now(UTC) + parse_duration(expires)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

    with factory() as session, session.begin():
        # 1. duplicate-name check (unchanged)
        existing = (
            session.execute(
                select(APIKeyModel)
                .where(APIKeyModel.name == name)
                .where(APIKeyModel.revoked == 0)
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise click.ClickException(
                f"token named {name!r} already exists (key_id={existing.key_id}). "
                "Revoke it first or use a different --name."
            )

        # 2. Per-zone Active+non-deleted check, with bootstrap escape if zones empty.
        any_zone = session.execute(select(ZoneModel).limit(1)).scalars().first()
        if any_zone is not None:
            for z in zones:
                active = (
                    session.execute(
                        select(ZoneModel)
                        .where(ZoneModel.zone_id == z)
                        .where(ZoneModel.phase == "Active")
                        .where(ZoneModel.deleted_at.is_(None))
                    )
                    .scalars()
                    .first()
                )
                if active is None:
                    known = [
                        zm.zone_id
                        for zm in session.execute(
                            select(ZoneModel)
                            .where(ZoneModel.phase == "Active")
                            .where(ZoneModel.deleted_at.is_(None))
                        )
                        .scalars()
                        .all()
                    ]
                    raise click.ClickException(
                        f"zone {z!r} is not active (not found, deleted, or "
                        f"terminating). Active zones: "
                        f"{', '.join(sorted(known)) or '(none)'}. "
                        "Create it first with `nexus zone create` or use a different --zones value."
                    )

        # 3. Mint.
        key_id, raw_key = create_api_key(
            session,
            user_id=user_id or name,
            name=name,
            zones=zones,
            is_admin=is_admin,
            expires_at=expires_at,
        )

    click.echo(f"key_id: {key_id}")
    click.echo(f"token:  {raw_key}")
    click.echo("")
    click.echo("Save this token now — it will not be shown again.")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/cli/test_hub.py -v 2>&1 | tail -40`
Expected: PASS — both new tests + existing tests still green.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3785): nexus hub token create --zones (CSV); --zone hidden alias"
```

---

## Task 9: CLI — `nexus hub token list` shows zones column

**Files:**
- Modify: `src/nexus/cli/commands/hub.py:131-200` (around `token_list`)
- Test: `tests/unit/cli/test_hub.py` (extend)

- [ ] **Step 1: Write the failing test**

In `tests/unit/cli/test_hub.py`, add:

```python
def test_token_list_json_includes_zones(monkeypatch):
    """`token list --json` emits 'zones': ['eng','ops'] per row (#3785)."""
    from nexus.storage.models import APIKeyModel, APIKeyZoneModel
    from datetime import UTC, datetime

    row = APIKeyModel(
        key_id="kid_a",
        key_hash="h",
        user_id="alice",
        name="alice",
        zone_id="eng",
        is_admin=0,
        revoked=0,
        created_at=datetime.now(UTC),
    )

    session = MagicMock()
    session.execute.return_value.scalars.return_value.all.side_effect = [
        [row],                                                 # APIKeyModel rows
        [APIKeyZoneModel(key_id="kid_a", zone_id="eng"),
         APIKeyZoneModel(key_id="kid_a", zone_id="ops")],     # junction rows
    ]

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tokens"][0]["zones"] == ["eng", "ops"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/cli/test_hub.py::test_token_list_json_includes_zones -v`
Expected: FAIL — `zones` key absent.

- [ ] **Step 3: Modify `token_list`**

In `src/nexus/cli/commands/hub.py`, in the `token_list` body, before building the output payload, fetch all junction rows in one query and group by key_id:

```python
from nexus.storage.models import APIKeyZoneModel

# After loading `rows = ...all()`:
key_ids = [r.key_id for r in rows]
junction_rows = []
if key_ids:
    junction_rows = (
        session.execute(
            select(APIKeyZoneModel).where(APIKeyZoneModel.key_id.in_(key_ids))
        )
        .scalars()
        .all()
    )
zones_by_key: dict[str, list[str]] = {}
for jr in junction_rows:
    zones_by_key.setdefault(jr.key_id, []).append(jr.zone_id)
# Stable: primary first (== APIKeyModel.zone_id), then sorted others
for kid in zones_by_key:
    primary = next((r.zone_id for r in rows if r.key_id == kid), None)
    others = sorted(z for z in zones_by_key[kid] if z != primary)
    zones_by_key[kid] = ([primary] if primary else []) + others
```

In the JSON branch, add the `"zones"` field to each token dict:

```python
payload = {
    "tokens": [
        {
            "key_id": r.key_id,
            "name": r.name,
            "zone_id": r.zone_id,                      # deprecated, kept for one release
            "zones": zones_by_key.get(r.key_id, [r.zone_id]),
            "is_admin": bool(r.is_admin),
            "created_at": _iso(r.created_at),
            "expires_at": _iso(r.expires_at),
            "revoked": bool(r.revoked),
        }
        for r in rows
    ],
}
```

In the table branch, add a `"zones"` column to the header and rows (comma-joined).

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/cli/test_hub.py -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3785): hub token list shows zones column"
```

---

## Task 10: CLI — `nexus hub token zones {add,remove,show}` group

**Files:**
- Modify: `src/nexus/cli/commands/hub.py` (new subgroup at end of `token` group)
- Test: `tests/unit/cli/test_hub.py` (extend)

- [ ] **Step 1: Write the failing tests**

In `tests/unit/cli/test_hub.py`, add:

```python
def test_token_zones_add_invokes_helper(monkeypatch):
    from nexus.storage.models import APIKeyModel

    row = MagicMock()
    row.key_id = "kid_a"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = row
    # Mock zone validation: target zone is Active
    active_zone = MagicMock()
    active_zone.zone_id = "ops"

    captured = {}
    def fake_add(s, key_id, zone_id):
        captured.update(key_id=key_id, zone_id=zone_id)
        return True

    monkeypatch.setattr("nexus.cli.commands.hub.add_zone_to_key", fake_add)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "zones", "add", "--name", "alice", "--zone", "ops"]
    )
    assert result.exit_code == 0, result.output
    assert captured == {"key_id": "kid_a", "zone_id": "ops"}


def test_token_zones_remove_refuses_last_zone(monkeypatch):
    from nexus.cli.commands.hub import remove_zone_from_key

    row = MagicMock()
    row.key_id = "kid_a"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = row

    def fake_remove(s, key_id, zone_id):
        raise ValueError(f"refusing to remove last zone {zone_id!r} from key {key_id!r}")

    monkeypatch.setattr("nexus.cli.commands.hub.remove_zone_from_key", fake_remove)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "zones", "remove", "--name", "alice", "--zone", "eng"]
    )
    assert result.exit_code != 0
    assert "last zone" in result.output


def test_token_zones_show_lists_zones(monkeypatch):
    row = MagicMock()
    row.key_id = "kid_a"
    session = MagicMock()
    session.execute.return_value.scalars.return_value.first.return_value = row

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_zones_for_key",
        lambda s, kid: ["eng", "ops"],
    )
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(hub, ["token", "zones", "show", "--name", "alice"])
    assert result.exit_code == 0, result.output
    assert "eng" in result.output
    assert "ops" in result.output
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/cli/test_hub.py -k token_zones -v`
Expected: FAIL — `zones` subcommand absent.

- [ ] **Step 3: Add the subcommand group**

In `src/nexus/cli/commands/hub.py`, near the top, add imports:

```python
from nexus.storage.api_key_ops import (
    add_zone_to_key,
    create_api_key,
    get_zones_for_key,
    remove_zone_from_key,
)
```

After the existing `@token.command(...)` definitions (after `token_revoke` or end of token group), add:

```python
@token.group("zones")
def token_zones() -> None:
    """Manage a token's zone allow-list (#3785)."""


def _resolve_token_by_name(session, name: str) -> APIKeyModel:
    row = (
        session.execute(
            select(APIKeyModel)
            .where(APIKeyModel.name == name)
            .where(APIKeyModel.revoked == 0)
        )
        .scalars()
        .first()
    )
    if row is None:
        raise click.ClickException(f"no active token named {name!r}")
    return row


@token_zones.command("add")
@click.option("--name", required=True, help="Token name.")
@click.option("--zone", "zone_id", required=True, help="Zone to add.")
def token_zones_add(name: str, zone_id: str) -> None:
    """Add a zone to a token's allow-list. Idempotent."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        # Validate zone is Active.
        active = (
            session.execute(
                select(ZoneModel)
                .where(ZoneModel.zone_id == zone_id)
                .where(ZoneModel.phase == "Active")
                .where(ZoneModel.deleted_at.is_(None))
            )
            .scalars()
            .first()
        )
        if active is None:
            raise click.ClickException(
                f"zone {zone_id!r} is not active. Use `nexus zone create` first."
            )
        token_row = _resolve_token_by_name(session, name)
        added = add_zone_to_key(session, token_row.key_id, zone_id)
    click.echo(f"{'added' if added else 'no change'}: {name} → {zone_id}")


@token_zones.command("remove")
@click.option("--name", required=True, help="Token name.")
@click.option("--zone", "zone_id", required=True, help="Zone to remove.")
def token_zones_remove(name: str, zone_id: str) -> None:
    """Remove a zone from a token's allow-list. Refuses to leave token zoneless."""
    factory = get_session_factory()
    with factory() as session, session.begin():
        token_row = _resolve_token_by_name(session, name)
        try:
            removed = remove_zone_from_key(session, token_row.key_id, zone_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    click.echo(f"{'removed' if removed else 'no change'}: {name} → {zone_id}")


@token_zones.command("show")
@click.option("--name", required=True, help="Token name.")
def token_zones_show(name: str) -> None:
    """Print the token's zone allow-list (primary first)."""
    factory = get_session_factory()
    with factory() as session:
        token_row = _resolve_token_by_name(session, name)
        zones = get_zones_for_key(session, token_row.key_id)
    primary = token_row.zone_id
    ordered = ([primary] if primary in zones else []) + sorted(z for z in zones if z != primary)
    for z in ordered:
        click.echo(z)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/cli/test_hub.py -v 2>&1 | tail -30`
Expected: PASS — all 3 new tests + existing.

- [ ] **Step 5: Commit**

```bash
git add src/nexus/cli/commands/hub.py tests/unit/cli/test_hub.py
git commit -m "feat(#3785): nexus hub token zones {add,remove,show} group"
```

---

## Task 11: Search router — multi-zone fan-out

**Files:**
- Modify: `src/nexus/server/api/v2/routers/search.py:165-230` (`/v2/search/query` endpoint)
- Test: `tests/integration/server/test_search_zone_scoping.py`

The endpoint at `search.py:165` (`@router.get("/query")`) takes no explicit `?zone=` param — it has `federated: bool = Query(False)` and reads `zone_id` from `auth_result`. So the change is: when the caller's token grants multiple zones, auto-promote to federated even if `federated=False` was passed (the operator may not know they have a multi-zone token).

- [ ] **Step 1: Write the failing integration tests (AC #2 + #3)**

Create `tests/integration/server/test_search_zone_scoping.py`. Reuse the project's existing search-router test harness — find a peer test (e.g. `tests/integration/server/test_search*.py` or `tests/integration/api/v2/test_search*.py`) and copy its fixture wiring (TestClient + zone seeding). Concretely:

```python
"""Token zone allow-list end-to-end through the search router (#3785, AC #2/#3)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_single_zone_token_excludes_other_zones(
    nexus_test_client, seed_docs
):
    """AC #2: token for [eng] does not return legal-zone docs."""
    seed_docs(zone="eng", docs=[("eng_doc.txt", "alpha bravo")])
    seed_docs(zone="legal", docs=[("legal_doc.txt", "alpha charlie")])
    token = nexus_test_client.mint_token(name="agent_a", zones=["eng"])

    resp = await nexus_test_client.get(
        "/v2/search/query?q=alpha",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    paths = [r["path"] for r in resp.json()["results"]]
    assert any("eng_doc" in p for p in paths)
    assert not any("legal_doc" in p for p in paths)


async def test_multi_zone_token_returns_both_zones(
    nexus_test_client, seed_docs
):
    """AC #3: token for [eng, legal] returns docs from both — auto fan-out."""
    seed_docs(zone="eng", docs=[("eng_doc.txt", "alpha bravo")])
    seed_docs(zone="legal", docs=[("legal_doc.txt", "alpha charlie")])
    token = nexus_test_client.mint_token(name="agent_b", zones=["eng", "legal"])

    # NOTE: federated=false (the default) — server auto-promotes because zone_set has 2 zones.
    resp = await nexus_test_client.get(
        "/v2/search/query?q=alpha",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    paths = [r["path"] for r in resp.json()["results"]]
    assert any("eng_doc" in p for p in paths)
    assert any("legal_doc" in p for p in paths)


async def test_single_zone_token_unchanged_path(
    nexus_test_client, seed_docs
):
    """Regression: single-zone token hits the single-zone code path verbatim."""
    seed_docs(zone="eng", docs=[("eng_doc.txt", "alpha bravo")])
    token = nexus_test_client.mint_token(name="agent_c", zones=["eng"])

    resp = await nexus_test_client.get(
        "/v2/search/query?q=alpha",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) >= 1
```

If the fixtures `nexus_test_client` / `seed_docs` don't exist under those names, create a `conftest.py` next to this file that builds:
- a FastAPI TestClient bound to the search router with `require_auth` overridden to look up the bearer token via `DatabaseAPIKeyAuth`,
- a `seed_docs(zone, docs)` helper that writes through the same path the existing search integration test uses.

Cross-reference `tests/integration/server/` listing first to find the live fixture name.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/server/test_search_zone_scoping.py -v`
Expected: `test_multi_zone_token_returns_both_zones` FAILS — only `eng_doc` is returned (single-zone path uses primary `zone_id` only).

- [ ] **Step 3: Modify search router for multi-zone auto-fan-out**

In `src/nexus/server/api/v2/routers/search.py`, edit the `search_query` handler. Locate the existing zone extraction at line 191:

```python
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
```

Insert immediately below it:

```python
    zone_set_raw = auth_result.get("zone_set") or [zone_id]
    zone_set = tuple(zone_set_raw)
    # Auto-promote to federated when token grants multiple zones, even if the
    # caller didn't pass federated=true. Single-zone tokens (zone_set == (zone_id,))
    # fall through to the original single-zone path with no behavior change. (#3785)
    if len(zone_set) > 1:
        federated = True
```

Then, in the existing `if federated:` branch (line 215), modify the call to `_handle_federated_search` to pass the token's zone_set:

```python
    if federated:
        return await _handle_federated_search(
            q=q,
            search_type=type,
            limit=limit,
            # ... preserve existing args ...
            auth_result=auth_result,
            zones=list(zone_set),
        )
```

In `_handle_federated_search` (line 368), add a `zones: list[str] | None = None` keyword argument and use it as the zone list to iterate over. If `zones is None`, fall back to the existing behavior (single zone from auth_result) so any other caller is unaffected. The internal dispatch loop should iterate `zones`; for each zone, run the existing per-zone search + ReBAC filter, then merge.

If the existing federated implementation in `nexus.bricks.search.federated_search` already accepts a zone list, simply pass `zones=list(zone_set)` through. Otherwise extend the brick API to accept a zone list — the existing single-zone call site stays compatible by passing `[zone_id]`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/server/test_search_zone_scoping.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Regression run on existing search tests**

Run: `pytest tests/integration/server/ -k search -v 2>&1 | tail -30`
Expected: no new failures. Single-zone tokens (the entire P3-1 install base) preserve their existing code path because `len(zone_set) == 1` does not auto-promote.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/api/v2/routers/search.py tests/integration/server/test_search_zone_scoping.py
git commit -m "feat(#3785): search auto-fans-out across token zone_set"
```

---

## Task 12: File-op router — `assert_zone_allowed` at entrypoint

**Files:**
- Modify: `src/nexus/server/api/v2/routers/async_files.py`
- Test: `tests/integration/server/test_file_read_zone_scoping.py`

- [ ] **Step 1: Write the failing tests (AC #4)**

Create `tests/integration/server/test_file_read_zone_scoping.py`:

```python
"""Token zone allow-list on file reads (#3785, AC #4)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_explicit_zone_outside_set_rejected(client_with_auth, seed_files_in_zones):
    """File read in zone outside token's set → 403."""
    seed_files_in_zones({"eng": "/eng/file.txt", "legal": "/legal/file.txt"})
    token_eng = client_with_auth.create_token(zones=["eng"])

    resp = await client_with_auth.get(
        "/v2/files/read?path=/legal/file.txt",
        headers={"Authorization": f"Bearer {token_eng}"},
    )
    assert resp.status_code == 403


async def test_explicit_zone_inside_set_allowed(client_with_auth, seed_files_in_zones):
    """File read in zone inside token's set → 200."""
    seed_files_in_zones({"eng": "/eng/file.txt"})
    token_eng = client_with_auth.create_token(zones=["eng"])

    resp = await client_with_auth.get(
        "/v2/files/read?path=/eng/file.txt",
        headers={"Authorization": f"Bearer {token_eng}"},
    )
    assert resp.status_code == 200
```

(Same fixture caveat as Task 11. Use the project's existing file-read integration test scaffolding. If fixture names differ, adapt.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/integration/server/test_file_read_zone_scoping.py -v`
Expected: FAIL — out-of-set read returns 200 (no gate).

- [ ] **Step 3: Add the gate**

In `src/nexus/server/api/v2/routers/async_files.py`, find each file-op handler that derives a target zone from the file path or arg. At each entrypoint, after the auth dependency runs, add:

```python
from nexus.contracts.types import assert_zone_allowed, OperationContext

# Inside each file-op handler, after auth_result is loaded:
target_zone = _extract_zone_from_path(path)  # use existing helper or inline
op_ctx_for_check = OperationContext(
    user_id=auth_result.get("user_id", ""),
    groups=[],
    zone_id=auth_result.get("zone_id"),
    zone_set=tuple(auth_result.get("zone_set") or (auth_result.get("zone_id") or ROOT_ZONE_ID,)),
    is_admin=auth_result.get("is_admin", False),
)
try:
    assert_zone_allowed(op_ctx_for_check, target_zone)
except PermissionError as exc:
    raise HTTPException(status_code=403, detail=str(exc))
```

For handlers that don't take an explicit zone (rely on the request's primary zone), no gate is needed — the primary is always in the set by invariant.

Cover the read, write, delete, and list endpoints — search for `@router.` decorators in the file and trace each handler. If the file is large, group changes into a helper:

```python
def _gate_zone(auth_result: dict, target_zone: str) -> None:
    zone_set = tuple(auth_result.get("zone_set") or (auth_result.get("zone_id") or ROOT_ZONE_ID,))
    if auth_result.get("is_admin") or target_zone in zone_set:
        return
    raise HTTPException(status_code=403, detail=f"zone {target_zone!r} not in token's allow-list {zone_set}")
```

Call `_gate_zone(auth_result, extracted_zone)` at the top of each handler.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/server/test_file_read_zone_scoping.py -v`
Expected: PASS.

- [ ] **Step 5: Run regression**

Run: `pytest tests/integration/server/ -k file -v 2>&1 | tail -30`
Expected: no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/nexus/server/api/v2/routers/async_files.py tests/integration/server/test_file_read_zone_scoping.py
git commit -m "feat(#3785): file-op routers gate explicit zone via allow-list"
```

---

## Task 13: Token expiration in zone-scoping context (AC #5)

**Files:**
- Test: `tests/integration/server/test_token_expiry_zone_scoping.py`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/server/test_token_expiry_zone_scoping.py`:

```python
"""Expired bearer tokens are rejected before zone resolution runs (#3785, AC #5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.asyncio


async def test_expired_token_rejected(client_with_auth, seed_docs_in_zones):
    seed_docs_in_zones({"eng": ["doc.txt"]})
    token_expired = client_with_auth.create_token(
        zones=["eng"],
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    resp = await client_with_auth.get(
        "/v2/search?q=doc",
        headers={"Authorization": f"Bearer {token_expired}"},
    )
    assert resp.status_code in (401, 403)
```

- [ ] **Step 2: Run to verify status**

Run: `pytest tests/integration/server/test_token_expiry_zone_scoping.py -v`

If `DatabaseAPIKeyAuth.authenticate()` already rejects expired tokens (it does — P3-1 plumbing), this test should pass immediately. If it does not, the bug is in pre-existing code; flag it and route the fix through the auth provider, not the search router.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/server/test_token_expiry_zone_scoping.py
git commit -m "test(#3785): regression — expired token rejected even with zones"
```

---

## Task 14: End-to-end smoke — `nexus hub token` CLI flow

**Files:**
- Test: `tests/e2e/self_contained/cli/test_hub_flow.py` (extend)

- [ ] **Step 1: Inspect the existing e2e file**

Open `tests/e2e/self_contained/cli/test_hub_flow.py`. It already contains end-to-end CLI tests with a fixture that spins up an isolated DB + zones. Identify the fixture name (likely `hub_db` or `hub_env`) and the helper that invokes the CLI (likely a `CliRunner` instance configured with `NEXUS_DATABASE_URL`).

- [ ] **Step 2: Append the multi-zone smoke test**

At the end of `tests/e2e/self_contained/cli/test_hub_flow.py`, append (adapting `hub_env` / `runner` names to the live fixtures):

```python
def test_hub_multi_zone_token_lifecycle(hub_env):
    """e2e: create multi-zone token, list, mutate zones, refuse last-zone removal (#3785)."""
    import json as _json
    from click.testing import CliRunner
    from nexus.cli.commands.hub import hub

    # Pre-seed Active zones the test will use. Adapt to the file's pattern.
    hub_env.create_zone("eng")
    hub_env.create_zone("ops")
    hub_env.create_zone("legal")

    runner = CliRunner()

    # 1. Create with --zones CSV.
    r = runner.invoke(hub, ["token", "create", "--name", "e2e", "--zones", "eng,ops"])
    assert r.exit_code == 0, r.output
    assert "token:" in r.output

    # 2. list --json shows both zones.
    r = runner.invoke(hub, ["token", "list", "--json"])
    assert r.exit_code == 0, r.output
    payload = _json.loads(r.output)
    row = next(t for t in payload["tokens"] if t["name"] == "e2e")
    assert sorted(row["zones"]) == ["eng", "ops"]

    # 3. zones add legal.
    r = runner.invoke(hub, ["token", "zones", "add", "--name", "e2e", "--zone", "legal"])
    assert r.exit_code == 0, r.output
    assert "added" in r.output

    # 4. zones show contains all three.
    r = runner.invoke(hub, ["token", "zones", "show", "--name", "e2e"])
    assert r.exit_code == 0, r.output
    out_zones = set(r.output.split())
    assert {"eng", "ops", "legal"}.issubset(out_zones)

    # 5. zones remove ops.
    r = runner.invoke(hub, ["token", "zones", "remove", "--name", "e2e", "--zone", "ops"])
    assert r.exit_code == 0, r.output
    assert "removed" in r.output

    # 6. Removing remaining zones one by one fails when only one is left.
    r = runner.invoke(hub, ["token", "zones", "remove", "--name", "e2e", "--zone", "legal"])
    assert r.exit_code == 0, r.output  # eng + legal → eng remaining
    r = runner.invoke(hub, ["token", "zones", "remove", "--name", "e2e", "--zone", "eng"])
    assert r.exit_code != 0
    assert "last zone" in r.output.lower()
```

If the file's existing fixture exposes a different surface (e.g. doesn't have `create_zone`), adapt: insert into `ZoneModel` directly via the same session factory the fixture provides. Match the pattern of the surrounding tests rather than inventing new helpers.

- [ ] **Step 2: Run**

Run: `pytest tests/e2e/self_contained/cli/test_hub_flow.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/self_contained/cli/test_hub_flow.py
git commit -m "test(#3785): e2e smoke for nexus hub token multi-zone flow"
```

---

## Task 15: Final acceptance run + lint + type check

- [ ] **Step 1: Run the full test suite**

Run: `pytest tests/ -x -q 2>&1 | tail -50`
Expected: 0 failures.

- [ ] **Step 2: Lint and type-check**

Run:
```bash
ruff check src/ tests/
mypy src/nexus/storage/api_key_ops.py src/nexus/cli/commands/hub.py src/nexus/contracts/types.py src/nexus/bricks/mcp/auth_bridge.py src/nexus/bricks/auth/providers/database_key.py src/nexus/server/api/v2/routers/search.py src/nexus/server/api/v2/routers/async_files.py
```

Expected: clean. Fix any issues inline; recommit.

- [ ] **Step 3: Confirm acceptance criteria**

Walk through each AC from issue #3785:

1. ✅ Tokens created with specific zone access — `test_token_create_zones_csv` (Task 8)
2. ✅ Agent A `[eng]` cannot see `legal` — `test_token_zone_filter_excludes_unauthorized` (Task 11)
3. ✅ Agent B `[eng, legal]` sees both — `test_token_multi_zone_returns_both_zones` (Task 11)
4. ✅ File reads respect token zone scope — `test_explicit_zone_outside_set_rejected` (Task 12)
5. ✅ Token expiration works — `test_expired_token_rejected` (Task 13)

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin <branch-name>
gh pr create --title "feat(#3785): per-agent zone scoping (P3-2)" \
  --body "$(cat <<'EOF'
## Summary
- Multi-zone bearer tokens via `api_key_zones` junction table.
- Auth + OperationContext carry both `zone_id` (primary) and `zone_set` (allow-list).
- Search router fans out across `zone_set` when no zone is given; explicit-zone references gated by `assert_zone_allowed`.
- CLI: `nexus hub token create --zones eng,ops`; new `nexus hub token zones {add,remove,show}` group.
- Backward-compatible: single-zone tokens (P3-1) unaffected; `--zone` retained as hidden alias.

## Test plan
- [x] Unit: model, api_key_ops, OperationContext, assert_zone_allowed, auth_bridge, CLI
- [x] Integration: search zone scoping (AC #2, #3), file read zone scoping (AC #4), token expiry (AC #5)
- [x] Migration: api_key_zones backfill verified
- [x] e2e: hub multi-zone CLI flow

Closes #3785.
EOF
)"
```

---

## Out of scope (carried in spec §12)

- Per-zone-per-token differentiated permissions.
- Wildcard / glob zone tokens.
- Drop `APIKeyModel.zone_id` after one release.

These do not block this plan and are tracked in the design doc.
