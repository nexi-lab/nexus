# Issue #3785 — Per-agent zone scoping (token → zone mapping)

**Date:** 2026-04-24
**Issue:** [#3785](https://github.com/nexi-lab/nexus/issues/3785)
**Epic:** [#3777](https://github.com/nexi-lab/nexus/issues/3777) — Nexus as Context Layer for Secure Agent Runtimes
**Phase:** P3-2
**Depends on:** P3-1 (#3784, hub mode — merged)

## 1. Context

P3-1 shipped hub mode: a shared MCP server fronted by bearer-token auth, with each token bound to a single zone (`APIKeyModel.zone_id`, `nexus hub token create --zone eng`). Auth resolves bearer → zone on every MCP request and the rest of the pipeline (`OperationContext.zone_id` → ReBAC → federated search) already routes that zone end-to-end.

P3-2 evolves the model to **multi-zone tokens**: one bearer credential can grant access to a set of zones, e.g. `--zones eng,ops`. Search and file reads fan out across the set; explicit zone references must lie inside the set or the request fails closed.

This is not a green-field build. The auth bridge, ReBAC layer, federated search, and CLI surface already exist. The work is a single-zone → set-of-zones refactor along the existing seams, plus the alembic migration to back the new shape.

## 2. Goals

- Tokens carry a non-empty zone set; bearer auth resolves it on every request.
- Search/list operations with no explicit zone fan out across the token's zone set; results from zones outside the set are unreachable.
- File reads/writes that name a zone are gated by the token's zone set — fail-closed on out-of-set requests.
- CLI mints, lists, and mutates token zones without re-issuing the credential.
- Existing single-zone tokens (P3-1) keep working without operator action.

## 3. Non-goals

- Per-zone-per-token differentiated permissions (read-only in eng, read-write in ops). The junction table can grow a `permissions` column later.
- Remote / over-the-wire admin of tokens (tracked as #3784 follow-up).
- Cross-tenant or cross-org zone isolation (separate concern).
- Migrating off `APIKeyModel.zone_id` as the "primary zone" pointer — kept by design (see §6).

## 4. Acceptance criteria (from issue)

1. Tokens can be created with specific zone access.
2. Agent A with token for `[eng]` cannot see `legal` zone results.
3. Agent B with token for `[eng, legal]` sees both.
4. File reads respect token's zone scope.
5. Token expiration works.

Each AC maps to a named test in §10.

## 5. Multi-zone semantics (Q1 — locked)

Token = **ambient zone set**, reads implicitly fan out (Option B from brainstorming).

- A request with no explicit `zone` argument operates over the full token zone set: search merges, list aggregates, file enumeration spans the set.
- A request with an explicit `zone` argument requires that zone to be in the token's set (admins bypass via `is_admin`). Out-of-set requests fail closed with a 403-equivalent error.
- Mutating ops (write, delete, admin) that don't name a zone default to the token's **primary zone** (first zone given at mint time, persisted as `APIKeyModel.zone_id`). They do not fan out.

## 6. Schema (Q2 — junction table)

### 6.1 New model

```python
class APIKeyZoneModel(Base):
    __tablename__ = "api_key_zones"

    key_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("api_keys.key_id", ondelete="CASCADE"), primary_key=True
    )
    zone_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("zones.zone_id", ondelete="RESTRICT"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))

    __table_args__ = (
        Index("idx_api_key_zones_key", "key_id"),
        Index("idx_api_key_zones_zone", "zone_id"),
    )
```

`ondelete="RESTRICT"` on `zone_id`: a zone with live junction rows cannot be deleted. Operator must revoke or rotate tokens first. Symmetric with the lifecycle gates already shipped in P3-1.

### 6.2 `APIKeyModel.zone_id` (kept)

Stays as the **primary zone** — used by single-zone defaults (mutating ops without a zone arg, audit logs that need one zone for indexing, telemetry tags). It is **not** the source of truth for authorization. The junction table is.

Invariant: every `api_keys` row has at least one corresponding `api_key_zones` row, and `APIKeyModel.zone_id` ∈ junction set for that key. Enforced by CLI and by the migration backfill; no DB-level constraint added (would require a trigger).

### 6.3 Migration

New alembic revision (descend from latest). Two operations:

1. `op.create_table("api_key_zones", ...)` with FKs and indexes.
2. **Backfill**:
   ```sql
   INSERT INTO api_key_zones (key_id, zone_id, granted_at)
   SELECT key_id, zone_id, created_at FROM api_keys WHERE revoked = 0
   ```
   Every live token gets one junction row matching its current `zone_id`. Behavior unchanged for single-zone tokens.

3. **Downgrade**: `op.drop_table("api_key_zones")`. `APIKeyModel.zone_id` is untouched, so a downgraded deployment continues to work in single-zone mode.

## 7. Auth pipeline (Q3 — dual fields)

### 7.1 `ResolvedIdentity` (`src/nexus/bricks/mcp/auth_bridge.py`)

```python
@dataclass(frozen=True)
class ResolvedIdentity:
    subject_type: str
    subject_id: str
    zone_id: str                  # primary (existing)
    zone_set: tuple[str, ...]     # NEW — full allow-list
    is_admin: bool
```

`zone_set` is a tuple (immutable, hashable for cache keys). Always non-empty: at minimum it equals `(zone_id,)`.

### 7.2 `DatabaseAPIKeyAuth.authenticate()` (`src/nexus/bricks/auth/providers/database_key.py`)

After loading `APIKeyModel`, runs:

```sql
SELECT zone_id FROM api_key_zones WHERE key_id = :key_id
```

Result populates `ResolvedIdentity.zone_set`. If the result is empty (legacy token from a moment between deploy and backfill), fall back to `(model.zone_id,)`. The fallback is logged at WARN once per token (then suppressed via `lru_cache` on `key_id`) so operators see if it ever fires post-migration.

### 7.3 `AuthIdentityCache`

Existing 60s TTL cache caches the full `ResolvedIdentity` including `zone_set`. No per-MCP-call DB hit. Cache is keyed on the same hash used today; no schema change to the cache layer.

### 7.4 `auth_bridge.op_context_to_auth_dict()` (lines 22-43)

```python
def op_context_to_auth_dict(ctx: OperationContext) -> dict[str, Any]:
    return {
        "subject_id": ctx.subject_id,
        "zone_id": ctx.zone_id,
        "zone_set": list(ctx.zone_set),   # NEW
        "is_admin": ctx.is_admin,
    }
```

### 7.5 `auth_bridge.resolve_mcp_operation_context()` (lines 132-259)

In the per-request API key branch (~line 203), zone_set is extracted from the resolved identity and passed to the constructed `OperationContext`. Other resolution branches (kernel cred, default ctx, whoami) construct `zone_set=(zone_id,)`. They are inherently single-zone today; this preserves current behavior with zero call-site churn.

### 7.6 `OperationContext`

Adds `zone_set: tuple[str, ...]`. Default factory: `(zone_id,)`. Every existing constructor that passes only `zone_id=` keeps working unchanged.

### 7.7 Allow-list helper

```python
def assert_zone_allowed(ctx: OperationContext, requested: str) -> None:
    if ctx.is_admin or requested in ctx.zone_set:
        return
    raise PermissionError(
        f"zone {requested!r} not in token's allow-list {ctx.zone_set}"
    )
```

Lives next to `OperationContext`. Called by routers at request entry whenever an explicit `zone` param/header is present. Admin bypass mirrors today's `is_admin` shortcut in ReBAC.

### 7.8 Cache invalidation on junction mutation

When CLI revokes a token or mutates the junction (`hub token zones add/remove`), the existing `AuthIdentityCache` entry for that token is bumped. Worst-case staleness window: 60s (existing TTL). Already-open MCP connections see the new zone set on their next request after invalidation.

## 8. CLI surface

### 8.1 `nexus hub token create` (modified)

```
nexus hub token create --name alice --zones eng,ops
nexus hub token create --name svc   --zones eng              # single still works
nexus hub token create --name root  --zones eng --admin      # admin still scoped to a primary
```

- `--zones` (CSV) replaces `--zone`. Required even for `--admin` tokens (primary zone is used as default for single-zone ops; allow-list bypass for admins is a runtime check, not a mint-time one).
- Parsed as `[z.strip() for z in value.split(",") if z.strip()]`. Empty → `ClickException`.
- Each zone validated against `ZoneModel` (Active + non-deleted) using existing logic in `hub.py:81-114`, looped per zone. Bootstrap escape: when the `zones` table is fully empty, all requested zones are accepted (mirrors P3-1 single-zone behavior). Once any zone exists, every requested zone must match an Active row.
- Primary zone = first in the list (becomes `APIKeyModel.zone_id`); rest land in `api_key_zones`.
- **Backward compat:** `--zone <single>` is a hidden alias of `--zones <single>` for one release. Help text on `--zones` notes the deprecation; `--zone` help is hidden.

### 8.2 `nexus hub token list` (modified)

Adds a `zones` column showing the full set (comma-separated, sorted, primary first). JSON output emits `"zones": ["eng", "ops"]` per token. Existing `"zone_id"` JSON field stays for one release, marked deprecated in `--help`.

### 8.3 `nexus hub token zones` (new subcommand group)

```
nexus hub token zones add    --name alice --zone legal
nexus hub token zones remove --name alice --zone ops
nexus hub token zones show   --name alice
```

- `add`: validates the new zone is Active, then `INSERT` into junction. Idempotent — no error if already present (returns "no change").
- `remove`: deletes from junction. Refuses to remove the primary `zone_id` unless `--force`, which also rotates `APIKeyModel.zone_id` to the lexicographically-first remaining zone. Refuses to leave a token with zero zones.
- `show`: prints zones in primary-first order; empty (impossible by invariant) prints a hard error.
- All mutations bump the auth cache entry (§7.8).

### 8.4 `nexus hub token revoke`

Unchanged. Junction rows are tied to `api_keys.key_id` via `ondelete="CASCADE"`. Soft revocation (`revoked=1`) keeps junction rows for audit.

## 9. Wiring (search, ReBAC, file ops)

### 9.1 Search router (`src/nexus/server/api/v2/routers/search.py`)

Three branches in `search_files` (or equivalent endpoint):

1. **Explicit zone** → `assert_zone_allowed(ctx, requested)`, then run as today.
2. **No zone, single-element `zone_set`** → unchanged single-zone path. Common case post-backfill.
3. **No zone, multi-element `zone_set`** → call federated_search across the set, merge results.

The single-element fast path is preserved exactly so single-zone tokens (the entire P3-1 install base) hit the same code path with zero overhead.

### 9.2 Federated search (`src/nexus/bricks/search/federated_search.py`)

Already accepts a zone list and merges. Caller change only: pass `ctx.zone_set` instead of `[ctx.zone_id]`. ReBAC zone-level filtering already runs per-zone inside the existing loop.

### 9.3 ReBAC filter (`src/nexus/lib/rebac_filter.py`)

For multi-zone fan-out, `permission_enforcer.filter_search_results()` is called once per zone (mirrors today's per-zone semantics in federated_search). No API change to the enforcer.

### 9.4 File ops

Every file path is already zone-scoped (path embeds zone, or zone is required arg). Add `assert_zone_allowed(ctx, file_zone)` at the entrypoint of read/write/delete/list. ReBAC's per-file relationship check stays as the inner gate — defence in depth.

### 9.5 MCP tool surface

MCP tools that accept a `zone` param assert at the tool boundary. Tools that don't take a zone inherit `zone_set` automatically via `OperationContext`.

## 10. Tests

| AC | Test path | Asserts |
|---|---|---|
| 1 | `tests/unit/cli/test_hub.py::test_token_create_multi_zone` | `--zones eng,ops` writes one `api_keys` row + 2 junction rows; primary = first |
| 2 | `tests/integration/server/test_search_zone_scoping.py::test_token_zone_filter_excludes_unauthorized` | seed docs in eng + legal; bearer-auth as eng-only token; legal docs absent from results |
| 3 | `tests/integration/server/test_search_zone_scoping.py::test_token_multi_zone_returns_both_zones` | bearer for `[eng, legal]`; merged results contain both |
| 4 | `tests/integration/server/test_file_read_zone_scoping.py::test_explicit_zone_outside_set_rejected` | request file in `legal` with `[eng]` token → 403 |
|   | `tests/integration/server/test_file_read_zone_scoping.py::test_explicit_zone_inside_set_allowed` | same token → request in `eng` → 200 |
| 5 | `tests/integration/server/test_token_expiry.py::test_expired_token_rejected` | `expires_at = now - 1m` → 401 before zone resolution runs |

Regression tests:

- `tests/unit/bricks/auth/providers/test_database_key.py::test_legacy_token_without_junction_falls_back_to_zone_id` — backfill safety net (§7.2 fallback).
- `tests/unit/bricks/mcp/test_auth_bridge_cache.py::test_zone_set_cached_with_identity` — cache stores the tuple; no per-call DB hit.
- `tests/unit/cli/test_hub.py::test_token_zones_add_remove_idempotent` — junction-mutation CLI.
- `tests/unit/cli/test_hub.py::test_token_zones_remove_primary_requires_force` — invariant guard.
- `tests/migrations/test_api_key_zones_backfill.py` — pre-migration single-zone row → post-migration junction row, behavior unchanged.

## 11. Rollout

- Single PR, single migration revision. Schema is additive (new table only); rollback is a clean drop.
- Backfill is idempotent (`INSERT ... SELECT`), runs in the migration's `upgrade()`. For deployments with very large `api_keys` tables, the backfill is a single-statement set-based insert — bounded by row count of `api_keys`, not multiplicative.
- During the deploy window between code-rolled / migration-not-yet-run, the `DatabaseAPIKeyAuth` fallback (§7.2) keeps single-zone tokens working. Multi-zone token creation is gated on the migration having run (CLI rejects with a clear error if `api_key_zones` table is absent).

## 12. Open follow-ups (out of scope)

- Per-zone-per-token differentiated permissions: junction structure supports a `permissions` column; not added now per YAGNI.
- Wildcard / glob zone tokens (e.g. `--zones-glob "team-*"`) — would resolve at mint time against the zone registry. Today `--admin` is the only "all zones" mechanism, scoped to a primary zone for default-zone ops.
- Drop `APIKeyModel.zone_id` after one release if "primary zone" turns out to be vestigial.
