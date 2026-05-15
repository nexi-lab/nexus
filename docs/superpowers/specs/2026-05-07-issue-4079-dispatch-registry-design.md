# Issue 4079 Dispatch Registry Design

## Context

Issue #4079 asks for a single operation registry keyed by operation name, file type, and backend/resource kind. Today Nexus separates these concerns across VFS routing, kernel syscalls, parser selection, backend implementations, and hook dispatch. That separation is useful and should remain intact, but it does not give one place for cross-cutting behavior such as "render parquet as JSON on cat" or "push grep down to Slack search."

The current Rust kernel already has:

- `rust/kernel/src/core/vfs_router.rs` for mount/backend path routing.
- `rust/kernel/src/kernel/io.rs` for `sys_read`, `sys_write`, and regular file operations.
- `rust/kernel/src/kernel/mod.rs` for `sys_grep` and `sys_glob`.
- `rust/kernel/src/core/dispatch/` for virtual path resolution, intercept hooks, and observers.
- Python parser and backend registries in `src/nexus/bricks/parsers/registry.py` and `src/nexus/backends/base/registry.py`.

The new registry should be operation selection, not a replacement for VFS routing or hook dispatch.

## Goals

- Add a Rust `OpsRegistry` with fall-through lookup by `(op_name, file_type, backend_kind)`.
- Wire `cat` and `grep` through the registry without regressing direct `sys_read` behavior.
- Provide a Python shim for read/write paths that still live above Rust.
- Register the full requested migration set:
  - S3 fingerprint backend override.
  - Slack search backend override.
  - GitHub raw read backend override.
  - Parquet `cat` parser override.
  - JSON `cat` parser override.
  - Default `cat` and `grep`.
- Keep registration deterministic during boot.
- Add tests and a benchmark proving registry lookup overhead stays within 5% of a direct default call.
- Document the registration model.

## Non-Goals

- Replacing `VFSRouter`, path resolvers, intercept hooks, or observers.
- Moving the entire read path out of Python in this change.
- Adding new public backend credentials or changing connector auth flows.
- Making every backend and parser use the registry immediately. This change creates the path and migrates the requested overrides.

## Resolution Model

`OpsRegistry` stores handlers by `OpKey`:

```rust
pub struct OpKey {
    pub name: OpName,
    pub filetype: Option<FileType>,
    pub backend: Option<BackendKind>,
}
```

Lookup order is:

1. `(op, filetype, backend)` for the most specific override.
2. `(op, *, backend)` for backend-wide overrides.
3. `(op, filetype, *)` for parser/file-type overrides.
4. `(op, *, *)` for the default implementation.

The issue sketch listed three levels and omitted `(op, filetype, *)`, but the worked examples require it for `(cat, parquet, *)` and `(cat, json, *)`. Backend-wide overrides outrank parser overrides so API pushdown can win before generic local rendering.

Registration rejects duplicate keys unless the caller uses an explicit replace API. Boot code uses deterministic phases:

1. Register default operations.
2. Register parser/file-type operations.
3. Register backend operations.

This makes overwrite intent visible and avoids import-order-dependent behavior.

## Rust Architecture

Create `rust/kernel/src/core/dispatch/ops_registry.rs` and export it from `rust/kernel/src/core/dispatch/mod.rs`.

Primary types:

- `OpName`: a small newtype around `Arc<str>` or `String`, with constructors for `"cat"`, `"grep"`, `"raw_read"`, and `"fingerprint"`.
- `FileType`: normalized file type values such as `Json`, `Parquet`, and `Other(String)`.
- `BackendKind`: normalized backend/resource values such as `S3`, `Slack`, `GitHub`, `Local`, and `Other(String)`.
- `OpKey`: `(name, filetype, backend)` lookup key.
- `CatHandler`: callable that receives bytes plus operation metadata and returns rendered bytes.
- `GrepHandler`: callable that receives grep parameters plus operation metadata and returns `Vec<GrepMatch>`.
- `RawReadHandler`: callable that receives route and operation metadata and returns backend bytes.
- `FingerprintHandler`: callable that receives route and operation metadata and returns a stable fingerprint string.
- `OpHandler`: enum over supported operation handler signatures.
- `OpsRegistry`: internally owns a `HashMap<OpKey, OpHandler>` and exposes `register`, `replace`, `resolve_cat`, `resolve_grep`, `resolve_raw_read`, and `resolve_fingerprint`.

`Kernel` owns an `Arc<OpsRegistry>` or `RwLock<OpsRegistry>` initialized at construction. The steady-state read path only needs shared reads because registration happens during boot.

`sys_read` remains byte-oriented and does not run `cat` rendering. New `sys_cat` or Python-facing `cat` plumbing calls `sys_read`, derives file type and backend kind, resolves `cat`, and applies the handler. This preserves current syscall semantics for code that expects raw bytes. Backend raw-read overrides are available as `(raw_read, *, backend)` for connector paths that need API-native byte retrieval before `cat` rendering.

`sys_grep` keeps its current default behavior as the `(grep, *, *)` handler. Before walking and reading files, it resolves the prefix route to determine backend kind. If `(grep, *, slack)` is registered, Slack receives the grep request and can push it to the connector API. If no override is found, the existing recursive read and `lib::search::search_lines` path is used.

## File Type And Backend Derivation

File type derivation:

1. Use metastore MIME type when present.
2. Fall back to path extension.
3. Fall back to `FileType::Unknown`.

Initial mappings:

- `.json` and `application/json` -> `Json`.
- `.parquet` and common parquet MIME values -> `Parquet`.

Backend kind derivation:

1. Use the routed backend `ObjectStore::name()`.
2. Normalize known names such as `path_s3`, `s3`, `slack`, `github`, and `local`.
3. Preserve unknown names as `BackendKind::Other`.

## Python Shim

Add `src/nexus/core/dispatch.py` as a transitional shim for Python-owned paths. It mirrors the Rust concepts with a simple registry:

- `OpKey`
- `OpsRegistry`
- `get_global_registry()`
- `register_default_ops()`
- `register_parser_ops()`
- `register_backend_ops()`
- `resolve(op, filetype, backend)`

The shim is used by Python read/write helpers that have not moved fully to Rust. It should be deliberately small and tested independently. It must not duplicate kernel routing logic; callers pass already-derived operation metadata.

Backend and parser modules register overrides through explicit functions imported during boot, not by incidental module import side effects.

## Override Implementations

### Default `cat`

Returns raw bytes from `sys_read` unchanged.

### JSON `cat`

Attempts to parse bytes as JSON and returns pretty-printed UTF-8 bytes with stable indentation. Invalid JSON falls back to raw bytes only if the caller requested permissive behavior; otherwise it returns a structured operation error.

### Parquet `cat`

Uses the existing Python parser stack first because parquet parsing is currently parser-owned. The Rust registry entry can delegate through the Python shim until a Rust parquet renderer exists. Output is JSON Lines or pretty JSON bytes, chosen consistently with existing parser output conventions.

### Default `grep`

Wraps the existing `sys_grep` recursive path: collect candidate files, read each regular file, decode UTF-8, and call `lib::search::search_lines`.

### Slack `grep`

Registers `(grep, *, slack)` and delegates to the Slack connector search API. The handler maps Nexus grep fields to the connector query, then maps results back into the existing `GrepMatch` shape.

### GitHub Raw Read

Registers `(raw_read, *, github)` so repository raw content paths can call the connector raw-read path before `cat` rendering. It keeps auth and error handling in the connector.

### S3 Fingerprint

Registers `(fingerprint, *, s3)` for S3-backed resources. The handler should use S3 metadata/ETag or the existing fingerprint helper where available, and must not force a full object download for normal fingerprint checks.

## Error Handling

Handlers return typed errors that include:

- operation name
- file type
- backend kind
- path or prefix
- whether a fallback was attempted

Registry miss is not an error if a default handler exists. Duplicate registration is an error unless using `replace`.

Backend override failures should not silently fall back if the override already committed to an API pushdown request. Default fallback is acceptable only for `NotSupported` or missing capability cases where no side effect occurred.

## Testing

Rust tests:

- Exact key outranks backend wildcard, file-type wildcard, and default.
- Backend wildcard outranks file-type wildcard.
- File-type wildcard resolves before default.
- Missing operation returns `None`.
- Duplicate registration rejects by default.
- Explicit replace updates the handler.
- `FileType` and `BackendKind` normalization covers JSON, parquet, S3, Slack, GitHub, local, and unknown values.
- Default `grep` still returns the same match shape as the current implementation.
- `raw_read` and `fingerprint` resolve through the same specificity rules as `cat` and `grep`.

Python tests:

- Python shim lookup order matches Rust.
- Parser registration adds JSON and parquet `cat`.
- Backend registration adds Slack, GitHub, and S3 overrides.
- Import/bootstrap registration order is deterministic.

Benchmark:

- Add a benchmark in `rust/kernel/benches/` comparing direct default handler invocation to registry resolve plus handler invocation.
- Passing criterion: registry path overhead is within 5% of the direct-call path for default `cat` or a synthetic no-op operation handler.

Integration-style tests:

- JSON `cat` pretty-prints valid JSON and errors on invalid JSON in strict mode.
- Parquet `cat` uses a small fixture when parquet dependencies are available; otherwise the test asserts clean skip/fallback behavior.
- Slack `grep` uses a fake connector API client and verifies query pushdown.
- GitHub raw read uses a fake connector transport and verifies raw-read path selection.
- S3 fingerprint uses a fake transport/metadata response and verifies no full read occurs.

## Documentation

Add a doc page under `docs/` describing:

- what operation dispatch is and how it differs from VFS routing and hooks
- key specificity order
- boot registration phases
- how parsers register `(op, filetype, *)`
- how backends register `(op, *, backend)`
- how to test a new override

## Rollout Plan

1. Implement and test the Rust registry in isolation.
2. Add the Python shim and tests.
3. Wire default `cat` and `grep`.
4. Register parser overrides for JSON and parquet.
5. Register backend overrides for Slack, GitHub, and S3.
6. Add benchmark and documentation.
7. Run focused Rust and Python tests, then broader smoke tests for kernel read/search paths.

## Risks And Mitigations

- **Hot-path overhead:** Keep lookup to a fixed sequence of hash probes and benchmark it.
- **Boot order drift:** Use explicit registration phases rather than incidental import side effects.
- **Semantic confusion with hooks:** Document that op dispatch selects an implementation; intercept hooks still enforce policy and audit.
- **Parser dependency gaps:** Keep parquet implementation behind existing parser availability and test clean fallback behavior.
- **Backend credential/API gaps in tests:** Use fake transports or fake connector clients so override behavior is tested without live credentials.
