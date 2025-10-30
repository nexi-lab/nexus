# Nexus API Testing Results

**Date:** 2025-10-30
**Test Objective:** Test user workspace creation and file operations with proper permissions

## Test Use Case

1. ✅ Create a new user and get API key
2. ✅ Use this user/key to create a workspace
3. ❌ Write a file to the path inside the workspace **without permission issues**

## Summary

The test revealed a **critical permission check bug** in the Nexus server. While the ReBAC permission system correctly grants permissions (verified via `rebac_check` and `rebac_explain` APIs), the internal permission enforcer fails to recognize these permissions during actual file operations.

## Test Scripts Created

1. **[test-user-workspace.sh](test-user-workspace.sh)** - Initial test (discovered the bug)
2. **[test-user-workspace-with-permissions.sh](test-user-workspace-with-permissions.sh)** - Test with ReBAC permissions
3. **[test-user-workspace-final.sh](test-user-workspace-final.sh)** - Comprehensive test with diagnostics
4. **[test-api-workaround.sh](test-api-workaround.sh)** - Workaround using admin privileges

## Detailed Findings

### ✅ What Works

1. **User Creation** - `admin_create_key` API works correctly
   ```bash
   curl http://localhost:8080/api/nfs/admin_create_key \
     -H "Authorization: Bearer ${ADMIN_KEY}" \
     -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob Key","is_admin":false},"id":1}'
   ```

2. **Workspace Registration** - `register_workspace` API works
   ```bash
   curl http://localhost:8080/api/nfs/register_workspace \
     -H "Authorization: Bearer ${ADMIN_KEY}" \
     -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"path":"/bob-workspace"},"id":1}'
   ```

3. **ReBAC Permission Granting** - `rebac_create` API works
   ```bash
   curl http://localhost:8080/api/nfs/rebac_create \
     -H "Authorization: Bearer ${ADMIN_KEY}" \
     -d '{"jsonrpc":"2.0","method":"rebac_create","params":{"subject":["user","bob"],"relation":"direct_owner","object":["file","/bob-workspace"]},"id":1}'
   ```

4. **ReBAC Permission Checking** - `rebac_check` returns **TRUE** ✓
   ```bash
   curl http://localhost:8080/api/nfs/rebac_check \
     -H "Authorization: Bearer ${ADMIN_KEY}" \
     -d '{"jsonrpc":"2.0","method":"rebac_check","params":{"subject":["user","bob"],"permission":"write","object":["file","/bob-workspace"]},"id":1}'

   # Result: {"result": true}  ✓
   ```

5. **Admin Operations** - All file operations work with admin API key
   - ✅ `write` - Admin can write files
   - ✅ `read` - Admin can read files
   - ✅ `mkdir` - Admin can create directories
   - ✅ `list` - Admin can list files

### ❌ What Doesn't Work

1. **User Write Operations** - `write` fails even with `direct_owner` permission
   ```bash
   curl http://localhost:8080/api/nfs/write \
     -H "Authorization: Bearer ${USER_API_KEY}" \
     -d '{"jsonrpc":"2.0","method":"write","params":{"path":"/bob-workspace/hello.txt","content":{"__type__":"bytes","data":"SGVsbG8h"}},"id":1}'

   # Error: "Access denied: User 'bob' does not have WRITE permission for '/bob-workspace'"
   ```

2. **User Read Operations** - `read` fails even with `direct_viewer` permission
   ```bash
   curl http://localhost:8080/api/nfs/read \
     -H "Authorization: Bearer ${USER_API_KEY}" \
     -d '{"jsonrpc":"2.0","method":"read","params":{"path":"/bob-workspace/hello.txt"},"id":1}'

   # Error: "Access denied: User 'bob' does not have READ permission for '/bob-workspace/hello.txt'"
   ```

3. **User Directory Operations** - `mkdir` fails even with `direct_owner` permission
   ```bash
   curl http://localhost:8080/api/nfs/mkdir \
     -H "Authorization: Bearer ${USER_API_KEY}" \
     -d '{"jsonrpc":"2.0","method":"mkdir","params":{"path":"/bob-workspace/subdir"},"id":1}'

   # Error: "Access denied: User 'bob' does not have WRITE permission for '/bob-workspace'"
   ```

## The Permission Check Bug

### Evidence

**ReBAC Check vs Internal Check Discrepancy:**

| API Call | Method | Result |
|----------|--------|--------|
| `rebac_check` | Check Bob's write permission on `/bob-workspace` | ✅ **TRUE** |
| `rebac_explain` | Explain permission path | ✅ **Shows valid path: direct_owner → owner → editor → write** |
| `write` | Actual write operation | ❌ **"Access denied"** |
| `mkdir` | Actual mkdir operation | ❌ **"Access denied"** |
| `read` | Actual read operation | ❌ **"Access denied"** |

### Permission Path (from `rebac_explain`)

```
user:bob → write → file:/bob-workspace
  ↓ (expanded to: editor, owner)
  ↓ via_userset: editor
  ├─ user:bob → editor → file:/bob-workspace
  │   ↓ (union: direct_editor, parent_editor, owner)
  │   ↓ via_union_member: owner
  │   ├─ user:bob → owner → file:/bob-workspace
  │       ↓ (union: direct_owner, parent_owner)
  │       ↓ via_union_member: direct_owner
  │       └─ user:bob → direct_owner → file:/bob-workspace ✓ (TUPLE EXISTS)
```

The permission path is **valid** and the ReBAC system correctly resolves it. But the internal `_check_permission()` method fails to recognize it.

### Root Cause Analysis

Based on code exploration of `/Users/jinjingzhou/nexi-lib/nexus/src/nexus/core/`:

1. **Permission Check Timing** ([nexus_fs_core.py:303-325](nexus_fs_core.py:303-325))
   - For NEW files, permission check happens on **parent directory**
   - Permission check executes **BEFORE** parent tuples are created
   - Parent tuples are created **AFTER** backend write (lines 384-392)

2. **Permission Enforcer** ([permissions.py:210-328](permissions.py:210-328))
   - `_check_permission()` calls `permission_enforcer.check()`
   - `EnhancedPermissionEnforcer` uses `rebac_manager.rebac_check()`
   - **BUT**: There appears to be a disconnect between the RPC API `rebac_check` and the internal enforcer

3. **Suspected Issue**
   - The internal permission enforcer may be:
     - Using a different `tenant_id` (not passing it correctly)
     - Using a different ReBAC manager instance
     - Bypassing the ReBAC check for some reason
     - Having a caching issue where it doesn't see the permissions

## Tested Configurations

### ReBAC Namespace Configuration (File Object Type)

```json
{
  "relations": {
    "parent": {},
    "direct_owner": {},
    "direct_editor": {},
    "direct_viewer": {},
    "parent_owner": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "owner"}},
    "parent_editor": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "editor"}},
    "parent_viewer": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "viewer"}},
    "owner": {"union": ["direct_owner", "parent_owner"]},
    "editor": {"union": ["direct_editor", "parent_editor", "owner"]},
    "viewer": {"union": ["direct_viewer", "parent_viewer"]}
  },
  "permissions": {
    "read": ["viewer", "editor", "owner"],
    "write": ["editor", "owner"],
    "execute": ["owner"]
  }
}
```

### Test User Identity

```json
{
  "authenticated": true,
  "subject_type": "user",
  "subject_id": "bob",
  "tenant_id": "default",
  "is_admin": false,
  "user": "bob"
}
```

### Test ReBAC Tuple

```json
{
  "tuple_id": "96f7f527-0463-4e35-9a4a-419b69a9028f",
  "subject_type": "user",
  "subject_id": "bob",
  "relation": "direct_owner",
  "object_type": "file",
  "object_id": "/bob-workspace",
  "tenant_id": "default"
}
```

## Workaround

Until the bug is fixed, use **admin API keys** for all file operations:

```bash
# 1. Create user (for identity)
curl http://localhost:8080/api/nfs/admin_create_key \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob","is_admin":false},"id":1}'

# 2. Create workspace directory as ADMIN
curl http://localhost:8080/api/nfs/mkdir \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"mkdir","params":{"path":"/bob-workspace"},"id":1}'

# 3. Register workspace as ADMIN
curl http://localhost:8080/api/nfs/register_workspace \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"path":"/bob-workspace"},"id":1}'

# 4. Write files as ADMIN
curl http://localhost:8080/api/nfs/write \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"write","params":{"path":"/bob-workspace/file.txt","content":{"__type__":"bytes","data":"..."}},"id":1}'

# 5. Read files as ADMIN (user API keys don't work)
curl http://localhost:8080/api/nfs/read \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"read","params":{"path":"/bob-workspace/file.txt"},"id":1}'
```

**Alternative:** Grant users **admin privileges** when creating their API keys:

```bash
curl http://localhost:8080/api/nfs/admin_create_key \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob Admin Key","is_admin":true},"id":1}'
```

## Recommendations

1. **Investigate Permission Enforcer** - Debug why `_check_permission()` fails when `rebac_check` API succeeds
2. **Check Tenant ID Handling** - Verify `tenant_id` is passed correctly in all permission checks
3. **Review Caching** - Check if permission cache is preventing updates from being seen
4. **Add Integration Tests** - Add tests that verify ReBAC permissions work for actual file operations, not just the API
5. **Consider Disabling Enforcement** - Add option to run server without permission enforcement for testing

## Files Modified

1. ✅ [demo-hierarchical-permissions.sh](demo-hierarchical-permissions.sh) - Updated admin key

## Test Results

| Use Case Step | Status | Notes |
|---------------|--------|-------|
| Create user and get API key | ✅ **PASS** | `admin_create_key` works |
| Create workspace | ✅ **PASS** | `register_workspace` works |
| Write file without permission issues | ❌ **FAIL** | Permission check bug blocks all user operations |

**Overall Status:** ❌ **BLOCKED BY BUG**

The test use case **cannot be completed** due to the permission enforcer bug. All user file operations fail even with correct ReBAC permissions.
