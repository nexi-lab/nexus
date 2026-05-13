# Issue 4085 VFS Capability Handshake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed VFS `Initialize` handshake that exposes server, command, workspace, and per-mount backend capabilities to Python, TypeScript, and nexus-fuse clients.

**Architecture:** The gRPC service gets a typed `Initialize` RPC and generated stubs. Python owns the canonical capability aggregation as a JSON/dict shape used by the Python bridge and HTTP endpoint; Rust converts that shape plus Rust mount facts into protobuf responses. Python remote clients cache the response and gate unsupported operations, while TypeScript and nexus-fuse consume the same HTTP JSON shape.

**Tech Stack:** protobuf/gRPC, tonic/prost, PyO3, FastAPI, pytest, TypeScript/Vitest, Rust reqwest/fuser/mockito.

---

## File Structure

- Create `proto/nexus/grpc/initialize.proto`: VFS capability message definitions.
- Modify `proto/nexus/grpc/vfs/vfs.proto`: import `initialize.proto` and add `Initialize` RPC.
- Generate `src/nexus/grpc/initialize_pb2.py`: Python protobuf messages.
- Regenerate `src/nexus/grpc/vfs/vfs_pb2.py` and `src/nexus/grpc/vfs/vfs_pb2_grpc.py`: service method update.
- Modify `rust/kernel/build.rs`: compile both VFS proto files and track rebuild inputs.
- Create `src/nexus/grpc/capability_discovery.py`: canonical Python dict builder, backend feature mapping, path capability checks.
- Modify `src/nexus/grpc/servicer.py`: add `initialize_sync()` bridge callback.
- Modify `src/nexus/grpc/server.py`: pass `initialize_sync` into the Rust server start function.
- Modify `rust/transport/src/grpc.rs`: add PyBridge callback, `Initialize` implementation, dict-to-protobuf conversion.
- Modify `src/nexus/server/api/core/debug.py`: add authenticated HTTP capability endpoint.
- Modify `src/nexus/remote/rpc_transport.py`: add `initialize()` and cached `capabilities`.
- Modify `src/nexus/__init__.py`: call initialize during `profile="remote"` connect and attach `nfs.capabilities`.
- Modify `src/nexus/factory/_remote.py`: gate known unsupported remote operations.
- Modify `packages/nexus-api-client/src/types.ts`, `fetch-client.ts`, `index.ts`: TypeScript capability types and client method.
- Modify `nexus-fuse/src/client.rs`, `fs.rs`, `daemon.rs`, `main.rs`: HTTP capability fetch and FUSE operation gating.
- Add `docs/architecture/vfs-capability-discovery.md`: user-facing protocol and client docs.

## Task 1: Proto And Codegen

**Files:**
- Create: `proto/nexus/grpc/initialize.proto`
- Modify: `proto/nexus/grpc/vfs/vfs.proto`
- Modify: `rust/kernel/build.rs`
- Generate: `src/nexus/grpc/initialize_pb2.py`
- Generate: `src/nexus/grpc/vfs/vfs_pb2.py`
- Generate: `src/nexus/grpc/vfs/vfs_pb2_grpc.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/grpc/test_initialize_proto.py`

- [ ] **Step 1: Write the failing proto import test**

Create `tests/unit/grpc/test_initialize_proto.py`:

```python
from nexus.grpc import initialize_pb2
from nexus.grpc.vfs import vfs_pb2, vfs_pb2_grpc


def test_initialize_messages_exist() -> None:
    request = initialize_pb2.InitializeRequest(
        client_name="pytest",
        client_version="0.0",
        protocol_version="0.1.0",
    )
    response = initialize_pb2.InitializeResponse(
        server_name="nexus",
        server_version="0.10.0",
        protocol_version="0.1.0",
        capabilities=initialize_pb2.Capabilities(
            posix=initialize_pb2.PosixCapabilities(read=True, stat=True),
            extensions=["x-nexus:versioning"],
        ),
    )

    assert request.client_name == "pytest"
    assert response.capabilities.posix.read is True
    assert "x-nexus:versioning" in response.capabilities.extensions


def test_nexus_vfs_stub_has_initialize() -> None:
    assert hasattr(vfs_pb2_grpc.NexusVFSServiceStub, "__init__")
    service = vfs_pb2.DESCRIPTOR.services_by_name["NexusVFSService"]
    assert "Initialize" in service.methods_by_name
```

- [ ] **Step 2: Run the proto test to verify it fails**

Run:

```bash
uv run pytest tests/unit/grpc/test_initialize_proto.py -q
```

Expected: FAIL with `ImportError: cannot import name 'initialize_pb2' from 'nexus.grpc'`.

- [ ] **Step 3: Add the initialize proto**

Create `proto/nexus/grpc/initialize.proto`:

```protobuf
// Capability discovery messages for Nexus VFS RPC.
syntax = "proto3";

package nexus.grpc.vfs;

message InitializeRequest {
  string client_name = 1;
  string client_version = 2;
  string protocol_version = 3;
  string auth_token = 4;
}

message InitializeResponse {
  string server_name = 1;
  string server_version = 2;
  string protocol_version = 3;
  Capabilities capabilities = 4;
}

message Capabilities {
  PosixCapabilities posix = 1;
  CommandCapabilities commands = 2;
  WorkspaceCapabilities workspace = 3;
  map<string, BackendCapabilities> backends = 4;
  repeated string extensions = 5;
}

message PosixCapabilities {
  bool read = 1;
  bool readdir = 2;
  bool stat = 3;
  bool write = 4;
  bool unlink = 5;
  bool mkdir = 6;
  bool rmdir = 7;
  bool rename = 8;
  bool glob = 9;
}

message StringFilter {
  repeated string allow = 1;
  repeated string deny = 2;
}

message CommandSupport {
  bool supported = 1;
  StringFilter filetype = 2;
}

message CommandCapabilities {
  CommandSupport grep = 1;
  CommandSupport glob = 2;
}

message WorkspaceCapabilities {
  bool snapshot = 1;
  bool restore = 2;
  bool watch = 3;
}

message BackendCapabilities {
  string backend_name = 1;
  string backend_type = 2;
  PosixCapabilities posix = 3;
  repeated string features = 4;
  repeated string extensions = 5;
  bool rust_native = 6;
  bool external = 7;
}
```

- [ ] **Step 4: Add the service method**

Modify `proto/nexus/grpc/vfs/vfs.proto`:

```protobuf
import "nexus/grpc/initialize.proto";

service NexusVFSService {
  rpc Initialize(InitializeRequest) returns (InitializeResponse);

  rpc Call(CallRequest) returns (CallResponse);
  rpc Read(ReadRequest) returns (ReadResponse);
  rpc Write(WriteRequest) returns (WriteResponse);
  rpc Delete(DeleteRequest) returns (DeleteResponse);
  rpc Ping(PingRequest) returns (PingResponse);
}
```

- [ ] **Step 5: Update Rust proto build input**

Modify `rust/kernel/build.rs` so the compile list includes both files:

```rust
fn main() -> Result<(), Box<dyn std::error::Error>> {
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }

    tonic_build::configure().compile_protos(
        &[
            "../../proto/nexus/grpc/initialize.proto",
            "../../proto/nexus/grpc/vfs/vfs.proto",
        ],
        &["../../proto"],
    )?;

    println!("cargo:rerun-if-changed=../../proto/nexus/grpc/initialize.proto");
    println!("cargo:rerun-if-changed=../../proto/nexus/grpc/vfs/vfs.proto");
    Ok(())
}
```

- [ ] **Step 6: Generate Python protobuf files**

Run:

```bash
uv run python -m grpc_tools.protoc -Iproto --python_out=src --grpc_python_out=src proto/nexus/grpc/initialize.proto proto/nexus/grpc/vfs/vfs.proto
```

Expected: `src/nexus/grpc/initialize_pb2.py`, `src/nexus/grpc/vfs/vfs_pb2.py`, and `src/nexus/grpc/vfs/vfs_pb2_grpc.py` are created or updated.

- [ ] **Step 7: Update generated-file config**

In `pyproject.toml`, add `nexus.grpc.initialize_pb2` beside the existing VFS protobuf modules in the mypy generated module list, and add `src/nexus/grpc/initialize_pb2.py` beside generated protobuf stub exclusions.

- [ ] **Step 8: Run the proto test to verify it passes**

Run:

```bash
uv run pytest tests/unit/grpc/test_initialize_proto.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit proto/codegen**

Run:

```bash
git add proto/nexus/grpc/initialize.proto proto/nexus/grpc/vfs/vfs.proto rust/kernel/build.rs src/nexus/grpc/initialize_pb2.py src/nexus/grpc/vfs/vfs_pb2.py src/nexus/grpc/vfs/vfs_pb2_grpc.py pyproject.toml tests/unit/grpc/test_initialize_proto.py
git commit -m "feat(#4085): add VFS initialize proto"
```

## Task 2: Shared Python Capability Discovery

**Files:**
- Create: `src/nexus/grpc/capability_discovery.py`
- Test: `tests/unit/grpc/test_capability_discovery.py`

- [ ] **Step 1: Write failing capability mapping tests**

Create `tests/unit/grpc/test_capability_discovery.py`:

```python
from types import SimpleNamespace

from nexus.contracts.backend_features import BackendFeature
from nexus.grpc.capability_discovery import (
    PROTOCOL_VERSION,
    build_initialize_response_dict,
    capability_for_path,
    empty_posix,
    posix_from_backend_features,
    writable_posix,
)


def test_posix_from_backend_features_maps_known_features() -> None:
    posix = posix_from_backend_features(
        {
            BackendFeature.DIRECTORY_LISTING,
            BackendFeature.PATH_DELETE,
            BackendFeature.RENAME,
        }
    )

    assert posix["read"] is True
    assert posix["stat"] is True
    assert posix["readdir"] is True
    assert posix["unlink"] is True
    assert posix["rmdir"] is True
    assert posix["rename"] is True
    assert posix["write"] is False


def test_build_initialize_response_dict_includes_mounts_and_extensions() -> None:
    kernel = SimpleNamespace(get_mount_points=lambda: ["/root", "/root/read-only"])
    nexus_fs = SimpleNamespace(_kernel=kernel)

    payload = build_initialize_response_dict(
        nexus_fs=nexus_fs,
        exposed_methods={"grep": object(), "glob": object(), "workspace_snapshot": object()},
        server_version="0.10.0",
        rust_mounts={
            "/": {
                "backend_name": "cas_local",
                "backend_type": "cas_local",
                "rust_native": True,
                "external": False,
                "posix": writable_posix(),
                "features": ["cas"],
                "extensions": [],
            },
            "/read-only": {
                "backend_name": "gdrive",
                "backend_type": "gdrive",
                "rust_native": False,
                "external": True,
                "posix": {**empty_posix(), "read": True, "stat": True, "readdir": True},
                "features": ["directory_listing"],
                "extensions": ["x-nexus:versioning"],
            },
        },
    )

    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["capabilities"]["commands"]["grep"]["supported"] is True
    assert payload["capabilities"]["commands"]["glob"]["supported"] is True
    assert payload["capabilities"]["workspace"]["snapshot"] is True
    assert payload["capabilities"]["backends"]["/"]["posix"]["write"] is True
    assert payload["capabilities"]["backends"]["/read-only"]["posix"]["write"] is False
    assert "x-nexus:versioning" in payload["capabilities"]["extensions"]


def test_capability_for_path_uses_longest_mount_prefix() -> None:
    capabilities = {
        "posix": writable_posix(),
        "backends": {
            "/": {"posix": writable_posix()},
            "/mnt/readonly": {
                "posix": {**empty_posix(), "read": True, "stat": True, "readdir": True}
            },
        },
    }

    assert capability_for_path(capabilities, "/tmp/file.txt", "write") is True
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "write") is False
    assert capability_for_path(capabilities, "/mnt/readonly/file.txt", "read") is True
```

- [ ] **Step 2: Run capability mapping tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/grpc/test_capability_discovery.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'nexus.grpc.capability_discovery'`.

- [ ] **Step 3: Implement shared capability discovery**

Create `src/nexus/grpc/capability_discovery.py`:

```python
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from nexus.contracts.backend_features import BackendFeature
from nexus.core.path_utils import extract_zone_id, normalize_path

PROTOCOL_VERSION = "0.1.0"
POSIX_KEYS = ("read", "readdir", "stat", "write", "unlink", "mkdir", "rmdir", "rename", "glob")


def empty_posix() -> dict[str, bool]:
    return dict.fromkeys(POSIX_KEYS, False)


def readonly_posix() -> dict[str, bool]:
    posix = empty_posix()
    posix.update({"read": True, "readdir": True, "stat": True})
    return posix


def writable_posix() -> dict[str, bool]:
    posix = readonly_posix()
    posix.update({"write": True, "unlink": True, "mkdir": True, "rmdir": True, "rename": True})
    return posix


def _feature_value(feature: BackendFeature | str) -> str:
    return feature.value if isinstance(feature, BackendFeature) else str(feature)


def _feature_values(features: Iterable[BackendFeature | str]) -> set[str]:
    return {_feature_value(feature) for feature in features}


def posix_from_backend_features(features: Iterable[BackendFeature | str]) -> dict[str, bool]:
    values = _feature_values(features)
    posix = readonly_posix()
    posix["readdir"] = BackendFeature.DIRECTORY_LISTING.value in values
    posix["unlink"] = BackendFeature.PATH_DELETE.value in values
    posix["rmdir"] = BackendFeature.PATH_DELETE.value in values
    posix["rename"] = BackendFeature.RENAME.value in values
    posix["write"] = bool(
        {
            BackendFeature.CAS.value,
            BackendFeature.ROOT_PATH.value,
            BackendFeature.PATH_DELETE.value,
            BackendFeature.MULTIPART_UPLOAD.value,
            BackendFeature.RESUMABLE_UPLOAD.value,
        }
        & values
    )
    return posix


def backend_capability_dict(
    *,
    backend_name: str = "",
    backend_type: str = "",
    features: Iterable[BackendFeature | str] = (),
    posix: Mapping[str, bool] | None = None,
    rust_native: bool = False,
    external: bool = False,
    extensions: Iterable[str] = (),
) -> dict[str, Any]:
    feature_values = sorted(_feature_values(features))
    extension_values = set(extensions)
    if BackendFeature.NATIVE_VERSIONING.value in feature_values:
        extension_values.add("x-nexus:versioning")
    return {
        "backend_name": backend_name,
        "backend_type": backend_type or backend_name,
        "posix": dict(posix or posix_from_backend_features(feature_values)),
        "features": feature_values,
        "extensions": sorted(extension_values),
        "rust_native": bool(rust_native),
        "external": bool(external),
    }


def _mount_points_from_kernel(nexus_fs: Any) -> list[str]:
    kernel = getattr(nexus_fs, "_kernel", None)
    if kernel is None or not hasattr(kernel, "get_mount_points"):
        return []
    points: list[str] = []
    for canonical in kernel.get_mount_points():
        _zone, user_path = extract_zone_id(str(canonical))
        points.append(user_path)
    return sorted(set(points))


def _command_capabilities(exposed_methods: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "grep": {"supported": "grep" in exposed_methods, "filetype": {"allow": [], "deny": []}},
        "glob": {"supported": "glob" in exposed_methods, "filetype": {"allow": [], "deny": []}},
    }


def _workspace_capabilities(exposed_methods: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "snapshot": "workspace_snapshot" in exposed_methods,
        "restore": "workspace_restore" in exposed_methods,
        "watch": "workspace_watch" in exposed_methods,
    }


def build_initialize_response_dict(
    *,
    nexus_fs: Any,
    exposed_methods: Mapping[str, Any],
    server_version: str,
    rust_mounts: Mapping[str, Mapping[str, Any]] | None = None,
    server_name: str = "nexus",
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    backends: dict[str, dict[str, Any]] = {}
    for mount_point in _mount_points_from_kernel(nexus_fs):
        backends[mount_point] = backend_capability_dict(
            backend_name="",
            backend_type="",
            posix=readonly_posix(),
        )
    for mount_point, raw in (rust_mounts or {}).items():
        backends[normalize_path(mount_point)] = backend_capability_dict(
            backend_name=str(raw.get("backend_name") or ""),
            backend_type=str(raw.get("backend_type") or raw.get("backend_name") or ""),
            features=raw.get("features") or (),
            posix=raw.get("posix"),
            rust_native=bool(raw.get("rust_native", False)),
            external=bool(raw.get("external", False)),
            extensions=raw.get("extensions") or (),
        )
    if "/" not in backends:
        backends["/"] = backend_capability_dict(
            backend_name="",
            backend_type="",
            posix=readonly_posix(),
        )
    root_posix = dict(backends["/"]["posix"])
    extensions = sorted(
        {extension for backend in backends.values() for extension in backend.get("extensions", [])}
    )
    return {
        "server_name": server_name,
        "server_version": server_version,
        "protocol_version": protocol_version,
        "capabilities": {
            "posix": root_posix,
            "commands": _command_capabilities(exposed_methods),
            "workspace": _workspace_capabilities(exposed_methods),
            "backends": dict(sorted(backends.items())),
            "extensions": extensions,
        },
    }


def capability_for_path(capabilities: Mapping[str, Any] | None, path: str, capability: str) -> bool | None:
    if not capabilities:
        return None
    normalized = normalize_path(path)
    backends = capabilities.get("backends") if isinstance(capabilities, Mapping) else None
    if isinstance(backends, Mapping):
        best_mount = ""
        best_posix: Mapping[str, Any] | None = None
        for mount_point, backend in backends.items():
            mount = normalize_path(str(mount_point))
            if normalized == mount or normalized.startswith(mount.rstrip("/") + "/"):
                if len(mount) > len(best_mount) and isinstance(backend, Mapping):
                    posix = backend.get("posix")
                    if isinstance(posix, Mapping):
                        best_mount = mount
                        best_posix = posix
        if best_posix is not None and capability in best_posix:
            return bool(best_posix[capability])
    posix = capabilities.get("posix")
    if isinstance(posix, Mapping) and capability in posix:
        return bool(posix[capability])
    return None
```

- [ ] **Step 4: Run capability mapping tests to verify they pass**

Run:

```bash
uv run pytest tests/unit/grpc/test_capability_discovery.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit capability mapping**

Run:

```bash
git add src/nexus/grpc/capability_discovery.py tests/unit/grpc/test_capability_discovery.py
git commit -m "feat(#4085): add capability discovery mapping"
```

## Task 3: gRPC Server Initialize Handler

**Files:**
- Modify: `src/nexus/grpc/servicer.py`
- Modify: `src/nexus/grpc/server.py`
- Modify: `rust/transport/src/grpc.rs`
- Test: `rust/transport/src/grpc.rs`
- Test: `tests/unit/grpc/test_vfs_initialize_dispatcher.py`

- [ ] **Step 1: Write failing Python dispatcher tests**

Create `tests/unit/grpc/test_vfs_initialize_dispatcher.py`:

```python
from types import SimpleNamespace

from nexus.grpc.capability_discovery import PROTOCOL_VERSION
from nexus.grpc.servicer import VFSCallDispatcher


def test_initialize_sync_returns_capability_payload(event_loop) -> None:
    kernel = SimpleNamespace(get_mount_points=lambda: ["/root"])
    nexus_fs = SimpleNamespace(_kernel=kernel)
    dispatcher = VFSCallDispatcher(
        nexus_fs=nexus_fs,
        exposed_methods={"grep": object(), "glob": object(), "workspace_snapshot": object()},
        api_key=None,
        loop=event_loop,
    )

    payload = dispatcher.initialize_sync(
        {
            "client_name": "pytest",
            "client_version": "0.0",
            "protocol_version": PROTOCOL_VERSION,
        },
        {
            "authenticated": True,
            "subject_type": "user",
            "subject_id": "alice",
            "zone_id": "root",
            "is_admin": False,
        },
        {},
    )

    assert payload["server_name"] == "nexus"
    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["capabilities"]["commands"]["grep"]["supported"] is True
    assert "/" in payload["capabilities"]["backends"]
```

- [ ] **Step 2: Run dispatcher test to verify it fails**

Run:

```bash
uv run pytest tests/unit/grpc/test_vfs_initialize_dispatcher.py -q
```

Expected: FAIL with `AttributeError: 'VFSCallDispatcher' object has no attribute 'initialize_sync'`.

- [ ] **Step 3: Add the Python initialize bridge**

Modify `src/nexus/grpc/servicer.py`:

```python
    def initialize_sync(
        self,
        request_dict: dict[str, Any],
        auth_dict: dict[str, Any],
        rust_mounts: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the VFS Initialize response payload for the Rust gRPC server."""
        try:
            from importlib.metadata import version as _version

            server_version = _version("nexus-ai-fs")
        except Exception:
            server_version = "unknown"

        from nexus.grpc.capability_discovery import build_initialize_response_dict

        return build_initialize_response_dict(
            nexus_fs=self._nexus_fs,
            exposed_methods=self._exposed_methods,
            server_version=server_version,
            rust_mounts=rust_mounts or {},
        )
```

Place it after `dispatch_call_sync()` and before `_dispatch_async()`.

- [ ] **Step 4: Wire the callback into startup**

Modify `src/nexus/grpc/server.py`:

```python
    handle = nexus_runtime.start_vfs_grpc_server(
        py_kernel,
        bind_addr,
        api_key,
        tls_cert_pem,
        tls_key_pem,
        tls_ca_pem,
        server_version,
        dispatcher.authenticate_sync,
        dispatcher.dispatch_call_sync,
        dispatcher.initialize_sync,
    )
```

- [ ] **Step 5: Extend Rust bridge and PyO3 signature**

Modify `rust/transport/src/grpc.rs`:

```rust
pub struct PyBridge {
    pub authenticate: Py<pyo3::PyAny>,
    pub dispatch_call: Py<pyo3::PyAny>,
    pub initialize: Py<pyo3::PyAny>,
}
```

Extend the `#[pyo3(signature = (...))]` and `start_vfs_grpc_server(...)` arguments with `initialize: Py<pyo3::PyAny>`, then build:

```rust
    let bridge = PyBridge {
        authenticate,
        dispatch_call,
        initialize,
    };
```

- [ ] **Step 6: Implement Rust Initialize conversion helpers**

Add imports near the existing generated proto import:

```rust
use kernel::kernel::vfs_proto::{
    nexus_vfs_service_server::{NexusVfsService, NexusVfsServiceServer},
    BackendCapabilities, CallRequest, CallResponse, Capabilities, CommandCapabilities,
    CommandSupport, DeleteRequest, DeleteResponse, InitializeRequest, InitializeResponse,
    PingRequest, PingResponse, PosixCapabilities, ReadRequest, ReadResponse, StringFilter,
    WorkspaceCapabilities, WriteRequest, WriteResponse,
};
use kernel::vfs_router::extract_zone_from_canonical;
use pyo3::types::{PyBool, PyDict, PyList};
use serde_json::Value as JsonValue;
```

Add helpers before `impl NexusVfsService for VfsServiceImpl`:

```rust
fn py_bool(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    Ok(dict
        .get_item(key)?
        .and_then(|v| v.cast::<PyBool>().ok())
        .map(|v| v.is_true())
        .unwrap_or(false))
}

fn py_string(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    Ok(dict
        .get_item(key)?
        .map(|v| v.extract::<String>())
        .transpose()?
        .unwrap_or_default())
}

fn py_string_list(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Vec<String>> {
    match dict.get_item(key)? {
        Some(value) => {
            let list = value.cast::<PyList>()?;
            list.iter().map(|item| item.extract::<String>()).collect()
        }
        None => Ok(Vec::new()),
    }
}

fn py_posix(value: Option<Bound<'_, PyAny>>) -> PyResult<PosixCapabilities> {
    let Some(value) = value else {
        return Ok(PosixCapabilities::default());
    };
    let dict = value.cast::<PyDict>()?;
    Ok(PosixCapabilities {
        read: py_bool(dict, "read")?,
        readdir: py_bool(dict, "readdir")?,
        stat: py_bool(dict, "stat")?,
        write: py_bool(dict, "write")?,
        unlink: py_bool(dict, "unlink")?,
        mkdir: py_bool(dict, "mkdir")?,
        rmdir: py_bool(dict, "rmdir")?,
        rename: py_bool(dict, "rename")?,
        glob: py_bool(dict, "glob")?,
    })
}
```

Add the remaining conversion helpers directly after `py_posix`:

```rust
fn py_string_filter(value: Option<Bound<'_, PyAny>>) -> PyResult<StringFilter> {
    let Some(value) = value else {
        return Ok(StringFilter::default());
    };
    let dict = value.cast::<PyDict>()?;
    Ok(StringFilter {
        allow: py_string_list(dict, "allow")?,
        deny: py_string_list(dict, "deny")?,
    })
}

fn py_command_support(value: Option<Bound<'_, PyAny>>) -> PyResult<CommandSupport> {
    let Some(value) = value else {
        return Ok(CommandSupport::default());
    };
    let dict = value.cast::<PyDict>()?;
    Ok(CommandSupport {
        supported: py_bool(dict, "supported")?,
        filetype: Some(py_string_filter(dict.get_item("filetype")?)?),
    })
}

fn py_command_capabilities(value: Option<Bound<'_, PyAny>>) -> PyResult<CommandCapabilities> {
    let Some(value) = value else {
        return Ok(CommandCapabilities::default());
    };
    let dict = value.cast::<PyDict>()?;
    Ok(CommandCapabilities {
        grep: Some(py_command_support(dict.get_item("grep")?)?),
        glob: Some(py_command_support(dict.get_item("glob")?)?),
    })
}

fn py_workspace_capabilities(value: Option<Bound<'_, PyAny>>) -> PyResult<WorkspaceCapabilities> {
    let Some(value) = value else {
        return Ok(WorkspaceCapabilities::default());
    };
    let dict = value.cast::<PyDict>()?;
    Ok(WorkspaceCapabilities {
        snapshot: py_bool(dict, "snapshot")?,
        restore: py_bool(dict, "restore")?,
        watch: py_bool(dict, "watch")?,
    })
}

fn py_backend_capabilities(value: Bound<'_, PyAny>) -> PyResult<BackendCapabilities> {
    let dict = value.cast::<PyDict>()?;
    Ok(BackendCapabilities {
        backend_name: py_string(dict, "backend_name")?,
        backend_type: py_string(dict, "backend_type")?,
        posix: Some(py_posix(dict.get_item("posix")?)?),
        features: py_string_list(dict, "features")?,
        extensions: py_string_list(dict, "extensions")?,
        rust_native: py_bool(dict, "rust_native")?,
        external: py_bool(dict, "external")?,
    })
}

fn py_capabilities(value: Option<Bound<'_, PyAny>>) -> PyResult<Capabilities> {
    let Some(value) = value else {
        return Ok(Capabilities::default());
    };
    let dict = value.cast::<PyDict>()?;
    let mut backends = std::collections::HashMap::new();
    if let Some(raw_backends) = dict.get_item("backends")? {
        for (key, backend) in raw_backends.cast::<PyDict>()? {
            backends.insert(key.extract::<String>()?, py_backend_capabilities(backend)?);
        }
    }
    Ok(Capabilities {
        posix: Some(py_posix(dict.get_item("posix")?)?),
        commands: Some(py_command_capabilities(dict.get_item("commands")?)?),
        workspace: Some(py_workspace_capabilities(dict.get_item("workspace")?)?),
        backends,
        extensions: py_string_list(dict, "extensions")?,
    })
}

fn initialize_response_from_py(value: &Bound<'_, PyAny>) -> PyResult<InitializeResponse> {
    let dict = value.cast::<PyDict>()?;
    Ok(InitializeResponse {
        server_name: py_string(dict, "server_name")?,
        server_version: py_string(dict, "server_version")?,
        protocol_version: py_string(dict, "protocol_version")?,
        capabilities: Some(py_capabilities(dict.get_item("capabilities")?)?),
    })
}

fn json_to_py(py: Python<'_>, value: &JsonValue) -> PyResult<Py<pyo3::PyAny>> {
    match value {
        JsonValue::Null => Ok(py.None()),
        JsonValue::Bool(v) => Ok(v.into_pyobject(py)?.into_any().unbind()),
        JsonValue::Number(v) => {
            if let Some(n) = v.as_i64() {
                Ok(n.into_pyobject(py)?.into_any().unbind())
            } else if let Some(n) = v.as_u64() {
                Ok(n.into_pyobject(py)?.into_any().unbind())
            } else {
                Ok(v.as_f64().unwrap_or_default().into_pyobject(py)?.into_any().unbind())
            }
        }
        JsonValue::String(v) => Ok(v.into_pyobject(py)?.into_any().unbind()),
        JsonValue::Array(values) => {
            let list = PyList::empty(py);
            for item in values {
                list.append(json_to_py(py, item)?)?;
            }
            Ok(list.into_any().unbind())
        }
        JsonValue::Object(values) => {
            let dict = PyDict::new(py);
            for (key, item) in values {
                dict.set_item(key, json_to_py(py, item)?)?;
            }
            Ok(dict.into_any().unbind())
        }
    }
}
```

- [ ] **Step 7: Add Rust mount facts**

Add a method on `VfsServiceImpl`:

```rust
fn rust_mounts_for_initialize(&self) -> serde_json::Map<String, serde_json::Value> {
    let mut mounts = serde_json::Map::new();
    for canonical in self.kernel.get_mount_points() {
        let (zone_id, mount_point) = extract_zone_from_canonical(&canonical);
        if let Ok(route) = self.kernel.route(&mount_point, &zone_id) {
            let backend_name = route
                .backend
                .as_ref()
                .map(|backend| backend.name().to_string())
                .unwrap_or_default();
            let posix = if route.backend.is_some() && !route.is_external {
                serde_json::json!({
                    "read": true,
                    "readdir": true,
                    "stat": true,
                    "write": true,
                    "unlink": true,
                    "mkdir": true,
                    "rmdir": true,
                    "rename": true,
                    "glob": false
                })
            } else {
                serde_json::json!({
                    "read": true,
                    "readdir": true,
                    "stat": true,
                    "write": false,
                    "unlink": false,
                    "mkdir": false,
                    "rmdir": false,
                    "rename": false,
                    "glob": false
                })
            };
            mounts.insert(
                mount_point,
                serde_json::json!({
                    "backend_name": backend_name,
                    "backend_type": backend_name,
                    "rust_native": route.backend.is_some(),
                    "external": route.is_external,
                    "posix": posix,
                    "features": [],
                    "extensions": []
                }),
            );
        }
    }
    mounts
}
```

- [ ] **Step 8: Add the `initialize` RPC implementation**

Inside `impl NexusVfsService for VfsServiceImpl`:

```rust
async fn initialize(
    &self,
    req: Request<InitializeRequest>,
) -> Result<Response<InitializeResponse>, Status> {
    let req = req.into_inner();
    let ctx = self.resolve_context(&req.auth_token).await?;
    let request_dict = serde_json::json!({
        "client_name": req.client_name,
        "client_version": req.client_version,
        "protocol_version": req.protocol_version,
    });
    let auth_dict = serde_json::json!({
        "authenticated": true,
        "subject_type": ctx.subject_type,
        "subject_id": ctx.subject_id.unwrap_or_else(|| ctx.user_id.clone()),
        "zone_id": ctx.zone_id,
        "is_admin": ctx.is_admin,
        "agent_id": ctx.agent_id,
    });
    let rust_mounts = serde_json::Value::Object(self.rust_mounts_for_initialize());
    let bridge = self.bridge.clone();

    let payload = tokio::task::spawn_blocking(move || {
        Python::attach(|py| -> PyResult<InitializeResponse> {
            let request_py = json_to_py(py, &request_dict)?;
            let auth_py = json_to_py(py, &auth_dict)?;
            let rust_mounts_py = json_to_py(py, &rust_mounts)?;
            let result = bridge.initialize.call1(py, (request_py, auth_py, rust_mounts_py))?;
            initialize_response_from_py(result.bind(py))
        })
    })
    .await
    .map_err(|e| Status::internal(format!("initialize task: {e}")))?
    .map_err(|e| Status::internal(format!("initialize bridge: {e}")))?;

    Ok(Response::new(payload))
}
```

- [ ] **Step 9: Add focused Rust tests for conversion helpers**

Add unit tests in `rust/transport/src/grpc.rs` that construct Python dicts under `Python::attach`, call `initialize_response_from_py`, and assert `server_name`, `capabilities.posix.write`, and a backend map entry.

Run:

```bash
cargo test -p transport --features python initialize_response_from_py
```

Expected: PASS.

- [ ] **Step 10: Run Python dispatcher test**

Run:

```bash
uv run pytest tests/unit/grpc/test_vfs_initialize_dispatcher.py -q
```

Expected: PASS.

- [ ] **Step 11: Commit gRPC server handler**

Run:

```bash
git add src/nexus/grpc/servicer.py src/nexus/grpc/server.py rust/transport/src/grpc.rs tests/unit/grpc/test_vfs_initialize_dispatcher.py
git commit -m "feat(#4085): serve VFS initialize handshake"
```

## Task 4: HTTP Endpoint

**Files:**
- Modify: `src/nexus/server/api/core/debug.py`
- Test: `tests/unit/server/test_vfs_initialize_endpoint.py`

- [ ] **Step 1: Write failing endpoint test**

Create `tests/unit/server/test_vfs_initialize_endpoint.py`:

```python
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.core.debug import router


def test_vfs_initialize_endpoint_returns_capabilities() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.api_key = None
    app.state.auth_provider = None
    app.state.auth_cache_store = None
    app.state.nexus_fs = SimpleNamespace(
        _kernel=SimpleNamespace(get_mount_points=lambda: ["/root"])
    )
    app.state.exposed_methods = {"grep": object(), "glob": object()}

    client = TestClient(app)
    response = client.get("/api/vfs/initialize")

    assert response.status_code == 200
    body = response.json()
    assert body["server_name"] == "nexus"
    assert body["capabilities"]["commands"]["grep"]["supported"] is True
    assert "/" in body["capabilities"]["backends"]
```

- [ ] **Step 2: Run endpoint test to verify it fails**

Run:

```bash
uv run pytest tests/unit/server/test_vfs_initialize_endpoint.py -q
```

Expected: FAIL with status `404`.

- [ ] **Step 3: Implement HTTP endpoint**

Modify `src/nexus/server/api/core/debug.py`:

```python
@router.get("/api/vfs/initialize")
async def initialize_vfs_capabilities(
    request: Request,
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Return VFS protocol capability metadata for HTTP-based clients."""
    try:
        from importlib.metadata import version as _version

        server_version = _version("nexus-ai-fs")
    except Exception:
        server_version = "unknown"

    from nexus.grpc.capability_discovery import build_initialize_response_dict

    return build_initialize_response_dict(
        nexus_fs=request.app.state.nexus_fs,
        exposed_methods=getattr(request.app.state, "exposed_methods", {}),
        server_version=server_version,
        rust_mounts={},
    )
```

- [ ] **Step 4: Run endpoint test to verify it passes**

Run:

```bash
uv run pytest tests/unit/server/test_vfs_initialize_endpoint.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit HTTP endpoint**

Run:

```bash
git add src/nexus/server/api/core/debug.py tests/unit/server/test_vfs_initialize_endpoint.py
git commit -m "feat(#4085): expose VFS capabilities over HTTP"
```

## Task 5: Python Remote Client And Capability Gating

**Files:**
- Modify: `src/nexus/remote/rpc_transport.py`
- Modify: `src/nexus/__init__.py`
- Modify: `src/nexus/factory/_remote.py`
- Test: `tests/unit/remote/test_rpc_transport.py`
- Test: `tests/integration/test_connect_quickstart.py`
- Test: `tests/unit/factory/test_remote_capability_gating.py`

- [ ] **Step 1: Add failing RPCTransport initialize tests**

Append to `tests/unit/remote/test_rpc_transport.py`:

```python
class TestRPCTransportInitialize:
    def test_initialize_success_caches_response(self, transport) -> None:
        mock_response = MagicMock()
        mock_response.server_name = "nexus"
        mock_response.server_version = "0.10.0"
        mock_response.protocol_version = "0.1.0"
        mock_response.capabilities = MagicMock()
        mock_response.capabilities.posix.write = True
        transport._mock_stub.Initialize.return_value = mock_response

        result = transport.initialize(client_name="pytest", client_version="0.0")

        assert result["server_name"] == "nexus"
        assert result["capabilities"]["posix"]["write"] is True
        assert transport.capabilities == result["capabilities"]

    def test_initialize_unimplemented_returns_none_for_old_server(self, transport) -> None:
        rpc_error = grpc.RpcError()
        rpc_error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
        rpc_error.details = lambda: "Method not found"
        transport._mock_stub.Initialize.side_effect = rpc_error

        assert transport.initialize(client_name="pytest", client_version="0.0") is None
        assert transport.capabilities is None
```

- [ ] **Step 2: Run RPCTransport initialize tests to verify they fail**

Run:

```bash
uv run pytest tests/unit/remote/test_rpc_transport.py::TestRPCTransportInitialize -q
```

Expected: FAIL with `AttributeError: 'RPCTransport' object has no attribute 'initialize'`.

- [ ] **Step 3: Implement RPCTransport.initialize**

Modify `src/nexus/remote/rpc_transport.py`:

```python
from nexus.grpc.capability_discovery import PROTOCOL_VERSION
```

Add in `__init__`:

```python
        self.capabilities: dict[str, Any] | None = None
```

Add methods:

```python
    def _posix_to_dict(self, posix: Any) -> dict[str, bool]:
        return {
            "read": bool(posix.read),
            "readdir": bool(posix.readdir),
            "stat": bool(posix.stat),
            "write": bool(posix.write),
            "unlink": bool(posix.unlink),
            "mkdir": bool(posix.mkdir),
            "rmdir": bool(posix.rmdir),
            "rename": bool(posix.rename),
            "glob": bool(posix.glob),
        }

    def _initialize_response_to_dict(self, response: Any) -> dict[str, Any]:
        capabilities = response.capabilities
        backends = {}
        for mount_point, backend in capabilities.backends.items():
            backends[mount_point] = {
                "backend_name": backend.backend_name,
                "backend_type": backend.backend_type,
                "posix": self._posix_to_dict(backend.posix),
                "features": list(backend.features),
                "extensions": list(backend.extensions),
                "rust_native": bool(backend.rust_native),
                "external": bool(backend.external),
            }
        return {
            "server_name": response.server_name,
            "server_version": response.server_version,
            "protocol_version": response.protocol_version,
            "capabilities": {
                "posix": self._posix_to_dict(capabilities.posix),
                "commands": {
                    "grep": {
                        "supported": bool(capabilities.commands.grep.supported),
                        "filetype": {
                            "allow": list(capabilities.commands.grep.filetype.allow),
                            "deny": list(capabilities.commands.grep.filetype.deny),
                        },
                    },
                    "glob": {
                        "supported": bool(capabilities.commands.glob.supported),
                        "filetype": {
                            "allow": list(capabilities.commands.glob.filetype.allow),
                            "deny": list(capabilities.commands.glob.filetype.deny),
                        },
                    },
                },
                "workspace": {
                    "snapshot": bool(capabilities.workspace.snapshot),
                    "restore": bool(capabilities.workspace.restore),
                    "watch": bool(capabilities.workspace.watch),
                },
                "backends": backends,
                "extensions": list(capabilities.extensions),
            },
        }

    def initialize(
        self,
        *,
        client_name: str = "nexus-python",
        client_version: str = "unknown",
        protocol_version: str = PROTOCOL_VERSION,
    ) -> dict[str, Any] | None:
        request = vfs_pb2.InitializeRequest(
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
            auth_token=self._auth_token,
        )
        try:
            response = self._stub.Initialize(request, timeout=self._connect_timeout)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                self.capabilities = None
                return None
            self._raise_transport_error(exc, self._connect_timeout, "Initialize")
        payload = self._initialize_response_to_dict(response)
        self.capabilities = payload["capabilities"]
        return payload
```

- [ ] **Step 4: Call initialize from remote connect**

Modify `src/nexus/__init__.py` after constructing `transport` and before returning `nfs`:

```python
        initialize_payload = transport.initialize(
            client_name="nexus-python",
            client_version=__version__,
        )
        nfs.capabilities = initialize_payload["capabilities"] if initialize_payload else None
```

- [ ] **Step 5: Add capability gating helper to remote overrides**

Modify `src/nexus/factory/_remote.py`:

```python
    def _ensure_capability(_self: Any, path: str, capability: str) -> None:
        from nexus.contracts.exceptions import NexusError
        from nexus.grpc.capability_discovery import capability_for_path

        capabilities = getattr(_self, "capabilities", None)
        allowed = capability_for_path(capabilities, path, capability)
        if allowed is False:
            raise NexusError(
                f"Remote mount does not declare {capability}",
                path=path,
                is_expected=True,
            )
```

Call it before network operations:

```python
        _ensure_capability(_self, path, "write")
```

for `_remote_sys_write` and `_remote_write`; use `rename` for `_remote_sys_rename`. Add `_remote_sys_unlink`, `_remote_mkdir`, and `_remote_rmdir` overrides only if those methods are not already remotely overridden elsewhere in this file.

- [ ] **Step 6: Run Python remote tests**

Run:

```bash
uv run pytest tests/unit/remote/test_rpc_transport.py::TestRPCTransportInitialize -q
uv run pytest tests/integration/test_connect_quickstart.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit Python client changes**

Run:

```bash
git add src/nexus/remote/rpc_transport.py src/nexus/__init__.py src/nexus/factory/_remote.py tests/unit/remote/test_rpc_transport.py tests/integration/test_connect_quickstart.py tests/unit/factory/test_remote_capability_gating.py
git commit -m "feat(#4085): cache and honor remote VFS capabilities"
```

## Task 6: TypeScript API Client

**Files:**
- Modify: `packages/nexus-api-client/src/types.ts`
- Modify: `packages/nexus-api-client/src/fetch-client.ts`
- Modify: `packages/nexus-api-client/src/index.ts`
- Test: `packages/nexus-api-client/tests/fetch-client.test.ts`

- [ ] **Step 1: Write failing TS client test**

Append to `packages/nexus-api-client/tests/fetch-client.test.ts`:

```typescript
  describe("initialize", () => {
    it("fetches VFS capabilities", async () => {
      const fetchFn = mockFetch([
        {
          status: 200,
          body: {
            server_name: "nexus",
            server_version: "0.10.0",
            protocol_version: "0.1.0",
            capabilities: {
              posix: { read: true, readdir: true, stat: true, write: false, unlink: false, mkdir: false, rmdir: false, rename: false, glob: false },
              commands: {
                grep: { supported: true, filetype: { allow: [], deny: [] } },
                glob: { supported: true, filetype: { allow: [], deny: [] } },
              },
              workspace: { snapshot: true, restore: false, watch: false },
              backends: {},
              extensions: ["x-nexus:versioning"],
            },
          },
        },
      ]);
      client = new FetchClient({ apiKey: "test-key", baseUrl: "http://localhost", fetch: fetchFn, maxRetries: 0 });

      const result = await client.initialize();

      expect(result.serverName).toBe("nexus");
      expect(result.capabilities.posix.write).toBe(false);
      expect(result.capabilities.commands.grep.supported).toBe(true);
      const [url, init] = (fetchFn as ReturnType<typeof vi.fn>).mock.calls[0]!;
      expect(url).toBe("http://localhost/api/vfs/initialize");
      expect(init.method).toBe("GET");
    });
  });
```

- [ ] **Step 2: Run TS test to verify it fails**

Run:

```bash
cd packages/nexus-api-client && npm test -- fetch-client.test.ts
```

Expected: FAIL with `client.initialize is not a function`.

- [ ] **Step 3: Add TypeScript capability interfaces**

Modify `packages/nexus-api-client/src/types.ts`:

```typescript
export interface PosixCapabilities {
  readonly read: boolean;
  readonly readdir: boolean;
  readonly stat: boolean;
  readonly write: boolean;
  readonly unlink: boolean;
  readonly mkdir: boolean;
  readonly rmdir: boolean;
  readonly rename: boolean;
  readonly glob: boolean;
}

export interface StringFilter {
  readonly allow: readonly string[];
  readonly deny: readonly string[];
}

export interface CommandSupport {
  readonly supported: boolean;
  readonly filetype: StringFilter;
}

export interface CommandCapabilities {
  readonly grep: CommandSupport;
  readonly glob: CommandSupport;
}

export interface WorkspaceCapabilities {
  readonly snapshot: boolean;
  readonly restore: boolean;
  readonly watch: boolean;
}

export interface BackendCapabilities {
  readonly backendName: string;
  readonly backendType: string;
  readonly posix: PosixCapabilities;
  readonly features: readonly string[];
  readonly extensions: readonly string[];
  readonly rustNative: boolean;
  readonly external: boolean;
}

export interface VfsCapabilities {
  readonly posix: PosixCapabilities;
  readonly commands: CommandCapabilities;
  readonly workspace: WorkspaceCapabilities;
  readonly backends: Readonly<Record<string, BackendCapabilities>>;
  readonly extensions: readonly string[];
}

export interface InitializeResponse {
  readonly serverName: string;
  readonly serverVersion: string;
  readonly protocolVersion: string;
  readonly capabilities: VfsCapabilities;
}
```

- [ ] **Step 4: Add FetchClient.initialize**

Modify imports in `packages/nexus-api-client/src/fetch-client.ts` to include `InitializeResponse`, then add:

```typescript
  async initialize(options?: RequestOptions): Promise<InitializeResponse> {
    return this.get<InitializeResponse>("/api/vfs/initialize", options);
  }
```

- [ ] **Step 5: Export types**

Modify `packages/nexus-api-client/src/index.ts` to export the new interfaces from `types.ts`.

- [ ] **Step 6: Run TS tests**

Run:

```bash
cd packages/nexus-api-client && npm test
cd packages/nexus-api-client && npm run lint
```

Expected: PASS.

- [ ] **Step 7: Commit TS client**

Run:

```bash
git add packages/nexus-api-client/src/types.ts packages/nexus-api-client/src/fetch-client.ts packages/nexus-api-client/src/index.ts packages/nexus-api-client/tests/fetch-client.test.ts
git commit -m "feat(#4085): add TS VFS capability discovery"
```

## Task 7: nexus-fuse Capability Fetch And Gating

**Files:**
- Modify: `nexus-fuse/src/client.rs`
- Modify: `nexus-fuse/src/fs.rs`
- Modify: `nexus-fuse/src/daemon.rs`
- Modify: `nexus-fuse/src/main.rs`
- Test: `nexus-fuse/tests/error_handling_test.rs`

- [ ] **Step 1: Write failing FUSE HTTP client test**

Append to `nexus-fuse/tests/error_handling_test.rs`:

```rust
#[test]
fn test_capabilities_endpoint_parses_write_false() {
    let mut server = Server::new();

    let _m = server
        .mock("GET", "/api/vfs/initialize")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{
            "server_name":"nexus",
            "server_version":"0.10.0",
            "protocol_version":"0.1.0",
            "capabilities":{
                "posix":{"read":true,"readdir":true,"stat":true,"write":false,"unlink":false,"mkdir":false,"rmdir":false,"rename":false,"glob":false},
                "commands":{"grep":{"supported":false,"filetype":{"allow":[],"deny":[]}},"glob":{"supported":false,"filetype":{"allow":[],"deny":[]}}},
                "workspace":{"snapshot":false,"restore":false,"watch":false},
                "backends":{},
                "extensions":[]
            }
        }"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let response = client.capabilities().unwrap().unwrap();

    assert!(!response.capabilities.posix.write);
    assert!(response.capabilities.posix.read);
}
```

- [ ] **Step 2: Run FUSE test to verify it fails**

Run:

```bash
cd nexus-fuse && cargo test --test error_handling_test test_capabilities_endpoint_parses_write_false -- --nocapture
```

Expected: FAIL with `no method named capabilities found for struct NexusClient`.

- [ ] **Step 3: Add capability structs and client method**

Modify `nexus-fuse/src/client.rs`:

```rust
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct PosixCapabilities {
    #[serde(default)]
    pub read: bool,
    #[serde(default)]
    pub readdir: bool,
    #[serde(default)]
    pub stat: bool,
    #[serde(default)]
    pub write: bool,
    #[serde(default)]
    pub unlink: bool,
    #[serde(default)]
    pub mkdir: bool,
    #[serde(default)]
    pub rmdir: bool,
    #[serde(default)]
    pub rename: bool,
    #[serde(default)]
    pub glob: bool,
}

#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct VfsCapabilities {
    #[serde(default)]
    pub posix: PosixCapabilities,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct InitializeResponse {
    pub server_name: String,
    pub server_version: String,
    pub protocol_version: String,
    pub capabilities: VfsCapabilities,
}

impl NexusClient {
    pub fn capabilities(&self) -> Result<Option<InitializeResponse>, NexusClientError> {
        let url = format!("{}/api/vfs/initialize", self.base_url);
        let resp = self.client.get(&url).headers(self.headers()).send()?;
        if resp.status().as_u16() == 404 {
            return Ok(None);
        }
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }
        Ok(Some(resp.json()?))
    }
}
```

- [ ] **Step 4: Store capabilities in NexusFs**

Modify `nexus-fuse/src/fs.rs`:

```rust
use crate::client::{FileEntry, InitializeResponse, NexusClient};
```

Add field:

```rust
    capabilities: Option<InitializeResponse>,
```

Update constructor:

```rust
    pub fn new(client: NexusClient, file_cache: Option<Arc<FileCache>>) -> Self {
        let capabilities = client.capabilities().ok().flatten();
        Self {
            client: Arc::new(client),
            inodes: Mutex::new(InodeTable::new()),
            attr_cache: Mutex::new(LruCache::new(NonZeroUsize::new(10000).unwrap())),
            dir_cache: Mutex::new(LruCache::new(NonZeroUsize::new(1000).unwrap())),
            file_cache,
            capabilities,
        }
    }

    fn capability_allowed(&self, capability: &str) -> bool {
        let Some(response) = &self.capabilities else {
            return true;
        };
        match capability {
            "write" => response.capabilities.posix.write,
            "unlink" => response.capabilities.posix.unlink,
            "mkdir" => response.capabilities.posix.mkdir,
            "rename" => response.capabilities.posix.rename,
            _ => true,
        }
    }
```

Before network calls in `write`, `create`, `mkdir`, `unlink`, and `rename`, add:

```rust
        if !self.capability_allowed("write") {
            reply.error(libc::EOPNOTSUPP);
            return;
        }
```

Use `"mkdir"`, `"unlink"`, or `"rename"` for the matching operation.

- [ ] **Step 5: Gate daemon handlers**

Modify `nexus-fuse/src/daemon.rs` so `Daemon` stores `capabilities: Option<InitializeResponse>`, initializes it from `client.capabilities().ok().flatten()`, and checks it in `handle_write`, `handle_mkdir`, and `handle_rename`. Return `JsonRpcResponse::error(..., libc::EOPNOTSUPP)` for unsupported operations.

- [ ] **Step 6: Run FUSE tests**

Run:

```bash
cd nexus-fuse && cargo test --test error_handling_test test_capabilities_endpoint_parses_write_false -- --nocapture
cd nexus-fuse && cargo test
```

Expected: PASS.

- [ ] **Step 7: Commit FUSE changes**

Run:

```bash
git add nexus-fuse/src/client.rs nexus-fuse/src/fs.rs nexus-fuse/src/daemon.rs nexus-fuse/src/main.rs nexus-fuse/tests/error_handling_test.rs
git commit -m "feat(#4085): gate fuse operations with VFS capabilities"
```

## Task 8: Documentation

**Files:**
- Create: `docs/architecture/vfs-capability-discovery.md`
- Modify: `README.md` or docs index only if the docs tree requires manual linking
- Test: `packages/nexus-fs/tests/test_docs.py`

- [ ] **Step 1: Write the docs page**

Create `docs/architecture/vfs-capability-discovery.md`:

```markdown
# VFS Capability Discovery

Nexus VFS clients discover server and mount capabilities with the `Initialize`
handshake. gRPC clients call `NexusVFSService.Initialize`; HTTP clients call
`GET /api/vfs/initialize`.

Clients should call initialize before issuing VFS operations and cache the
response for the lifetime of the connection. Clients may call initialize again
after mount topology changes.

## Semantics

Capabilities are declarations, not permission grants. Server-side auth,
permissions, and backend checks remain authoritative.

Proto3 defaults matter: missing fields and `false` booleans mean the capability
was not declared. Clients must not assume support from an absent field.

## Per-Mount Backends

The top-level `capabilities.posix` describes the root/default capability set.
`capabilities.backends` contains per-mount overrides keyed by user-facing mount
path. Clients should choose the longest matching mount prefix for a path.

## Extensions

Nexus extensions use the `x-nexus:` prefix. Backend or vendor-specific
extensions use `x-<vendor>:`. Unknown extensions are ignorable.

## Python

```python
from nexus.sdk import connect

nx = await connect({"profile": "remote", "url": "http://localhost:2026"})
caps = nx.capabilities
if caps and caps["posix"]["write"]:
    nx.write("/notes.txt", b"hello")
```

## TypeScript

```ts
import { FetchClient } from "@nexus-ai-fs/api-client";

const client = new FetchClient({ apiKey: "sk-test" });
const init = await client.initialize();
if (init.capabilities.posix.write) {
  await client.post("/api/v2/files/write", { path: "/notes.txt", content: "hello" });
}
```
```

- [ ] **Step 2: Run docs tests**

Run:

```bash
uv run pytest packages/nexus-fs/tests/test_docs.py -q
```

Expected: PASS, or SKIP if docs test environment is not configured.

- [ ] **Step 3: Commit docs**

Run:

```bash
git add docs/architecture/vfs-capability-discovery.md README.md
git commit -m "docs(#4085): document VFS capability discovery"
```

If `README.md` was not modified, omit it from `git add`.

## Task 9: Final Verification

**Files:**
- No source changes unless verification exposes a defect.

- [ ] **Step 1: Run focused Python tests**

Run:

```bash
uv run pytest tests/unit/grpc/test_initialize_proto.py tests/unit/grpc/test_capability_discovery.py tests/unit/grpc/test_vfs_initialize_dispatcher.py tests/unit/remote/test_rpc_transport.py tests/unit/server/test_vfs_initialize_endpoint.py tests/integration/test_connect_quickstart.py -q
```

Expected: PASS.

- [ ] **Step 2: Run Rust transport checks**

Run:

```bash
cargo test -p transport --features python initialize
```

Expected: PASS.

- [ ] **Step 3: Run TypeScript checks**

Run:

```bash
cd packages/nexus-api-client && npm test
cd packages/nexus-api-client && npm run lint
```

Expected: PASS.

- [ ] **Step 4: Run FUSE checks**

Run:

```bash
cd nexus-fuse && cargo test
```

Expected: PASS.

- [ ] **Step 5: Run repository status and review diff**

Run:

```bash
git status --short
git log --oneline --decorate -n 8
```

Expected: working tree clean except intentional untracked local artifacts, with commits for each task.

- [ ] **Step 6: Prepare final PR summary**

Write a PR summary with:

```markdown
## Summary
- Added typed VFS Initialize protocol and generated stubs.
- Implemented server capability aggregation and HTTP discovery.
- Exposed and honored capabilities in Python remote clients, TS clients, and nexus-fuse.
- Documented per-mount capability discovery and extension semantics.

## Tests
- uv run pytest tests/unit/grpc/test_initialize_proto.py tests/unit/grpc/test_capability_discovery.py tests/unit/grpc/test_vfs_initialize_dispatcher.py tests/unit/remote/test_rpc_transport.py tests/unit/server/test_vfs_initialize_endpoint.py tests/integration/test_connect_quickstart.py -q
- cargo test -p transport --features python initialize
- cd packages/nexus-api-client && npm test
- cd packages/nexus-api-client && npm run lint
- cd nexus-fuse && cargo test
```
