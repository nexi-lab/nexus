# FULL deployment profile

Nexus's `full` profile is the all-feature shared hub for a team. The
`shared`/`demo` preset stack provisions **PostgreSQL + Dragonfly** (plus
the Nexus server), the complete brick set, and local inference. Keyword
search uses **BM25S**; Zoekt is an *optional, separately-run* code-search
backend the preset does **not** start (see the user guide, "What about
Zoekt?"). Use this profile for a shared node that exposes the full
CLI/RPC surface; use `sandbox` for per-agent clients that connect to it.

## Three things called "profile" (read this first)

| Term | Where | What it controls |
|---|---|---|
| Docker Compose profile (`core`, `cache`) | `nexus up` / `docker-compose.yml` | Which containers start |
| CLI connection profile | `nexus profile use <name>` (`~/.nexus/config.yaml`) | Which hub the CLI talks to |
| Deployment profile (`full`) | `nexusd --profile full` / `NEXUS_PROFILE` | Which bricks/drivers are enabled |

`nexus up` runs the FULL deployment profile because
`docker-compose.yml` sets `NEXUS_PROFILE=full`. No `nexus init` preset
is literally named `full`; the `shared` and `demo` presets both run
FULL.

## What you get

| Surface | FULL |
|---|---|
| Storage | PostgreSQL |
| Cache | Dragonfly / Redis |
| Keyword search | BM25S (Zoekt optional, not started by the preset) |
| Bricks | LITE + search, pay, llm, mcp, workspace, snapshot, versioning, identity, delegation, share_link, portability, task_manager, observability, … (see contract test) |
| Federation | OFF (that is the `cloud` profile) |
| Auth | static (`NEXUS_API_KEY`) or database (`DatabaseAPIKeyAuth`) |
| Remote clients | `profile=remote` SDK; requires gRPC, not just HTTP |

## Running

### Via the daemon directly (supported)

```bash
nexusd --profile full --host 0.0.0.0 --port 2026 \
  --data-dir ./nexus-data --auth-type static --api-key "$NEXUS_API_KEY"
```

`nexusd --profile remote` is rejected: a daemon cannot be a thin
client of another daemon.

### Via the managed stack (known issue — see below)

```bash
nexus init --preset shared
nexus up                 # ⚠ currently exits rc=1 (see note)
eval $(nexus env)
nexus status
```

> **Known issue (Bug B, tracked):** `nexus up --preset shared`
> currently returns a non-zero exit code because the `nexus up` health
> gate waits on a `zoekt` service that the `shared` preset does not
> start. **The hub itself boots and serves correctly** (`/health`,
> `/api/v2/features`, gRPC all work) — only the `nexus up` wrapper's
> aggregate exit status is wrong. This is a pre-existing `nexus up`
> health-gate defect, out of this docs/test issue's scope, tracked in
> the #4132 design spec ("Bug B"). Until it is fixed, prefer the
> **direct daemon path above**; if you use the stack, the containers
> are healthy despite the rc=1 (verify with `nexus status` / a direct
> `curl $URL/health`).

## Auth

- **static**: `--api-key` / `NEXUS_API_KEY` / `NEXUS_API_KEY_FILE`.
  Request without a key → 401; with key → 200.
- **database**: `--auth-type database` + `--database-url` (or
  `POSTGRES_URL`) → `DatabaseAPIKeyAuth`. Use for multi-user key
  issuance/revocation.

## Remote client

```python
from nexus.sdk import connect

nx = connect(config={"profile": "remote",
                     "url": "http://hub:2026",
                     "api_key": "..."})
```

Set `NEXUS_GRPC_PORT` if the server's gRPC port is non-default. The
HTTP URL alone is not sufficient.

## Correctness check you can run

The FULL contract is locked by
`tests/unit/core/test_full_profile.py`. Run:

```bash
pytest tests/unit/core/test_full_profile.py -v
```

You can also verify a *running* hub's resolved contract directly:

```bash
nexus profile contract
```

It prints JSON with a `_sources` map marking each field's provenance:

- **hub-authoritative** (from the hub's `/api/v2/features`):
  `deployment_profile`, `bricks`, `disabled_bricks`, `mode`, `version`.
- **client-inferred** (NOT hub-authoritative — derived from the hub's
  profile name via this CLI's `DeploymentProfile`; may differ under
  CLI/server version skew): `client_inferred_drivers`.
- **local/contextual**: `auth_mode` reflects the local `nexus.yaml`
  only for the locally-managed stack; for an explicit remote target
  (`--url` / `NEXUS_URL` / global `--profile`) it is `"unknown"`.
- **invariant**: `grpc_required` is always `true` (the remote SDK path
  requires gRPC, not just HTTP).

`nexus profile contract --url <hub> --api-key <key>` targets a remote
hub; `nexus --profile <name> profile contract` uses a saved connection
profile.

## Benchmark guidance

Boot time and idle RSS are setup-path metrics, not CI gates; the FULL
stack (PostgreSQL + Dragonfly + the Nexus server) targets multi-GB RSS and a
15–60 s boot. `health` / `features` / `Ping` are control-plane calls
with sub-100 ms expectations on a warm hub. There is no steady-state
data-plane hot path in the startup story.

## Troubleshooting

- Remote SDK hangs / connection refused: gRPC port unreachable — set
  `NEXUS_GRPC_PORT`, confirm `nexus status` shows gRPC healthy.
- 401 from every call: static auth with no `NEXUS_API_KEY`, or
  database auth with no issued key.

## Filesystem surface

FULL exposes the complete file API over four equivalent paths: kernel
syscalls, typed gRPC (`Read`/`Write`/`Delete`/`Ping`/`BatchRead`), generic
gRPC `Call`, and the CLI (a thin wrapper). The deprecated HTTP
`POST /api/nfs/{method}` is migration-only (sunset 2026-06-25, Issue #1133).

| Group    | RPC                                                  | CLI                                                   |
|----------|------------------------------------------------------|-------------------------------------------------------|
| Read     | `read`, `read_range`, `read_bulk`, `read_batch`      | `cat` (+`--offset/--length/--stream`), `read-bulk`    |
| Write    | `write`, `write_stream`, `write_batch`, `append`, `edit` | `write` (+`--stream`), `write-batch`, `append`, `edit` |
| Metadata | `stat`, `stat_bulk`, `metadata_batch`, `exists_batch` | `stat`, `metadata`, `exists`                          |
| Mutate   | `rename_batch`, `delete_batch`, `rename`, `delete`   | `rename-batch`, `rm-batch`, `move`, `rm`              |
| Stream   | `stream`, `stream_range`                             | `cat --stream`                                        |
| Locks    | `sys_lock`, `sys_unlock`, `lock_acquire`, `release_lock` | `lock list/info/release`                           |
| Admin    | `backfill_directory_index`, `flush_write_observer`   | `admin fs backfill-index`, `admin fs flush-write-observer` |

**Semantics that matter:**

- `read_range(start, end)` is start-inclusive, end-exclusive. End past EOF
  returns the available bytes (bounded, not an error).
- `rename_batch` / `delete_batch` / `write_batch` are **per-item
  independent** (not atomic) — the result maps each literal path to
  `{success, ...}` or `{success, error}`.
- `content_id` is stable across `write`/`stat`/`read` for identical bytes;
  use the CAS helpers in `nexus.lib.occ` to compose If-Match writes (a
  stale `content_id` is rejected).
- Admin ops (`backfill_directory_index`, `flush_write_observer`) require
  admin; non-admin callers are refused server-side.
- Stream commands (`cat --stream`, `write --stream`) honor Unix
  `SIGPIPE = SIG_DFL` — piping into `head`, `tee`, or any reader that
  closes early exits cleanly (status 141), no traceback.

**CLI ↔ RPC mapping (verified by `tests/unit/cli/test_fs_parity.py`):**

| CLI                                       | RPC method                | Parity test                          |
|-------------------------------------------|---------------------------|--------------------------------------|
| `nexus stat <path>...`                    | `stat` / `stat_bulk`      | `test_stat_single_parity`, `test_stat_multi_uses_stat_bulk` |
| `nexus metadata <path>...`                | `metadata_batch`          | `test_metadata_extended_parity`      |
| `nexus exists <path>...`                  | `exists_batch`            | `test_exists_batch_parity_and_exit`  |
| `nexus read-bulk <path>...`               | `read_bulk` / `read_batch`| `test_read_bulk_parity`, `test_read_bulk_atomic_raises_on_missing` |
| `nexus rename-batch a:b ...`              | `rename_batch`            | `test_rename_batch_per_item_independent` |
| `nexus rm-batch <path>...`                | `delete_batch`            | `test_rm_batch_per_item_independent` |
| `nexus cat --offset N --length M`         | `read_range`              | `test_cat_range_equals_slice`, `test_range_out_of_bounds_is_bounded` |
| `nexus cat --stream` / `write --stream`   | `stream` / `write_stream` | `test_cat_stream_matches_full`, `test_write_stream_from_stdin`, `test_cat_stream_survives_broken_pipe` |
| `nexus admin fs backfill-index`           | `backfill_directory_index` | `test_admin_fs_flush_and_backfill`, `test_admin_only_metadata_is_set` |
| `nexus admin fs flush-write-observer`     | `flush_write_observer`    | `test_admin_fs_flush_and_backfill`, `test_admin_only_metadata_is_set` |

**Verification status** (PR #4173) — maps to the 8 spec correctness assertions:

| # | Assertion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Round-trip byte-identity + stable content_id | ✅ | `test_inproc_fixture_roundtrips`, `test_write_roundtrips_content_id`, E2E |
| 2 | Range correctness + OOB bounded | ✅ | `test_cat_range_equals_slice`, `test_range_out_of_bounds_is_bounded` |
| 3 | Batch independence (per-item success/error) | ✅ | `test_read_bulk_*`, `test_rename_batch_*`, `test_rm_batch_*`, E2E |
| 4 | Lock semantics (second acquirer refused) | ✅ | `test_lock_contention_second_acquirer_refused` (raises `NexusError("contention")` on second acquire; first holder releases; fresh acquire returns new lid) |
| 5 | Cross-path parity (syscall == generic Call == CLI) | ✅ | `test_cross_path_parity_syscall_rpc_cli` (`sys_read` vs `dispatch_kernel_syscall("read")` vs `nexus cat` — byte-identical; same `content_id`/`size` from stat) |
| 6 | Deprecated HTTP `/api/nfs/{method}` parity | ✅ | E2E uses HTTP `/api/nfs/read|stat|read_range|read_bulk|exists_batch|metadata_batch|rename_batch|sys_lock|sys_unlock|delete_batch|backfill_directory_index|flush_write_observer` against a booted stack |
| 7 | Auth denial: 401 unauth + 403 unpermitted + admin-only | ✅ | `test_auth_denial_401_unauth_and_403_admin_only` exercises `require_auth`/`require_admin` directly; `test_admin_only_dispatch_rejects_non_admin` exercises the kernel-side gate via `dispatch_method` |
| 8 | ETag / If-Match (OCC) stale-content_id rejection | ✅ | `test_etag_if_match_occ_conflict` (`occ_write_sync` with stale `if_match` → `ConflictError`; matching id → succeeds; bytes update, version advances) |

**Additional coverage:**

| Layer                                    | Status      | Evidence                                           |
|------------------------------------------|-------------|----------------------------------------------------|
| Auth CLI parity                          | ✅ verified | 4 tests in `test_auth_cli_parity.py`               |
| Admin-only @rpc_expose metadata          | ✅ verified | `test_admin_only_metadata_is_set` (source-level)   |
| Stream broken-pipe exit                  | ✅ verified | `test_cat_stream_survives_broken_pipe` (subprocess + real pipe) |
| Smoke regression (cat / write existing)  | ✅ verified | 34 tests in `test_commands_smoke.py`               |
| Concurrent multi-thread FS stress        | ✅ verified | `test_concurrent_fs_stress`: 200 files × 4 ops × 16 threads, no errors, post-state correct |
| Benchmark medians above                  | ✅ executed | `tests/benchmarks/bench_read_write_overhead.py` with `--benchmark-min-rounds=20` |
| Over-the-wire (real Docker stack)        | ✅ verified | `test_full_profile_fs.py::test_full_fs_lifecycle_batch_range_lock` (12 RPC methods, HTTP wire, ~80s; Bug B from #4132 bypassed by `full_stack_tolerant` fixture stripping zoekt from services) |
| Large-file (>10MB) end-to-end            | ⚠️ not run  | 10 MB threshold flips `cat` to streaming; no stress harness explicitly above the boundary |

**Benchmark guidance** (dev-laptop medians on Apple Silicon, in-process
kernel; from `tests/benchmarks/bench_read_write_overhead.py`,
`--benchmark-min-rounds=20`). Numbers are reference points, not CI gates:

| Operation                       | Median   | Rounds | Class                  |
|---------------------------------|----------|--------|------------------------|
| Typed `nx.read` (1 KiB file)    | ~595 µs  | 1460   | hot path               |
| `read_range(64 KiB)` of 1 MiB   | ~3.1 ms  |  191   | hot path               |
| `stat_bulk` of 100 files        | ~1.9 ms  |  381   | hot path (≈19 µs/path) |
| `sys_lock` + `sys_unlock` cycle | ~956 µs  |  561   | control plane          |
| `backfill_directory_index`      | —        |  —     | not perf-sensitive     |
| `flush_write_observer`          | —        |  —     | not perf-sensitive     |
