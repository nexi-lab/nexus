# Namespace Visibility Feature - Implementation Summary

## Overview
Added support for displaying built-in namespace directories (`/workspace`, `/shared`, `/archives`, `/external`, `/system`) in FUSE mounts and remote clients.

## Changes Made

### 1. Core Filesystem (Local)
**File:** `src/nexus/core/nexus_fs.py`

Added `get_available_namespaces()` method (lines 895-922):
```python
def get_available_namespaces(self) -> builtins.list[str]:
    """Get list of available namespace directories.

    Returns the built-in namespaces that should appear at root level.
    Filters based on tenant and admin context.

    Returns:
        List of namespace names (e.g., ["workspace", "shared", "external"])
    """
    namespaces = []

    for name, config in self.router._namespaces.items():
        # Include namespace if:
        # - It's not admin-only OR user is admin
        # - It doesn't require tenant OR user has a tenant
        if (not config.admin_only or self.is_admin) and (
            not config.requires_tenant or self.tenant_id
        ):
            namespaces.append(name)

    return sorted(namespaces)
```

### 2. Remote Client
**File:** `src/nexus/remote/client.py`

Added `get_available_namespaces()` method (lines 251-261):
```python
def get_available_namespaces(self) -> builtins.list[str]:
    """Get list of available namespace directories."""
    result = self._call_rpc("get_available_namespaces", {})
    return result["namespaces"]
```

### 3. RPC Server
**File:** `src/nexus/server/rpc_server.py`

Added handler for `get_available_namespaces` RPC method (lines 340-341):
```python
elif method == "get_available_namespaces":
    return {"namespaces": self.nexus_fs.get_available_namespaces()}
```

### 4. FUSE Operations - readdir()
**File:** `src/nexus/fuse/operations.py`

Updated `readdir()` to inject namespace directories at root (lines 179-189):
```python
# At root level, add built-in namespace directories and .raw
if path == "/":
    entries.append(".raw")

    # Add namespace directories (workspace, shared, external, etc.)
    try:
        namespaces = self.nexus_fs.get_available_namespaces()
        entries.extend(namespaces)
    except (AttributeError, Exception):
        # Fallback if method doesn't exist or fails
        pass
```

### 5. FUSE Operations - getattr()
**File:** `src/nexus/fuse/operations.py`

Updated `getattr()` to recognize namespace directories (lines 106-114):
```python
# Check if it's a namespace directory (e.g., /workspace, /shared, /archives, /external, /system)
if path.startswith("/") and "/" not in path[1:]:  # Top-level directory
    try:
        namespaces = self.nexus_fs.get_available_namespaces()
        namespace_name = path[1:]  # Remove leading /
        if namespace_name in namespaces:
            return self._dir_attrs()
    except (AttributeError, Exception):
        pass
```

### 6. Configuration Default
**File:** `src/nexus/config.py`

Changed default `tenant_id` from `None` to `"default"` (lines 75-77):
```python
tenant_id: str | None = Field(
    default="default", description="Tenant identifier for multi-tenant isolation"
)
```

This ensures single-user local mounts show all tenant-requiring namespaces by default.

### 7. Tests
**File:** `tests/unit/test_fuse_operations.py`

Updated test fixture to include new method (lines 40):
```python
spec=[
    ...
    "get_available_namespaces",
]
```

Updated test expectations (lines 143-149):
```python
mock_nexus_fs.get_available_namespaces.return_value = [
    "archives",
    "external",
    "shared",
    "workspace",
]
```

## Namespace Access Control

The implementation respects namespace configurations:

| Namespace | Readonly | Admin-Only | Requires Tenant |
|-----------|----------|------------|-----------------|
| `/workspace` | No | No | Yes |
| `/shared` | No | No | Yes |
| `/external` | No | No | No |
| `/system` | Yes | **Yes** | No |
| `/archives` | Yes | No | Yes |

**Visibility Rules:**
- `tenant_id=None` → Only `/external` visible
- `tenant_id="default"` → `/workspace`, `/shared`, `/archives`, `/external` visible
- `is_admin=True` → All namespaces including `/system` visible

## How It Works

1. **Namespace Registration**: PathRouter registers 5 built-in namespaces on initialization
2. **Namespace Filtering**: `get_available_namespaces()` filters based on user context
3. **FUSE Integration**:
   - `readdir("/")` adds namespaces to directory listing
   - `getattr("/workspace")` recognizes namespaces as valid directories
4. **Remote Support**: RPC client/server propagate namespace visibility over network

## Testing

```bash
# Local mount
cd /Users/jinjingzhou/nexi-lib/nexus
source .venv/bin/activate
pip install -e . --no-deps

mkdir -p /tmp/nexus-test
python -m nexus.cli mount /tmp/nexus-test --daemon
ls -la /tmp/nexus-test

# Expected output:
# archives/
# external/
# shared/
# workspace/
# .raw/

# Cleanup
nexus unmount /tmp/nexus-test
```

## Files Modified

1. `src/nexus/core/nexus_fs.py` - Added `get_available_namespaces()`
2. `src/nexus/remote/client.py` - Added RPC client method
3. `src/nexus/server/rpc_server.py` - Added RPC server handler
4. `src/nexus/fuse/operations.py` - Updated `readdir()` and `getattr()`
5. `src/nexus/config.py` - Changed default `tenant_id` to `"default"`
6. `tests/unit/test_fuse_operations.py` - Updated tests

## Impact

- ✅ **Local mounts** now show namespace directories automatically
- ✅ **Remote mounts** (via RPC) also show namespace directories
- ✅ **Access control** is preserved (tenant isolation, admin-only)
- ✅ **Backward compatible** (graceful fallback if method doesn't exist)
- ✅ **No breaking changes** to existing APIs

## Future Enhancements

- Add caching for namespace lists in FUSE layer
- Support custom namespaces from config file
- Add namespace metadata (description, icon, etc.)
- Implement namespace-level permissions/ACLs
