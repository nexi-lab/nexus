# Issue #4133 — Core filesystem, metadata, streaming, and batch RPC/CLI story

**Issue**: [nexi-lab/nexus#4133](https://github.com/nexi-lab/nexus/issues/4133)
**Epic**: [#4121 — full profile CLI/RPC user story, tests, and benchmarks](https://github.com/nexi-lab/nexus/issues/4121)
**Story group**: Core filesystem, metadata, streaming, and batch operations
**Sibling precedent**: [#4132](https://github.com/nexi-lab/nexus/issues/4132) (closed) —
`docs/superpowers/specs/2026-05-18-issue-4132-full-profile-design.md`

## Problem

A full-profile user wants the complete file API documented and tested across
CLI, gRPC, and SDK paths: core syscalls, typed gRPC content ops, generic
`Call`, batch read/write/delete/rename/stat, stream/range, ETag/content ID,
locks, and error behavior under auth/permissions. Today this knowledge is
spread across source (`_kernel_syscall_dispatch.py`, `nexus_fs_content.py`,
`nexus_fs_metadata.py`, `vfs.proto`) with no coherent user workflow, no
CLI/RPC parity tests for the core methods, and several RPC methods that have
no CLI equivalent at all (a missing-surface gap, not a docs-only problem).

## Non-goals

- Sibling feature surfaces (#4134–#4138): ReBAC/sharing, search, MCP, agents,
  admin. This story is filesystem/metadata/streaming/batch/locks only.
- `cloud` profile, federation, multi-tenant behavior.
- Windows. CI covers Linux + macOS only (matches #4132 / #3778).
- A new shared test-harness package. Reuse #4132's profile-agnostic boot
  fixture / `tests/testkit/profiles.py`; no epic-wide scaffolding.
- Root-fixing `nexus up`'s zoekt health-gate defect (Bug B in #4132 spec) —
  out of scope, already recorded for separate follow-up.

## Decisions (from brainstorming)

| # | Question | Decision |
|---|----------|----------|
| Q1 | Missing-surface gate | **Build ALL gaps** directly on this branch with tests (matches #4132 expanded-scope precedent). Every RPC lacking a CLI gets one. No FS build issues filed because none are left untracked. |
| Q2 | Doc shape | **Hybrid**: new narrative section in `docs/guides/user-guide.md` + an appended FS-surface reference section in `docs/deployment/full-profile.md`. Consistent with #4132. |
| Q3 | Benchmark posture | Extend `tests/benchmarks/bench_read_write_overhead.py` with typed-vs-generic, read/write/list/stat, read_range, batch, lock acquire/release. Results recorded as **guidance ranges**, not a CI perf gate (flaky). Matches #4132. |
| Q4 | Test depth | Always-on unit/parity tests (no Docker) for CLI ↔ typed gRPC ↔ generic Call ↔ syscall parity; one gated real-stack E2E behind `NEXUS_E2E=1` (`@pytest.mark.integration`). Matches #4132 / #3778. |

## Source anchors

- `src/nexus/server/_kernel_syscall_dispatch.py` — kernel syscall dispatch
- `src/nexus/core/nexus_fs_content.py` — content `@rpc_expose` ops
- `src/nexus/core/nexus_fs_metadata.py` — metadata `@rpc_expose` ops
- `src/nexus/core/nexus_fs.py` — lock ops (`sys_lock`/`sys_unlock`/`lock_acquire`/`release_lock`)
- `proto/nexus/grpc/vfs/vfs.proto`, `src/nexus/grpc/vfs/vfs_pb2_grpc.py` — typed gRPC
- `src/nexus/remote/rpc_transport.py` — gRPC client transport
- `src/nexus/server/api/core/rpc.py` — deprecated HTTP `/api/nfs/{method}` (sunset 2026-06-25, #1133)
- `src/nexus/cli/commands/file_ops.py`, `directory.py`, `locks.py` — CLI surface
- `tests/benchmarks/bench_read_write_overhead.py` — benchmark anchor (#3710)
- Precedent: `docs/deployment/full-profile.md`,
  `docs/superpowers/specs/2026-05-18-issue-4132-full-profile-design.md`

## Ground-truth surface inventory (verified against source)

**Kernel syscalls** (`_kernel_syscall_dispatch.py` `KERNEL_SYSCALL_NAMES`,
lines 44–71; dispatch `dispatch_kernel_syscall()` line 356): `read`/`sys_read`,
`write`/`sys_write`, `delete`/`sys_unlink`, `rename`/`sys_rename`,
`stat`/`sys_stat`, `mkdir`/`sys_mkdir`, `rmdir`/`sys_rmdir`,
`list`/`sys_readdir`, `is_directory`, `access`/`exists`, `sys_copy`,
`sys_setattr`, `sys_lock`/`lock_acquire`, `sys_unlock`. Write syscalls route
through `_occ_write_dispatch()` (OCC / If-Match). Mutation syscalls fire
pub/sub events (`file_write`/`file_delete`/`file_rename`/`dir_create`/`dir_delete`).

**Typed gRPC** (`vfs.proto` service `NexusVFSService`): `Call` (generic JSON
dispatch, line 20), `Read` (line 23, native bytes + content_id + gen),
`Write` (line 24, If-Match via content_id), `Delete` (line 25, recursive),
`Ping` (line 28), `BatchRead` (line 32, #4058 vectored read). Client transport
methods in `rpc_transport.py`: `call_rpc` (229), `read_file` (302),
`write_file` (332), `delete_file` (365), `ping` (392). No client-side
`batch_read()` wrapper yet.

**`@rpc_expose` content** (`nexus_fs_content.py`): `read_bulk` (220),
`read_range` (393), `stream` (458), `stream_range` (506), `write_stream`
(548), `append` (1043), `edit` (1164), `write_batch` (1408), `read_batch`
(1570). **Metadata** (`nexus_fs_metadata.py`): `stat` (1096), `stat_bulk`
(1184), `exists_batch` (1316), `metadata_batch` (1326), `delete_batch`
(1421), `rename_batch` (1560), `backfill_directory_index` (1896, admin_only),
`flush_write_observer` (1926, admin_only). **Locks** (`nexus_fs.py`):
`sys_lock` (450), `sys_unlock` (479), `lock_acquire` (1070), `release_lock`
(1094).

**Existing CLI** (`file_ops.py`): `cat` (115), `write` (393), `append` (497),
`write-batch` (588), `cp`/`copy` (742/788), `move` (887), `sync` (951), `rm`
(1050), `edit` (1120). **Directory** (`directory.py`): `ls` (68), `mkdir`
(235), `rmdir` (287), `tree` (338). **Locks** (`locks.py`): `lock list` (38),
`lock info` (97), `lock release` (147).

**Deprecated HTTP**: `POST /api/nfs/{method}` (`server/api/core/rpc.py:45`) —
routes to the same dispatch as gRPC `Call`; deprecated, sunset **2026-06-25**,
Issue #1133. Documented migration-only.

## Missing-surface gate — build ALL gaps on this branch

RPC methods with no CLI equivalent become CLI commands here, each with tests.
After this story `api-rpc-surface-gaps.yaml` gets **no new FS rows** and the
coverage matrix `full` column = covered for every FS/metadata/stream/batch/lock
operation. No FS build issues are filed because none remain untracked.

| New/changed CLI | Maps to RPC | Shape |
|-----------------|-------------|-------|
| `nexus stat PATH [--json]` | `stat` | dedicated metadata-only (today only `cat --metadata`); JSON = `{path, content_id, version/gen, size, is_directory, mtime, ...}` |
| `nexus stat PATH... --batch [--json]` | `stat_bulk` | multi-path; one round-trip; per-path result or error |
| `nexus metadata PATH... [--json]` | `metadata_batch` | extended metadata batch; spec pins distinction vs `stat_bulk` (stat_bulk = core stat fields; metadata_batch = full metadata incl. custom attrs) |
| `nexus cat --offset N --length M` | `read_range` | new options on existing `cat`; bytes `[N, N+M)` |
| `nexus cat --stream [--chunk-size]` | `stream` / `stream_range` | explicit chunked read (with `--offset/--length` → `stream_range`) |
| `nexus write --stream` / stdin chunking | `write_stream` | streaming write from stdin in chunks |
| `nexus read-bulk PATH...` | `read_bulk` / `read_batch` | atomic multi-read; `--json` emits map path→content; `--atomic` selects `read_batch` |
| `nexus exists PATH...` | `exists_batch` | exit 0 if all exist (single path) / `--json` map for batch |
| `nexus rename-batch SRC:DST...` | `rename_batch` | atomic multi-rename; pairs as `src:dst` or `--from/--to` repeated |
| `nexus rm PATH... --batch` | `delete_batch` | verify existing `rm` first; add `--batch` only if `rm` isn't already multi-path atomic |
| `nexus admin fs backfill-index PATH` | `backfill_directory_index` | admin-gated; denied for non-admin with clear error |
| `nexus admin fs flush-write-observer` | `flush_write_observer` | admin-gated |

CLI naming follows existing conventions: flat verbs for single ops
(`stat`, `exists`), explicit batch via `--batch` or a `-batch` suffix command
where a flat verb would be ambiguous (`rename-batch`, existing `write-batch`),
admin maintenance under a new `nexus admin fs` group. `cp`/`copy`/`move`
already exist — not re-added. `rm` parity with `delete_batch` verified before
adding `--batch` (avoid duplicate surface).

## Deliverables

| Artifact | Path | Purpose |
|----------|------|---------|
| User narrative | `docs/guides/user-guide.md` (new section, after §4/§5 area, linked from full-profile.md) | lifecycle → batch → stream/range → locks; CLI + SDK/gRPC examples; success/denied/unavailable; one runnable correctness check |
| FS reference | `docs/deployment/full-profile.md` (appended FS-surface section) | typed-vs-generic surface table, batch/range/lock contract, ETag/content_id/gen semantics, deprecated HTTP `/api/nfs/{method}` migration-only note, benchmark guidance ranges |
| New CLI | `src/nexus/cli/commands/file_ops.py`, `directory.py`, new `admin fs` group, new batch cmds | every gap command in the table above |
| Parity tests | `tests/unit/cli/test_fs_parity.py` (+ targeted unit modules) | CLI ↔ typed gRPC ↔ generic `Call` ↔ syscall byte/metadata parity for every core method; positive + denied + profile-gating |
| Gated E2E | `tests/integration/test_full_profile_fs.py` (`@pytest.mark.integration`, skip unless `NEXUS_E2E=1`) | real FULL-stack lifecycle/batch/stream/range/lock end-to-end; reuses #4132 boot fixture |
| Benchmarks | extend `tests/benchmarks/bench_read_write_overhead.py` | typed-vs-generic, read/write/list/stat, read_range, batch, lock acquire/release — guidance ranges, no CI gate |
| Matrix rows | `docs/architecture/api-rpc-surface-coverage.yaml` | `full` = covered for all FS/metadata/stream/batch/lock operations |

## Correctness assertions (guide states, tests prove)

1. **Round-trip identity**: `write(p, b)` then `stat(p)` then `read(p)`
   returns byte-identical `b`; `content_id`/`gen` from `write` == from `stat`
   == from `read`.
2. **Range correctness**: `read_range(p, off, len)` ==
   `read(p)[off:off+len]`; out-of-range returns documented bounded result,
   not a crash.
3. **Batch atomicity & equivalence**: `read_bulk`/`read_batch` over N paths
   == N single reads (same bytes, same ids); `write_batch` is all-or-nothing
   (a failing member rolls back / reports per-item error per actual
   semantics — pinned by test); `rename_batch`/`delete_batch` likewise.
4. **Lock semantics**: `sys_lock`/`lock_acquire` returns a lock id; a second
   acquirer is refused/blocked per actual contention semantics;
   `sys_unlock`/`release_lock` releases; `nexus lock info` reflects state.
5. **Cross-path parity**: typed gRPC `Read` == generic `Call("read", …)` ==
   CLI `nexus cat` == kernel syscall — byte-identical, same `content_id`/`gen`.
6. **Deprecated-path parity**: `POST /api/nfs/read` returns the same result
   as gRPC `Call("read")` (documented as migration-only; behavior asserted so
   the migration note is trustworthy).
7. **Auth/permission denial**: unauthenticated request → 401; authenticated
   but unpermitted → documented denial (not a traceback); admin-only
   (`backfill_directory_index`, `flush_write_observer`) denied for non-admin.
8. **ETag / If-Match (OCC)**: `write` with stale `content_id` is rejected
   with the documented conflict; with matching id succeeds.

## Benchmark classification

| Path | Class | Treatment |
|------|-------|-----------|
| Typed vs generic RPC latency | hot path | benchmarked; guidance range in `full-profile.md` |
| read / write / list / stat (single) | hot path | benchmarked; guidance range |
| `read_range` | hot path | benchmarked |
| batch read/write/stat (vs N singles) | hot path | benchmarked |
| lock acquire/release | control plane | benchmarked with generous bounds |
| `backfill_directory_index`, `flush_write_observer` | not performance-sensitive | classified, not benchmarked |

No CI perf gate (flaky on shared runners); results recorded as ranges in the
reference doc, matching #4132's setup/control-plane treatment.

## Test strategy

- **Always-on (CI, no Docker)** — `test_fs_parity.py` + targeted unit
  modules: every core method exercised through CLI, typed gRPC, generic
  `Call`, and kernel syscall, asserting byte/metadata parity; positive flow,
  denied flow (auth/permission/admin-only), profile-gating, ETag/OCC, batch
  atomicity, range bounds, lock contention. Pure and fast (in-process /
  fake-backend testkit).
- **Gated E2E (`NEXUS_E2E=1`)** — one module boots the FULL stack (reuses
  #4132's profile-agnostic boot fixture / `tests/testkit/profiles.py`),
  exercises the full lifecycle + batch + stream/range + locks against a real
  server, tears down. Skips with a precise environmental diagnosis when
  Docker images aren't pullable (same posture as #4132).
- No new harness package.

## Acceptance-criteria mapping

| Issue criterion | Satisfied by |
|-----------------|--------------|
| Guide has full lifecycle, batch, stream/range, lock examples | `user-guide.md` narrative section + `full-profile.md` FS reference |
| Tests cover CLI/RPC parity for every core method | `test_fs_parity.py` + gated E2E |
| Deprecated HTTP endpoint documented as migration-only | `full-profile.md` note + assertion #6 (deprecated-path parity test) |
| Missing CLI for a supported RPC filed as build issue | **Superseded — built directly** (Q1): all gap commands implemented with tests; no untracked FS gap remains, so none filed |
| Benchmarks exist for hot path groups | extended `bench_read_write_overhead.py` (typed-vs-generic, read/write/list/stat, range, batch, lock) |
| Coverage matrix assigns every FS/metadata/stream/batch/lock op | `api-rpc-surface-coverage.yaml` `full` rows = covered |

## Risks

- **Scope (build ALL gaps)**: ~11 CLI commands/option-sets is the largest
  lever in this story. Mitigation: each is a thin CLI wrapper over an
  existing, already-tested `@rpc_expose`/syscall — the work is wiring +
  parity tests, not new core logic. Sequence by dependency in the plan;
  commands land incrementally with their tests.
- **stat_bulk vs metadata_batch ambiguity**: distinct RPCs with overlapping
  intent. The plan must read both implementations and pin the exact field
  difference before naming `nexus stat --batch` vs `nexus metadata`; the
  spec's stated distinction (core stat fields vs full metadata incl. custom
  attrs) is a hypothesis to verify, not a settled fact.
- **`rm` / `delete_batch` duplicate surface**: verify whether existing `rm`
  is already multi-path atomic before adding `--batch`; avoid two surfaces
  for one RPC.
- **write_batch atomicity semantics**: all-or-nothing vs per-item error is
  asserted by reading the implementation, not assumed; the guide states
  whatever the code actually does.
- **E2E flakiness / Docker pull** on shared hosts → gated + skip-with-
  diagnosis, inherited from #4132.
- **Deprecated-path test couples to a sunsetting endpoint** (2026-06-25):
  acceptable — the assertion's purpose is to make the migration note
  trustworthy until sunset; mark it clearly so it's removed at sunset.
