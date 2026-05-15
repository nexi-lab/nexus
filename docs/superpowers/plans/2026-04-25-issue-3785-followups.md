# Issue #3785 follow-ups — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Land four follow-ups deferred from the original #3785 PR onto the same branch (`worktree-parallel-launching-hoare`, PR #3886): glob-zone tokens, explicit `?zone=` on file ops, per-zone permissions, drop `APIKeyModel.zone_id`.

**Branch:** `worktree-parallel-launching-hoare` (PR #3886 already open against `develop`).

**Conventions** (carried from prior plan):
- Test runner: `uv run pytest …`
- `Base` import: `from nexus.storage.models._base import Base`
- `ZoneModel` requires `name=` in fixtures
- `pytest.raises(IntegrityError)` not bare `Exception`

---

## Task F1: `--zones-glob` CLI flag

**Files:**
- Modify: `src/nexus/cli/commands/hub.py token_create`
- Test: `tests/unit/cli/test_hub.py` (extend)

**Step 1: Failing test**

```python
def test_token_create_zones_glob_expands_to_active_zones(monkeypatch):
    """--zones-glob 'team-*' expands to all active zones matching pattern (#3785 follow-up)."""
    captured = {}

    def fake_create_api_key(session, **kwargs):
        captured.update(kwargs)
        return ("kid", "sk-x")

    team_eng = MagicMock()
    team_eng.zone_id = "team-eng"
    team_ops = MagicMock()
    team_ops.zone_id = "team-ops"
    other = MagicMock()
    other.zone_id = "ops"  # not matching

    session = MagicMock()
    # Sequence: no existing token, any_zone exists, all-active-zones list (for glob match)
    session.execute.return_value.scalars.return_value.first.side_effect = [
        None,   # no existing token by name
        team_eng,  # any_zone exists
    ]
    session.execute.return_value.scalars.return_value.all.return_value = [
        team_eng, team_ops, other,
    ]

    monkeypatch.setattr("nexus.cli.commands.hub.create_api_key", fake_create_api_key)
    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "create", "--name", "alice", "--zones-glob", "team-*"]
    )
    assert result.exit_code == 0, result.output
    assert sorted(captured["zones"]) == ["team-eng", "team-ops"]


def test_token_create_zones_glob_no_match_rejects(monkeypatch):
    """--zones-glob with no matches → ClickException."""
    session = MagicMock()
    any_zone = MagicMock()
    any_zone.zone_id = "root"
    session.execute.return_value.scalars.return_value.first.side_effect = [None, any_zone]
    session.execute.return_value.scalars.return_value.all.return_value = [any_zone]

    monkeypatch.setattr(
        "nexus.cli.commands.hub.get_session_factory",
        lambda: _mock_session_ctx(session),
    )

    runner = CliRunner()
    result = runner.invoke(
        hub, ["token", "create", "--name", "alice", "--zones-glob", "team-*"]
    )
    assert result.exit_code != 0
    assert "no active zones match" in result.output.lower()
```

**Step 2: Implementation**

Add `--zones-glob` Click option to `token_create`:

```python
@click.option(
    "--zones-glob",
    "zones_glob",
    default=None,
    help="Glob pattern resolved against active zones (e.g. 'team-*'). "
         "Mutually exclusive with --zones / --zone.",
)
```

Update body:
- If `zones_glob` is set, `zones_csv` and `zone_alias` must be None (else ClickException).
- Load all active+non-deleted zones via the existing `select(ZoneModel)... .all()` query.
- `import fnmatch; matched = sorted(z.zone_id for z in active if fnmatch.fnmatch(z.zone_id, zones_glob))`.
- If `not matched`: `raise ClickException(f"--zones-glob {zones_glob!r} matched no active zones; available: {known}")`.
- Otherwise set `zones = matched`.
- Skip the per-zone Active loop (already filtered).

**Commit:** `feat(#3785): nexus hub token create --zones-glob`

---

## Task F2: file-op `?zone=` (`read_file`)

**Files:**
- Modify: `src/nexus/server/api/v2/routers/async_files.py read_file` handler
- Test: `tests/unit/server/api/v2/routers/test_async_files_zone_param.py`

**Step 1: Failing tests** (use fastapi TestClient + mock fs).

```python
def test_read_file_zone_param_in_set_overrides_context(monkeypatch):
    """?zone=ops uses ops as zone_id when ops is in token's zone_set."""
    # Mock fs.read returns a sentinel; assert _gate_zone passes and OperationContext.zone_id="ops".
    ...

def test_read_file_zone_param_outside_set_returns_403():
    """?zone=legal with token zone_set=[eng] → 403 from _gate_zone."""
    ...

def test_read_file_no_zone_param_uses_context_default():
    """No ?zone= → unchanged single-zone behavior (zone_id from context)."""
    ...
```

**Step 2: Implementation**

In `read_file` handler signature, add:

```python
zone: str | None = Query(None, description="Override zone (must be in token's zone_set)."),
```

In handler body, before the existing logic uses `context`:

```python
if zone is not None:
    # Validate against the per-request auth_result if available.
    auth_result = getattr(request.state, "auth_result", None)
    if auth_result is not None:
        _gate_zone(auth_result, zone)
    # Override context.zone_id for this request.
    context = dataclasses.replace(context, zone_id=zone) if hasattr(context, '__dataclass_fields__') else context
```

(Or — if `context` is constructed via `get_context` from auth_result already, just call `_gate_zone(auth_result, zone)` and pass the new zone through.)

**Commit:** `feat(#3785): file-op ?zone= override (read_file)`

---

## Task F2b: Apply `?zone=` to other file-op handlers

**Files:**
- Modify: `src/nexus/server/api/v2/routers/async_files.py` — write_file, delete_file, list_directory, file_exists, get_file_metadata, create_directory, batch_read_files.

**Step 1:** Extract a helper:

```python
def _apply_zone_override(
    context: OperationContext,
    zone: str | None,
    auth_result: dict[str, Any] | None,
) -> OperationContext:
    """If `zone` is set, gate it against the auth allow-list and rebuild context."""
    if zone is None:
        return context
    if auth_result is not None:
        _gate_zone(auth_result, zone)
    return dataclasses.replace(context, zone_id=zone)
```

**Step 2:** Add `zone: str | None = Query(None)` to each handler signature; call `context = _apply_zone_override(context, zone, getattr(request.state, "auth_result", None))` at the top of each handler body.

**Step 3:** Tests for at least write + delete + list paths. Reuse the conftest from Task 12.

**Commit:** `feat(#3785): file-op ?zone= override (write/delete/list/exists/metadata/mkdir)`

---

## Task F3a: alembic — `api_key_zones.permissions` column

**Files:**
- Create: new alembic revision
- Modify: `src/nexus/storage/models/auth.py APIKeyZoneModel`
- Test: `tests/migrations/test_api_key_zones_permissions.py`

**Step 1:** Generate `uv run alembic revision -m "add permissions to api_key_zones for #3785"`.

**Step 2:** Migration:

```python
def upgrade() -> None:
    op.add_column(
        "api_key_zones",
        sa.Column("permissions", sa.String(length=8), nullable=False, server_default="rw"),
    )

def downgrade() -> None:
    op.drop_column("api_key_zones", "permissions")
```

**Step 3:** ORM:

```python
permissions: Mapped[str] = mapped_column(String(8), nullable=False, default="rw")
```

`permissions` is a small string: `"r"`, `"w"`, `"rw"`, `"rwx"` (admin). Validation lives in CLI / api_key_ops, not the DB.

**Step 4:** Backfill verification test.

**Commit:** `feat(#3785): api_key_zones.permissions column`

---

## Task F3b: api_key_ops + CLI per-zone permissions

**Files:**
- Modify: `src/nexus/storage/api_key_ops.py create_api_key`, `add_zone_to_key`, `get_zones_for_key`, `remove_zone_from_key`.
- Modify: `src/nexus/cli/commands/hub.py token_create`, `token_zones_add`.
- Test: `tests/unit/storage/test_api_key_ops_zone_perms.py`, `tests/unit/cli/test_hub.py` extensions.

**Step 1:** Storage helpers accept `(zone_id, perms)` tuples OR a separate `zone_perms: dict[str, str]` map.

```python
def create_api_key(session, *, ..., zones: list[str | tuple[str, str]] | None = None, ...):
    # parse: ["eng", ("ops", "r")] → primary="eng" (rw default), then ops with "r"
```

`add_zone_to_key(session, key_id, zone_id, permissions="rw")`.

`get_zones_for_key` returns `list[tuple[str, str]]` (zone_id, perms). Change return type — check call sites.

**Step 2:** CLI:

`--zones eng:rw,ops:r` parser. `eng` (no colon) defaults to `rw`. `eng:rwx` for admin.

`nexus hub token zones add --name X --zone Z --perms rw|r|rwx`.

**Step 3:** Tests covering parser + storage round-trip.

**Commit:** `feat(#3785): per-zone token permissions (eng:rw,ops:r syntax)`

---

## Task F3c: zone_set carries permissions; assert_zone_allowed gates write

**Files:**
- Modify: `src/nexus/contracts/types.py OperationContext`, `assert_zone_allowed`.
- Modify: `src/nexus/bricks/auth/types.py AuthResult`.
- Modify: `src/nexus/bricks/mcp/auth_cache.py ResolvedIdentity`.
- Modify: `src/nexus/bricks/auth/providers/database_key.py DatabaseAPIKeyAuth.authenticate` (load perms with zones).
- Modify: `src/nexus/bricks/mcp/auth_bridge.py op_context_to_auth_dict`, `resolve_mcp_operation_context`.
- Modify: `src/nexus/server/api/v2/routers/async_files.py _gate_zone` to accept `required_perm`.

**Step 1:** Replace `zone_set: tuple[str, ...]` with `zone_perms: tuple[tuple[str, str], ...]` (zone_id, perms). Keep `zone_set` as a derived `@property` for backward-compat with code that just needs the zone names.

**Step 2:** `assert_zone_allowed(ctx, requested, *, required_perm: str = "r")`:

```python
def assert_zone_allowed(ctx, requested, *, required_perm: str = "r"):
    if ctx.is_admin:
        return
    for zone, perms in ctx.zone_perms:
        if zone == requested:
            if required_perm in perms or "x" in perms:
                return
            raise PermissionError(f"zone {requested!r} requires {required_perm!r}, has {perms!r}")
    raise PermissionError(f"zone {requested!r} not in token's allow-list")
```

Update `_gate_zone` similarly.

**Step 3:** File-op handlers:
- `read_file`, `list_directory`, `file_exists`, `get_file_metadata`, `batch_read_files` → `required_perm="r"`.
- `write_file`, `create_directory` → `required_perm="w"`.
- `delete_file` → `required_perm="w"`.

**Commit:** `feat(#3785): per-zone permissions enforced at router boundary`

---

## Task F4a: audit `APIKeyModel.zone_id` callers

Pure investigation — no code change. Output: a comment block in this plan documenting every read of `APIKeyModel.zone_id` in `src/`, with disposition:
- "junction-only" — replace with `get_zones_for_key()[0][0]` or similar
- "kept" — call site is informational (audit logs, telemetry); column stays nullable

Run:

```bash
rg -n "APIKeyModel\.zone_id|api_key\.zone_id|api_key_row\.zone_id|\.zone_id" --type py src/nexus | grep -v test
```

Categorize each hit. Append findings to this plan as a new section.

### F4a findings (audit run 2026-04-25)

| File:line | Use | Disposition |
| --- | --- | --- |
| `bricks/auth/providers/database_key.py:115-121` | Look up zone phase (Active/Terminating) for the token's primary zone — UI/error context | **kept** — informational |
| `bricks/auth/providers/database_key.py:148-160` | Legacy fallback: derive `zone_perms` from `api_key.zone_id` when junction empty | **kept-as-fallback** — required for legacy keys minted before junction landed |
| `bricks/auth/providers/database_key.py:295` | Filter `list_keys()` by zone | **junction-only candidate** — replace with `INNER JOIN api_key_zones`. Deferred (not in F4b — would silently drop NULL-zone-id rows from list views) |
| `server/api/v2/routers/auth_keys.py:380` | Same `list_keys` zone filter | **junction-only candidate** — same deferral |
| `server/rpc/handlers/admin.py:173` | Echo `zone_id` in admin-list-keys RPC response | **kept** — deprecated alias (matches CLI's deprecated `zone` JSON field) |
| `server/rpc/handlers/admin.py:309,348,389` | Filter admin queries by `zone_id` | **junction-only candidate** — same deferral |
| `server/rpc/handlers/admin.py:403-404` | Scope a count query to the requesting key's zone | **kept-as-fallback** — admin telemetry tied to caller's primary zone |
| `storage/auth_stores/sqlalchemy_api_key_store.py:96` | `list_keys` zone filter (storage layer) | **junction-only candidate** — same deferral |
| `cli/commands/hub.py:271,436` | Compute primary zone for ordering in `token_zones_show` | **kept** — needs explicit "primary" concept until junction has a flag |
| `cli/commands/hub.py:277` | Fallback zones list when junction has zero rows | **kept-as-fallback** |
| `cli/commands/hub.py:288,308` | JSON `zone` field & table column (already labelled deprecated) | **kept** — deprecated alias, one release of compat |

**F4b decision (informed by audit):** the plan's "Phase 1: stop writing `zone_id`" step would silently drop NULL-zone-id rows from the four `list_keys`-style filter queries above. Override that step — KEEP writing `primary_zone` to `APIKeyModel.zone_id` (as a backfill alias for the junction's first row) and only:
1. Make the column `nullable=True` at the schema level (alembic migration).
2. Update the docstring to mark it deprecated and direct callers to `get_zones_for_key`/`get_zone_perms_for_key`.

A future PR can migrate the four filter sites to junction queries and then truly stop writing the column.

---

## Task F4b: drop `APIKeyModel.zone_id`

**Files:**
- Create: alembic revision dropping the column (or making nullable + deprecating).
- Modify: `src/nexus/storage/models/auth.py` — remove `zone_id` column.
- Modify: `src/nexus/bricks/auth/providers/database_key.py` — fallback path `(api_key.zone_id,) if api_key.zone_id else ()` becomes `()` only.
- Modify: `src/nexus/cli/commands/hub.py token_create` — primary-zone usage via `zones[0]` only (already so for prefix).
- Modify: `src/nexus/cli/commands/hub.py token_list` — JSON `zone` field becomes a deprecated alias for `zones[0]`.
- Modify: `src/nexus/cli/commands/hub.py token_zones_show` — primary-first ordering uses `zones[0]` from junction.

**Strategy:**
- **Phase 1 (this PR):** make `APIKeyModel.zone_id` nullable + stop writing to it. Keep reads with fallback. No migration needed yet.
- **Phase 2 (follow-up):** drop the column once we're sure no caller reads it.

For YAGNI in this PR, do Phase 1 only — set zone_id to nullable, stop writing it, log warnings on read. Phase 2 (actual drop) deferred.

(If the audit in F4a shows zero callers, do both phases at once.)

**Commit:** `feat(#3785): deprecate APIKeyModel.zone_id (junction is source of truth)`

---

## Final task: lint + full test + PR refresh

Run:

```bash
uv run ruff check src tests
uv run mypy src/nexus/storage/api_key_ops.py src/nexus/cli/commands/hub.py src/nexus/contracts/types.py src/nexus/bricks/auth/providers/database_key.py src/nexus/bricks/mcp/auth_bridge.py src/nexus/bricks/mcp/auth_cache.py src/nexus/bricks/auth/types.py src/nexus/server/api/v2/routers/async_files.py
uv run pytest tests/unit/cli/test_hub.py tests/unit/storage/ tests/unit/contracts/ tests/unit/auth/ tests/unit/bricks/mcp/ tests/unit/server/api/v2/routers/ tests/integration/services/test_search_zone_set.py tests/migrations/ -v
```

Push branch. Update PR #3886 description (or open follow-up PR if changes are too large to bundle).

---

## Out of scope (still)

- Wildcard zones across deployments / cross-tenant.
- Per-zone-per-resource permissions (resource-level granularity beyond zone-level).
- Cache invalidation on permission mutation (60s `AuthIdentityCache` TTL accepted).
