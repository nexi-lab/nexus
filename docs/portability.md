# Zone Data Portability

Nexus zone bundles (`.nexus` archives) carry files, permissions, embeddings,
and — as of bundle format `3.0.0` — mount configurations. This document
covers the export/import flow with particular attention to credential
handling.

## Mount-config redaction (Issue #4083)

Mount configurations contain backend credentials: S3 access keys, OAuth tokens,
KMS material, etc. Bundles **must never** contain live credential values, so
the export pipeline strips them and the import pipeline refuses to proceed
without explicit re-injection.

### What gets stripped

For each connector, the redaction set is derived from `CONNECTION_ARGS`: every
argument whose `secret=True` flag is set has its value replaced by a
`${MOUNT_<id>_<FIELD>}` placeholder. This is the **only** source of truth — no
parallel list to maintain.

### Heuristic guard

On export, a heuristic regex (`(?i)(key|secret|token|password|cred)`) scans
every `CONNECTION_ARGS` key. If any matching name is **not** marked
`secret=True` and **not** marked `audit_safe=True`, the export aborts with
`SensitiveFieldNotDeclaredError`. This prevents a forgotten flag from silently
leaking credentials.

### Audit_safe escape hatch

For benign fields whose name happens to match the heuristic (e.g., a
filesystem path containing `token`), set `audit_safe=True` on the
`ConnectionArg` and document the reason in the description. The CI gate
(`tests/test_redaction_audit.py`) enforces this contract for every registered
connector.

### Forced re-injection on import

Imports require a `mount_overrides` dict per mount:

```python
from nexus.bricks.portability import (
    ZoneImportService,
    ZoneImportOptions,
)

importer = ZoneImportService(nexus_fs, mount_manager=mount_manager)
importer.import_zone(ZoneImportOptions(
    bundle_path="zone.nexus",
    mount_overrides={
        "m-1": {
            "access_key_id": "AKIA...",
            "secret_access_key": "wJalr...",
        },
    },
))
```

Without `mount_overrides` for a redacted field, the import raises
`MissingCredentialsError` listing **every** missing credential in one message
— operators see the full set up front, before any backend is initialized.

### Skipping mount restore

For dry-run inspections or destination instances that don't need the mounts:

```python
importer.import_zone(ZoneImportOptions(
    bundle_path="zone.nexus",
    restore_mounts=False,
))
```

### Bundle format compatibility

- v1/v2 bundles (no `mounts.jsonl`) import unchanged on the new code path.
- v3 bundles read by older Nexus instances fail loudly at schema validation
  — `additionalProperties: false` in `manifest-v1.json` rejects the new
  `mount_count` field. The failure is explicit, not silent corruption.
