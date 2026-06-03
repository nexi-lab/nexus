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

Decision: **hybrid**. An in-process deterministic parity matrix carries the bulk of the
proof; one full Docker-stack smoke proves the real end-to-end search path where the
search daemon and Rust-served S3 actually run.

### PR1 — In-process parity matrix (deterministic, no Docker)

A single shared, parametrized harness boots **one** `NexusFS` kernel with **two mounts**:

- `/local/<name>` → `PathLocalBackend` (the control)
- `/s3/<name>` → `PathS3Backend` under `moto`'s `mock_aws`

Every parity test runs the **identical** operation against both mount points and asserts
byte- and metadata-identical results. The storage backend is the only independent
variable. Rationale for moto (not MinIO) here: the acceptance criterion is **correctness /
byte-parity**, which moto faithfully reproduces; wire-fidelity (real `endpoint_url`,
multipart, HTTP Range) is covered by the PR2 MinIO smoke, and efficiency is explicitly
out of scope.

Boot pattern (mirrors existing `tests/unit/fs/test_s3_integration.py`):

```python
metastore = create_kernel(str(tmp_path / "metadata.db"))
kernel = NexusFS(metadata_store=metastore, permissions=PermissionConfig(enforce=...))
kernel._init_cred = OperationContext(user_id=..., zone_id=ROOT_ZONE_ID, is_admin=True)
kernel.sys_setattr("/local/x", entry_type=DT_MOUNT, backend=PathLocalBackend(...))
kernel.sys_setattr("/s3/x",    entry_type=DT_MOUNT, backend=PathS3Backend(bucket_name=..., prefix=""))
```

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

| Issue acceptance criterion | Where proven |
|---|---|
| Gated suite (`NEXUS_RUN_S3_INTEGRATION=1`) boots S3 + local control | PR1 (in-process) + PR2 (full stack) |
| ReBAC allow/deny identical on both | PR1 `test_rebac_parity` (fallback PR2) |
| All VFS ops identical on both | PR1 `test_vfs_parity` |
| Written file retrievable via semantic search (op-log → index) on S3 | PR2 `test_s3_fullstack_parity` |
| Batch read/write parity; `read_bulk` question resolved + documented | PR1 `test_batch_parity` (+ follow-up issue) |
| Range read correctness on S3 | PR1 `test_range_parity` + PR2 wire |
| Divergence documented as (a) acceptable or (b) bug w/ follow-up | §6 + follow-up issue |

## 9. Test strategy

- Gating: module-level `pytest.mark.skipif(os.environ.get("NEXUS_RUN_S3_INTEGRATION") != "1")`
  plus `pytest.importorskip("moto")` / `importorskip("boto3")` (graceful skip; moto is not
  a pinned dependency).
- Determinism: in-process matrix is fully deterministic (moto + SQLite + `tmp_path`).
- Async handling: PR2 search assertion uses a bounded poll loop with an explicit timeout
  and a descriptive failure on timeout.
- Isolation: follow the existing `isolate_storage_tests`-style env hygiene to avoid
  cross-test pollution.
