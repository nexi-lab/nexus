# Issue #3871 — Multi-Zone Tokens Phase 2 Cleanup

**Status:** Draft
**Issue:** [#3871](https://github.com/nexi-lab/nexus/issues/3871)
**Predecessor:** [PR #3886 (closes #3785)](https://github.com/nexi-lab/nexus/pull/3886)
**Author:** windoliver
**Date:** 2026-04-25

## 1. Background

Issue #3871 was filed as a follow-up to #3784 (hub mode) requesting multi-zone token support. While #3871 was open, the same scope was implemented under #3785 and is now in flight as PR #3886. PR #3886 ships:

- Junction table `api_key_zones (key_id, zone_id, granted_at, permissions)` as the source of truth for token → zone mapping.
- `OperationContext.zone_set` + `zone_perms` propagation through the auth pipeline.
- Search auto fan-out, file-op `?zone=` override, CLI `--zones eng:rw,ops:r` + `--zones-glob`, `token zones add/remove/show`.
- Backfill migration; `APIKeyModel.zone_id` made nullable; legacy single-zone tokens still work.

PR #3886's "out of scope" section explicitly defers four `WHERE APIKeyModel.zone_id = ?` filter sites and the eventual column drop. The F4a audit (`docs/superpowers/plans/2026-04-25-issue-3785-followups.md`) categorizes every remaining `zone_id` caller. **#3871 is repurposed as the Phase 2 cleanup that addresses the deferred items**, short of dropping the column.

**Dependency:** This PR cannot merge until PR #3886 lands on `develop`. The junction table, `OperationContext.zone_set`/`zone_perms` plumbing, and the `add_zone_to_key`/`remove_zone_from_key`/`get_zones_for_key`/`get_zone_perms_for_key` helpers all originate in #3886 and are prerequisites here.

## 2. Goals & non-goals

### In scope (Phase 2)

- Migrate the four `WHERE APIKeyModel.zone_id = ?` filter sites to junction queries.
- Introduce a `get_primary_zone(key_id) → str | None` helper using `MIN(granted_at)` ordering with `zone_id ASC` tiebreaker.
- Migrate the audit's "kept" primary-zone callers to the helper (CLI ordering, admin telemetry, deprecated REST `zone` alias).
- Stop writing `APIKeyModel.zone_id` on key creation. Column stays nullable; new keys persist `NULL`.
- Remove the `database_key.py:148-160` legacy fallback that derives `zone_perms` from `APIKeyModel.zone_id` when the junction is empty.
- Add an alembic data-assertion migration that fails loudly if any non-revoked, non-admin key lacks a junction row.
- Update the deprecated singular `zone` JSON alias (CLI `token list --json`, admin RPC echo, REST create response) to emit `get_primary_zone(key_id)` instead of `NULL`.

### Out of scope

- Dropping the `APIKeyModel.zone_id` column (deferred to a future Phase 3).
- `OperationLogModel.zone_id` — already correct under multi-zone (the ReBAC enforcer emits one log row per zone touched). No change needed.
- Postgres RLS policies — none exist in the repo today; zone isolation is enforced exclusively at the application layer.
- Removing the deprecated `zone` JSON field outright (separate breaking-change PR; this spec only changes its source value).
- Cross-deployment / wildcard / cross-tenant zones (out of scope of #3785 too).

## 3. Design

### 3.1 Helper API

A new function in `src/nexus/storage/api_key_ops.py`:

```python
def get_primary_zone(session: Session, key_id: str) -> str | None:
    """Return the token's primary zone, or None for zoneless admin keys.

    Primary = the row with minimum granted_at. Ties broken by zone_id ASC
    so the result is deterministic across snapshots and replays.
    """
    stmt = (
        select(APIKeyZoneModel.zone_id)
        .where(APIKeyZoneModel.key_id == key_id)
        .order_by(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def get_primary_zones_for_keys(
    session: Session, key_ids: list[str]
) -> dict[str, str]:
    """Batch variant for renderers that walk many rows (e.g., `token list`).

    Single round-trip. Returns {key_id: primary_zone}; missing keys (zoneless
    admin keys with empty junction) are absent from the dict.
    """
    if not key_ids:
        return {}
    rn = func.row_number().over(
        partition_by=APIKeyZoneModel.key_id,
        order_by=(APIKeyZoneModel.granted_at.asc(), APIKeyZoneModel.zone_id.asc()),
    ).label("rn")
    inner = (
        select(APIKeyZoneModel.key_id, APIKeyZoneModel.zone_id, rn)
        .where(APIKeyZoneModel.key_id.in_(key_ids))
        .subquery()
    )
    stmt = select(inner.c.key_id, inner.c.zone_id).where(inner.c.rn == 1)
    return {row.key_id: row.zone_id for row in session.execute(stmt)}
```

Tiebreaker rationale: `granted_at` may collide at microsecond resolution (especially when a token is created with multiple zones in one INSERT). `zone_id ASC` gives a stable secondary order without requiring a schema change.

### 3.2 Filter migration (4 sites)

Pattern applied uniformly:

```python
# Before
stmt = stmt.where(APIKeyModel.zone_id == zone_id)

# After
stmt = (
    stmt.join(APIKeyZoneModel, APIKeyZoneModel.key_id == APIKeyModel.key_id)
        .where(APIKeyZoneModel.zone_id == zone_id)
)
```

Sites:

| # | File:line | Function |
|---|---|---|
| 1 | `bricks/auth/providers/database_key.py:295` | `list_keys(zone_id=…)` |
| 2 | `server/api/v2/routers/auth_keys.py:380` | REST `GET /v2/auth/keys?zone_id=…` |
| 3a | `server/rpc/handlers/admin.py:309` | `admin.list_keys` filter |
| 3b | `server/rpc/handlers/admin.py:348` | `admin.list_keys_by_zone` filter |
| 3c | `server/rpc/handlers/admin.py:389` | `admin.revoke_key` zone filter |
| 4 | `storage/auth_stores/sqlalchemy_api_key_store.py:96` | `revoke_key(zone_id=…)` |

(Three statements at site 3 share the same fix.)

#### 3.2.1 Visible behavior change

Today, `WHERE APIKeyModel.zone_id = 'eng'` matches only keys whose primary zone is `eng`. After migration, the same filter matches **every key that grants `eng` in any position** (via the junction). For multi-zone keys this is the correct semantic. Two consequences for admin UIs:

- "List keys in zone eng" returns more rows than before — every key with `eng` access, not just keys "primarily owned" by `eng`.
- A key listed under both `eng` and `ops` filters is the same key. Renderers that aggregate counts must dedupe by `key_id` if they care about distinct-key totals.

Documented in PR description and changelog. No code rollback needed; this is the intended end state.

### 3.3 Stop writing `APIKeyModel.zone_id`

`src/nexus/storage/api_key_ops.py::create_api_key`:

```python
# Before (#3886 F4b — backfill alias)
api_key = APIKeyModel(..., zone_id=primary_zone)

# After
api_key = APIKeyModel(..., zone_id=None)
```

`add_zone_to_key` and `remove_zone_from_key` already only touch the junction; no change.

`APIKeyModel.zone_id` docstring updated:

> DEPRECATED — column scheduled for removal in Phase 3 of #3871. Always `NULL` on keys minted on or after Phase 2 (#3871). Source of truth is `api_key_zones`. Use `get_primary_zone(key_id)` for "primary zone" semantics or `get_zones_for_key(key_id)` for the full set.

### 3.4 Remove legacy fallback

`src/nexus/bricks/auth/providers/database_key.py:148-160`:

```python
# Before
if not zone_perms_rows:
    if api_key.zone_id:
        zone_perms = ((api_key.zone_id, "rw"),)  # legacy fallback
    else:
        zone_perms = ()

# After
zone_perms = tuple(zone_perms_rows)
```

If the junction is empty post-Phase 2, `zone_perms = ()`. Downstream `_gate_zone` correctly treats this as "no zone access" and rejects MCP requests. Admin keys (`is_admin=1`) bypass `_gate_zone` and remain unaffected.

### 3.5 Tripwire migration

New alembic revision `<rev>_assert_api_key_junction_populated_for_3871.py`:

```python
def upgrade():
    bind = op.get_bind()
    rows = bind.execute(text("""
        SELECT k.key_id
        FROM api_keys k
        LEFT JOIN api_key_zones z ON z.key_id = k.key_id
        WHERE k.revoked = 0
          AND k.is_admin = 0
          AND z.key_id IS NULL
    """)).fetchall()
    if rows:
        raise RuntimeError(
            f"Phase 2 cleanup blocked: {len(rows)} non-admin live keys lack junction "
            f"rows. Re-run the #3785 backfill before upgrading. "
            f"Sample key_ids: {[r[0] for r in rows[:5]]}"
        )

def downgrade():
    pass  # assertion-only
```

The migration is purely diagnostic. On healthy databases it completes in milliseconds and writes nothing. On broken data it surfaces drift before §3.4 starts denying real requests.

`down_revision` chains to the F4b migration that made `api_keys.zone_id` nullable, so the assertion only runs once the multi-zone schema is fully in place.

### 3.6 Deprecated `zone` alias → primary

Each of the three callers that today emit `zone_id` as the deprecated `zone` field is updated to call the new helper:

| File:line | Before | After |
|---|---|---|
| `cli/commands/hub.py:288,308` | `row["zone_id"]` in `token_list` JSON + table | `primary_map[row["key_id"]]` from `get_primary_zones_for_keys` (single batch query) |
| `server/rpc/handlers/admin.py:173` | `api_key.zone_id` in admin echo | `get_primary_zone(session, api_key.key_id)` |
| `server/api/v2/routers/auth_keys.py:265` | `result.get("zone_id", body.zone_id)` | `get_primary_zone(session, result["key_id"])` |

For single-zone tokens (the dominant case) the emitted value is identical to today's behavior. For multi-zone tokens, clients reading the deprecated `zone` field see the first granted zone — an honest, deterministic value rather than `NULL`.

## 4. Testing

### 4.1 Unit

- `tests/unit/storage/test_api_key_ops_primary_zone.py` — `get_primary_zone`: zoneless returns `None`; single-zone; multi-zone returns MIN granted_at; tiebreaker on `zone_id` ASC; `get_primary_zones_for_keys` batch correctness + single-query (assert via `query_count` fixture).
- `tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py` — `revoke_key(zone_id=…)` matches multi-zone keys via junction.
- `tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py` — REST list filter via junction; multi-zone key visible in both zones' filtered lists.
- `tests/unit/server/rpc/handlers/test_admin_junction_filter.py` — admin list/revoke 3 sites via junction.
- `tests/unit/bricks/auth/providers/test_database_key_no_fallback.py` — junction empty + non-admin → `zone_perms=()`; admin keys still resolve.
- `tests/unit/storage/migrations/test_assert_api_key_junction_populated.py` — upgrade raises on synthetic broken row; passes on healthy fixture; downgrade is a no-op.

### 4.2 Migration

- `alembic upgrade head` → `downgrade -1` → `upgrade head` clean against sqlite.
- Healthy-DB run completes without raising.
- Broken-DB fixture (`api_keys` row with no junction row, `revoked=0`, `is_admin=0`) raises `RuntimeError` with sample `key_ids` in the message.

### 4.3 End-to-end

Per `feedback_e2e_for_auth_pipeline` (memory): unit tests construct dicts directly and miss serializer gaps. This change removes a field from the read path and changes one helper's source — lower risk than adding a field, but the same pattern applies. New e2e file `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py`:

1. `nexus hub token create --zones eng:rw,ops:r --name alice` → assert `api_keys.zone_id IS NULL` in DB and junction has 2 rows.
2. `nexus hub token list --json` → `zone` field equals `eng` (primary by granted_at).
3. MCP request with the token → both zones accessible via `_gate_zone` (no fallback regression).
4. Admin list filtered by `zone=ops` → key appears.

Run `nexus up --build` once and exercise end-to-end. The file follows the existing `test_hub_flow.py` pattern: skips cleanly when no live stack is present so CI can include it without infrastructure.

### 4.4 Regression sweep

Rerun the same scope #3886 used: `storage + cli + auth + bricks/auth + bricks/mcp + server/api/v2/routers + contracts` (1085 tests). Acceptable bar: same 3 pre-existing environmental flakes documented in #3886, no new failures.

## 5. Files touched

### New

- `alembic/versions/<rev>_assert_api_key_junction_populated_for_3871.py`
- `tests/unit/storage/test_api_key_ops_primary_zone.py`
- `tests/unit/storage/auth_stores/test_sqlalchemy_api_key_store_junction_filter.py`
- `tests/unit/server/api/v2/routers/test_auth_keys_junction_filter.py`
- `tests/unit/server/rpc/handlers/test_admin_junction_filter.py`
- `tests/unit/bricks/auth/providers/test_database_key_no_fallback.py`
- `tests/unit/storage/migrations/test_assert_api_key_junction_populated.py`
- `tests/e2e/self_contained/cli/test_hub_phase2_cleanup.py`

### Modified

- `src/nexus/storage/api_key_ops.py` — add `get_primary_zone`, `get_primary_zones_for_keys`; `create_api_key` stops writing `zone_id`.
- `src/nexus/storage/models/auth.py` — `APIKeyModel.zone_id` docstring updated.
- `src/nexus/storage/auth_stores/sqlalchemy_api_key_store.py` — junction join in `revoke_key` filter.
- `src/nexus/bricks/auth/providers/database_key.py` — drop legacy fallback (lines 148-160); junction join in `list_keys` filter.
- `src/nexus/server/api/v2/routers/auth_keys.py` — junction join in list filter; primary alias in create response.
- `src/nexus/server/rpc/handlers/admin.py` — junction join (3 statements); primary alias in echo.
- `src/nexus/cli/commands/hub.py` — primary alias in `token_list` JSON + table column (single batch query).

**Estimated change size:** ~250 LoC (~80 production, ~170 test) + 1 alembic migration.

## 6. Acceptance criteria

- [ ] All four filter sites use junction joins; admin "list keys in zone X" returns every key with X access.
- [ ] `get_primary_zone` and batch helper land in `api_key_ops.py` with deterministic ordering.
- [ ] `create_api_key` writes `zone_id=NULL`; existing tests asserting otherwise are updated.
- [ ] Legacy `zone_perms` fallback removed; non-admin keys with empty junction get `zone_perms=()`.
- [ ] Tripwire migration raises on broken fixture, no-ops on healthy DB.
- [ ] Deprecated `zone` JSON field equals `get_primary_zone(key_id)` everywhere it is emitted.
- [ ] Single-zone token behavior is byte-identical to pre-PR for all three deprecated-alias call sites.
- [ ] Multi-zone token visible under both zones' admin filters.
- [ ] E2E test passes against `nexus up --build`.
- [ ] Regression sweep: 1085 tests, no new failures beyond the 3 pre-existing environmental flakes.

## 7. Phase 3 (deferred, not part of this PR)

Once Phase 2 has soaked one release cycle:

- Drop the `APIKeyModel.zone_id` column via alembic.
- Remove the deprecated `zone` JSON alias from CLI / admin / REST responses (breaking change for clients still on it).
- Remove the tripwire migration (its job is done; junction is structurally enforced).
