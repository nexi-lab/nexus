# ReBAC Remote Support

**Status**: ✅ **IMPLEMENTED**

ReBAC (Relationship-Based Access Control) operations are now fully supported for remote Nexus connections!

## Summary

Previously, ReBAC commands only worked in embedded mode. We've enabled all ReBAC operations to work remotely by exposing them through the RPC protocol.

## What Was Changed

### 1. Core Methods (`src/nexus/core/nexus_fs_rebac.py`)

Added `@rpc_expose` decorators to all 5 ReBAC methods:

- ✅ `rebac_create()` - Create relationship tuple
- ✅ `rebac_check()` - Check permission via relationships
- ✅ `rebac_expand()` - Find all subjects with permission
- ✅ `rebac_delete()` - Delete relationship tuple
- ✅ `rebac_list_tuples()` - List relationship tuples

### 2. Remote Client (`src/nexus/remote/client.py`)

Added corresponding client methods that call the RPC server:

```python
# Example: Create a relationship
client_nx = RemoteNexusFS("http://server:8080")
tuple_id = client_nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "developers")
)
```

### 3. RPC Protocol (`src/nexus/server/protocol.py`)

Added parameter dataclasses and registered ReBAC methods:

- `RebacCreateParams`
- `RebacCheckParams`
- `RebacExpandParams`
- `RebacDeleteParams`
- `RebacListTuplesParams`

All registered in `METHOD_PARAMS` dictionary.

### 4. RPC Server (`src/nexus/server/rpc_server.py`)

Fixed bug where `exposed_methods` was referenced instead of `_exposed_methods`, preventing auto-dispatch from working.

### 5. CLI Commands (`src/nexus/cli/commands/rebac.py`)

Removed embedded-only restrictions from all ReBAC CLI commands:
- `nexus rebac create`
- `nexus rebac check`
- `nexus rebac expand`
- `nexus rebac delete`

## Usage

### Python SDK

```python
from nexus.remote import RemoteNexusFS

# Connect to remote Nexus server
nx = RemoteNexusFS("http://nexus-server:8080")

# Create relationship
tuple_id = nx.rebac_create(
    subject=("agent", "alice"),
    relation="member-of",
    object=("group", "engineers")
)

# Check permission
granted = nx.rebac_check(
    subject=("agent", "alice"),
    permission="read",
    object=("file", "/workspace/doc.txt")
)

# Find all subjects with permission
subjects = nx.rebac_expand(
    permission="write",
    object=("workspace", "/workspace")
)

# List tuples for a subject
tuples = nx.rebac_list_tuples(subject=("agent", "alice"))

# Delete tuple
nx.rebac_delete(tuple_id)

nx.close()
```

### CLI

```bash
# Connect to remote server
export NEXUS_SERVER_URL="http://nexus-server:8080"

# Create relationship
nexus rebac create agent alice member-of group engineers

# Check permission
nexus rebac check agent alice read file /workspace/doc.txt

# Expand permissions
nexus rebac expand write workspace /workspace

# Delete relationship
nexus rebac delete <tuple-id>
```

## Architecture

```
┌─────────────────┐           ┌─────────────────┐
│  Client App     │           │  Nexus Server   │
│                 │           │                 │
│ RemoteNexusFS   │  ─RPC──>  │   NexusFS       │
│                 │           │                 │
│ nx.rebac_create │           │ _rebac_manager  │
│      ↓          │           │       ↓         │
│ HTTP POST       │           │  Execute ReBAC  │
│ rebac_create    │           │  operations     │
│ {subject, ...}  │           │       ↓         │
│                 │   <────   │  Return result  │
└─────────────────┘           └────────┬────────┘
                                       │
                                       ▼
                                 ┌──────────┐
                                 │ Database │
                                 │ ReBAC    │
                                 │ Tables   │
                                 └──────────┘
```

## Testing

**RPC Parity Test**: ✅ All 31 RPC methods detected
  - Includes all 5 ReBAC methods
  - Run: `pytest tests/unit/test_rpc_parity.py -v`

**Protocol Test**: ✅ Method parameters parse correctly
  - All ReBAC param classes work
  - Test: `parse_method_params('rebac_create', {...})`

## Benefits

1. ✅ **Centralized Permission Management** - Manage ReBAC relationships from any client
2. ✅ **Consistent API** - Same methods work locally and remotely
3. ✅ **Multi-User Support** - Multiple clients can manage permissions
4. ✅ **Scalability** - Server handles all database operations
5. ✅ **Security** - RPC server can enforce authentication/authorization

## Limitations

- **Export/Import**: `export_metadata` and `import_metadata` work but require server-side file paths (not client paths)
- **Performance**: Remote calls have HTTP overhead vs. direct database access

## Backward Compatibility

✅ Fully backward compatible!
- Existing embedded mode code continues to work
- Old RPC clients that don't use ReBAC are unaffected
- Manual dispatch fallback still works

## Files Modified

1. `src/nexus/core/nexus_fs_rebac.py` - Added `@rpc_expose` decorators
2. `src/nexus/remote/client.py` - Added client methods
3. `src/nexus/server/protocol.py` - Added parameter classes and METHOD_PARAMS
4. `src/nexus/server/rpc_server.py` - Fixed `_exposed_methods` reference
5. `src/nexus/cli/commands/rebac.py` - Removed embedded-only restrictions

## Related Issues

- Issue #268 - Remote parity gaps (chmod, chown, chgrp, import/export, ReBAC)
- Issue #256 - Decorator-based RPC endpoint registration

---

**Implementation Date**: 2025-10-24
**Author**: Claude Code
**Status**: ✅ Complete and tested
