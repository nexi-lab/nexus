# Portability: Declared Redacted-Fields Contract with Forced Re-injection on Import

**Issue:** [#4083](https://github.com/nexi-lab/nexus/issues/4083)
**Status:** Design — pending implementation
**Date:** 2026-05-09

## Problem

`bricks/portability/` exports zone bundles containing files, permissions, and embeddings — but does not yet export the **mount configurations** that resolve those zones to physical backends. As soon as bundles include mounts (required for true cross-instance migration), every backend's `backend_config: dict[str, Any]` becomes a vehicle for credentials: S3 access keys, OAuth tokens, KMS material, Slack bot tokens, and so on.

A new backend's author can easily forget to strip a field; a leaked snapshot containing a live S3 access key is a P0 incident. The contract for *what gets stripped* must be typed and enforced at both ends — strip on export, refuse to import without an explicit re-injection.

## Goal

1. Bundle export starts including mount configurations.
2. Stripping which fields are sensitive is a **typed contract** derived from each backend's existing `ConnectionArg.secret=True` declarations — no parallel list to keep in sync.
3. A heuristic guard hard-fails export when a backend has secret-shaped argument names not marked `secret=True`.
4. Bundle import **requires** an `overrides` dict re-injecting every redacted field, validated before any backend is initialized.
5. CI test enforces the audit at registration time, not just at export time.

## Non-goals

- DB-row credential stripping (`providers` / `federations` / `webhooks` / `settings` tables in `strip.py`) is an existing, orthogonal subsystem and is not modified by this work.
- Owner remap on import (callers can update `owner_user_id` post-import). Out of scope; flag as follow-up if needed.
- Per-mount metadata stripping (sensitive fields outside `CONNECTION_ARGS`). Today's `MountConfig` has no such field; the design leaves room (`redact_config` could later walk a configurable set of metadata keys), but no code is written for it now.

## Source-of-truth decision

Each connector already declares `CONNECTION_ARGS: dict[str, ConnectionArg]`, and every `ConnectionArg` has a `secret: bool` field. For example, `path_s3.py` already marks `access_key_id`, `secret_access_key`, `session_token`, and `credentials_path` as `secret=True`.

The redaction set for a backend is **derived from this existing flag** rather than maintained as a separate `redacted_fields` list on `ConnectorManifestEntry`. This avoids two-sources-of-truth drift and keeps the responsibility on the connector module that already owns connection metadata.

## Architecture

### File layout

```
src/nexus/bricks/portability/
├── redaction.py         # NEW — derive redaction set; secret-shape regex audit; redact one config dict
├── mount_export.py      # NEW — collect MountConfig list; redact via redaction.py; emit mounts.jsonl
├── mount_import.py      # NEW — read mounts.jsonl; validate overrides; restore via MountManager
├── models.py            # MOD — BUNDLE_FORMAT_VERSION "2.0.0" → "3.0.0"; MountRecord; mount_count; new errors
├── export_service.py    # MOD — call mount_export when options.include_mounts
├── import_service.py    # MOD — call mount_import; validate overrides before any backend init
└── schemas/manifest-v3.json   # NEW — schema for new bundle version
```

### Public API additions

In `bricks/portability/__init__.py`:

- `MountRecord` — frozen dataclass mirroring `mounts.jsonl` line shape.
- `SensitiveFieldNotDeclaredError` — export-time error.
- `MissingCredentialsError` — import-time error, raised before any side effect.
- `ZoneExportOptions.include_mounts: bool = False` — opt-in, default off.
- `ZoneImportOptions.mount_overrides: dict[str, dict[str, str]] | None = None` — keyed by `mount_id`, then by field name.
- `ZoneImportOptions.restore_mounts: bool = True` — allow callers to skip mount restore even when present in bundle.

### Bundle format

`BUNDLE_FORMAT_VERSION` bumps from `"2.0.0"` → `"3.0.0"` (existing semver-string format; matches `^\d+\.\d+\.\d+$` schema pattern).

`mounts.jsonl` (one JSON object per line, sorted by `mount_id`):

```json
{"mount_id": "uuid",
 "mount_point": "/personal/alice",
 "backend_type": "path_s3",
 "owner_user_id": "alice",
 "zone_id": "acme",
 "description": null,
 "backend_config": {
   "bucket_name": "acme-data",
   "region_name": "us-east-1",
   "access_key_id": "${MOUNT_uuid_ACCESS_KEY_ID}",
   "secret_access_key": "${MOUNT_uuid_SECRET_ACCESS_KEY}"
 }}
```

Manifest gains `mount_count: int` and `BUNDLE_PATHS["mounts"] = "mounts.jsonl"`. `manifest-v3.json` is added next to existing `manifest-v1.json` (cloned, plus the new `mount_count` property and an updated `$id` URL). The new reader recognises both schemas; old bundles (v1, no `mounts.jsonl`) import unchanged. v3 bundles read by an older Nexus instance fail loudly at schema validation (`additionalProperties: false` at the manifest root rejects `mount_count`) — the failure is explicit, not silent corruption.

### Data flow on export

1. `MountManager.list_mounts(zone_id=...)` returns the current mount configs.
2. For each mount: look up `ConnectorRegistry.get_info(backend_type)`.
3. Run `redaction.audit_backend(backend_type)` — compares `CONNECTION_ARGS` keys against the `(?i)(key|secret|token|password|cred)` regex; returns the names that match the heuristic but have `secret=False`.
4. If audit returns any names → raise `SensitiveFieldNotDeclaredError(backend_type, fields)`. Export aborts. **Hard fail.**
5. Otherwise: replace each `secret=True` field's value with `${MOUNT_<id>_<FIELD_UPPER>}` and append a `PlaceholderRef` to the export manifest's existing `placeholders` list (uniform with DB-row stripping).
6. Write `mounts.jsonl` and update `manifest.mount_count`.

### Data flow on import

1. Read `bundle_dir / "mounts.jsonl"`. If absent → no-op (back-compat with v2 bundles).
2. `validate_overrides(mounts, options.mount_overrides)` walks every redacted field across every mount and collects `(mount_id, field)` pairs not present in `overrides[mount_id]`.
3. If anything missing → raise `MissingCredentialsError(missing)` with all gaps reported in one error (not first-fail). **No backend has been instantiated at this point.**
4. After existing files/permissions/embeddings restore: for each mount, substitute placeholders with override values, apply zone remap if `target_zone_id` set, and call `MountManager.save_mount(...)` (or `update_mount` per `conflict_mode`).
5. Per-mount errors collected in `ImportResult.errors`; `result.mount_count` reflects successful restores.

### Conflict semantics (mount_point already exists in target instance)

- `SKIP` — leave existing mount in place; record `ImportError(severity=info)`.
- `OVERWRITE` — `MountManager.update_mount(mount_point, backend_config=...)`.
- `FAIL` — abort import per existing `import_service` rollback convention.

### Service-init contract

- `ZoneExportService.__init__` accepts optional `mount_manager: MountManager | None`. If `options.include_mounts=True` and `mount_manager is None` → `ValueError` at export start (fail loud, not silently skip).
- `ZoneImportService.__init__` accepts optional `mount_manager`. If bundle contains `mounts.jsonl` and `options.restore_mounts=True` and `mount_manager is None` → `ValueError`.

## `redaction.py` contract

```python
SECRET_SHAPED = re.compile(r"(?i)(key|secret|token|password|cred)")

def declared_secret_fields(backend_type: str) -> frozenset[str]:
    """CONNECTION_ARGS keys with secret=True for a backend."""

def audit_backend(backend_type: str) -> list[str]:
    """Arg names that look secret-shaped but aren't marked secret=True.
    Empty list = backend passes audit."""

def redact_config(
    backend_type: str,
    config: dict[str, Any],
    *,
    mount_id: str,
) -> tuple[dict[str, Any], list[PlaceholderRef]]:
    """Strip declared secret fields. Raises SensitiveFieldNotDeclaredError if audit fails."""
```

Reuses `PlaceholderRef` from existing `models.py` so the manifest's placeholder list has uniform UX across DB rows and mount configs.

## `mount_export.py` contract

```python
def collect_mounts(mount_manager: MountManager, *, zone_id: str | None) -> list[MountConfigRow]:
    """Pull raw mount configs from MountManager (zone-filtered if zone_id given)."""

def redact_and_write(
    mounts: list[MountConfigRow],
    *,
    out_path: Path,
) -> list[PlaceholderRef]:
    """Per-mount: redaction.redact_config; write mounts.jsonl; return aggregated placeholders.
    Raises SensitiveFieldNotDeclaredError on first audit failure."""
```

## `mount_import.py` contract

```python
def read_mounts(bundle_dir: Path) -> list[MountRecord]:
    """Parse mounts.jsonl. Returns [] if absent (v2 bundle)."""

def validate_overrides(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]] | None,
) -> None:
    """Walk every redacted field; raise MissingCredentialsError listing all gaps.
    Pure: no side effects. Runs before any backend init."""

def materialize(
    mount_record: MountRecord,
    overrides_for_mount: dict[str, str],
) -> dict[str, Any]:
    """Substitute ${MOUNT_<id>_<FIELD>} placeholders; return final backend_config."""

def import_mounts(
    mounts: list[MountRecord],
    overrides: dict[str, dict[str, str]],
    mount_manager: MountManager,
    *,
    target_zone_id: str | None,
    conflict_mode: ConflictMode,
) -> list[ImportError]:
    """Per mount: materialize + save_mount/update_mount per conflict_mode.
    Returns per-mount errors; does not raise except on programmer bugs."""
```

## Errors

```python
class SensitiveFieldNotDeclaredError(ValueError):
    """Export-time. Backend has secret-shaped CONNECTION_ARGS keys not marked secret=True."""
    backend_type: str
    fields: list[str]

class MissingCredentialsError(ValueError):
    """Import-time. Bundle has redacted mount fields with no override supplied."""
    missing: dict[str, list[str]]  # mount_id -> [field, ...]
```

`MissingCredentialsError` reports **every** gap in one error message (not first-fail), so an operator sees the full set of credentials they need to provide.

## Testing

| File | Coverage |
|---|---|
| `tests/test_redaction_audit.py` | Every `CONNECTOR_MANIFEST` entry passes `audit_backend()`. **CI gate.** Skip entries whose runtime extras aren't installed. |
| `tests/test_redaction_unit.py` | `declared_secret_fields()`, `audit_backend()` positive/negative, `redact_config()` idempotence, placeholder shape, `None` values skipped (no placeholder generated). |
| `tests/test_mount_export.py` | Collect+redact happy path, audit failure raises, manifest placeholders extended, `mounts.jsonl` byte-stable across runs. |
| `tests/test_mount_import.py` | `validate_overrides` reports all missing in one error (not first-fail), `materialize` substitutes correctly, conflict modes (SKIP/OVERWRITE/FAIL), zone remap, `restore_mounts=False` skips. |
| `tests/test_export_strip.py` (existing) | Extend to assert mount placeholders join existing DB-row placeholders cleanly in `manifest.placeholders`. |
| `tests/test_manifest_v3.py` | New `BUNDLE_FORMAT_VERSION` validates against `manifest-v3.json`; v1/v2 bundle still imports (mount step no-op when `mounts.jsonl` absent); v3 bundle validates against v1 schema fails loudly (assert the error message). |
| `tests/integration/test_roundtrip_with_mounts.py` | Export bundle with two mounts → import without overrides fails with `MissingCredentialsError` listing both → import with overrides succeeds → mounts restored verbatim. |

**TDD order**: redaction unit tests first → `mount_export` tests → `mount_import` tests (focus on the validate-then-materialize split) → end-to-end roundtrip last.

## Migration & back-compat

- `BUNDLE_FORMAT_VERSION` bumps `"2.0.0"` → `"3.0.0"`.
- New reader accepts both v1/v2 bundles (no `mounts.jsonl`, schema `manifest-v1.json`) and v3 bundles. v1/v2 import simply skips the mount step.
- v3 bundles read by an older Nexus instance: schema validation rejects `mount_count` because `manifest-v1.json` has `additionalProperties: false` at the root. The failure is loud and unambiguous, not silent data drop. This is correct behavior — refusing unknown bundle versions prevents partial restores.
- New `manifest-v3.json` schema added alongside the existing `manifest-v1.json` (clone the v1 file, change `$id`, add `mount_count` and a top-level `mounts` reference path if needed).
- One-paragraph addition to a portability doc page (e.g. `docs/portability.md` if it exists; otherwise create one under `docs/`) covering the export → override → import flow and the security rationale.

## Effort

3 days, matching the issue's S–M estimate. Most cost is integration tests and the cross-version manifest sanity check.

## Risks

- **Heuristic false positives.** The `(?i)(key|secret|token|password|cred)` regex could flag a benign field name (e.g., a future `keyspace` arg). Resolution: when this happens, the connector author must either rename the arg or mark it `secret=True`. We deliberately do not provide a per-arg "acknowledged_safe" allowlist — every false positive is a forcing function for a clearer name. If real friction emerges, revisit by adding `ConnectionArg.audit_safe: bool = False` later.
- **Mount restore order.** Mounts may depend on each other (e.g., a brick that mounts under `/zone/data` then another under `/zone/data/sub`). Ordering: sort by `len(mount_point.split("/"))` ascending so parents restore first. Documented in `import_mounts`.
- **`MountManager` not always available.** Some import contexts (e.g., dry-run bundle inspection) don't have a MountManager. The opt-in `restore_mounts: bool = True` knob lets those callers skip cleanly.

## Acceptance criteria

- [ ] `ConnectionArg.secret=True` declarations are the sole source of truth for redaction; no `redacted_fields` list added anywhere.
- [ ] Every connector in `CONNECTOR_MANIFEST` passes `audit_backend()`; CI test enforces this at registration time.
- [ ] `ZoneExportOptions.include_mounts=True` produces a bundle whose `mounts.jsonl` has every secret field replaced with a `${MOUNT_<id>_<FIELD>}` placeholder, and whose `manifest.placeholders` lists every replacement.
- [ ] Export aborts with `SensitiveFieldNotDeclaredError` if a backend has secret-shaped argument names not marked `secret=True`.
- [ ] Import without `mount_overrides` for any redacted field raises `MissingCredentialsError` listing every gap in one message — before any backend is initialized.
- [ ] Import with full `mount_overrides` restores mounts via `MountManager.save_mount` (or `update_mount` per `conflict_mode`).
- [ ] v2 bundles still import; v3 reader is back-compat with v2.
- [ ] Doc page covering export/import flow and security rationale.
