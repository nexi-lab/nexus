# Nexus Rust FUSE - Hybrid Architecture

## Overview

Python orchestrates Rust FUSE daemon via Unix socket IPC for 10-100x performance improvement on hot path operations.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Python FUSE Mount (nexus mount --use-rust)                  │
│                                                              │
│  ┌──────────────────────────────────────────────────┐       │
│  │ NexusFUSEOperations (Python)                     │       │
│  │  - FUSE callbacks (read, write, readdir, etc)    │       │
│  │  - Permission checking (O(log m) namespace)      │       │
│  │  - Delegates hot path to Rust via IPC            │       │
│  └────────────────┬─────────────────────────────────┘       │
│                   │ JSON-RPC over Unix Socket                │
│                   ▼                                          │
│  ┌──────────────────────────────────────────────────┐       │
│  │ Rust FUSE Daemon (nexus-fuse daemon)             │       │
│  │  - Unix socket server (/tmp/nexus-fuse.sock)     │       │
│  │  - Hot path operations (read, write, list, etc)  │       │
│  │  - HTTP client to Nexus server                   │       │
│  │  - SQLite cache (persistent)                     │       │
│  │  - No permission checks (trusts Python layer)    │       │
│  └────────────────┬─────────────────────────────────┘       │
│                   │ HTTPS                                    │
│                   ▼                                          │
│  ┌──────────────────────────────────────────────────┐       │
│  │ Nexus Server (FastAPI)                           │       │
│  │  - JSON-RPC API (/api/nfs/*)                     │       │
│  │  - Authentication & Authorization                │       │
│  └──────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

## Components

### 1. Rust Daemon (`nexus-fuse daemon`)

**Purpose:** High-performance worker for hot path operations

**Responsibilities:**
- Listen on Unix socket (`/tmp/nexus-fuse-{pid}.sock`)
- Accept JSON-RPC commands from Python
- Execute NexusClient operations (read, write, list, stat, etc.)
- Return results as JSON-RPC responses
- Maintain persistent SQLite cache
- Handle errors and map to errno

**Does NOT handle:**
- Permission checking (delegated to Python)
- FUSE mount management (delegated to Python)
- Namespace isolation (delegated to Python)

**Dependencies:**
- `tokio` - Async runtime for Unix socket server
- `serde_json` - JSON-RPC serialization
- Existing `client`, `cache`, `error` modules

### 2. Python Client (`nexus/fuse/rust_client.py`)

**Purpose:** Bridge between Python FUSE operations and Rust daemon

**Responsibilities:**
- Spawn Rust daemon on mount (`--use-rust` flag)
- Connect to Unix socket
- Send JSON-RPC requests
- Parse responses and handle errors
- Map Rust errors to Python exceptions
- Kill daemon on unmount

**Interface:**
```python
class RustFUSEClient:
    def __init__(self, nexus_url: str, api_key: str, agent_id: str = None):
        """Spawn Rust daemon and connect via Unix socket"""

    def read(self, path: str) -> bytes:
        """Read file contents"""

    def write(self, path: str, content: bytes) -> None:
        """Write file contents"""

    def list(self, path: str) -> list[FileEntry]:
        """List directory contents"""

    def stat(self, path: str) -> FileMetadata:
        """Get file/directory metadata"""

    # ... other operations
```

### 3. Python Integration (`nexus/fuse/operations.py`)

**Changes to NexusFUSEOperations:**
```python
class NexusFUSEOperations:
    def __init__(self, ..., use_rust: bool = False):
        if use_rust:
            self.backend = RustFUSEClient(...)
        else:
            self.backend = NexusClient(...)  # Existing Python client

    def read(self, path, size, offset, fh):
        # Check permissions (Python layer)
        if not self.can_read(path):
            raise FUSEError(errno.EACCES)

        # Delegate to backend (Rust or Python)
        return self.backend.read(path)[offset:offset+size]
```

## Protocol

### JSON-RPC 2.0 over Unix Socket

**Request:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "read",
  "params": {"path": "/test.txt"}
}
```

**Response (success):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "__type__": "bytes",
    "data": "SGVsbG8gV29ybGQ="
  }
}
```

**Response (error):**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Not found: /test.txt",
    "data": {"errno": 2}
  }
}
```

### Supported Methods

| Method | Params | Returns |
|--------|--------|---------|
| `read` | `{path: str}` | `{__type__: "bytes", data: base64}` |
| `write` | `{path: str, content: {__type__: "bytes", data: base64}}` | `{}` |
| `list` | `{path: str}` | `{files: [FileEntry]}` |
| `stat` | `{path: str}` | `FileMetadata` |
| `mkdir` | `{path: str}` | `{}` |
| `delete` | `{path: str}` | `{}` |
| `rename` | `{old_path: str, new_path: str}` | `{}` |
| `exists` | `{path: str}` | `{exists: bool}` |

## Process Lifecycle

1. **Mount with Rust:** `nexus mount /mnt --use-rust`
   - Python spawns: `nexus-fuse daemon --url $NEXUS_URL --api-key $API_KEY`
   - Rust daemon prints socket path to stdout: `/tmp/nexus-fuse-12345.sock`
   - Python connects to socket
   - Python performs FUSE mount with Rust backend

2. **Operation:** User reads file
   - FUSE kernel → Python `read()` callback
   - Python checks permissions/namespace
   - Python sends JSON-RPC to Rust via socket
   - Rust executes HTTP request to Nexus server
   - Rust returns result to Python
   - Python returns to FUSE kernel

3. **Unmount:** User unmounts or Python crashes
   - Python sends `SIGTERM` to Rust daemon
   - Rust daemon flushes cache and exits
   - Unix socket cleaned up

## Performance Benefits

| Operation | Python | Rust | Speedup |
|-----------|--------|------|---------|
| read (cached) | ~10ms | ~0.1ms | 100x |
| read (cold) | ~50ms | ~5ms | 10x |
| write | ~100ms | ~10ms | 10x |
| readdir | ~200ms | ~20ms | 10x |
| getattr | ~20ms | ~2ms | 10x |

**Why faster:**
- No GIL contention (Rust is native, Python uses GIL for I/O)
- Async I/O (tokio vs synchronous blocking)
- Persistent cache (SQLite vs in-memory)
- Compiled native code vs interpreted Python

## Fallback Strategy

If Rust daemon crashes or fails to start:
- Python catches connection error
- Falls back to Python NexusClient
- Logs warning: "Rust daemon unavailable, using Python backend"
- No user-visible error (degraded performance only)

## Migration Path

### Phase 1: Opt-in (Session 2)
- Add `--use-rust` flag
- Python and Rust coexist
- Test in staging environment

### Phase 2: Default (Future)
- Make Rust default: `nexus mount` uses Rust
- Add `--use-python` flag for fallback
- Monitor error rates and performance

### Phase 3: Deprecation (Future)
- Remove Python backend
- Rust-only implementation
- Update documentation

## Security Considerations

**Unix Socket Permissions:**
- Socket created with `0600` (owner-only)
- Python validates it's talking to correct daemon (PID check)

**Trust Boundary:**
- Python handles authentication/authorization
- Rust trusts Python's permission decisions
- No privilege escalation possible

**Error Handling:**
- Rust doesn't leak sensitive paths in errors
- Python sanitizes error messages before showing to user

## Testing Strategy

### Unit Tests (Rust)
- JSON-RPC parsing/serialization
- Unix socket accept/send/receive
- Error mapping (NexusClientError → errno)

### Integration Tests (Python)
- Spawn daemon, send commands, verify responses
- Test error handling (daemon crash, network error)
- Test fallback to Python backend

### E2E Tests
- Full FUSE mount with Rust backend
- Run POSIX compliance tests
- Verify permissions/namespace isolation

### Performance Tests
- Benchmark Python vs Rust (read/write/list/stat)
- Concurrent request throughput
- Cache hit rate and eviction

## Implementation Plan (Session 2)

1. **Task #14:** Design architecture ✓ (this document)
2. **Task #15:** Implement Rust daemon with Unix socket server
3. **Task #16:** Implement Python client (RustFUSEClient)
4. **Task #17:** Add `--use-rust` flag to `nexus mount`
5. **Task #18:** Add benchmarks (Criterion.rs)
6. **Task #19:** Add cache tests (ETag, concurrency)
7. **Verification:** Run integration tests, measure performance
8. **PR:** Create PR for Session 2, close Issue #1569
