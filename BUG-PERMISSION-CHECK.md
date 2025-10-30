# Bug Report: Permission Enforcer Fails to Recognize Valid ReBAC Permissions

## Summary

The internal permission enforcer (`_check_permission()`) rejects file operations for non-admin users even when the ReBAC API (`rebac_check`) confirms they have the required permissions. This makes it impossible for regular users to perform any file operations (read, write, mkdir) even with correct permission tuples.

## Impact

**CRITICAL** - Blocks all user file operations
- ❌ Users cannot write files to their own workspaces
- ❌ Users cannot read files they have permissions for
- ❌ Users cannot create directories
- ✅ Only admin users can perform file operations
- ✅ ReBAC APIs (`rebac_check`, `rebac_explain`) work correctly

## Environment

- **Nexus Version:** 0.5.1+
- **Server:** `http://localhost:8080`
- **Backend:** GCS (nexi-hub)
- **Metadata Store:** PostgreSQL (nexi-lab-888:us-west1:nexus-hub)
- **Permission Enforcement:** Enabled
- **Date:** 2025-10-30

## Steps to Reproduce

### 1. Create a User and Workspace

```bash
ADMIN_KEY="sk-default_admin_89dd329f_58aff805c19c2ac0099d56b18778a8bd"

# Create user 'bob'
curl -X POST http://localhost:8080/api/nfs/admin_create_key \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob Key","is_admin":false},"id":1}'

# Result: api_key = "sk-default_bob_..."
USER_KEY="sk-default_bob_baa2c0a3_a01e979a2d3485febec9e95c3127cd80"

# Create workspace directory
curl -X POST http://localhost:8080/api/nfs/mkdir \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"mkdir","params":{"path":"/bob-workspace"},"id":1}'

# Register workspace
curl -X POST http://localhost:8080/api/nfs/register_workspace \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"path":"/bob-workspace","name":"bob-workspace"},"id":1}'
```

### 2. Grant Bob Owner Permission

```bash
# Grant 'direct_owner' relation to Bob on /bob-workspace
curl -X POST http://localhost:8080/api/nfs/rebac_create \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"rebac_create","params":{"subject":["user","bob"],"relation":"direct_owner","object":["file","/bob-workspace"]},"id":1}'

# Result: tuple_id = "96f7f527-0463-4e35-9a4a-419b69a9028f"
```

### 3. Verify Permission Using rebac_check (Returns TRUE ✓)

```bash
curl -X POST http://localhost:8080/api/nfs/rebac_check \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"rebac_check","params":{"subject":["user","bob"],"permission":"write","object":["file","/bob-workspace"]},"id":1}'

# Result: {"result": true} ✓
```

### 4. Try to Write File as Bob (FAILS ✗)

```bash
CONTENT=$(echo -n "Hello, World!" | base64)

curl -X POST http://localhost:8080/api/nfs/write \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${USER_KEY}" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"write\",\"params\":{\"path\":\"/bob-workspace/hello.txt\",\"content\":{\"__type__\":\"bytes\",\"data\":\"${CONTENT}\"}},\"id\":1}"

# Error: "Access denied: User 'bob' does not have WRITE permission for '/bob-workspace'"
```

## Expected Behavior

Bob should be able to write files to `/bob-workspace/hello.txt` because:

1. ✅ Bob has `direct_owner` relation on `/bob-workspace`
2. ✅ According to namespace config: `direct_owner` → `owner` → `editor` → `write` permission
3. ✅ `rebac_check` confirms Bob has write permission
4. ✅ Bob's identity is correct (verified via `/api/auth/whoami`)
5. ✅ The ReBAC tuple exists (verified via `rebac_list_tuples`)
6. ✅ The permission path is valid (verified via `rebac_explain`)

## Actual Behavior

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32603,
    "message": "Internal error: Access denied: User 'bob' does not have WRITE permission for '/bob-workspace'"
  }
}
```

## Evidence: rebac_check vs Internal Check Discrepancy

| Test | API Call | Parameters | Result |
|------|----------|------------|--------|
| **ReBAC Check** | `rebac_check` | subject=`["user","bob"]`<br>permission=`"write"`<br>object=`["file","/bob-workspace"]` | ✅ **TRUE** |
| **ReBAC Explain** | `rebac_explain` | Same as above | ✅ **Valid path:<br>direct_owner → owner → editor → write** |
| **Actual Write** | `write` (as Bob) | path=`"/bob-workspace/hello.txt"` | ❌ **"Access denied"** |
| **Actual Mkdir** | `mkdir` (as Bob) | path=`"/bob-workspace/subdir"` | ❌ **"Access denied"** |
| **Actual Read** | `read` (as Bob with viewer perm) | path=`"/bob-workspace/file.txt"` | ❌ **"Access denied"** |

## rebac_explain Output (Permission Path is Valid!)

```json
{
  "result": true,
  "successful_path": {
    "subject": "user:bob",
    "permission": "write",
    "object": "file:/bob-workspace",
    "granted": true,
    "expanded_to": ["editor", "owner"],
    "via_userset": "editor",
    "sub_paths": [
      {
        "subject": "user:bob",
        "permission": "editor",
        "object": "file:/bob-workspace",
        "granted": true,
        "union": ["direct_editor", "parent_editor", "owner"],
        "via_union_member": "owner",
        "sub_paths": [
          {
            "subject": "user:bob",
            "permission": "owner",
            "object": "file:/bob-workspace",
            "granted": true,
            "union": ["direct_owner", "parent_owner"],
            "via_union_member": "direct_owner",
            "sub_paths": [
              {
                "subject": "user:bob",
                "permission": "direct_owner",
                "object": "file:/bob-workspace",
                "granted": true,
                "direct_relation": true,
                "tuple": {
                  "tuple_id": "96f7f527-0463-4e35-9a4a-419b69a9028f",
                  "subject_type": "user",
                  "subject_id": "bob",
                  "relation": "direct_owner",
                  "object_type": "file",
                  "object_id": "/bob-workspace",
                  "tenant_id": "default"
                }
              }
            ]
          }
        ]
      }
    ]
  }
}
```

The permission path is **completely valid**. The ReBAC system correctly resolves `direct_owner` → `owner` → `editor` → `write`.

## Diagnostic Information

### User Identity (from `/api/auth/whoami`)

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

### ReBAC Tuple (from `rebac_list_tuples`)

```json
{
  "tuple_id": "96f7f527-0463-4e35-9a4a-419b69a9028f",
  "subject_type": "user",
  "subject_id": "bob",
  "relation": "direct_owner",
  "object_type": "file",
  "object_id": "/bob-workspace",
  "tenant_id": "default",
  "created_at": "2025-10-30T10:01:45.621631-07:00",
  "expires_at": null
}
```

### Namespace Configuration (File Object Type)

```json
{
  "relations": {
    "direct_owner": {},
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

## Code Analysis

Based on exploration of `/Users/jinjingzhou/nexi-lib/nexus/src/nexus/core/`:

### Permission Check Flow (for NEW files)

**File: `nexus_fs_core.py`, Lines 303-325**

```python
if self._enforce_permissions:
    ctx = context or self._default_context
    if meta is not None:
        # Existing file - check permission on file
        self._check_permission(path, Permission.WRITE, ctx)
    else:
        # NEW file - check permission on PARENT directory
        parent_path = self._get_parent_path(path)  # Returns "/bob-workspace"
        if parent_path:
            self._check_permission(parent_path, Permission.WRITE, ctx)  # Line 325 - FAILS HERE
```

For a NEW file `/bob-workspace/hello.txt`, the code checks WRITE permission on **parent directory** `/bob-workspace`.

### Permission Enforcer

**File: `permissions.py`, Lines 569-608**

```python
def _check_permission(
    self,
    path: str,
    permission: Permission,
    context: OperationContext | None = None,
) -> None:
    """Check if operation is permitted."""
    if not self._enforce_permissions:
        return

    ctx = context or self._default_context

    # Check permission using enforcer
    result = self._permission_enforcer.check(path, permission, ctx)  # RETURNS FALSE

    if not result:
        raise PermissionError(
            f"Access denied: User '{ctx.user}' does not have {permission.name} "
            f"permission for '{path}'"
        )
```

The `permission_enforcer.check()` method returns **FALSE** even though `rebac_check` API returns **TRUE**.

## Suspected Root Causes

### 1. Different ReBAC Manager Instances

The RPC API's `rebac_check` endpoint may use a different `ReBAC_manager` instance than the internal `EnhancedPermissionEnforcer`. This could cause:
- Different caching states
- Different namespace configurations
- Different tenant ID handling

### 2. Tenant ID Not Passed Correctly

The internal `_check_permission()` may not be passing `tenant_id` correctly to the ReBAC check, causing the permission query to fail.

**Evidence needed:**
- Log the exact parameters passed to `rebac_manager.rebac_check()` inside `EnhancedPermissionEnforcer.check()`
- Compare with parameters used by the RPC API endpoint

### 3. Permission Enforcer Bypasses ReBAC

The `EnhancedPermissionEnforcer` may have logic that bypasses the ReBAC check under certain conditions, falling back to a simpler permission model that doesn't work correctly.

### 4. Caching Issue

The internal permission enforcer may be using a stale cache that doesn't include the newly created permission tuple, while the RPC API correctly sees the updated state.

## Investigation Steps

1. **Add Debug Logging**
   - Log exact parameters in `EnhancedPermissionEnforcer.check()` before calling `rebac_manager.rebac_check()`
   - Log the result from `rebac_manager.rebac_check()` inside the enforcer
   - Compare with RPC API parameters

2. **Check Tenant ID Handling**
   - Verify `tenant_id` from `OperationContext` is passed to ReBAC check
   - Check if `tenant_id=None` vs `tenant_id="default"` causes different results

3. **Verify ReBAC Manager Instance**
   - Confirm both RPC API and permission enforcer use the same ReBAC manager instance
   - Check if they share the same cache

4. **Test with Permission Enforcement Disabled**
   - Start server with `enforce_permissions=False` to verify file operations work
   - This confirms the issue is specifically in permission checking

## Workaround (Temporary)

Grant users **admin privileges** to bypass the broken permission enforcer:

```bash
curl -X POST http://localhost:8080/api/nfs/admin_create_key \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob Admin Key","is_admin":true},"id":1}'
```

**Note:** This grants unrestricted access and should only be used for testing.

## Test Scripts

Created comprehensive test scripts in `/Users/jinjingzhou/nexi-lib/nexus/`:

1. **[test-user-workspace.sh](test-user-workspace.sh)** - Reproduces the bug
2. **[test-user-workspace-final.sh](test-user-workspace-final.sh)** - Comprehensive diagnostics
3. **[test-api-workaround.sh](test-api-workaround.sh)** - Admin workaround
4. **[API-TEST-RESULTS.md](API-TEST-RESULTS.md)** - Full test report

## Related Files

- `src/nexus/core/nexus_fs_core.py` (Lines 303-325) - Permission check for write operations
- `src/nexus/core/nexus_fs.py` (Lines 569-608) - `_check_permission()` implementation
- `src/nexus/core/permissions.py` (Lines 210-328) - `EnhancedPermissionEnforcer` class
- `src/nexus/core/hierarchy_manager.py` - Parent tuple creation (happens AFTER permission check)

## Priority

**CRITICAL** - This bug blocks all non-admin user file operations, making the ReBAC permission system unusable for regular users.

## Labels

`bug`, `critical`, `permissions`, `rebac`, `authentication`
