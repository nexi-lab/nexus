# Issue #4060 - FUSE Passthrough For Large Reads Design

**Date**: 2026-05-12
**Issue**: [#4060](https://github.com/nexi-lab/nexus/issues/4060) - [P1] FUSE_PASSTHROUGH for large reads (Linux 6.9+)

## Context

`nexus-fuse` currently serves reads through userspace FUSE callbacks. The direct Rust mount path handles `open()` and `read()` in `nexus-fuse/src/fs.rs`; the Python `NexusFUSE(..., use_rust=True)` path still owns the FUSE session in Python and delegates hot operations to the Rust Unix-socket daemon in `nexus-fuse/src/daemon.rs` through `src/nexus/fuse/rust_client.py`.

Linux 6.9 added FUSE passthrough. A FUSE filesystem can register a real backing file with the kernel and return a backing ID in the `OPEN` reply. Subsequent reads for that handle bypass userspace FUSE and are served directly from the backing file. This only works when the process answering FUSE `open()` also negotiates passthrough support and registers the backing file.

The current `nexus-fuse` crate uses `fuser = 0.14`, which does not expose passthrough open replies. Current `fuser` releases expose passthrough through `KernelConfig` and `ReplyOpen` APIs, so this feature requires upgrading `fuser` before wiring passthrough into `NexusFs`.

## Decision

Implement the full issue scope in one PR by making the Rust process the passthrough-capable FUSE owner for both supported paths:

1. Direct `nexus-fuse mount` negotiates passthrough and returns backing IDs for eligible read-only large files.
2. Python `NexusFUSE(..., use_rust=True)` can launch a Rust-owned `nexus-fuse mount` when passthrough is enabled, instead of constructing Python FUSE callbacks plus the Rust IPC daemon.
3. The existing Rust IPC daemon remains available for non-passthrough Python acceleration and for platforms or configurations where passthrough is disabled or unsupported.

The design is conservative: passthrough is opt-in, pattern-gated, read-only, Linux-only, and falls back to the existing userspace read path whenever capability negotiation, policy checks, materialization, or backing registration fails.

## Non-Goals

1. Enabling passthrough by default.
2. Supporting passthrough on macOS or non-Linux platforms.
3. Allowing passthrough for writable opens.
4. Allowing passthrough for paths where Nexus must intercept reads for parser, hook, snapshot, workflow, or audit semantics.
5. Replacing the existing foyer read-through cache for normal userspace reads.
6. Rewriting remote/cloud storage to use signed direct download URLs in this PR.
7. Guaranteeing passthrough for every backend. The first implementation uses local materialized backing files.

## Architecture

Add a small passthrough subsystem under `nexus-fuse` with three boundaries:

- `passthrough::config`: CLI/env parsing, glob policy, thresholds, and feature enablement.
- `passthrough::policy`: kernel capability state plus per-path eligibility checks.
- `passthrough::backing`: materialized backing-file lifecycle, backing-key selection, open file descriptor ownership, and invalidation.

`NexusFs` owns an optional passthrough manager. During FUSE initialization, it asks `fuser` to enable passthrough and records whether the kernel/session accepted it. During `open()`, `NexusFs` resolves the path, checks policy, materializes a validated local backing file if needed, registers it through the upgraded `fuser` passthrough API, and replies with a backing ID. For ordinary handles it replies exactly as it does today.

The Python mount integration gets a new passthrough branch. When `use_rust=True` and passthrough is requested, Python starts `nexus-fuse mount` as the actual FUSE daemon and supervises its lifecycle. Python still owns credentials, mount orchestration, and fallback behavior. The old Python FUSE plus Rust IPC daemon path remains the fallback when passthrough is off, unsupported, or explicitly not required.

## Configuration

Passthrough is disabled unless the user enables it explicitly.

Rust CLI and environment:

```text
--passthrough / NEXUS_FUSE_PASSTHROUGH=true
--passthrough-pattern <glob> / NEXUS_FUSE_PASSTHROUGH_PATTERNS
--passthrough-threshold-bytes / NEXUS_FUSE_PASSTHROUGH_THRESHOLD_BYTES
--passthrough-require / NEXUS_FUSE_PASSTHROUGH_REQUIRE=true
```

Defaults:

- passthrough enabled: false
- patterns: empty, meaning no file is eligible
- threshold: 131072 bytes
- require mode: false

`NEXUS_FUSE_PASSTHROUGH_PATTERNS` is comma-separated. Repeated CLI patterns append to env patterns. If the final allow-pattern set is empty, no file is eligible even when `--passthrough` is set.

Python `NexusFUSE` exposes equivalent constructor and mount options, then passes them to `nexus-fuse mount` when using the Rust-owned passthrough path.

## Eligibility

A file is eligible only when all conditions are true:

1. The platform is Linux.
2. The FUSE session negotiated passthrough support successfully.
3. Passthrough is enabled in config.
4. The virtual path matches at least one configured passthrough allow pattern.
5. The path does not match a deny policy derived from hook/parser/snapshot/workflow constraints.
6. `stat` says the entry is a regular file and size is at least the threshold.
7. The open flags are read-only and do not request truncation, create, append write, or read-write access.
8. No active read/stat intercept semantics require Nexus to observe this read.
9. A local backing file can be materialized and validated against the current ETag/content identity.

The safe default is to deny passthrough. Policy code returns a structured reason for ineligibility so tests and logs can distinguish "disabled", "unsupported kernel", "pattern miss", "too small", "write open", "hook sensitive", and "materialization failed".

## Hook And Parser Semantics

The Rust FUSE process must not guess whether Python-side services need interception. Python computes a conservative passthrough policy before launching Rust:

- If read/stat intercept hooks are active globally, passthrough is disabled unless the user supplies a narrower allow-pattern set that Python can mark safe.
- If parser auto-parse, snapshot observers, workflow triggers, or audit policies require read interception for a path family, Python passes deny patterns for those path families.
- If Python cannot determine whether a path family is safe, it denies passthrough for that family.

The first PR does not rebuild a dynamic hook-policy RPC. It passes a startup policy snapshot from Python to Rust. This matches the existing mount lifecycle: hook and parser configuration is established before the mount starts. If a later feature supports dynamic hook registration after mount, it must add a policy-update channel or force passthrough off for dynamic mounts.

## Backing Files

Passthrough needs a real local file descriptor. The first implementation materializes eligible files into local immutable backing files under a cache-owned directory.

Backing key:

```text
hash(server_url, virtual_path, etag_or_content_id, size)
```

Rules:

- A backing file is opened read-only.
- The key includes ETag or content identity when available, so stale data is not reused after mutation.
- Materialization writes to a temporary file, verifies final size, then atomically renames into place.
- The manager keeps handle state for registered backing files so `release()` can close and unregister state cleanly.
- Writes, truncates, deletes, renames, and explicit cache invalidations remove affected backing entries.
- Startup may leave old backing files on disk; a best-effort cleanup can prune files not referenced by the current in-process index.

The existing foyer cache remains the userspace read cache. Backing files are a passthrough-specific representation. The implementation may share the same root directory, but should keep a separate subdirectory to avoid depending on foyer internals.

## Data Flow

### Open

1. Resolve inode to virtual path.
2. Reject directories with `EISDIR` as today.
3. Check whether passthrough is enabled, negotiated, and policy-eligible.
4. If ineligible, return a normal opened handle.
5. If eligible, materialize or reuse the local backing file.
6. Register the backing file through `fuser`.
7. Return the passthrough backing ID in the `OPEN` reply.
8. If any passthrough step fails, log the reason and return a normal opened handle unless require mode is enabled.
9. In require mode, return an error when passthrough was requested but cannot be established for an otherwise eligible file.

### Read

Normal handles continue through `NexusFs::read()` and `read_with_cache()`.

Passthrough handles do not call `NexusFs::read()`. The kernel reads from the registered backing file.

### Release

Normal handles behave as they do today.

Passthrough handles release their registered backing state. The backing file may remain on disk for reuse if its key still matches current metadata.

### Invalidation

All mutation paths that call `invalidate_path()` also invalidate passthrough backing entries:

- `write`
- `create`
- `unlink`
- `rename`
- `setattr` truncate
- daemon write/delete/rename equivalents where applicable

Rename invalidates both old and new paths.

## Python Integration

`src/nexus/fuse/mount.py` gains passthrough options. When `use_rust=True` and passthrough is enabled, mount orchestration uses a Rust-owned FUSE process:

```text
nexus-fuse mount <mount_point> --url <url> --api-key-file <file> --passthrough --passthrough-pattern ...
```

The API key should continue to avoid process-list exposure. Python should prefer `--api-key-file` or a private temporary file over `NEXUS_API_KEY` when launching long-lived mount processes.

Fallback behavior:

- If passthrough is disabled, preserve current behavior for `use_rust`: Python owns the FUSE session and may delegate hot operations to the Rust IPC daemon.
- If passthrough startup reports unsupported kernel/session and require mode is false, fall back to the current Python FUSE plus Rust IPC daemon path.
- If require mode is true, fail mount startup with a clear error.
- If Rust-owned mount starts successfully, Python tracks and terminates that process on unmount.

## Error Handling

Passthrough is an optimization unless require mode is set. Runtime failures degrade to userspace reads:

- unsupported kernel or missing FUSE capability: normal userspace path
- `fuser` passthrough registration error: normal userspace path
- materialization download error: normal userspace path
- ETag/content mismatch after materialization: invalidate backing file and normal userspace path
- open flags are writable or truncating: normal userspace path
- path is hook-sensitive: normal userspace path

Logs should include structured reasons at debug or info level, but avoid logging API keys or local backing filenames if those include sensitive path fragments.

## Tests

Most tests do not require a Linux 6.9 FUSE host.

Rust unit tests:

- passthrough config is disabled by default
- CLI/env pattern parsing handles repeated and comma-separated patterns
- empty pattern set denies all files
- threshold defaults to 131072 bytes
- paths below threshold are denied
- read-only flags are accepted
- write, read-write, create, truncate, and append flags are denied
- unsupported kernel/session denies passthrough
- path allow-pattern miss denies passthrough
- deny-pattern match denies passthrough
- hook-sensitive policy denies passthrough
- backing key changes when ETag/content identity changes
- materialization writes through a temporary file and verifies size
- backing invalidation removes affected path entries

Rust integration-style tests with mocked Nexus HTTP:

- materialization downloads content on miss
- ETag match reuses an existing backing file
- ETag/content change creates a new backing file
- materialization failure falls back to normal open decision

Python tests:

- passthrough-enabled `use_rust=True` selects Rust-owned FUSE mount orchestration
- passthrough-disabled `use_rust=True` keeps current Python FUSE plus Rust IPC daemon path
- unsupported Rust startup falls back when require mode is false
- unsupported Rust startup fails when require mode is true
- passthrough patterns and threshold are passed to the Rust command
- API key is passed through file-based credentials, not command-line arguments

Linux-gated FUSE integration:

- skipped unless running on Linux with `/dev/fuse`, mount privileges, and passthrough negotiation support
- mounts a test filesystem
- reads an eligible large file and verifies userspace `read()` counter does not increment for passthrough reads
- reads an ineligible small or hook-sensitive file and verifies userspace `read()` counter increments
- verifies writes invalidate backing state and subsequent reads see new content

## Benchmark

Record a benchmark under `nexus-fuse/PERFORMANCE_RESULTS.md`:

```text
1 GiB sequential read, userspace FUSE path
1 GiB sequential read, passthrough path
```

The benchmark report must include:

- kernel version
- distro
- filesystem for backing cache
- Rust version
- `fuser` version
- `nexus-fuse` command line
- passthrough threshold and patterns
- userspace throughput
- passthrough throughput
- speedup

Acceptance target: passthrough throughput at least 2x the userspace FUSE path for the same already-materialized blob. The issue target is approximately 6+ GB/s, but the required acceptance gate is the 2x comparison because hardware and cache filesystem strongly affect absolute throughput.

## Acceptance Mapping

- Passthrough enabled for large reads on supported kernels: Rust mount negotiates passthrough and returns backing IDs for eligible opens.
- Per-pattern gating: allow patterns are required and deny patterns protect hook/parser-sensitive paths.
- Graceful fallback on kernel < 6.9: unsupported capability disables passthrough unless require mode is set.
- Benchmark: documented 1 GiB sequential read comparison with at least 2x speedup.
- Hook semantics preserved: passthrough is denied by default for active read/stat hooks, parser/snapshot/workflow-sensitive paths, writable opens, and ambiguous policy states.

## Implementation Notes

Implementation should proceed TDD-first:

1. Add pure config and policy tests.
2. Add backing-key and materialization tests.
3. Upgrade `fuser` and wire capability negotiation.
4. Wire direct Rust mount passthrough open/release.
5. Add Python orchestration tests and integration.
6. Add Linux-gated FUSE integration and benchmark documentation.

Do not start by wiring kernel passthrough APIs directly into `fs.rs`; the policy and backing-file units need tests first so the fallback matrix stays understandable.
