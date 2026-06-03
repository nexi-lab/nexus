# Design: Full-stack S3-vs-local parity verification (#4267)

**Issue:** [#4267](https://github.com/nexi-lab/nexus/issues/4267) — Part of epic [#4259](https://github.com/nexi-lab/nexus/issues/4259)
**Date:** 2026-06-02
**Status:** Approved (brainstorming → spec)

## 1. Goal & framing

Turn "S3-compatible storage is on par with local" from an *architectural claim* into a
*proven* one. No existing test exercises the full stack against object storage; backend
tests cover CRUD/dedup/multipart in isolation only.

The architectural insight (from the issue and epic #4259): ReBAC, VFS semantics, and
semantic search are all enforced **above** the backend boundary, so parity is expected
*by construction*. This suite **observes** it end to end. Every divergence is classified
as either:

- **(a) documented-acceptable** — performance or storage-internals differences, or
- **(b) a bug** — captured with a follow-up issue.

## 2. Dependencies (all landed)

| Issue | What | State |
|------|------|-------|
| #4260 | Phase 0: connector-vs-CAS decision (connector path chosen) | CLOSED 2026-05-28 |
| #4262 | bridge-2: backend params over gRPC + Rust-side mount construction | CLOSED 2026-05-31 |
| #4263 | bridge-3: nexusd-cluster startup/config mount for S3 backends | CLOSED 2026-06-01 |
| #4265 | cas-2: S3CasTransport + Rust CasS3Backend | CLOSED 2026-06-02 |
| #4266 | Python S3 parity: `S3Transport.endpoint_url` + `cas_s3` essentials | CLOSED 2026-06-02 |

`cas_s3` (dedup) remains deferred per the #4260 decision — the **connector** path
(`path_s3` / `PathS3Backend`) is primary. Consequently **GC / reachability sweep is out
of scope** for this suite (it only applies to a CAS backend).

## 3. Architecture — two layers, staged across two PRs

Decision: **hybrid**. An in-process kernel + MinIO parity matrix carries the bulk of the
proof; one full Docker-stack smoke proves the real end-to-end search path where the
search daemon also runs. (Both layers use a real S3 wire via MinIO — see the PIVOT note
below; the original moto plan was infeasible.)

### PIVOT (2026-06-02): moto → MinIO + `driver-s3`

The original PR1 plan backed the S3 side with `moto` (`mock_aws`). **That does not work**, discovered by actually running it: the in-process `NexusFS` spawns the Rust `nexusd-cluster` kernel (`KernelClient`), and S3 I/O is served **Rust-side** (`core/nexus_fs_metadata.py::_extract_rust_backend_params` → backend_type `"s3"` → Rust `S3Transport`). Python's `moto` only intercepts Python `boto3`, so it never sees the Rust client's calls. Two consequences:

1. The cluster binary must be built with the S3 driver: `cargo build -p nexus-cluster --bin nexusd-cluster --features backends/driver-s3`. The default build only has `driver-path-local` + `driver-remote`; a `path_s3` mount otherwise fails at runtime with `ObjectStoreProvider failed to build 's3' backend: unknown backend_type: 's3'`.
2. Tests need a **real S3 endpoint** the Rust client can reach → **MinIO** (`minio/minio`). Validated working: `PathS3Backend(bucket_name, prefix=..., endpoint_url="http://localhost:9100", access_key_id="minioadmin", secret_access_key="minioadmin", region_name="us-east-1")` → Rust `driver-s3` → MinIO; the blob physically lands in the bucket.

The pre-existing `tests/unit/fs/test_s3_integration.py` (moto + `PathS3Backend`) has only ever "passed" by **skipping** (no `nexusd-cluster` on PATH); with the binary built it errors identically. A follow-up should fix or re-gate it.

This is a *stronger* proof than moto would have been (real Rust S3 wire). Cost: PR1 now needs a MinIO container + a `driver-s3` build, so it is no longer "no Docker" — but it is still far lighter than the full `nexus up` stack (no server, no search daemon).

### PR1 — In-process kernel + MinIO parity matrix

A single shared harness boots **one** `NexusFS` kernel with **two mounts**:

- `/local/data` → `PathLocalBackend` (the control, Rust `driver-path-local`)
- `/s3/data` → `PathS3Backend` pointed at **MinIO** (Rust `driver-s3`)

Every parity test runs the **identical** operation against both mount points and asserts
byte- and metadata-identical results. The storage backend is the only independent variable.

Boot pattern (validated):

```python
k = KernelClient(); k.set_metastore_path(str(tmp_path / "meta.redb")); k.open()  # spawns nexusd-cluster
kernel = NexusFS(metadata_store=k, permissions=PermissionConfig(enforce=...))
kernel._init_cred = OperationContext(user_id=..., zone_id=ROOT_ZONE_ID, is_admin=True)
kernel.sys_setattr("/local/data", entry_type=DT_MOUNT, backend=PathLocalBackend(root_path=...))
kernel.sys_setattr("/s3/data", entry_type=DT_MOUNT, backend=PathS3Backend(
    bucket_name=..., prefix=f"parity/{tmp_path.name}",  # unique prefix per test (MinIO persists)
    endpoint_url=S3_ENDPOINT, access_key_id=..., secret_access_key=..., region_name="us-east-1"))
```

Gating: skip the whole suite if MinIO is unreachable, and skip with a "rebuild with
`--features backends/driver-s3`" message if the S3 mount reports `unknown backend_type`.
No env *flag* gate — it runs whenever the infra (MinIO + driver-s3 binary) is present, so
CI that provides both runs it continuously.

Covers: ReBAC allow/deny · all VFS ops · batch read/write · range read · the `read_bulk`
resolution.

### PR2 — Full-stack MinIO smoke (the "real" proof)

`nexus up` with a **MinIO** service and the Rust `driver-s3` feature enabled, with storage
exercised through an **S3-backed mount** (see §7, risk 2). One end-to-end path:

> write a file under the S3 mount → confirm it lands in `operation_log` → poll until the
> search daemon indexes it → `semantic_search` returns it — all **ReBAC-gated**.

Plus range-read correctness over the real wire. Runs as a **new CI job** gated on
`NEXUS_RUN_S3_INTEGRATION=1` **and** `NEXUS_E2E=1`.

## 4. Components

### PR1

- `tests/integration/s3_parity/conftest.py`
  - `parity_kernel` fixture: `mock_aws`, SQLite metastore, `NexusFS`, both backends mounted.
  - `ParityHarness` helper exposing `run_on_both(op) -> (local_result, s3_result)` and
    `assert_parity(op)` — keeps each test a single operation expressed once.
- `tests/integration/s3_parity/test_vfs_parity.py`
  - read / write / delete / mkdir / rmdir / rename / stat / list — each asserted identical
    across backends. Metadata (size, mtime semantics) compared with documented-acceptable
    exclusions (see §6).
- `tests/integration/s3_parity/test_rebac_parity.py`
  - `PermissionConfig(enforce=True)`. Grant `viewer` on a path-backed file via
    `rebac_create_sync` → read succeeds; deny → read raises identical `PermissionError`.
    Asserted identical on both backends (enforcement is backend-agnostic).
  - *Fallback:* if in-process enforcement proves unwired (see §7, risk 1), this test moves
    to PR2.
- `tests/integration/s3_parity/test_batch_parity.py`
  - `read_bulk` / `write_batch` results identical across backends.
  - **Documentation assertion** (resolves the open question): `read_bulk` calls `sys_read`
    per file and **never** invokes `backend.batch_read_content`. Confirmed in
    `src/nexus/core/nexus_fs_content.py` — the small-batch path (≤4 paths) loops
    `sys_read`, and the large-batch path batches only the *permission check* and *metadata*
    lookups, then still loops `sys_read` per file. **Resolution:** this is a
    *performance* gap, not a correctness gap (results are identical), so it is
    documented-acceptable **and** a follow-up issue will be filed to wire
    `backend.batch_read_content` into `read_bulk`.
- `tests/integration/s3_parity/test_range_parity.py`
  - Large file; `read_range(start, end)` correctness identical across backends. The S3
    HTTP-Range efficiency gap (#4266) is **out of scope** — noted, not asserted.

### PR2

- MinIO service in a test compose overlay; `driver-s3` build flag; S3-backed mount config.
- `tests/e2e/self_contained/test_s3_fullstack_parity.py` — gated smoke:
  search-over-an-S3-file-gated-by-ReBAC, with **bounded polling** for async indexing
  (explicit timeout + clear failure message; never `sleep`-and-hope).
- CI job wiring (new gated job).

## 5. Data flow (PR2 search path)

```
write(/s3/.../doc.txt, bytes)  [ReBAC write check]
        │
        ▼
operation_log row (operation_type='write', status='success', sequence_number=N)
        │  (search daemon polls _fetch_operation_log_events(after_seq))
        ▼
SearchMutationEvent (upsert) → indexing pipeline
        │  (eventual; test polls with timeout)
        ▼
semantic_search(query, path)  [ReBAC read filtering] → result includes doc.txt
```

## 6. Out of scope — assert these are the ONLY differences

- Performance (network RTT vs local mmap) — not a correctness gap.
- Volume packing / CDC / cold tiering / Rust mmap fast-path — local-only storage internals.
- Dedup — applies only to `cas_s3`, not `path_s3` (connector).
- GC / reachability sweep — CAS-only; connector path was chosen, so not applicable.
- `read_bulk` sequential-vs-batch — performance gap, documented + follow-up issue (§4).

## 7. Open risks to resolve in implementation

1. **In-process ReBAC enforcement.** Verify `PermissionConfig(enforce=True)` actually runs
   the permission hooks in the bare kernel and that `rebac_create_sync` tuples are honored
   in-process. No existing in-process test enables enforcement (the existing S3 test runs
   `enforce=False`). **Fallback:** prove ReBAC allow/deny in the PR2 full-stack smoke, where
   the rebac brick definitely runs.
2. **Storage-root-on-S3 vs mount-on-S3.** The issue says "storage root on an S3 backend,"
   but the realistic mechanism is an **S3-backed mount**, not literally relocating the
   kernel root. The plan will pin the exact mechanism during PR2; functionally it proves
   the same path (full stack writing/reading/searching over S3-backed storage).

## 8. Acceptance criteria mapping

| Issue acceptance criterion | Status / where proven |
|---|---|
| Gated suite boots S3 + local control | ✅ PR1 in-process (kernel + MinIO); literal full-`nexus up` deferred to #4308 |
| ReBAC allow/deny identical on both | ✅ PR1 `test_rebac_parity` |
| All VFS ops identical on both | ✅ PR1 `test_vfs_parity` (rename = documented divergence #4306) |
| Written file retrievable via semantic search on S3 | ✅ PR1 `test_search_parity` (in-process, substantive); op-log→daemon e2e in #4308 |
| Batch read/write parity; `read_bulk` question resolved + documented | ✅ PR1 `test_batch_parity` (+ #4307) |
| Range read correctness on S3 | ✅ PR1 `test_range_parity` |
| Divergence documented as (a) acceptable or (b) bug w/ follow-up | ✅ §6, §10 — #4306 (rename), #4307 (read_bulk), mtime acceptable |

## 9. Test strategy

- Gating (PR1): skip the suite if MinIO is unreachable (probe via `boto3` against
  `NEXUS_TEST_S3_ENDPOINT`, default `http://localhost:9100`); skip with a rebuild hint if
  the S3 mount reports `unknown backend_type` (binary lacks `driver-s3`). No env *flag*
  gate — runs whenever infra is present. PR2's full-stack smoke keeps the
  `NEXUS_RUN_S3_INTEGRATION=1` + `NEXUS_E2E=1` gate (needs Docker).
- Isolation: local control under `tmp_path`; S3 side under a unique per-test bucket prefix
  (`parity/<tmp_path.name>`) because MinIO persists across tests (unlike a fresh moto mock).
- Async handling: PR2 search assertion uses a bounded poll loop with an explicit timeout
  and a descriptive failure on timeout.

## 10. PR1 results (2026-06-02)

PR1 landed as `tests/integration/s3_parity/` (harness + 5 test modules). Full suite:
**20 passed, 0 xfailed, 0 skipped** against real MinIO + Rust `driver-s3`. The fixes below
were also validated end-to-end through the real `nexus up` stack (driver-s3 image + OpenAI
embeddings): S3 mount + VFS + rename + read_bulk + semantic-search-over-S3 all pass.

Findings:

- **VFS parity (write/read/stat/delete/mkdir/rmdir/list/copy):** identical on both backends.
  `stat` compares `{size, is_directory}` only — mtime excluded (local FS mtime vs S3
  LastModified differ by construction; documented-acceptable).
- **rename — was a divergence, now FIXED ([#4306](https://github.com/nexi-lab/nexus/issues/4306) closed).**
  The Rust `driver-s3` `ObjectStore::rename` default returned `NotSupported`; implemented it
  as an S3 server-side copy (`x-amz-copy-source`) + delete. Test flipped from xfail to a
  passing parity assertion (read-after-rename returns content; source removed). Validated
  in-process and full-stack.
- **range read:** `read_range` and `sys_read(offset,count)` byte-identical on both backends.
- **batch + read_bulk — resolved then FIXED ([#4307](https://github.com/nexi-lab/nexus/issues/4307) closed).**
  Originally `read_bulk` looped `sys_read` per file (N sequential RPCs). Now its large-batch
  path issues one parallel `kernel.read_batch` (rayon fan-out) for all paths — the Rust
  kernel services S3 connector mounts in that batch too, so bulk reads over S3 are a single
  RPC. Per-item `sys_read` fallback preserves connector/pipe/not-found behaviour. Validated:
  s3_parity 20 passed + CLI↔RPC↔kernel parity 31 passed.
- **connector re-mount idempotency — FIXED.** Re-mounting an existing point made the kernel
  cancel the gRPC stream (`RST_STREAM`); the mount route now returns a clean "already
  mounted at X".
- **ReBAC allow/deny — stayed in PR1** (not deferred to PR2). The spike confirmed bare
  `NexusFS(enforce=True)` does NOT auto-wire enforcement, so the `parity_kernel_enforced`
  fixture manually wires the real classes (`PermissionEnforcer` → `PermissionChecker` →
  `RebacPermissionCheckHook`, installed at `/__sys__/services/permission`) over an in-memory
  `ReBACManager` — the same seam unit tests use. Deny-without-grant and allow-after-grant are
  identical on both backends; the enforcer keys on `("file", <virtual_path>)` regardless of
  backend, directly observing the issue's "enforcement above the backend boundary" claim.
  Grants use relation `direct_viewer` (plain `viewer` is a namespace-union alias). ReBAC is
  also exercised more faithfully end-to-end in PR2's full server stack.
- **Semantic search over S3 — CLOSED in-process** (`test_search_parity.py`). The
  in-process kernel can't run the async search daemon, but the *substantive* criterion
  ("a written file is retrievable via semantic search, same as local") is proven: the
  production indexing file-reader (`_NexusFSFileReader`) reads S3-backed content
  byte-identical to local, and that content — indexed into the production SANDBOX vector
  backend (`SqliteVecBackend` + offline `fastembed`) — is retrieved by a *semantic* query
  with no lexical overlap, identically for both backends. The literal op-log → daemon →
  index mechanism is **backend-agnostic** (the Rust kernel logs every write regardless of
  backend; the daemon consumes it the same way), so it is not re-proven per-backend here;
  the full-`nexus up` daemon path is tracked by [#4308](https://github.com/nexi-lab/nexus/issues/4308).
  Gated via `importorskip(fastembed, sqlite_vec)`; first run downloads a small ONNX model.
- **Full-stack `nexus up` on S3 — deferred to [#4308](https://github.com/nexi-lab/nexus/issues/4308)** (infra-blocked).
  The shipped Docker image builds the cluster binary without `--features` → no `driver-s3`,
  and enabling it fights the deliberate cluster-binary size gate. That issue covers a
  `driver-s3` test image, MinIO compose overlay, the real async search daemon end-to-end,
  and a gated CI job.
- **Pre-existing test gap:** `tests/unit/fs/test_s3_integration.py` (moto + PathS3Backend)
  only ever passed by skipping; with the binary built it errors (`unknown backend_type`).
  Worth a follow-up to fix or re-gate (not done in PR1).

Out-of-scope items confirmed untouched: dedup (cas_s3), GC, volume packing/CDC/mmap, perf.

Env to run PR1 locally: `nexusd-cluster` built `--features backends/driver-s3` on PATH +
MinIO reachable at `NEXUS_TEST_S3_ENDPOINT` (default `http://localhost:9100`); then
`uv run pytest tests/integration/s3_parity/ -n0`.
