# Plan: Redesign NexusFilesystemABC — Linux Syscall Alignment + sys_ Prefix

**Tasks**: #834 (sys_ prefix), new (ABC redesign) | **Prerequisite for**: #1202 (gRPC transport)
**Approach**: No backward compatibility — one-shot rename across entire codebase.

---

## Context

The current `NexusFilesystemABC` has three design issues:

1. **Combined operations**: `write(path, content)` does content + metadata in one call.
2. **No sys_ prefix**: Kernel syscalls (`read`, `write`, `delete`) indistinguishable from service-layer methods.
3. **Non-syscall methods in ABC**: `glob()`, `grep()`, `append()`, `edit()` are user-space utilities, not kernel syscalls.

**Design principle**: Align with Linux — each syscall does ONE thing, names follow POSIX conventions.

---

## New NexusFilesystemABC Interface

### Naming Convention

| Category | Method | Origin |
|----------|--------|--------|
| Content I/O | `sys_read` | POSIX `read(2)` |
| | `sys_write` | POSIX `write(2)` |
| Metadata I/O | `sys_stat` | POSIX `stat(2)` — **NEW** |
| | `sys_setattr` | Our design — **NEW** |
| Namespace | `sys_unlink` | POSIX `unlink(2)` — was `delete` |
| | `sys_rename` | POSIX `rename(2)` |
| Directory | `sys_mkdir` | POSIX `mkdir(2)` |
| | `sys_rmdir` | POSIX `rmdir(2)` |
| | `sys_readdir` | POSIX `readdir(3)` — was `list` |
| Query | `sys_access` | POSIX `access(2)` — was `exists` |
| | `sys_is_directory` | Our design |

### Two-Tier ABC Design (No Backward Compat)

```python
class NexusFilesystemABC(ABC):
    """Kernel syscall contract — Linux VFS-aligned.

    Two tiers:
      Tier 1: Abstract sys_ syscalls — implementors MUST override
      Tier 2: Convenience methods — concrete, compose syscalls (user-space utilities)
    """

    # ── Tier 1: Abstract Syscalls ──────────────────────────────────

    # Content I/O
    @abstractmethod
    def sys_read(self, path, context=None, return_metadata=False) -> bytes | dict: ...
    @abstractmethod
    def sys_write(self, path, content, context=None, *, if_match=None,
                  if_none_match=False, force=False, lock=False, lock_timeout=30.0) -> dict: ...

    # Metadata I/O (NEW)
    @abstractmethod
    def sys_stat(self, path, context=None) -> dict | None: ...
    @abstractmethod
    def sys_setattr(self, path, context=None, **attrs) -> dict: ...

    # Namespace
    @abstractmethod
    def sys_unlink(self, path, context=None) -> dict: ...
    @abstractmethod
    def sys_rename(self, old_path, new_path, context=None) -> dict: ...

    # Directory
    @abstractmethod
    def sys_mkdir(self, path, parents=False, exist_ok=False, context=None) -> None: ...
    @abstractmethod
    def sys_rmdir(self, path, recursive=False, context=None) -> None: ...
    @abstractmethod
    def sys_readdir(self, path="/", recursive=True, details=False,
                    show_parsed=True, context=None) -> list: ...

    # Query
    @abstractmethod
    def sys_access(self, path, context=None) -> bool: ...
    @abstractmethod
    def sys_is_directory(self, path, context=None) -> bool: ...

    # System info + lifecycle
    @abstractmethod
    def get_top_level_mounts(self) -> list[str]: ...
    @abstractmethod
    def close(self) -> None: ...

    # ── Tier 2: Convenience Methods (user-space utilities) ─────────

    def append(self, path, content, context=None, **kw) -> dict:
        """User-space: sys_read + sys_write (like shell >>)."""
        ...

    def edit(self, path, edits, context=None, **kw) -> dict:
        """User-space: sys_read + modify + sys_write."""
        raise NotImplementedError("Override in NexusFS")

    def write_batch(self, files, context=None) -> list[dict]:
        """Optimization: default falls back to N × sys_write."""
        return [self.sys_write(p, c, context) for p, c in files]

    def glob(self, pattern, path="/", context=None) -> list[str]:
        raise NotImplementedError("Override in NexusFS")

    def grep(self, pattern, path="/", **kw) -> list[dict]:
        raise NotImplementedError("Override in NexusFS")
```

---

## Blast Radius (No Backward Compat)

~1,090 call sites across ~120 files must be renamed.

| Method | →becomes→ | src/ calls | test calls | Total |
|--------|-----------|-----------|------------|-------|
| `.read(` | `.sys_read(` | 64 | 151 | 215 |
| `.write(` | `.sys_write(` | 57 | 365 | 422 |
| `.delete(` | `.sys_unlink(` | 13 | 52 | 65 |
| `.rename(` | `.sys_rename(` | 8 | 22 | 30 |
| `.exists(` | `.sys_access(` | 54 | 85 | 139 |
| `.list(` | `.sys_readdir(` | 37 | 51 | 88 |
| `.mkdir(` | `.sys_mkdir(` | 38 | 55 | 93 |
| `.rmdir(` | `.sys_rmdir(` | 9 | 5 | 14 |
| `.is_directory(` | `.sys_is_directory(` | 13 | 4 | 17 |
| `.get_metadata(` | `.sys_stat(` | 2 | 5 | 7 |

Convenience methods (`append`, `edit`, `write_batch`, `glob`, `grep`) keep their names — only demoted from abstract to concrete.

---

## Implementation Steps

### Step 1: Rewrite `filesystem_abc.py`

**File**: `src/nexus/contracts/filesystem/filesystem_abc.py`

Two-tier design: 12 abstract sys_ syscalls + 2 unchanged abstracts + 5 concrete convenience methods.

### Step 2: Rename NexusFS kernel methods + add sys_stat / sys_setattr

**File**: `src/nexus/core/nexus_fs.py`

| Current method | New name |
|---------------|----------|
| `read()` | `sys_read()` |
| `write()` | `sys_write()` |
| `delete()` | `sys_unlink()` |
| `rename()` | `sys_rename()` |
| `exists()` | `sys_access()` |
| `list()` | `sys_readdir()` |
| `mkdir()` | `sys_mkdir()` |
| `rmdir()` | `sys_rmdir()` |
| `is_directory()` | `sys_is_directory()` |
| `get_metadata()` | `sys_stat()` |

Plus: add `sys_setattr()`, update all internal self-calls.

### Step 3: Update RPC dispatch table

**File**: `src/nexus/server/rpc/dispatch.py`

Replace old names with `sys_` names in dispatch table. No legacy aliases.

### Step 4: Update RPC handlers

**File**: `src/nexus/server/rpc/handlers/filesystem.py`

Change `nexus_fs.write(...)` → `nexus_fs.sys_write(...)`, etc.

### Step 5: Update RPC param overrides + generated params

**Files**: `src/nexus/server/_rpc_param_overrides.py`, `src/nexus/server/_rpc_params_generated.py`

Rename dispatch keys: `"read"` → `"sys_read"`, `"write"` → `"sys_write"`, etc.

### Step 6: Update ScopedFilesystem

**File**: `src/nexus/bricks/filesystem/scoped_filesystem.py`

Rename all methods to `sys_` names. Both the method defs and the `self._fs.X()` calls.

### Step 7: Update method_registry

**File**: `src/nexus/remote/method_registry.py`

Replace old names with `sys_` names.

### Step 8: Update ALL callers (by layer)

No backward compat — every call site must be renamed.

| Layer | ~Files | Pattern |
|-------|--------|---------|
| `src/nexus/server/` | 4 | `nexus_fs.read(` → `nexus_fs.sys_read(` |
| `src/nexus/bricks/` | 20+ | `self._fs.read(` etc |
| `src/nexus/fuse/` | 5 | `ctx.nexus_fs.read(` etc |
| `src/nexus/cli/` | 7 | `nx.read(` etc |
| `src/nexus/mcp/` | 5 | same as bricks/mcp |
| `src/nexus/services/` | 12 | various patterns |
| `src/nexus/sync.py` + misc | 8 | various |
| `tests/` | 60+ | `nx.read(`, `nx.write(` etc |

### Step 9: Tests

| Test | What it verifies |
|------|-----------------|
| `tests/unit/contracts/test_syscall_abc.py` | ABC tier design |
| `tests/unit/core/test_sys_stat.py` | `sys_stat` returns correct FileMetadata |
| `tests/unit/core/test_sys_setattr.py` | `sys_setattr` updates metadata |
| `tests/unit/server/test_dispatch_sys_names.py` | `sys_write` dispatches correctly |
| All existing tests | Pass (call sites renamed) |

---

## Files Summary

| File | Action |
|------|--------|
| `src/nexus/contracts/filesystem/filesystem_abc.py` | **Rewrite** |
| `src/nexus/core/nexus_fs.py` | **Modify** — rename methods, add sys_stat/sys_setattr |
| `src/nexus/server/rpc/dispatch.py` | **Modify** — rename dispatch keys |
| `src/nexus/server/rpc/handlers/filesystem.py` | **Modify** — rename calls |
| `src/nexus/server/_rpc_param_overrides.py` | **Modify** — rename keys |
| `src/nexus/server/_rpc_params_generated.py` | **Modify** — rename keys |
| `src/nexus/bricks/filesystem/scoped_filesystem.py` | **Modify** — rename methods |
| `src/nexus/remote/method_registry.py` | **Modify** — rename keys |
| `src/nexus/bricks/mcp/*.py` | **Modify** — rename calls |
| `src/nexus/mcp/*.py` | **Modify** — rename calls |
| `src/nexus/fuse/ops/*.py` | **Modify** — rename calls |
| `src/nexus/cli/commands/*.py` | **Modify** — rename calls |
| `src/nexus/services/**/*.py` | **Modify** — rename calls |
| `src/nexus/sync.py` + misc | **Modify** — rename calls |
| `tests/**/*.py` | **Modify** — rename calls |
| Tests (4 new files) | **New** |

---

## Verification

```bash
uv run pytest tests/ -x --timeout=60
uv run mypy src/nexus/contracts/filesystem/ src/nexus/core/nexus_fs.py
uv run ruff check src/
PYTHONPATH=src uv run lint-imports
```

---

---

# ARCHIVED: Old Plan — gRPC VFS Service (Phase 1)

> This plan will be revisited after the ABC redesign. Key changes needed:
> - Proto uses generic `Call` RPC (not typed Read/Write/Delete) — SSOT is dispatch table
> - No GrpcBackend/GrpcMetastore — inject `_call_rpc` transport into existing RemoteBackend/RemoteMetastore
> - Wire to sys_ prefixed dispatch entries

(See conversation transcript for full details of the original gRPC plan.)
