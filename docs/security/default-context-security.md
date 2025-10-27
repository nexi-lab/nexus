# 🚨 CRITICAL SECURITY ISSUE: _default_context.is_system=True

## The Problem

**`_default_context` has `is_system=True` which BYPASSES ALL PERMISSIONS!**

```python
# src/nexus/core/nexus_fs.py:200-207
self._default_context = OperationContext(
    user="system",
    groups=[],
    is_admin=is_admin,
    is_system=True,  # ⚠️ ALWAYS TRUE - BYPASSES EVERYTHING!
)
```

### What this means:

1. **Any operation without `context` parameter uses system bypass**
2. **RPC server doesn't extract JWT claims into context**
3. **All remote API calls run as "system" admin**

---

## The Attack Vector

### Scenario 1: RPC Server (Remote API)

**Current vulnerable code:**

```python
# src/nexus/server/rpc_server.py:184-204
def _validate_auth(self) -> bool:
    """Validate API key authentication."""
    # If no API key is configured, allow all requests
    if not self.api_key:
        return True  # ⚠️ No auth required!

    # Check Authorization header
    auth_header = self.headers.get("Authorization")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header[7:]  # Remove "Bearer " prefix
    return bool(token == self.api_key)  # ⚠️ Only checks static API key!

def _handle_rpc_call(self, request: RPCRequest) -> None:
    # ... validate auth ...

    # Call method on NexusFS
    result = method(**params)  # ⚠️ NO context parameter passed!
    #                          # Uses _default_context (is_system=True)
    #                          # BYPASSES ALL PERMISSIONS!
```

**The vulnerability:**

1. RPC server validates API key ✅
2. But when calling `nx.write()`, **NO context is passed** ❌
3. Uses `_default_context` with `is_system=True` ❌
4. **ALL permission checks are bypassed!** ❌

**Example attack:**

```bash
# Attacker gets valid API key (or no auth required)
curl -X POST http://nexus.example.com/api/nfs/write \
  -H "Authorization: Bearer valid-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/admin/secrets.txt",
    "content": "HACKED"
  }'

# ✅ Succeeds even if user shouldn't have access!
# Because nx.write() uses _default_context.is_system=True
```

---

## The Root Causes

### 1. **RPC Server Doesn't Create OperationContext**

The RPC server validates auth but doesn't extract user identity:

```python
# CURRENT (BROKEN):
def _handle_rpc_call(self, request: RPCRequest):
    # Validates API key
    if not self._validate_auth():
        return error

    # Calls method WITHOUT context
    result = nx.write(path, content)  # Uses _default_context!

# SHOULD BE:
def _handle_rpc_call(self, request: RPCRequest):
    # Parse JWT from Authorization header
    token = self.headers.get("Authorization")[7:]
    claims = jwt_provider.verify_token(token)

    # Create context from JWT claims
    ctx = OperationContext(
        user=claims["subject_id"],
        groups=claims.get("groups", []),
        is_admin=claims.get("is_admin", False)
    )

    # Pass context to operation
    result = nx.write(path, content, context=ctx)  # ✅ CORRECT
```

### 2. **_default_context Should NOT Have is_system=True**

The default context should be restrictive, not permissive:

```python
# CURRENT (INSECURE):
self._default_context = OperationContext(
    user="system",
    groups=[],
    is_admin=is_admin,
    is_system=True,  # ❌ BYPASSES ALL PERMISSIONS
)

# SHOULD BE (SECURE):
self._default_context = OperationContext(
    user="anonymous",
    groups=[],
    is_admin=False,  # ❌ NO admin
    is_system=False,  # ❌ NO system bypass
)

# Or even better - REQUIRE context:
def write(self, path, content, context):  # NO default!
    if context is None:
        raise ValueError("context is required when enforce_permissions=True")
```

---

## Security Impact

### ✅ **Local usage (single user):**
- OK - user owns the NexusFS instance
- `enforce_permissions=False` is appropriate

### ⚠️ **RPC Server (multi-user):**
- **CRITICAL VULNERABILITY**
- All users can access all files
- No permission enforcement
- ReBAC/ACL/UNIX permissions completely bypassed

### Example Vulnerable Deployment:

```python
# Production deployment (VULNERABLE)
from nexus import NexusFS
from nexus.server import RPCServer

nx = NexusFS(
    backend=GCSBackend("production-bucket"),
    enforce_permissions=True,  # ⚠️ Doesn't matter!
    is_admin=False  # ⚠️ Doesn't matter!
)

# Start RPC server
server = RPCServer(
    nexus_fs=nx,
    api_key="static-api-key"  # All users share same key
)
server.start()

# ALL USERS CAN:
# - Read any file (including /admin/secrets.txt)
# - Write any file (including /etc/passwd)
# - Delete any file (including /production/data)
#
# Because nx.write() uses _default_context.is_system=True
```

---

## The Fix

### Option 1: **Require context parameter**

```python
# src/nexus/core/nexus_fs_core.py
def write(
    self,
    path: str,
    content: bytes,
    context: OperationContext | None = None,  # ← Make this required
    ...
):
    # Enforce context when permissions are enabled
    if self._enforce_permissions and context is None:
        raise ValueError(
            "context parameter is required when enforce_permissions=True. "
            "Use OperationContext(user='...', groups=[], is_admin=False)"
        )

    # Use default context ONLY when permissions are disabled
    ctx = context or self._default_context
    ...
```

### Option 2: **Fix RPC Server to extract JWT claims**

```python
# src/nexus/server/rpc_server.py
class RPCRequestHandler(BaseHTTPRequestHandler):
    nexus_fs: NexusFilesystem
    jwt_provider: JWTProvider  # ← Add JWT provider

    def _create_context_from_jwt(self) -> OperationContext:
        """Extract user context from JWT token."""
        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            # No auth - use anonymous context
            return OperationContext(
                user="anonymous",
                groups=[],
                is_admin=False,
                is_system=False
            )

        token = auth_header[7:]

        # Verify JWT and extract claims
        try:
            claims = self.jwt_provider.verify_token(token)

            return OperationContext(
                user=claims["subject_id"],
                groups=claims.get("groups", []),
                is_admin=claims.get("is_admin", False),
                is_system=False  # ← NEVER allow is_system from JWT
            )
        except Exception:
            # Invalid token - deny access
            return OperationContext(
                user="anonymous",
                groups=[],
                is_admin=False,
                is_system=False
            )

    def _handle_rpc_call(self, request: RPCRequest):
        # ... existing code ...

        # Create context from JWT
        context = self._create_context_from_jwt()

        # Add context to params if not already present
        if "context" not in params:
            params["context"] = context

        # Call method with context
        result = method(**params)
```

### Option 3: **Change _default_context to be restrictive**

```python
# src/nexus/core/nexus_fs.py:200-207
self._default_context = OperationContext(
    user="anonymous",      # ← Not "system"
    groups=[],
    is_admin=False,        # ← Not based on init param
    is_system=False,       # ← CRITICAL: No bypass!
)

# Document that operations REQUIRE context when permissions enabled
# For local usage, set enforce_permissions=False
```

---

## Recommended Action

### Immediate (Critical):

1. **🚨 Do NOT deploy RPC server to production** until this is fixed
2. **🚨 Warn existing deployments** about this vulnerability
3. **Fix RPC server** to extract JWT claims and create proper context

### Short-term (High Priority):

1. **Change _default_context.is_system to False**
2. **Make context parameter required** when `enforce_permissions=True`
3. **Update all examples** to pass context explicitly

### Long-term (Best Practice):

1. **Remove _default_context entirely** - always require explicit context
2. **Add authentication middleware** to RPC server
3. **Audit all @rpc_expose methods** to ensure they accept context parameter

---

## Current State of Security

| Component | Status | Risk |
|-----------|--------|------|
| **Local usage** (single user) | ✅ OK | Low - user owns instance |
| **RPC Server** (multi-user) | ❌ VULNERABLE | **CRITICAL** - all permissions bypassed |
| **Embedded usage** (app controls context) | ⚠️ Depends | Medium - app must pass context |
| **Production with enforce_permissions=True** | ❌ BROKEN | **CRITICAL** - is_system bypasses |

---

## Test to Verify Vulnerability

```python
# Test script
from nexus import NexusFS
from nexus.backends.local import LocalBackend
from nexus.core.permissions import OperationContext

# Setup
nx = NexusFS(
    backend=LocalBackend("/tmp/test"),
    enforce_permissions=True  # ← Permissions enabled
)

# Create file as admin
admin_ctx = OperationContext(user="admin", groups=[], is_admin=True)
nx.write("/admin/secret.txt", b"secret data", context=admin_ctx)

# Create ReBAC permission - ONLY admin can read
nx.rebac_create(
    subject=("user", "admin"),
    relation="direct_reader",
    object=("file", "/admin/secret.txt")
)

# Try to read as regular user
user_ctx = OperationContext(user="bob", groups=[], is_admin=False)
try:
    nx.read("/admin/secret.txt", context=user_ctx)
    print("❌ FAIL: Bob can read admin file (should be denied)")
except PermissionError:
    print("✅ PASS: Bob denied (permissions work)")

# Try to read WITHOUT context
try:
    content = nx.read("/admin/secret.txt")  # No context!
    print(f"🚨 VULNERABILITY: Read succeeded without context!")
    print(f"   Content: {content}")
    print(f"   Reason: _default_context.is_system=True bypassed all permissions")
except PermissionError:
    print("✅ PASS: Denied even without context")
```

**Expected result:** 🚨 The vulnerability exists - read succeeds without context.

---

## Summary

**YES, this is a CRITICAL security issue:**

1. ✅ `_default_context.is_system=True` bypasses ALL permissions
2. ✅ RPC server doesn't create user-specific context
3. ✅ All remote API calls run as "system" with full access
4. ✅ `enforce_permissions=True` has NO EFFECT in RPC server

**Do NOT deploy RPC server to production until this is fixed.**

For local usage with `enforce_permissions=False`, it's fine.
