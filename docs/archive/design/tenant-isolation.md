
---

## 🚨 THE ACTUAL IMPLEMENTATION (v0.5.0 is Incomplete!)

Looking at the actual code, tenant isolation is **STILL using the OLD v0.4.x instance-level API!**

### Found in src/nexus/core/nexus_fs_core.py:

```python
route = self.router.route(
    path,
    zone_id=self.zone_id,  # ← Instance variable (v0.4.x style)
    agent_id=self.agent_id,    # ← Instance variable (v0.4.x style)
    is_admin=self.is_admin,    # ← Instance variable (v0.4.x style)
    check_write=False,
)
```

### Where do these come from?

From `src/nexus/core/nexus_fs.py:133-137`:

```python
# v0.5.0: No longer accept zone_id/agent_id/user_id in __init__
# These are set to None - operations must pass subject parameter instead
self.zone_id: str | None = None
self.agent_id: str | None = None
self.user_id: str | None = None
```

**They're ALL None!**

---

## 🚨 THE BROKEN ARCHITECTURE

### What v0.5.0 CLAIMED to do:

```python
# "New way" (from docstring)
nx = NexusFS(backend)
nx.read("/file.txt", context=OperationContext(user="alice", ...))
```

### What v0.5.0 ACTUALLY does:

```python
# Instance variables are still used!
nx.zone_id  # → None (never set!)
nx.agent_id   # → None (never set!)
nx.is_admin   # → False (from init param)

# File operations use these None values:
def read(self, path, context=None):
    route = self.router.route(
        path,
        zone_id=self.zone_id,  # ← ALWAYS None!
        agent_id=self.agent_id,    # ← ALWAYS None!
        is_admin=self.is_admin     # ← From init, not context!
    )
```

### What this means:

1. ❌ **`zone_id` is ALWAYS None** - no tenant isolation!
2. ❌ **`agent_id` is ALWAYS None** - no agent isolation!
3. ❌ **`is_admin` comes from init, not context** - can't change per-operation!
4. ❌ **`OperationContext` is NOT used for routing** - only for ReBAC permissions!

---

## The Complete Picture

### Two Parallel Permission Systems:

#### 1. **Path Router** (tenant isolation)
- Uses: `self.zone_id`, `self.agent_id`, `self.is_admin`
- Problem: Always None in v0.5.0
- Result: **NO tenant isolation enforcement**

#### 2. **Permission Enforcer** (ReBAC/ACL/UNIX)
- Uses: `OperationContext`
- Works: ✅ When `context` is passed
- Problem: Bypassed when `context` is None (uses `_default_context`)

### The Migration Never Finished:

```python
# v0.4.x (WORKED):
nx = NexusFS(backend, zone_id="org_acme", user_id="alice")
nx.write("/workspace/org_acme/alice/file.txt", data)
# ✅ self.zone_id = "org_acme"
# ✅ Router checks: path tenant matches self.zone_id
# ✅ Tenant isolation enforced

# v0.5.0 (BROKEN):
nx = NexusFS(backend)  # zone_id=None
ctx = OperationContext(user="alice", ...)
nx.write("/workspace/org_acme/alice/file.txt", data, context=ctx)
# ❌ self.zone_id = None
# ❌ Router can't enforce tenant isolation!
# ❌ OperationContext not used for routing!
```

---

## How Tenant Isolation SHOULD Work in v0.5.0

### Option A: Extract zone_id from OperationContext

```python
# Add zone_id to OperationContext
@dataclass
class OperationContext:
    user: str
    groups: list[str]
    zone_id: str | None = None  # ← ADD THIS
    is_admin: bool = False
    is_system: bool = False

# Use it in routing
def write(self, path, content, context=None):
    ctx = context or self._default_context

    route = self.router.route(
        path,
        zone_id=ctx.zone_id,  # ← From context!
        agent_id=None,  # Or extract from user string
        is_admin=ctx.is_admin,
        check_write=True
    )
```

### Option B: Extract zone_id from JWT/AuthResult

```python
# AuthResult already has zone_id!
@dataclass
class AuthResult:
    authenticated: bool
    subject_id: str
    zone_id: str | None  # ← Already exists!
    is_admin: bool = False

# RPC server should create context WITH zone_id
def handle_request(jwt_token):
    auth_result = auth.authenticate(jwt_token)

    ctx = OperationContext(
        user=auth_result.subject_id,
        zone_id=auth_result.zone_id,  # ← Pass it through!
        groups=[],
        is_admin=auth_result.is_admin
    )

    nx.write(path, data, context=ctx)
```

### Option C: Extract from path (current broken approach)

```python
# This is what it TRIES to do now but fails because:
# 1. self.zone_id is always None
# 2. Can't check if path tenant matches user tenant
# 3. No enforcement
```

---

## Current State Summary

| Component | Claims to Support | Actually Works | Why |
|-----------|-------------------|----------------|-----|
| **Path routing** | Tenant isolation | ❌ NO | `self.zone_id` always None |
| **OperationContext** | Per-operation identity | ⚠️ Partial | Only for ReBAC, not routing |
| **_default_context** | Backward compat | ❌ NO | is_system=True bypasses everything |
| **Multi-user RPC** | Yes | ❌ NO | No context passed to operations |
| **Tenant isolation** | Yes | ❌ NO | Router never checks tenant |

---

## What Needs to Happen

### Immediate Fix:

1. **Add `zone_id` to `OperationContext`**
2. **Use `ctx.zone_id` in router.route() calls**
3. **Set `_default_context.is_system = False`**
4. **RPC server: create proper context from JWT**

### Code Changes Required:

```python
# 1. Update OperationContext
@dataclass
class OperationContext:
    user: str
    groups: list[str]
    zone_id: str | None = None  # ← ADD
    is_admin: bool = False
    is_system: bool = False

# 2. Update all router.route() calls
def write(self, path, content, context=None):
    ctx = context or self._default_context

    route = self.router.route(
        path,
        zone_id=ctx.zone_id,  # ← Use context
        agent_id=None,            # ← Or ctx.agent_id if added
        is_admin=ctx.is_admin,    # ← Use context
        check_write=True
    )

# 3. RPC server: create context from JWT
auth_result = await auth.authenticate(token)
ctx = OperationContext(
    user=auth_result.subject_id,
    zone_id=auth_result.zone_id,  # ← From JWT!
    groups=[],
    is_admin=auth_result.is_admin
)
```

---

## Final Answer to Your Question

**Q: How do we decide tenant in `ctx = OperationContext(user="alice", groups=[], is_admin=False)`?**

**A: WE DON'T! That's the bug!**

`OperationContext` doesn't have `zone_id`, so:
1. ❌ No way to pass tenant from auth to router
2. ❌ Router uses `self.zone_id` which is always None
3. ❌ No tenant isolation enforcement
4. ❌ Multi-user deployments are completely broken

**The v0.5.0 migration is incomplete. It changed the API but didn't wire up the new OperationContext to the router.**
