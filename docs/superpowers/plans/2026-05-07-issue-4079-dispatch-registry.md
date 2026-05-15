# Issue 4079 Dispatch Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the full three-axis operation dispatch registry for Nexus and migrate the requested default, parser, and backend overrides.

**Architecture:** Add a Rust `OpsRegistry` under `rust/kernel/src/core/dispatch/` for fixed-cost fall-through resolution. Expose backend/filetype derivation from the kernel, then add a Python transitional registry that executes current Python-owned parser and connector overrides while Rust read/search paths continue to own raw syscall behavior.

**Tech Stack:** Rust 2021, PyO3, Criterion, Python 3.12+, pytest, existing Nexus parser/backend registries.

---

## File Structure

- Create `rust/kernel/src/core/dispatch/ops_registry.rs`: Rust registry types, normalization helpers, registration, resolution, and unit tests.
- Modify `rust/kernel/src/core/dispatch/mod.rs`: export `ops_registry`.
- Modify `rust/kernel/src/kernel/mod.rs`: add `ops_registry` field, initialize defaults, add kernel facade methods and result structs.
- Modify `rust/kernel/src/kernel/io.rs`: add `sys_cat`, `op_metadata_for_path`, and `backend_fingerprint` helpers; add unit tests around JSON/default cat and S3 fingerprint.
- Modify `rust/kernel/src/generated_kernel_abi_pyo3.rs`: expose `sys_cat`, `op_metadata_for_path`, and `backend_fingerprint` to Python. Run `uv run python scripts/codegen_kernel_abi.py` after editing.
- Modify `rust/kernel/Cargo.toml`: add `ops_registry_bench`.
- Create `rust/kernel/benches/ops_registry_bench.rs`: Criterion benchmark for direct handler vs registry resolution.
- Create `src/nexus/core/dispatch.py`: Python shim registry, normalization helpers, default operations, parser operations, backend operations, and execution helpers.
- Modify `src/nexus/core/nexus_fs.py`: initialize Python operation dispatch once during `NexusFS` construction.
- Modify `src/nexus/fs/_helpers.py`: route `grep` through Python shim before Rust default.
- Modify `src/nexus/fs/_cli.py`: route `nexus-fs cat` through Python shim.
- Modify `src/nexus/fs/_sync.py`: add `SyncNexusFS.cat()` and keep `read()` raw.
- Modify `src/nexus/backends/connectors/slack/transport.py`: add `search_messages()`.
- Modify `src/nexus/backends/connectors/slack/connector.py`: expose `grep_messages()` for dispatch shim.
- Modify `src/nexus/backends/connectors/github/connector.py`: expose `raw_read()` for dispatch shim.
- Modify `src/nexus/backends/storage/path_s3.py`: expose `fingerprint()` for dispatch shim.
- Modify `src/nexus/backends/transports/s3_transport.py`: expose `fingerprint()` using `head_object`.
- Create `tests/unit/core/test_dispatch_registry.py`: Python shim resolution and default/parser/backend registrations.
- Create `tests/unit/fs/test_dispatch_cat.py`: CLI/helper-facing JSON and raw cat behavior.
- Create `tests/unit/backends/connectors/test_slack_dispatch.py`: Slack grep pushdown with fake client.
- Create `tests/unit/backends/connectors/test_github_dispatch.py`: GitHub raw-read dispatch with fake CLI execution.
- Create `tests/unit/backends/test_s3_dispatch.py`: S3 fingerprint with fake transport.
- Create `docs/architecture/ops-dispatch-registry.md`: user-facing registration model documentation.

## Task 1: Rust OpsRegistry Core

**Files:**
- Create: `rust/kernel/src/core/dispatch/ops_registry.rs`
- Modify: `rust/kernel/src/core/dispatch/mod.rs`

- [ ] **Step 1: Write failing Rust registry tests**

Add this test module at the bottom of the new file before implementing the registry fully:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn key(op: &str, ft: Option<FileType>, be: Option<BackendKind>) -> OpKey {
        OpKey::new(OpName::new(op), ft, be)
    }

    #[test]
    fn resolve_prefers_exact_backend_then_filetype_then_default() {
        let registry = OpsRegistry::new();
        registry
            .register(
                key("cat", None, None),
                OpHandler::Cat(CatHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key("cat", Some(FileType::Json), None),
                OpHandler::Cat(CatHandlerKind::JsonPretty),
            )
            .unwrap();
        registry
            .register(
                key("cat", None, Some(BackendKind::GitHub)),
                OpHandler::RawRead(RawReadHandlerKind::GitHub),
            )
            .unwrap();
        registry
            .register(
                key("cat", Some(FileType::Json), Some(BackendKind::GitHub)),
                OpHandler::Cat(CatHandlerKind::GitHubJson),
            )
            .unwrap();

        assert_eq!(
            registry.resolve("cat", &FileType::Json, &BackendKind::GitHub),
            Some(OpHandler::Cat(CatHandlerKind::GitHubJson))
        );
        assert_eq!(
            registry.resolve("cat", &FileType::Json, &BackendKind::Local),
            Some(OpHandler::Cat(CatHandlerKind::JsonPretty))
        );
        assert_eq!(
            registry.resolve("cat", &FileType::Unknown, &BackendKind::Local),
            Some(OpHandler::Cat(CatHandlerKind::Default))
        );
    }

    #[test]
    fn backend_wildcard_precedes_filetype_wildcard() {
        let registry = OpsRegistry::new();
        registry
            .register(
                key("grep", Some(FileType::Json), None),
                OpHandler::Grep(GrepHandlerKind::Default),
            )
            .unwrap();
        registry
            .register(
                key("grep", None, Some(BackendKind::Slack)),
                OpHandler::Grep(GrepHandlerKind::SlackSearch),
            )
            .unwrap();

        assert_eq!(
            registry.resolve("grep", &FileType::Json, &BackendKind::Slack),
            Some(OpHandler::Grep(GrepHandlerKind::SlackSearch))
        );
    }

    #[test]
    fn duplicate_register_rejects_and_replace_overwrites() {
        let registry = OpsRegistry::new();
        let key = key("cat", None, None);
        registry
            .register(key.clone(), OpHandler::Cat(CatHandlerKind::Default))
            .unwrap();

        let err = registry
            .register(key.clone(), OpHandler::Cat(CatHandlerKind::JsonPretty))
            .unwrap_err();
        assert_eq!(err.kind, OpsRegistryErrorKind::DuplicateKey);

        registry.replace(key, OpHandler::Cat(CatHandlerKind::JsonPretty));
        assert_eq!(
            registry.resolve("cat", &FileType::Unknown, &BackendKind::Unknown),
            Some(OpHandler::Cat(CatHandlerKind::JsonPretty))
        );
    }

    #[test]
    fn normalizes_filetypes_and_backends() {
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a.json", None),
            FileType::Json
        );
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a.parquet", None),
            FileType::Parquet
        );
        assert_eq!(
            FileType::from_path_and_mime("/tmp/a", Some("application/json")),
            FileType::Json
        );
        assert_eq!(BackendKind::from_backend_name("path_s3"), BackendKind::S3);
        assert_eq!(
            BackendKind::from_backend_name("slack_connector"),
            BackendKind::Slack
        );
        assert_eq!(
            BackendKind::from_backend_name("github_connector"),
            BackendKind::GitHub
        );
    }
}
```

- [ ] **Step 2: Run the focused Rust test and confirm it fails**

Run:

```bash
cargo test -p kernel ops_registry --lib
```

Expected: compile failure because `ops_registry` and its types do not exist yet.

- [ ] **Step 3: Implement the Rust registry**

Create `rust/kernel/src/core/dispatch/ops_registry.rs` with:

```rust
use parking_lot::RwLock;
use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct OpName(Arc<str>);

impl OpName {
    pub fn new(name: impl AsRef<str>) -> Self {
        Self(Arc::from(name.as_ref().to_ascii_lowercase()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl From<&str> for OpName {
    fn from(value: &str) -> Self {
        Self::new(value)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum FileType {
    Json,
    Parquet,
    Unknown,
    Other(Arc<str>),
}

impl FileType {
    pub fn from_path_and_mime(path: &str, mime_type: Option<&str>) -> Self {
        let mime = mime_type.unwrap_or("").trim().to_ascii_lowercase();
        if matches!(mime.as_str(), "application/json" | "text/json") {
            return Self::Json;
        }
        if matches!(
            mime.as_str(),
            "application/parquet" | "application/x-parquet" | "application/vnd.apache.parquet"
        ) {
            return Self::Parquet;
        }

        let ext = path
            .rsplit_once('.')
            .map(|(_, ext)| ext.trim().to_ascii_lowercase())
            .unwrap_or_default();
        match ext.as_str() {
            "json" | "jsonl" | "ndjson" => Self::Json,
            "parquet" | "pq" => Self::Parquet,
            "" => Self::Unknown,
            other => Self::Other(Arc::from(other)),
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            Self::Json => "json",
            Self::Parquet => "parquet",
            Self::Unknown => "unknown",
            Self::Other(v) => v,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub enum BackendKind {
    S3,
    Slack,
    GitHub,
    Local,
    Unknown,
    Other(Arc<str>),
}

impl BackendKind {
    pub fn from_backend_name(name: &str) -> Self {
        let normalized = name
            .trim()
            .to_ascii_lowercase()
            .replace('-', "_");
        match normalized.as_str() {
            "path_s3" | "s3" | "s3_connector" => Self::S3,
            "slack" | "path_slack" | "slack_connector" => Self::Slack,
            "github" | "github_connector" | "gws_github" => Self::GitHub,
            "local" | "path_local" | "cas_local" => Self::Local,
            "" => Self::Unknown,
            other => Self::Other(Arc::from(other)),
        }
    }

    pub fn as_str(&self) -> &str {
        match self {
            Self::S3 => "s3",
            Self::Slack => "slack",
            Self::GitHub => "github",
            Self::Local => "local",
            Self::Unknown => "unknown",
            Self::Other(v) => v,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Hash)]
pub struct OpKey {
    pub name: OpName,
    pub filetype: Option<FileType>,
    pub backend: Option<BackendKind>,
}

impl OpKey {
    pub fn new(name: OpName, filetype: Option<FileType>, backend: Option<BackendKind>) -> Self {
        Self {
            name,
            filetype,
            backend,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CatHandlerKind {
    Default,
    JsonPretty,
    ParquetJson,
    GitHubJson,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum GrepHandlerKind {
    Default,
    SlackSearch,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RawReadHandlerKind {
    GitHub,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FingerprintHandlerKind {
    S3,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OpHandler {
    Cat(CatHandlerKind),
    Grep(GrepHandlerKind),
    RawRead(RawReadHandlerKind),
    Fingerprint(FingerprintHandlerKind),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OpsRegistryErrorKind {
    DuplicateKey,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OpsRegistryError {
    pub kind: OpsRegistryErrorKind,
    pub key: OpKey,
}

impl fmt::Display for OpsRegistryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "operation handler already registered for {:?}", self.key)
    }
}

impl std::error::Error for OpsRegistryError {}

pub struct OpsRegistry {
    table: RwLock<HashMap<OpKey, OpHandler>>,
}

impl OpsRegistry {
    pub fn new() -> Self {
        Self {
            table: RwLock::new(HashMap::new()),
        }
    }

    pub fn register(&self, key: OpKey, handler: OpHandler) -> Result<(), OpsRegistryError> {
        let mut table = self.table.write();
        if table.contains_key(&key) {
            return Err(OpsRegistryError {
                kind: OpsRegistryErrorKind::DuplicateKey,
                key,
            });
        }
        table.insert(key, handler);
        Ok(())
    }

    pub fn replace(&self, key: OpKey, handler: OpHandler) {
        self.table.write().insert(key, handler);
    }

    pub fn resolve(&self, op: &str, filetype: &FileType, backend: &BackendKind) -> Option<OpHandler> {
        let name = OpName::new(op);
        let table = self.table.read();
        let probes = [
            OpKey::new(name.clone(), Some(filetype.clone()), Some(backend.clone())),
            OpKey::new(name.clone(), None, Some(backend.clone())),
            OpKey::new(name.clone(), Some(filetype.clone()), None),
            OpKey::new(name, None, None),
        ];
        probes.iter().find_map(|key| table.get(key).copied())
    }

    pub fn len(&self) -> usize {
        self.table.read().len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl Default for OpsRegistry {
    fn default() -> Self {
        Self::new()
    }
}
```

Add this export to `rust/kernel/src/core/dispatch/mod.rs` near the top:

```rust
pub mod ops_registry;
```

- [ ] **Step 4: Run the focused Rust test and confirm it passes**

Run:

```bash
cargo test -p kernel ops_registry --lib
```

Expected: all `ops_registry` tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add rust/kernel/src/core/dispatch/mod.rs rust/kernel/src/core/dispatch/ops_registry.rs
git commit -m "feat: add rust operation registry"
```

## Task 2: Kernel Facade And Cat/Fingerprint Helpers

**Files:**
- Modify: `rust/kernel/src/kernel/mod.rs`
- Modify: `rust/kernel/src/kernel/io.rs`
- Modify: `rust/kernel/src/generated_kernel_abi_pyo3.rs`
- Modify generated outputs after running `uv run python scripts/codegen_kernel_abi.py`

- [ ] **Step 1: Write failing kernel tests**

Add these tests inside the existing `#[cfg(test)] mod tests` in `rust/kernel/src/kernel/mod.rs`:

```rust
#[test]
fn sys_cat_pretty_prints_json_without_changing_sys_read() {
    let k = Kernel::new();
    let ctx = OperationContext::new("system", "root", true, None, true);
    k.sys_write("/doc.json", &ctx, br#"{"b":2,"a":1}"#, 0).unwrap();
    let raw = k.sys_read("/doc.json", &ctx, 5000, 0).unwrap();
    assert_eq!(raw.data.unwrap(), br#"{"b":2,"a":1}"#);

    let cat = k.sys_cat("/doc.json", &ctx, true).unwrap();
    assert_eq!(cat.data, b"{\n  \"b\": 2,\n  \"a\": 1\n}\n");
    assert_eq!(cat.filetype.as_str(), "json");
}

#[test]
fn sys_cat_returns_raw_bytes_for_unknown_filetype() {
    let k = Kernel::new();
    let ctx = OperationContext::new("system", "root", true, None, true);
    k.sys_write("/plain.bin", &ctx, b"abc", 0).unwrap();

    let cat = k.sys_cat("/plain.bin", &ctx, true).unwrap();
    assert_eq!(cat.data, b"abc");
    assert_eq!(cat.handler, "cat/default");
}
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
cargo test -p kernel sys_cat --lib
```

Expected: compile failure because `Kernel::sys_cat` and `SysCatResult` do not exist.

- [ ] **Step 3: Add result structs and registry field**

In `rust/kernel/src/kernel/mod.rs`, add imports near existing dispatch imports:

```rust
use crate::dispatch::ops_registry::{
    BackendKind, CatHandlerKind, FileType, FingerprintHandlerKind, OpHandler, OpKey, OpName,
    OpsRegistry,
};
```

Add result types near `SysReadResult`:

```rust
pub struct SysCatResult {
    pub data: Vec<u8>,
    pub handler: String,
    pub filetype: FileType,
    pub backend: BackendKind,
}

pub struct OpMetadataResult {
    pub filetype: FileType,
    pub backend: BackendKind,
    pub mime_type: Option<String>,
    pub backend_name: String,
}
```

Add this field to `Kernel`:

```rust
pub(crate) ops_registry: OpsRegistry,
```

Initialize it in `Kernel::new()`:

```rust
ops_registry: OpsRegistry::new(),
```

After constructing `k`, before `k` is returned, call:

```rust
k.register_default_ops();
```

Add these methods inside `impl Kernel`:

```rust
fn register_default_ops(&self) {
    let _ = self.ops_registry.register(
        OpKey::new(OpName::new("cat"), None, None),
        OpHandler::Cat(CatHandlerKind::Default),
    );
    let _ = self.ops_registry.register(
        OpKey::new(OpName::new("cat"), Some(FileType::Json), None),
        OpHandler::Cat(CatHandlerKind::JsonPretty),
    );
    let _ = self.ops_registry.register(
        OpKey::new(OpName::new("grep"), None, None),
        OpHandler::Grep(crate::dispatch::ops_registry::GrepHandlerKind::Default),
    );
    let _ = self.ops_registry.register(
        OpKey::new(OpName::new("fingerprint"), None, Some(BackendKind::S3)),
        OpHandler::Fingerprint(FingerprintHandlerKind::S3),
    );
}

pub fn resolve_op_handler(
    &self,
    op: &str,
    filetype: &FileType,
    backend: &BackendKind,
) -> Option<OpHandler> {
    self.ops_registry.resolve(op, filetype, backend)
}
```

- [ ] **Step 4: Implement `sys_cat` and metadata helper**

Add to `rust/kernel/src/kernel/io.rs`:

```rust
use crate::dispatch::ops_registry::{
    BackendKind, CatHandlerKind, FileType, FingerprintHandlerKind, OpHandler,
};
```

Add these methods inside `impl Kernel`:

```rust
pub fn op_metadata_for_path(
    &self,
    path: &str,
    ctx: &OperationContext,
) -> Result<OpMetadataResult, KernelError> {
    validate_path_fast(path)?;
    let route = self.vfs_router.route(path, &ctx.zone_id)?;
    let backend_name = route
        .backend
        .as_ref()
        .map(|b| b.name().to_string())
        .unwrap_or_default();
    let mime_type = self
        .with_metastore_route(&route, |ms| ms.get(path).ok().flatten())
        .flatten()
        .and_then(|meta| meta.mime_type);
    let filetype = FileType::from_path_and_mime(path, mime_type.as_deref());
    let backend = BackendKind::from_backend_name(&backend_name);
    Ok(OpMetadataResult {
        filetype,
        backend,
        mime_type,
        backend_name,
    })
}

pub fn sys_cat(
    &self,
    path: &str,
    ctx: &OperationContext,
    strict_json: bool,
) -> Result<SysCatResult, KernelError> {
    let meta = self.op_metadata_for_path(path, ctx)?;
    let read = self.sys_read(path, ctx, 5000, 0)?;
    let data = read.data.unwrap_or_default();
    let handler = self
        .resolve_op_handler("cat", &meta.filetype, &meta.backend)
        .unwrap_or(OpHandler::Cat(CatHandlerKind::Default));

    match handler {
        OpHandler::Cat(CatHandlerKind::JsonPretty) => {
            let value: serde_json::Value = match serde_json::from_slice(&data) {
                Ok(value) => value,
                Err(e) if !strict_json => {
                    return Ok(SysCatResult {
                        data,
                        handler: format!("cat/json_permissive_fallback:{e}"),
                        filetype: meta.filetype,
                        backend: meta.backend,
                    });
                }
                Err(e) => {
                    return Err(KernelError::IOError(format!(
                        "cat json parse failed for {path}: {e}"
                    )));
                }
            };
            let mut rendered = serde_json::to_vec_pretty(&value)
                .map_err(|e| KernelError::IOError(format!("cat json render failed: {e}")))?;
            rendered.push(b'\n');
            Ok(SysCatResult {
                data: rendered,
                handler: "cat/json_pretty".to_string(),
                filetype: meta.filetype,
                backend: meta.backend,
            })
        }
        OpHandler::Cat(CatHandlerKind::Default)
        | OpHandler::Cat(CatHandlerKind::ParquetJson)
        | OpHandler::Cat(CatHandlerKind::GitHubJson)
        | OpHandler::RawRead(_)
        | OpHandler::Grep(_)
        | OpHandler::Fingerprint(_) => Ok(SysCatResult {
            data,
            handler: "cat/default".to_string(),
            filetype: meta.filetype,
            backend: meta.backend,
        }),
    }
}

pub fn backend_fingerprint(
    &self,
    path: &str,
    ctx: &OperationContext,
) -> Result<Option<String>, KernelError> {
    let meta = self.op_metadata_for_path(path, ctx)?;
    match self.resolve_op_handler("fingerprint", &meta.filetype, &meta.backend) {
        Some(OpHandler::Fingerprint(FingerprintHandlerKind::S3)) => {
            let stat = self.sys_stat(path, &ctx.zone_id);
            Ok(stat.and_then(|s| s.content_id))
        }
        _ => Ok(None),
    }
}
```

Keep the strict JSON branch returning an error for invalid JSON. The Python shim handles permissive fallback separately.

- [ ] **Step 5: Expose PyO3 methods**

In `rust/kernel/src/generated_kernel_abi_pyo3.rs`, add methods in the `#[pymethods] impl PyKernel` block near `sys_read_raw`:

```rust
    #[pyo3(signature = (path, zone_id="root", strict_json=true))]
    fn sys_cat<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
        strict_json: bool,
    ) -> PyResult<Py<PyAny>> {
        let ctx = OperationContext::new("system", zone_id, true, None, true);
        let result = self
            .inner
            .sys_cat(path, &ctx, strict_json)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(format!("sys_cat: {:?}", e)))?;
        Ok(PyBytes::new(py, &result.data).into())
    }

    #[pyo3(signature = (path, zone_id="root"))]
    fn op_metadata_for_path<'py>(
        &self,
        py: Python<'py>,
        path: &str,
        zone_id: &str,
    ) -> PyResult<Bound<'py, PyDict>> {
        let ctx = OperationContext::new("system", zone_id, true, None, true);
        let result = self.inner.op_metadata_for_path(path, &ctx).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("op_metadata_for_path: {:?}", e))
        })?;
        let dict = PyDict::new(py);
        dict.set_item("filetype", result.filetype.as_str())?;
        dict.set_item("backend", result.backend.as_str())?;
        dict.set_item("mime_type", result.mime_type)?;
        dict.set_item("backend_name", result.backend_name)?;
        Ok(dict)
    }

    #[pyo3(signature = (path, zone_id="root"))]
    fn backend_fingerprint(&self, path: &str, zone_id: &str) -> PyResult<Option<String>> {
        let ctx = OperationContext::new("system", zone_id, true, None, true);
        self.inner.backend_fingerprint(path, &ctx).map_err(|e| {
            pyo3::exceptions::PyRuntimeError::new_err(format!("backend_fingerprint: {:?}", e))
        })
    }
```

Run:

```bash
uv run python scripts/codegen_kernel_abi.py
```

- [ ] **Step 6: Run focused Rust tests and codegen check**

Run:

```bash
cargo test -p kernel sys_cat --lib
uv run python scripts/codegen_kernel_abi.py --check
```

Expected: Rust tests pass and codegen check reports no diffs.

- [ ] **Step 7: Commit Task 2**

```bash
git add rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs rust/kernel/src/generated_kernel_abi_pyo3.rs stubs src/nexus/core/kernel_exports.py src/nexus/core/kernel_protocols.py src/nexus/server/_kernel_syscall_dispatch.py src/nexus/_kernel_api_groups.py
git commit -m "feat: expose kernel operation dispatch metadata"
```

## Task 3: Python Dispatch Shim

**Files:**
- Create: `src/nexus/core/dispatch.py`
- Create: `tests/unit/core/test_dispatch_registry.py`
- Modify: `src/nexus/core/nexus_fs.py`

- [ ] **Step 1: Write failing Python shim tests**

Create `tests/unit/core/test_dispatch_registry.py`:

```python
from __future__ import annotations

import json

import pytest

from nexus.core.dispatch import (
    BackendKind,
    FileType,
    OpKey,
    OpsRegistry,
    OperationRequest,
    default_cat,
    get_global_registry,
    normalize_backend,
    normalize_filetype,
    register_backend_ops,
    register_default_ops,
    register_parser_ops,
    reset_global_registry_for_tests,
)


def test_registry_resolution_order() -> None:
    registry = OpsRegistry()
    registry.register(OpKey("cat", None, None), lambda req: b"default")
    registry.register(OpKey("cat", FileType.JSON, None), lambda req: b"json")
    registry.register(OpKey("cat", None, BackendKind.GITHUB), lambda req: b"github")
    registry.register(OpKey("cat", FileType.JSON, BackendKind.GITHUB), lambda req: b"exact")

    req = OperationRequest(
        op="cat",
        path="/repo/data.json",
        filetype=FileType.JSON,
        backend=BackendKind.GITHUB,
        content=b"{}",
    )
    assert registry.resolve(req.op, req.filetype, req.backend)(req) == b"exact"
    assert registry.resolve("cat", FileType.JSON, BackendKind.LOCAL)(req) == b"json"
    assert registry.resolve("cat", FileType.UNKNOWN, BackendKind.LOCAL)(req) == b"default"


def test_duplicate_register_rejects_and_replace_updates() -> None:
    registry = OpsRegistry()
    key = OpKey("cat", None, None)
    registry.register(key, lambda req: b"one")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(key, lambda req: b"two")
    registry.replace(key, lambda req: b"two")
    req = OperationRequest(
        op="cat",
        path="/a",
        filetype=FileType.UNKNOWN,
        backend=BackendKind.UNKNOWN,
        content=b"",
    )
    assert registry.resolve("cat", FileType.UNKNOWN, BackendKind.UNKNOWN)(req) == b"two"


def test_normalizers_cover_requested_types() -> None:
    assert normalize_filetype("/tmp/data.json", None) == FileType.JSON
    assert normalize_filetype("/tmp/data.parquet", None) == FileType.PARQUET
    assert normalize_filetype("/tmp/data", "application/json") == FileType.JSON
    assert normalize_backend("path_s3") == BackendKind.S3
    assert normalize_backend("slack_connector") == BackendKind.SLACK
    assert normalize_backend("github_connector") == BackendKind.GITHUB


def test_default_and_parser_registration() -> None:
    registry = OpsRegistry()
    register_default_ops(registry)
    register_parser_ops(registry)
    req = OperationRequest(
        op="cat",
        path="/data.json",
        filetype=FileType.JSON,
        backend=BackendKind.LOCAL,
        content=b'{"b":2,"a":1}',
        strict=True,
    )
    rendered = registry.resolve("cat", FileType.JSON, BackendKind.LOCAL)(req)
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_backend_registration_adds_requested_overrides() -> None:
    registry = OpsRegistry()
    register_default_ops(registry)
    register_backend_ops(registry)
    assert registry.resolve("grep", FileType.UNKNOWN, BackendKind.SLACK) is not None
    assert registry.resolve("raw_read", FileType.UNKNOWN, BackendKind.GITHUB) is not None
    assert registry.resolve("fingerprint", FileType.UNKNOWN, BackendKind.S3) is not None


def test_global_registry_bootstrap_is_idempotent() -> None:
    reset_global_registry_for_tests()
    first = get_global_registry()
    second = get_global_registry()
    assert first is second
    assert first.resolve("cat", FileType.JSON, BackendKind.LOCAL) is not None
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run:

```bash
uv run pytest tests/unit/core/test_dispatch_registry.py -q
```

Expected: import failure because `nexus.core.dispatch` does not exist.

- [ ] **Step 3: Implement `src/nexus/core/dispatch.py`**

Create the module:

```python
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FileType(StrEnum):
    JSON = "json"
    PARQUET = "parquet"
    UNKNOWN = "unknown"


class BackendKind(StrEnum):
    S3 = "s3"
    SLACK = "slack"
    GITHUB = "github"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class OpKey:
    op: str
    filetype: FileType | None
    backend: BackendKind | None


@dataclass
class OperationRequest:
    op: str
    path: str
    filetype: FileType
    backend: BackendKind
    content: bytes | None = None
    kernel: Any = None
    context: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    strict: bool = True
    pattern: str | None = None
    ignore_case: bool = False
    max_results: int = 1000


Handler = Callable[[OperationRequest], Any]


def normalize_filetype(path: str, mime_type: str | None = None) -> FileType:
    mime = (mime_type or "").strip().lower()
    if mime in {"application/json", "text/json"}:
        return FileType.JSON
    if mime in {"application/parquet", "application/x-parquet", "application/vnd.apache.parquet"}:
        return FileType.PARQUET
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in {"json", "jsonl", "ndjson"}:
        return FileType.JSON
    if suffix in {"parquet", "pq"}:
        return FileType.PARQUET
    return FileType.UNKNOWN


def normalize_backend(name: str | None) -> BackendKind:
    normalized = (name or "").strip().lower().replace("-", "_")
    if normalized in {"path_s3", "s3", "s3_connector"}:
        return BackendKind.S3
    if normalized in {"slack", "path_slack", "slack_connector"}:
        return BackendKind.SLACK
    if normalized in {"github", "github_connector", "gws_github"}:
        return BackendKind.GITHUB
    if normalized in {"local", "path_local", "cas_local"}:
        return BackendKind.LOCAL
    return BackendKind.UNKNOWN


class OpsRegistry:
    def __init__(self) -> None:
        self._handlers: dict[OpKey, Handler] = {}

    def register(self, key: OpKey, handler: Handler) -> None:
        if key in self._handlers:
            raise ValueError(f"operation handler already registered for {key}")
        self._handlers[key] = handler

    def replace(self, key: OpKey, handler: Handler) -> None:
        self._handlers[key] = handler

    def resolve(self, op: str, filetype: FileType, backend: BackendKind) -> Handler | None:
        normalized_op = op.lower()
        probes = (
            OpKey(normalized_op, filetype, backend),
            OpKey(normalized_op, None, backend),
            OpKey(normalized_op, filetype, None),
            OpKey(normalized_op, None, None),
        )
        for key in probes:
            handler = self._handlers.get(key)
            if handler is not None:
                return handler
        return None


def default_cat(req: OperationRequest) -> bytes:
    return req.content or b""


def json_cat(req: OperationRequest) -> bytes:
    raw = req.content or b""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if req.strict:
            raise
        return raw
    return (json.dumps(parsed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def parquet_cat(req: OperationRequest) -> bytes:
    raw = req.content or b""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        if req.strict:
            raise RuntimeError("pyarrow is required for parquet cat") from None
        return raw
    import io

    table = pq.read_table(io.BytesIO(raw))
    rows = table.to_pylist()
    return (json.dumps(rows, indent=2, default=str, ensure_ascii=False) + "\n").encode("utf-8")


def slack_grep(req: OperationRequest) -> list[dict[str, Any]]:
    backend = req.metadata.get("backend_instance")
    if backend is None or not hasattr(backend, "grep_messages"):
        raise RuntimeError("Slack grep dispatch requires a backend with grep_messages")
    return backend.grep_messages(
        req.pattern or "",
        context=req.context,
        max_results=req.max_results,
        ignore_case=req.ignore_case,
    )


def github_raw_read(req: OperationRequest) -> bytes:
    backend = req.metadata.get("backend_instance")
    if backend is None or not hasattr(backend, "raw_read"):
        raise RuntimeError("GitHub raw_read dispatch requires a backend with raw_read")
    return backend.raw_read(req.path, context=req.context)


def s3_fingerprint(req: OperationRequest) -> str | None:
    backend = req.metadata.get("backend_instance")
    if backend is not None and hasattr(backend, "fingerprint"):
        return backend.fingerprint(req.path, context=req.context)
    kernel = req.kernel
    if kernel is not None and hasattr(kernel, "_kernel"):
        return kernel._kernel.backend_fingerprint(req.path)
    return None


def register_default_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("cat", None, None), default_cat)


def register_parser_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("cat", FileType.JSON, None), json_cat)
    registry.register(OpKey("cat", FileType.PARQUET, None), parquet_cat)


def register_backend_ops(registry: OpsRegistry) -> None:
    registry.register(OpKey("grep", None, BackendKind.SLACK), slack_grep)
    registry.register(OpKey("raw_read", None, BackendKind.GITHUB), github_raw_read)
    registry.register(OpKey("fingerprint", None, BackendKind.S3), s3_fingerprint)


_GLOBAL_REGISTRY: OpsRegistry | None = None


def get_global_registry() -> OpsRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        registry = OpsRegistry()
        register_default_ops(registry)
        register_parser_ops(registry)
        register_backend_ops(registry)
        _GLOBAL_REGISTRY = registry
    return _GLOBAL_REGISTRY


def reset_global_registry_for_tests() -> None:
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = None
```

- [ ] **Step 4: Initialize registry during `NexusFS` boot**

In `src/nexus/core/nexus_fs.py`, after `self._init_dispatch()` in `__init__`, add:

```python
        from nexus.core.dispatch import get_global_registry

        self._ops_registry = get_global_registry()
```

- [ ] **Step 5: Run the shim tests**

Run:

```bash
uv run pytest tests/unit/core/test_dispatch_registry.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/nexus/core/dispatch.py src/nexus/core/nexus_fs.py tests/unit/core/test_dispatch_registry.py
git commit -m "feat: add python operation dispatch shim"
```

## Task 4: User-Facing Cat And JSON/Parquet Parser Overrides

**Files:**
- Modify: `src/nexus/core/dispatch.py`
- Modify: `src/nexus/fs/_cli.py`
- Modify: `src/nexus/fs/_sync.py`
- Create: `tests/unit/fs/test_dispatch_cat.py`

- [ ] **Step 1: Write failing cat tests**

Create `tests/unit/fs/test_dispatch_cat.py`:

```python
from __future__ import annotations

import json

import pytest

from nexus.core.dispatch import cat_path


class FakePyKernel:
    def op_metadata_for_path(self, path: str, zone_id: str = "root") -> dict[str, str | None]:
        if path.endswith(".json"):
            return {
                "filetype": "json",
                "backend": "local",
                "mime_type": "application/json",
                "backend_name": "local",
            }
        return {
            "filetype": "unknown",
            "backend": "local",
            "mime_type": None,
            "backend_name": "local",
        }


class FakeKernel:
    _kernel = FakePyKernel()

    def __init__(self, content: bytes) -> None:
        self.content = content

    def sys_read(self, path: str, *, context=None) -> bytes:
        return self.content

    def sys_stat(self, path: str, context=None) -> dict[str, object]:
        return {"mime_type": "application/json" if path.endswith(".json") else None}


def test_cat_path_pretty_prints_json() -> None:
    kernel = FakeKernel(b'{"b":2,"a":1}')
    rendered = cat_path(kernel, "/data.json")
    assert json.loads(rendered) == {"a": 1, "b": 2}
    assert rendered.endswith(b"\n")


def test_cat_path_returns_raw_for_unknown_filetype() -> None:
    kernel = FakeKernel(b"raw")
    assert cat_path(kernel, "/data.bin") == b"raw"


def test_cat_path_strict_json_error() -> None:
    kernel = FakeKernel(b"{bad")
    with pytest.raises(json.JSONDecodeError):
        cat_path(kernel, "/data.json", strict=True)


def test_cat_path_permissive_json_falls_back_to_raw() -> None:
    kernel = FakeKernel(b"{bad")
    assert cat_path(kernel, "/data.json", strict=False) == b"{bad"
```

- [ ] **Step 2: Run the cat tests and confirm they fail**

Run:

```bash
uv run pytest tests/unit/fs/test_dispatch_cat.py -q
```

Expected: import failure because `cat_path` does not exist.

- [ ] **Step 3: Add cat execution helper**

Add to `src/nexus/core/dispatch.py`:

```python
def _metadata_from_kernel(kernel: Any, path: str, context: Any = None) -> dict[str, Any]:
    py_kernel = getattr(kernel, "_kernel", None)
    zone_id = getattr(context, "zone_id", None) or getattr(kernel, "_zone_id", "root")
    if py_kernel is not None and hasattr(py_kernel, "op_metadata_for_path"):
        try:
            return dict(py_kernel.op_metadata_for_path(path, zone_id))
        except Exception:
            pass
    stat = kernel.sys_stat(path, context=context) if hasattr(kernel, "sys_stat") else {}
    mime_type = stat.get("mime_type") if isinstance(stat, dict) else None
    return {
        "filetype": normalize_filetype(path, mime_type).value,
        "backend": BackendKind.UNKNOWN.value,
        "mime_type": mime_type,
        "backend_name": "",
    }


def cat_path(kernel: Any, path: str, *, context: Any = None, strict: bool = True) -> bytes:
    metadata = _metadata_from_kernel(kernel, path, context=context)
    filetype = FileType(metadata.get("filetype") or FileType.UNKNOWN)
    backend = normalize_backend(str(metadata.get("backend") or metadata.get("backend_name") or ""))
    content = kernel.sys_read(path, context=context)
    if not isinstance(content, bytes):
        return content.get("data", b"") if isinstance(content, dict) else bytes(content)
    req = OperationRequest(
        op="cat",
        path=path,
        filetype=filetype,
        backend=backend,
        content=content,
        kernel=kernel,
        context=context,
        metadata=metadata,
        strict=strict,
    )
    handler = get_global_registry().resolve("cat", filetype, backend)
    if handler is None:
        return content
    return handler(req)
```

- [ ] **Step 4: Wire CLI and sync helper**

In `src/nexus/fs/_cli.py`, replace the body of the nested `_run()` in `cat()` with:

```python
    async def _run() -> bytes:
        from nexus.core.dispatch import cat_path
        from nexus.fs._helpers import LOCAL_CONTEXT
        from nexus.fs._helpers import close as _close_kernel

        kernel = await _boot_kernel()
        content = cat_path(kernel, path, context=LOCAL_CONTEXT, strict=True)
        _close_kernel(kernel)
        return content
```

In `src/nexus/fs/_sync.py`, add:

```python
    def cat(self, path: str, *, strict: bool = True) -> bytes:
        from nexus.core.dispatch import cat_path

        return cat_path(self._kernel, path, context=LOCAL_CONTEXT, strict=strict)
```

- [ ] **Step 5: Run cat tests and CLI import smoke**

Run:

```bash
uv run pytest tests/unit/fs/test_dispatch_cat.py tests/unit/core/test_dispatch_registry.py -q
uv run python -c "from nexus.fs._cli import cat; print(cat.name)"
```

Expected: tests pass and the CLI command import prints `cat`.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/nexus/core/dispatch.py src/nexus/fs/_cli.py src/nexus/fs/_sync.py tests/unit/fs/test_dispatch_cat.py
git commit -m "feat: route cat through operation dispatch"
```

## Task 5: Backend Overrides For Slack, GitHub, And S3

**Files:**
- Modify: `src/nexus/core/dispatch.py`
- Modify: `src/nexus/fs/_helpers.py`
- Modify: `src/nexus/backends/connectors/slack/transport.py`
- Modify: `src/nexus/backends/connectors/slack/connector.py`
- Modify: `src/nexus/backends/connectors/github/connector.py`
- Modify: `src/nexus/backends/storage/path_s3.py`
- Modify: `src/nexus/backends/transports/s3_transport.py`
- Create: `tests/unit/backends/connectors/test_slack_dispatch.py`
- Create: `tests/unit/backends/connectors/test_github_dispatch.py`
- Create: `tests/unit/backends/test_s3_dispatch.py`

- [ ] **Step 1: Write failing Slack dispatch test**

Create `tests/unit/backends/connectors/test_slack_dispatch.py`:

```python
from __future__ import annotations

from nexus.backends.connectors.slack.transport import SlackTransport


class FakeClient:
    def search_messages(self, query: str, count: int = 20) -> dict[str, object]:
        assert query == "error"
        assert count == 2
        return {
            "ok": True,
            "messages": {
                "matches": [
                    {
                        "channel": {"name": "general"},
                        "text": "first error",
                        "ts": "1.000",
                    },
                    {
                        "channel": {"name": "random"},
                        "text": "second error",
                        "ts": "2.000",
                    },
                ]
            },
        }


class FakeSlackTransport(SlackTransport):
    def __init__(self) -> None:
        self._context = None
        self._max_messages_per_channel = 100

    def _get_slack_client(self) -> FakeClient:
        return FakeClient()


def test_slack_transport_search_messages_maps_to_grep_shape() -> None:
    transport = FakeSlackTransport()
    matches = transport.search_messages("error", max_results=2, ignore_case=False)
    assert matches == [
        {
            "file": "/slack/channels/general.yaml",
            "line": 1,
            "content": "first error",
            "match": "error",
        },
        {
            "file": "/slack/channels/random.yaml",
            "line": 1,
            "content": "second error",
            "match": "error",
        },
    ]
```

- [ ] **Step 2: Write failing GitHub and S3 tests**

Create `tests/unit/backends/connectors/test_github_dispatch.py`:

```python
from __future__ import annotations

from nexus.backends.connectors.github.connector import GitHubConnector


class FakeGitHub(GitHubConnector):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _execute_cli(self, args, context=None, env=None):
        self.calls.append(list(args))

        class Result:
            ok = True
            stdout = "hello"

        return Result()

    def _get_user_token(self, context):
        return None


def test_github_raw_read_uses_gh_api_for_raw_paths() -> None:
    backend = FakeGitHub()
    assert backend.raw_read("owner/repo/main/README.md") == b"hello"
    assert backend.calls == [["gh", "api", "repos/owner/repo/contents/README.md?ref=main", "-H", "Accept: application/vnd.github.raw"]]
```

Create `tests/unit/backends/test_s3_dispatch.py`:

```python
from __future__ import annotations

from nexus.backends.storage.path_s3 import PathS3Backend


class FakeTransport:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def fingerprint(self, key: str) -> str:
        self.keys.append(key)
        return "etag:abc"


class FakeS3(PathS3Backend):
    def __init__(self) -> None:
        self._s3_transport = FakeTransport()
        self.prefix = "prefix"

    def _get_key_path(self, backend_path: str) -> str:
        return f"{self.prefix}/{backend_path.lstrip('/')}"


def test_s3_fingerprint_uses_head_metadata_path() -> None:
    backend = FakeS3()
    assert backend.fingerprint("/docs/a.txt") == "etag:abc"
    assert backend._s3_transport.keys == ["prefix/docs/a.txt"]
```

- [ ] **Step 3: Run backend tests and confirm they fail**

Run:

```bash
uv run pytest tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py -q
```

Expected: attribute failures for `search_messages`, `raw_read`, and `fingerprint`.

- [ ] **Step 4: Implement Slack search pushdown**

Add to `src/nexus/backends/connectors/slack/transport.py`:

```python
    def search_messages(
        self,
        query: str,
        *,
        max_results: int = 100,
        ignore_case: bool = False,
    ) -> list[dict[str, Any]]:
        """Push grep-style search down to Slack search.messages."""
        client = self._get_slack_client()
        result = client.search_messages(query, count=max_results)
        if not result.get("ok"):
            error = result.get("error", "unknown_error")
            raise BackendError(f"Slack search failed: {error}", backend="slack")
        matches: list[dict[str, Any]] = []
        needle = query.lower() if ignore_case else query
        raw_matches = result.get("messages", {}).get("matches", [])
        for item in raw_matches[:max_results]:
            text = str(item.get("text", ""))
            haystack = text.lower() if ignore_case else text
            if needle and needle not in haystack:
                continue
            channel = item.get("channel", {})
            channel_name = channel.get("name") if isinstance(channel, dict) else None
            channel_name = channel_name or str(item.get("channel_name", "unknown"))
            matches.append(
                {
                    "file": f"/slack/channels/{channel_name}.yaml",
                    "line": 1,
                    "content": text,
                    "match": query,
                }
            )
        return matches
```

Add to `src/nexus/backends/connectors/slack/connector.py`:

```python
    def grep_messages(
        self,
        pattern: str,
        *,
        context: "OperationContext | None" = None,
        max_results: int = 100,
        ignore_case: bool = False,
    ) -> list[dict[str, Any]]:
        self._bind_transport(context)
        return self._transport.search_messages(
            pattern,
            max_results=max_results,
            ignore_case=ignore_case,
        )
```

- [ ] **Step 5: Implement GitHub raw read**

Add to `src/nexus/backends/connectors/github/connector.py`:

```python
    def raw_read(self, path: str, context: Any = None) -> bytes:
        """Read raw repository content through gh api.

        Path shape: owner/repo/ref/path/to/file. The ref segment may be a branch
        or tag that does not contain slashes.
        """
        parts = path.strip("/").split("/", 3)
        if len(parts) != 4:
            return self.read_content(path, context=context)
        owner, repo, ref, file_path = parts
        args = [
            self.CLI_NAME,
            "api",
            f"repos/{owner}/{repo}/contents/{file_path}?ref={ref}",
            "-H",
            "Accept: application/vnd.github.raw",
        ]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        result = self._execute_cli(args, context=context, env=auth_env)
        return result.stdout.encode("utf-8") if result.ok else b""
```

- [ ] **Step 6: Implement S3 fingerprint**

Add to `src/nexus/backends/transports/s3_transport.py`:

```python
    def fingerprint(self, key: str) -> str:
        """Return a stable S3 object fingerprint without downloading the object."""
        meta = self.get_object_metadata(key)
        version = meta.get("version_id")
        etag = meta.get("etag")
        if version and version != "null":
            return f"version:{version}"
        if etag:
            return f"etag:{etag}"
        return f"size:{meta.get('size', 0)}"
```

Add to `src/nexus/backends/storage/path_s3.py`:

```python
    def fingerprint(self, path: str, context: "OperationContext | None" = None) -> str:
        backend_path = (
            context.backend_path if context and context.backend_path else path.lstrip("/")
        )
        return self._s3_transport.fingerprint(self._get_key_path(backend_path))
```

- [ ] **Step 7: Route grep through Python registry for Slack override**

Add this helper to `src/nexus/core/dispatch.py`:

```python
def grep_path(
    kernel: Any,
    pattern: str,
    path: str = "/",
    *,
    context: Any = None,
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    metadata = _metadata_from_kernel(kernel, path, context=context)
    backend = normalize_backend(str(metadata.get("backend") or metadata.get("backend_name") or ""))
    filetype = FileType(metadata.get("filetype") or FileType.UNKNOWN)
    handler = get_global_registry().resolve("grep", filetype, backend)
    if handler is None:
        return None
    req = OperationRequest(
        op="grep",
        path=path,
        filetype=filetype,
        backend=backend,
        kernel=kernel,
        context=context,
        metadata=metadata,
        pattern=pattern,
        ignore_case=ignore_case,
        max_results=max_results,
    )
    return handler(req)
```

In `src/nexus/fs/_helpers.py`, at the start of `grep()` before `inner = getattr(...)`, add:

```python
    from nexus.core.dispatch import grep_path

    pushed_down = grep_path(
        kernel,
        pattern,
        path,
        context=LOCAL_CONTEXT,
        ignore_case=ignore_case,
        max_results=max_results,
    )
    if pushed_down is not None:
        return pushed_down
```

- [ ] **Step 8: Run backend and dispatch tests**

Run:

```bash
uv run pytest tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py tests/unit/core/test_dispatch_registry.py -q
```

Expected: tests pass.

- [ ] **Step 9: Commit Task 5**

```bash
git add src/nexus/core/dispatch.py src/nexus/fs/_helpers.py src/nexus/backends/connectors/slack/transport.py src/nexus/backends/connectors/slack/connector.py src/nexus/backends/connectors/github/connector.py src/nexus/backends/storage/path_s3.py src/nexus/backends/transports/s3_transport.py tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py
git commit -m "feat: migrate backend operation overrides"
```

## Task 6: Benchmark And Documentation

**Files:**
- Modify: `rust/kernel/Cargo.toml`
- Create: `rust/kernel/benches/ops_registry_bench.rs`
- Create: `docs/architecture/ops-dispatch-registry.md`

- [ ] **Step 1: Add benchmark file**

Add to `rust/kernel/Cargo.toml`:

```toml
[[bench]]
name = "ops_registry_bench"
harness = false
```

Create `rust/kernel/benches/ops_registry_bench.rs`:

```rust
use criterion::{black_box, criterion_group, criterion_main, Criterion};
use kernel::core::dispatch::ops_registry::{
    BackendKind, CatHandlerKind, FileType, OpHandler, OpKey, OpName, OpsRegistry,
};

fn direct_default() -> OpHandler {
    OpHandler::Cat(CatHandlerKind::Default)
}

fn registry_default(registry: &OpsRegistry) -> Option<OpHandler> {
    registry.resolve("cat", &FileType::Unknown, &BackendKind::Local)
}

fn bench_ops_registry(c: &mut Criterion) {
    let registry = OpsRegistry::new();
    registry
        .register(
            OpKey::new(OpName::new("cat"), None, None),
            OpHandler::Cat(CatHandlerKind::Default),
        )
        .unwrap();
    c.bench_function("ops_direct_default", |b| {
        b.iter(|| black_box(direct_default()))
    });
    c.bench_function("ops_registry_default", |b| {
        b.iter(|| black_box(registry_default(black_box(&registry))))
    });
}

criterion_group!(benches, bench_ops_registry);
criterion_main!(benches);
```

- [ ] **Step 2: Add docs page**

Create `docs/architecture/ops-dispatch-registry.md`:

```markdown
# Operation Dispatch Registry

The operation dispatch registry selects an implementation for a filesystem operation after VFS routing has identified the path and backend. It does not replace mount routing, virtual path resolvers, intercept hooks, permission checks, or observers.

Each handler is keyed by:

- operation name, such as `cat`, `grep`, `raw_read`, or `fingerprint`
- file type, such as `json` or `parquet`
- backend kind, such as `s3`, `slack`, or `github`

Resolution probes four keys in order:

1. `(op, filetype, backend)`
2. `(op, *, backend)`
3. `(op, filetype, *)`
4. `(op, *, *)`

Backends use `(op, *, backend)` for API pushdown, for example Slack grep. Parsers use `(op, filetype, *)` for content rendering, for example JSON or parquet cat. Defaults use `(op, *, *)`.

Boot registration is explicit:

1. default operations
2. parser operations
3. backend operations

Duplicate registration is rejected unless the caller uses the replace API. This keeps boot order deterministic and makes override intent visible in tests.

To add an override:

1. Add or reuse a handler function.
2. Register it with the most specific key it needs.
3. Add a unit test for resolution order and a behavior test using a fake backend or parser dependency.
4. Run focused tests plus the operation registry benchmark when the handler affects hot paths.
```

- [ ] **Step 3: Run benchmark smoke and doc checks**

Run:

```bash
cargo bench -p kernel --bench ops_registry_bench -- --warm-up-time 1 --measurement-time 2
git diff --check
```

Expected: benchmark runs and prints timings for direct and registry paths; `git diff --check` has no whitespace errors. Compare the reported direct and registry median/mean values. If registry overhead is above 5%, replace `HashMap` with `ahash::AHashMap` in `OpsRegistry` and rerun the benchmark.

- [ ] **Step 4: Commit Task 6**

```bash
git add rust/kernel/Cargo.toml rust/kernel/benches/ops_registry_bench.rs docs/architecture/ops-dispatch-registry.md
git commit -m "docs: document operation dispatch registry"
```

## Task 7: Final Verification

**Files:**
- No new files unless verification reveals a defect.

- [ ] **Step 1: Run focused Rust tests**

```bash
cargo test -p kernel ops_registry --lib
cargo test -p kernel sys_cat --lib
```

Expected: both commands pass.

- [ ] **Step 2: Run focused Python tests**

```bash
uv run pytest tests/unit/core/test_dispatch_registry.py tests/unit/fs/test_dispatch_cat.py tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run codegen check**

```bash
uv run python scripts/codegen_kernel_abi.py --check
```

Expected: no generated file diffs.

- [ ] **Step 4: Run formatting/lint smoke**

```bash
uv run ruff check src/nexus/core/dispatch.py tests/unit/core/test_dispatch_registry.py tests/unit/fs/test_dispatch_cat.py tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py
uv run ruff format --check src/nexus/core/dispatch.py tests/unit/core/test_dispatch_registry.py tests/unit/fs/test_dispatch_cat.py tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py
cargo fmt -p kernel --check
```

Expected: all commands pass.

- [ ] **Step 5: Run benchmark evidence**

```bash
cargo bench -p kernel --bench ops_registry_bench -- --warm-up-time 1 --measurement-time 2
```

Expected: `ops_registry_default` mean/median is within 5% of `ops_direct_default`, or the implementation has been optimized and rerun until it meets that bar.

- [ ] **Step 6: Commit any verification fixes**

If fixes were required:

```bash
git add rust/kernel/src/core/dispatch/ops_registry.rs rust/kernel/src/core/dispatch/mod.rs rust/kernel/src/kernel/mod.rs rust/kernel/src/kernel/io.rs rust/kernel/src/generated_kernel_abi_pyo3.rs rust/kernel/Cargo.toml rust/kernel/benches/ops_registry_bench.rs src/nexus/core/dispatch.py src/nexus/core/nexus_fs.py src/nexus/fs/_helpers.py src/nexus/fs/_cli.py src/nexus/fs/_sync.py src/nexus/backends/connectors/slack/transport.py src/nexus/backends/connectors/slack/connector.py src/nexus/backends/connectors/github/connector.py src/nexus/backends/storage/path_s3.py src/nexus/backends/transports/s3_transport.py tests/unit/core/test_dispatch_registry.py tests/unit/fs/test_dispatch_cat.py tests/unit/backends/connectors/test_slack_dispatch.py tests/unit/backends/connectors/test_github_dispatch.py tests/unit/backends/test_s3_dispatch.py docs/architecture/ops-dispatch-registry.md
git commit -m "fix: stabilize operation dispatch verification"
```

If no fixes were required, do not create an empty commit.
