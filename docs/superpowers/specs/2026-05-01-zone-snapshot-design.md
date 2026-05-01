# Zone Snapshot + Safe Migration with Credential Stripping (#3793)

**Status**: design approved 2026-05-01
**Tracks**: Phase 3 — Enterprise Context Layer
**Depends on**: #3778 (lightweight profile, closed), #3786 (federation, closed), #3791 (activity foundation, in progress)
**Epic**: #3777

## Summary

A first-class snapshot/restore tool for nexus zones. Produces a portable, signed, content-addressed archive (`.nxsnap` = `.tar.zst`) for backup, disaster recovery, host migration, contractor zone hand-off, and audit takeout. Strips credentials before writing the archive and refuses to restore until placeholders are re-injected.

## Goals

- Round-trip a zone (or a whole hub) through `create → destroy → restore` with byte-identical search results.
- No credentials ever land in a snapshot artifact (provider keys, hub tokens, webhook secrets, workspace paths).
- Tamper detection: signature, Merkle root, per-file SHA256.
- Operator-friendly: single-file artifact, single CLI verb per operation, sane defaults, fail-closed posture.
- Scheduled snapshots in hub mode with GFS retention (daily/weekly/monthly) to local/S3/GCS.
- Compliance audit export with date-window filtering.

## Non-goals (deferred)

- Incremental snapshots. Format is always full; content-addressing makes storage delta cheap regardless.
- Compression tuning. Zstd level 3 default; configurable later if measured.
- Multi-region replication. Operator's storage-backend concern.
- GUI for browse/restore.

## Architecture

New brick `src/nexus/bricks/snapshot_archive/` (sibling to existing `bricks/snapshot/`, which is the unrelated MVCC transactional snapshot service — different concern, distinct name).

```
src/nexus/bricks/snapshot_archive/
├── __init__.py
├── format.py          # nxsnap reader/writer, manifest schema (pydantic)
├── signer.py          # ed25519 keypair management + TOFU trust store
├── strip.py           # schema-aware + regex credential stripper
├── builder.py         # snapshot create pipeline
├── restorer.py        # snapshot restore pipeline
├── verifier.py        # signature + Merkle + schema checks
├── differ.py          # snapshot diff
├── inspector.py       # snapshot inspect (manifest dump)
├── scheduler.py       # cron + GFS retention loop (hub-only)
├── storage/
│   ├── __init__.py
│   ├── base.py        # SnapshotStorage protocol
│   ├── local.py
│   ├── s3.py
│   └── gcs.py
├── errors.py
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

CLI lives in `src/nexus/cli/snapshot.py`, wires Click subcommands to the brick's public API. Service registration via the existing brick lifespan pattern (mirror `bricks/snapshot/__init__.py`).

## Archive format (`.nxsnap`)

Single `.tar.zst` artifact. Manifest is the first entry so `verify` can stream-parse without extracting the body. Internal layout:

```
<archive>.nxsnap (= .tar.zst)
├── manifest.json
├── signatures.json
├── meta.db                              # SQLite snapshot of metadata, sanitized
├── policy.yaml                          # ReBAC + network policy, sanitized
├── docs/<aa>/<sha256>                   # content-addressed raw documents
├── embeddings/<zone>/<shard>.bin        # vector index shards
├── activity/                            # audit-export only: event slice
└── NOTES.md                             # human-readable summary
```

### Manifest schema

```jsonc
{
  "format_version": "1",
  "nexus_version": "0.10.x",
  "min_nexus_version": "0.10.0",
  "created_at": "2026-05-01T02:00:00Z",
  "snapshot_kind": "full" | "audit",
  "audit_window": {"from": "2026-04-01T00:00:00Z", "to": "2026-05-01T00:00:00Z"} | null,
  "zones": ["eng", "legal"],
  "embedding_model": "BAAI/bge-small-en-v1.5",
  "embedding_dim": 384,
  "signer_pubkey_b64": "…",
  "merkle_root_b64": "…",
  "files": [
    {"path": "meta.db", "sha256": "…", "size": 12345},
    {"path": "docs/aa/aa3f…", "sha256": "…", "size": 4096},
    …
  ],
  "placeholders": [
    {"name": "HUB_TOKEN_eng_hub", "field": "federations.eng_hub.auth_token"},
    {"name": "PROVIDER_KEY_anthropic", "field": "providers.anthropic.api_key"}
  ]
}
```

Merkle root is the root of a binary Merkle tree built over the sorted list of `(path, sha256)` pairs. Per-file SHAs let `verify` detect targeted byte-level tampering.

## Signing + trust

- Ed25519 keypair auto-generated on first `snapshot create`. Stored at `~/.nexus/snapshot_signing_key` (private, mode 0600) and `~/.nexus/snapshot_signing_key.pub`.
- Pubkey is embedded in every snapshot's `manifest.signer_pubkey_b64`.
- TOFU trust store at `~/.nexus/trusted_signers.json`: `{<pubkey_b64>: {"first_seen": ts, "label": "..."}}`. `verify` warns on unseen signer; `restore --require-trusted` hard-fails on unseen.
- Rotation: `nexus snapshot keys rotate` generates a fresh keypair, archives the old to `~/.nexus/snapshot_signing_key.<unix-ts>.bak`. Old snapshots still verify against their embedded pubkey.

## Credential stripping (non-negotiable)

Two-layer pipeline applied before the archive is written:

### Layer 1 — schema-aware

Known sensitive columns are nulled by name. Replaced with placeholder strings of the form `${PLACEHOLDER_NAME}`. Manifest's `placeholders[]` enumerates which placeholders the operator must re-inject on restore. Coverage:

| Source | Field | Placeholder |
|---|---|---|
| `providers` table | `api_key` | `${PROVIDER_KEY_<name>}` |
| `federations` table | `auth_token` | `${HUB_TOKEN_<name>}` |
| `webhooks` table | `secret` | `${WEBHOOK_SECRET_<name>}` |
| `settings` table | rows whose key matches deny-list | `${SETTING_<key>}` |
| Anywhere | local workspace path absolute prefix | `${WORKSPACE_ROOT}` |

### Layer 2 — regex deny-list backstop

Configurable regex list scans free-text fields (doc bodies, log lines, settings JSON values). Matches → `***REDACTED***` + warning logged with `(zone, table, row_id, pattern_name)` context. Default patterns:

- Anthropic: `sk-ant-[A-Za-z0-9_-]{20,}`
- OpenAI: `sk-[A-Za-z0-9]{20,}`
- GitHub PAT: `ghp_[A-Za-z0-9]{36}` and `gho_…`
- GitLab PAT: `glpat-[A-Za-z0-9_-]{20}`
- Slack bot: `xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+`
- AWS access key: `AKIA[0-9A-Z]{16}`
- Google API key: `AIza[0-9A-Za-z_-]{35}`

Operators add patterns via `nexus.yaml`:

```yaml
snapshots:
  redact_patterns:
    - name: corp-internal-token
      pattern: 'corp-[A-Z0-9]{32}'
```

### Restore guard

Restore parses every restored row that contains a `${…}` token. If any placeholder remains un-injected after `--inject` flags are applied, restore aborts before bringing services online (`SnapshotPlaceholderNotInjected`). Satisfies AC #5 fail-closed posture.

## Embedding portability

Manifest declares `embedding_model` + `embedding_dim`. Restore behavior:

- Default: refuse on mismatch with error naming both old and current model.
- `--rebuild-embeddings`: drop shipped vectors, re-embed shipped documents (already content-addressed in `docs/`) using the current configured embedder. Emit progress to stderr; final vector count compared against doc count.

## CLI surface

| Command | Purpose |
|---|---|
| `nexus snapshot create [--zone <z>] [--output FILE] [--audit --from <d1> --to <d2>]` | Build a full snapshot of one zone or the whole hub. Audit mode filters to the date window and bundles the activity event slice. |
| `nexus snapshot verify <file>` | Signature + Merkle + per-file SHA + version compatibility check. Exit 0 = valid, non-zero with structured error otherwise. |
| `nexus snapshot restore <file> [--target <host>] [--require-trusted] [--rebuild-embeddings] [--force] [--inject KEY=VALUE]…` | Verify → strip-check → re-inject placeholders → write to fresh nexus. Refuses if target has existing zones unless `--force`. |
| `nexus snapshot diff <a> <b> [--detail]` | Per-zone summary of doc/policy/embedding deltas. `--detail` lists doc paths and SHAs. |
| `nexus snapshot inspect <file>` | Dump manifest + file tree without touching the running nexus. |
| `nexus snapshot keys rotate` | Rotate signing keypair (keeps old pubkey verifiable for prior snapshots). |
| `nexus snapshot keys trust <pubkey-b64> [--label <name>]` | Add a signer to TOFU trust store. |

CLI documented in `CLI.md`.

## Scheduled snapshots (hub-only)

Background task in hub mode; lightweight profile skips registration entirely. Config:

```yaml
snapshots:
  schedule: "0 2 * * *"
  retention:
    daily: 7
    weekly: 4
    monthly: 6
  destination:
    kind: s3
    bucket: my-nexus-snapshots
    prefix: prod/
    region: us-east-1
```

`SnapshotStorage` protocol with `local`, `s3`, `gcs` impls. After each successful create, retention runs against the destination listing applying GFS rules (keep N most recent daily, N weekly per ISO week, N monthly per calendar month). Failed snapshots emit a metric (`nexus_snapshot_failed_total`); successful runs emit `nexus_snapshot_bytes`, `nexus_snapshot_duration_seconds`.

## Federated migration

Restore brings back federation config rows but `auth_token` columns hold `${HUB_TOKEN_<name>}`. Restore prints:

```
Federation re-pair required:
  nexus federation auth https://hub.example.com
  nexus federation auth https://other-hub.example.com
```

Operator runs the existing #3786 pairing flow — same code path, no new auth surface, no resurrected stale tokens.

## Diff semantics

`snapshot diff <a> <b>`:

- Default summary: `+12 docs, -3 docs, ~7 docs changed, embedding_model: same, policy: 2 grants added, 1 zone added`.
- `--detail`: lists `(zone, doc_sha, change_kind)` rows.
- Comparison is cheap because docs are content-addressed by SHA256 — set difference of doc SHAs is the hot path.

## Audit export

`snapshot create --audit --from <d1> --to <d2>`:

- Produces a `.nxsnap` whose `meta.db` contains only docs whose `created_at` or `modified_at` falls in `[d1, d2)`.
- `policy.yaml` is the full policy snapshot at `d2` (auditor needs context).
- New `activity/` directory contains the event slice from #3791's activity store for the same window, exported as JSONL.
- Manifest's `snapshot_kind: "audit"` and `audit_window` set.

## Data flow

### Create
1. Acquire DB-level read consistency (snapshot isolation transaction).
2. Walk zones / filter by audit window.
3. Schema-strip `meta.db` clone; apply regex backstop to free-text fields.
4. Write content-addressed docs into `docs/<aa>/<sha>`.
5. Write embedding shards into `embeddings/<zone>/`.
6. Sanitize and write `policy.yaml`.
7. (Audit only) export activity events into `activity/`.
8. Compute per-file SHA256, build Merkle tree, fill manifest.
9. Sign manifest+root with ed25519 → `signatures.json`.
10. Tar + zstd into `<output>.nxsnap` via streaming writer.

### Verify
1. Open archive header-only, locate manifest at offset 0.
2. Parse manifest; check `format_version`, `min_nexus_version`.
3. Verify ed25519 signature against embedded pubkey.
4. Walk file entries, compare each entry's SHA256 against manifest.
5. Recompute Merkle root from file list, compare with manifest.
6. Optional: TOFU trust check against `~/.nexus/trusted_signers.json`.

### Restore
1. Verify (above).
2. Refuse if target nexus already has zones (use `--force` to overwrite; default is fail-closed to prevent accidental clobbering).
3. Parse `placeholders[]`; apply `--inject KEY=VALUE` flags.
4. Abort if any `${…}` placeholder remains.
5. Check `embedding_dim` against current configured embedder; abort or branch to rebuild.
6. Restore `meta.db` into target backend (SQLite or Postgres — abstraction at the storage layer).
7. Restore content-addressed docs.
8. Restore embedding shards (or trigger re-embed pass).
9. Restore policy.
10. Restart services; print federation re-pair list if applicable.

## Errors

All in `errors.py`, each with a stable error code for CLI exit + structured log:

| Class | Code | When |
|---|---|---|
| `SnapshotSignatureError` | 10 | ed25519 sig mismatch |
| `SnapshotMerkleMismatch` | 11 | Merkle root differs |
| `SnapshotFileHashMismatch` | 12 | Per-file SHA differs |
| `SnapshotVersionIncompatible` | 13 | `min_nexus_version` > current |
| `SnapshotPlaceholderNotInjected` | 20 | restore guard tripped |
| `SnapshotEmbeddingDimMismatch` | 21 | dim differs and `--rebuild-embeddings` not set |
| `SnapshotCredentialLeakDetected` | 30 | regex backstop matched during create (warning, not fatal) |
| `SnapshotUntrustedSigner` | 40 | `--require-trusted` and signer unseen |

## Testing strategy

### Unit
- Format serializer round-trip (write → read → equal).
- Manifest schema validation (pydantic) — required fields, version coercion.
- Credential-strip regex matrix: positive matches for every default pattern, negative cases for false-positive-prone strings.
- Merkle hash correctness against a known fixture.
- Ed25519 sign/verify round-trip; tamper detection on signed payload.
- GFS retention math: given a list of `(name, ts)`, returns expected keep/prune sets across day/week/month boundaries.

### Integration
- Round-trip create → verify → restore on:
  - SQLite backend (lightweight profile).
  - Postgres backend (hub).
- Tamper tests: corrupt manifest byte → `SnapshotMerkleMismatch`; flip sig byte → `SnapshotSignatureError`; swap a doc file → `SnapshotFileHashMismatch`.
- Planted-secret fixtures: load fixture rows containing every default regex pattern, snapshot, assert archive contains zero matches.
- Placeholder fail-closed: snapshot, attempt restore without `--inject`, assert `SnapshotPlaceholderNotInjected`.
- Embedding mismatch: snapshot under model A, switch config to model B, restore → abort; with `--rebuild-embeddings` → succeed and re-embed.
- Audit window: ingest docs at staggered timestamps, snapshot with window covering subset, assert only window-matching docs present.

### E2E
- Spin docker stack via `nexus-stack.yml`; ingest a known fixture corpus; `nexus snapshot create`; tear down stack; restore on fresh stack; assert search results byte-identical given deterministic ranking inputs (fixed seed, fixed query set).
- Repeated for both lightweight and hub profiles.
- Scheduled snapshot test against `local` storage backend: bump simulated clock, assert N daily files kept, M pruned.

## Migration / rollout

- Brick is additive — no changes to existing tables, no downtime.
- New `~/.nexus/snapshot_signing_key` generated lazily on first create.
- Scheduled snapshots opt-in via config; default is no schedule.
- Documentation: `docs/operations/snapshots.md` for operator guide, plus `CLI.md` updates.

## Open questions

None at design time — all scope decisions captured above.
