# Zone Archive: Snapshot + Safe Migration with Credential Stripping (#3793)

**Status**: design approved 2026-05-01 (amended after portability brick discovery)
**Tracks**: Phase 3 — Enterprise Context Layer
**Depends on**: #3778 (lightweight profile, closed), #3786 (federation, closed), #3791 (activity foundation, in progress), #1161/#1162 (portability brick, shipped)
**Epic**: #3777

## Summary

Operator-facing snapshot/restore tool for nexus zones, layered on top of the existing
`bricks/portability/` zone export brick. Adds five capabilities the portability brick
does not have: (1) signed manifests with TOFU trust, (2) credential stripping with
placeholder re-injection, (3) scheduled archives with GFS retention to local/S3/GCS,
(4) audit-window export bundling activity events from #3791, and (5) the `nexus archive`
CLI that operators actually use day-to-day.

The user-facing artifact stays `.nexus` (tar.gz). Format version bumps from `1.0.0` to
`2.0.0` to add the new fields. Backward compatibility: old `1.x` bundles remain readable
by `BundleReader.inspect()`, and `nexus archive verify --strict` is the only place that
hard-requires v2.

## Why layer on portability instead of a new brick

`bricks/portability/` already ships:
- `.nexus` tar.gz format with manifest, files.jsonl, content CAS, ReBAC tuples, embeddings parquet
- `BundleChecksums` with per-file SHA + Merkle root
- `BundleReader` with manifest read, validate, list_contents, extract
- `ZoneExportService` (single-zone export)
- `ZoneImportService` (with conflict modes, remapping, dry_run)
- Date filtering on export via `after_time`/`before_time`

Building a parallel brick would duplicate ~1000 lines and produce two bundle formats.
Extending portability gives us a single source of truth.

## Goals

- Round-trip a zone (or a whole hub) through `create → destroy → restore` with byte-identical search results.
- No credentials ever land in an archive artifact (provider keys, hub tokens, webhook secrets, workspace paths).
- Tamper detection: signature, Merkle root, per-file SHA256.
- Operator-friendly: single-file artifact, single CLI verb per operation, sane defaults, fail-closed posture.
- Scheduled archives in hub mode with GFS retention (daily/weekly/monthly) to local/S3/GCS.
- Compliance audit export with date-window filtering and activity-event slice.

## Non-goals (deferred)

- Incremental archives. Format is always full; CAS makes storage delta cheap regardless.
- Compression tuning beyond the existing `compression_level` knob.
- Multi-region replication. Operator's storage-backend concern.
- GUI for browse/restore.

## Architecture

Components split across two locations:

### Extensions inside `bricks/portability/`
- `signer.py` — ed25519 keypair management + sign/verify of manifest.
- `trust.py` — TOFU trust store at `~/.nexus/trusted_signers.json`.
- `strip.py` — schema-aware + regex credential stripper (export-time pre-pass).
- `models.py` — extend `ExportManifest` with `signature`, `signer_pubkey_b64`, `embedding_model`, `embedding_dim`, `placeholders`, `activity_window`, `archive_kind`. Bump `BUNDLE_FORMAT_VERSION` to `2.0.0`.
- `export_service.py` — wire stripper + signer into the export pipeline (opt-in via `ZoneExportOptions.sign=True`, `strip_credentials=True`).
- `import_service.py` — add placeholder guard (`require_no_placeholders` default True), embedding model/dim check (`rebuild_embeddings` flag), force-empty-target check.
- `differ.py` — bundle diff using existing CAS hashes.

### New brick `bricks/archive/` (orchestrator + scheduler)
- `orchestrator.py` — multi-zone orchestration: export each zone via `ZoneExportService`, merge into a single bundle (or one per zone, configurable).
- `scheduler.py` — cron-string trigger + GFS retention sweep (hub-only background task).
- `storage/` — pluggable `ArchiveStorage` protocol with `local`, `s3`, `gcs` impls.
- `audit_export.py` — extends export with activity event slice from #3791.
- `errors.py` — archive-level errors (signature, trust, placeholder, embedding mismatch).

### CLI
- `src/nexus/cli/commands/archive.py` — new `nexus archive` Click group: `create / verify / restore / diff / inspect / keys`.

```
src/nexus/bricks/portability/         # extended (existing brick)
├── models.py                         # +signature, +placeholders, +activity_window, etc.
├── signer.py                         # NEW
├── trust.py                          # NEW
├── strip.py                          # NEW
├── differ.py                         # NEW
├── export_service.py                 # extended
└── import_service.py                 # extended

src/nexus/bricks/archive/             # NEW brick
├── __init__.py
├── orchestrator.py                   # multi-zone export coordinator
├── scheduler.py                      # cron + GFS retention
├── audit_export.py                   # activity slice integration
├── storage/
│   ├── base.py                       # ArchiveStorage protocol
│   ├── local.py
│   ├── s3.py
│   └── gcs.py
├── errors.py
└── tests/

src/nexus/cli/commands/archive.py     # NEW: nexus archive group
```

## Bundle format (v2)

Backward-compatible extension of the existing `.nexus` tar.gz. Internal layout unchanged
from v1; new fields added to `manifest.json` and a new `signatures.json` peer file:

```
my-zone-2026-04-16.nexus  (= tar.gz)
├── manifest.json                          # extended schema, format_version "2.0.0"
├── signatures.json                        # NEW: ed25519 sig + signer pubkey
├── metadata/files.jsonl                   # existing
├── metadata/versions.jsonl                # existing
├── permissions/rebac_tuples.jsonl         # existing (after credential strip)
├── content/cas/<aa>/<sha256>              # existing
├── embeddings/vectors.parquet             # existing (now manifest-tagged with model+dim)
└── activity/events.jsonl                  # NEW: present only for archive_kind=audit
```

### New manifest fields (v2 additions)

```jsonc
{
  "format_version": "2.0.0",
  // … existing v1 fields kept verbatim …
  "archive_kind": "full" | "audit",
  "activity_window": {"from": "...", "to": "..."} | null,
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "embedding_dim": 384,
  "signer_pubkey_b64": "…",
  "placeholders": [
    {"name": "HUB_TOKEN_eng_hub", "field": "federations.eng_hub.auth_token"},
    {"name": "PROVIDER_KEY_anthropic", "field": "providers.anthropic.api_key"}
  ],
  "min_nexus_version": "0.10.0"
}
```

### `signatures.json`

```jsonc
{
  "algorithm": "ed25519",
  "signer_pubkey_b64": "…",
  "signature_b64": "…",            // sig over canonical-json(manifest) || merkle_root
  "manifest_sha256": "…"           // sanity check
}
```

`BundleChecksums.compute_merkle_root()` is reused as-is. The signature covers the
canonical-JSON encoding of the manifest concatenated with the Merkle root bytes.

## Signing + trust

- Ed25519 keypair auto-generated on first `archive create`. Stored at
  `~/.nexus/archive_signing_key` (private, mode 0600) and `…_signing_key.pub`.
- Pubkey embedded in every archive's `signatures.json` and mirrored in `manifest.signer_pubkey_b64`.
- TOFU trust store at `~/.nexus/trusted_signers.json`:
  `{<pubkey_b64>: {"first_seen": ts, "label": "..."}}`. `verify` warns on unseen
  signer; `restore --require-trusted` hard-fails on unseen.
- Rotation: `nexus archive keys rotate` generates a fresh keypair, archives the old to
  `~/.nexus/archive_signing_key.<unix-ts>.bak`. Old archives still verify against their
  embedded pubkey.

## Credential stripping (non-negotiable)

Two-layer pipeline applied as an export-time pre-pass:

### Layer 1 — schema-aware

Known sensitive columns are nulled by name and replaced with `${PLACEHOLDER_NAME}`
strings. The manifest's `placeholders[]` enumerates which placeholders the operator
must re-inject on restore. Coverage:

| Source | Field | Placeholder |
|---|---|---|
| `providers` table | `api_key` | `${PROVIDER_KEY_<name>}` |
| `federations` table | `auth_token` | `${HUB_TOKEN_<name>}` |
| `webhooks` table | `secret` | `${WEBHOOK_SECRET_<name>}` |
| `settings` table | rows whose key matches deny-list | `${SETTING_<key>}` |
| Anywhere | local workspace path absolute prefix | `${WORKSPACE_ROOT}` |

### Layer 2 — regex deny-list backstop

Configurable regex list scans free-text fields (doc bodies, log lines, settings JSON
values). Matches → `***REDACTED***` + warning logged with `(zone, table, row_id, pattern_name)`
context. Default patterns:

- Anthropic: `sk-ant-[A-Za-z0-9_-]{20,}`
- OpenAI: `sk-[A-Za-z0-9]{20,}`
- GitHub PAT: `ghp_[A-Za-z0-9]{36}` and `gho_…`
- GitLab PAT: `glpat-[A-Za-z0-9_-]{20}`
- Slack bot: `xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+`
- AWS access key: `AKIA[0-9A-Z]{16}`
- Google API key: `AIza[0-9A-Za-z_-]{35}`

Operators add patterns via `nexus.yaml`:

```yaml
archive:
  redact_patterns:
    - name: corp-internal-token
      pattern: 'corp-[A-Z0-9]{32}'
```

### Restore guard

`ZoneImportService` is extended with a `require_no_placeholders` flag (default True).
It scans every restored row that contains a `${…}` token. If any placeholder remains
un-injected after `--inject` flags are applied, restore aborts before bringing services
online (`ArchivePlaceholderNotInjected`). Satisfies AC #5 fail-closed posture.

## Embedding portability

`ExportManifest` gains `embedding_model` + `embedding_dim`. Restore behavior:

- Default: refuse on mismatch with error naming both old and current model.
- `--rebuild-embeddings`: drop shipped vectors, re-embed shipped documents (already
  content-addressed in `content/cas/`) using the current configured embedder. Emit
  progress to stderr; final vector count compared against doc count.

## CLI surface

The top-level verb is `nexus archive` to avoid collision with the existing
`nexus snapshot` group (which manages MVCC transactional filesystem snapshots,
a different concept).

| Command | Purpose |
|---|---|
| `nexus archive create [--zone <z>]… [--all-zones] [--output FILE] [--audit --from <d1> --to <d2>] [--no-sign] [--no-strip]` | Build an archive of one zone, several zones, or the whole hub. Audit mode filters to date window and bundles the activity event slice. |
| `nexus archive verify <file> [--strict]` | Signature + Merkle + per-file SHA + version compatibility. `--strict` requires v2 (signed). Exit 0 = valid, non-zero with structured error otherwise. |
| `nexus archive restore <file> [--target-zone <z>] [--require-trusted] [--rebuild-embeddings] [--force] [--inject KEY=VALUE]…` | Verify → strip-check → re-inject placeholders → write to fresh nexus. Refuses if target has existing zones unless `--force`. |
| `nexus archive diff <a> <b> [--detail]` | Per-zone summary of doc/policy/embedding deltas. `--detail` lists doc paths and SHAs. |
| `nexus archive inspect <file>` | Dump manifest + file tree without touching the running nexus. |
| `nexus archive keys rotate` | Rotate signing keypair (keeps old pubkey verifiable for prior archives). |
| `nexus archive keys trust <pubkey-b64> [--label <name>]` | Add a signer to TOFU trust store. |

CLI documented in `CLI.md`.

## Scheduled archives (hub-only)

Background task in hub mode; lightweight profile skips registration entirely. Config:

```yaml
archive:
  schedule: "0 2 * * *"
  retention:
    daily: 7
    weekly: 4
    monthly: 6
  destination:
    kind: s3
    bucket: my-nexus-archives
    prefix: prod/
    region: us-east-1
```

`ArchiveStorage` protocol with `local`, `s3`, `gcs` impls. After each successful
create, retention runs against the destination listing applying GFS rules (keep N most
recent daily, N weekly per ISO week, N monthly per calendar month). Failed archives
emit a metric (`nexus_archive_failed_total`); successful runs emit
`nexus_archive_bytes`, `nexus_archive_duration_seconds`.

## Federated migration

Restore brings back federation config rows but `auth_token` columns hold
`${HUB_TOKEN_<name>}`. Restore prints:

```
Federation re-pair required:
  nexus federation auth https://hub.example.com
  nexus federation auth https://other-hub.example.com
```

Operator runs the existing #3786 pairing flow — same code path, no new auth surface,
no resurrected stale tokens.

## Diff semantics

`archive diff <a> <b>`:

- Default summary: `+12 docs, -3 docs, ~7 docs changed, embedding_model: same, policy: 2 grants added, 1 zone added`.
- `--detail`: lists `(zone, doc_sha, change_kind)` rows.
- Comparison cheap because docs are content-addressed by SHA256 — set difference of
  CAS blob IDs is the hot path; reuses existing `BundleReader` for both sides.

## Audit export

`archive create --audit --from <d1> --to <d2>`:

- Sets `ZoneExportOptions.after_time=d1`, `before_time=d2` (existing capability).
- Sets `archive_kind="audit"` and `activity_window` in manifest.
- New `audit_export.py` queries the activity store from #3791 for events in the same
  window and writes them to `activity/events.jsonl` inside the bundle.
- Full policy snapshot at `d2` is included regardless of window (auditor needs context).

## Data flow

### Create
1. Open DB-level read consistency (snapshot isolation transaction).
2. For each zone in scope: run credential-strip pre-pass on a metadata clone.
3. Call `ZoneExportService.export_zone()` against the stripped data.
4. (Audit only) export activity event slice into `activity/events.jsonl`.
5. Compute per-file SHA256 + Merkle root via existing `BundleChecksums`.
6. Sign canonical-json(manifest) || merkle_root with ed25519 → write `signatures.json`.
7. Re-tar bundle to include `signatures.json` and finalized v2 manifest.

### Verify
1. Open archive via `BundleReader`.
2. Parse manifest; check `format_version`, `min_nexus_version`.
3. Verify ed25519 signature against embedded pubkey (`signatures.json`).
4. Reuse `BundleChecksums.verify_merkle_root()` and per-file checksum verification.
5. Optional: TOFU trust check against `~/.nexus/trusted_signers.json`.

### Restore
1. Verify (above).
2. Refuse if target nexus already has zones (use `--force` to overwrite; default
   fail-closed prevents accidental clobbering).
3. Parse `placeholders[]`; apply `--inject KEY=VALUE` flags.
4. Abort if any `${…}` placeholder remains.
5. Check `embedding_dim` against current configured embedder; abort or branch to rebuild.
6. Call `ZoneImportService.import_zone()` to restore content + metadata + permissions.
7. Restore embedding shards (or trigger re-embed pass).
8. Restart services; print federation re-pair list if applicable.

## Errors

All in `bricks/archive/errors.py`, each with a stable error code for CLI exit + structured log:

| Class | Code | When |
|---|---|---|
| `ArchiveSignatureError` | 10 | ed25519 sig mismatch |
| `ArchiveMerkleMismatch` | 11 | Merkle root differs |
| `ArchiveFileHashMismatch` | 12 | Per-file SHA differs |
| `ArchiveVersionIncompatible` | 13 | `min_nexus_version` > current |
| `ArchivePlaceholderNotInjected` | 20 | restore guard tripped |
| `ArchiveEmbeddingDimMismatch` | 21 | dim differs and `--rebuild-embeddings` not set |
| `ArchiveCredentialLeakDetected` | 30 | regex backstop matched during create (warning, not fatal) |
| `ArchiveUntrustedSigner` | 40 | `--require-trusted` and signer unseen |
| `ArchiveTargetNotEmpty` | 50 | target has zones and `--force` not set |

## Testing strategy

### Unit
- Manifest v2 schema validation (pydantic) — required fields, version coercion, v1→v2 read-only compat.
- Credential-strip regex matrix: positive matches for every default pattern, negative cases for false-positive-prone strings.
- Schema-aware strip on each known sensitive column (mocked DB rows).
- Ed25519 sign/verify round-trip; tamper detection on signed payload.
- TOFU trust store: first-see-warn, second-see-no-warn, rotation behavior.
- GFS retention math: given a list of `(name, ts)`, returns expected keep/prune sets across day/week/month boundaries.
- Storage backend protocol conformance: `local` writes/lists; `s3` and `gcs` against `moto`/`gcs-emulator`.

### Integration
- Round-trip create → verify → restore on:
  - SQLite backend (lightweight profile).
  - Postgres backend (hub).
- Tamper tests: corrupt manifest byte → `ArchiveSignatureError`/`ArchiveMerkleMismatch`;
  flip sig byte → `ArchiveSignatureError`; swap a CAS blob → `ArchiveFileHashMismatch`.
- Planted-secret fixtures: load fixture rows containing every default regex pattern, archive, assert bundle contains zero matches.
- Placeholder fail-closed: archive, attempt restore without `--inject`, assert `ArchivePlaceholderNotInjected`.
- Embedding mismatch: archive under model A, switch config to model B, restore → abort; with `--rebuild-embeddings` → succeed and re-embed.
- Audit window: ingest docs at staggered timestamps, archive with window covering subset, assert only window-matching docs present and `activity/events.jsonl` matches.
- v1 backward compat: existing v1 bundle from portability tests still loads via `BundleReader.inspect()`; `archive verify --strict` rejects it.

### E2E
- Spin docker stack via `nexus-stack.yml`; ingest a known fixture corpus; `nexus archive create`; tear down stack; restore on fresh stack; assert search results byte-identical given deterministic ranking inputs (fixed seed, fixed query set).
- Repeated for both lightweight and hub profiles.
- Scheduled archive test against `local` storage backend: bump simulated clock, assert N daily files kept, M pruned.

## Migration / rollout

- All changes additive — no schema changes to existing tables, no downtime.
- New `~/.nexus/archive_signing_key` generated lazily on first create.
- Scheduled archives opt-in via config; default is no schedule.
- Existing `bricks/portability/` tests must continue to pass unchanged.
- Documentation: `docs/operations/archives.md` for operator guide, plus `CLI.md` updates.

## Open questions

None at design time — all scope decisions captured above.
