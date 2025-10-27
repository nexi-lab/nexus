# Why _default_context Exists (And Why It's Problematic)

## The Reason

Looking at the code comment in `src/nexus/core/nexus_fs.py:200-201`:

```python
# v0.5.0: Create minimal _default_context for backward compatibility only
# Operations should pass subject parameter instead of relying on this
self._default_context = OperationContext(
    user="system",
    groups=[],
    is_admin=is_admin,
    is_system=True,  # ← The problem
)
```

**_default_context exists for BACKWARD COMPATIBILITY.**

---

## The History

### Before v0.5.0: Instance-Level Identity

```python
# OLD API (v0.4.x and earlier)
nx = NexusFS(
    backend,
    tenant_id="org_acme",  # ← Configured at init
    agent_id="bot1",       # ← Configured at init
    user_id="alice"        # ← Configured at init
)

# Operations used instance-level identity
nx.write("/file.txt", content)  # Implicitly uses nx.tenant_id, nx.user_id
nx.read("/file.txt")             # Implicitly uses nx.tenant_id, nx.user_id
```

**Problem:** One NexusFS instance = one user. Can't handle multiple users.

### After v0.5.0: Operation-Level Identity

```python
# NEW API (v0.5.0+)
nx = NexusFS(backend)  # ← No user identity at init

# Operations require explicit context
ctx_alice = OperationContext(user="alice", groups=[], is_admin=False)
ctx_bob = OperationContext(user="bob", groups=[], is_admin=False)

nx.write("/file.txt", content, context=ctx_alice)  # Alice's operation
nx.read("/file.txt", context=ctx_bob)              # Bob's operation
```

**Benefit:** One NexusFS instance can serve multiple users!

### The Compatibility Layer

```python
# To support code that doesn't pass context:
def write(self, path, content, context=None):
    ctx = context or self._default_context  # ← Fallback for old code
    ...
```

This allows old code (that doesn't pass `context`) to still work.

---

## Why _default_context Has is_system=True

The comment says:

> "v0.5.0: Create minimal _default_context for backward compatibility only"

**The intent was:**
- Old code that doesn't pass `context` should still work
- Since old code had no permission checks, give it system bypass
- Preserve old behavior (no permission enforcement by default)

**From `src/nexus/core/nexus_fs.py:104-114`:**

```python
# Note:
#     v0.5.0 BREAKING CHANGE: Removed tenant_id, user_id, and agent_id parameters.
#     Use the subject parameter in individual operations instead:
#
#         # Old way (v0.4.x - NO LONGER WORKS):
#         nx = NexusFS(backend, tenant_id="acme", agent_id="bot")
#         nx.read("/file.txt")
#
#         # New way (v0.5.0+):
#         nx = NexusFS(backend)
#         nx.read("/file.txt", subject=("agent", "bot"))
```

**The migration path:**
1. v0.4.x: Instance-level identity, no permission checks
2. v0.5.0: Operation-level identity, permission checks opt-in
3. _default_context bridges the gap for old code

---

## The Fundamental Design Flaw

### Good Intentions:

```python
# Backward compatibility for code without context
self._default_context = OperationContext(
    user="system",
    is_system=True,  # ← Allow old code to work
)
```

### Catastrophic Result:

```python
# NEW code that forgets to pass context ALSO gets system bypass!
nx = NexusFS(backend, enforce_permissions=True)
nx.write("/admin/secrets.txt", data)  # ⚠️ Forgot context parameter!
#                                     # Gets system bypass anyway!
```

**The flaw:** Can't distinguish between:
1. Old code (v0.4.x) that needs compatibility
2. New code (v0.5.0+) that forgot to pass context

Both get system bypass!

---

## Better Alternatives

### Option 1: Require Context When Permissions Enabled

```python
def write(self, path, content, context=None):
    # Enforce context when permissions are enabled
    if self._enforce_permissions and context is None:
        raise ValueError(
            "context parameter is required when enforce_permissions=True.\n"
            "For backward compatibility, set enforce_permissions=False."
        )

    ctx = context or self._default_context  # Only used if permissions disabled
    ...
```

**Benefits:**
- Old code without permissions: Works (enforce_permissions=False)
- New code with permissions: Must pass context (enforce_permissions=True)
- Security: Can't accidentally bypass permissions

### Option 2: Restrictive Default Context

```python
# Backward compatibility but SECURE by default
self._default_context = OperationContext(
    user="anonymous",      # Not "system"
    groups=[],
    is_admin=False,        # Not True
    is_system=False,       # ← CRITICAL: No bypass
)
```

**Benefits:**
- Old code: May fail with permission errors (forces migration)
- New code: Fails safely (permission denied, not granted)
- Security: Fail-closed instead of fail-open

### Option 3: Deprecation Warning

```python
def write(self, path, content, context=None):
    if context is None:
        warnings.warn(
            "Calling write() without context is deprecated and will be removed in v0.7.0. "
            "Pass context=OperationContext(...) explicitly.",
            DeprecationWarning,
            stacklevel=2
        )

    ctx = context or self._default_context
    ...
```

**Benefits:**
- Alerts developers to update their code
- Provides migration path
- Can remove _default_context in future version

---

## The Current Consequences

| Code Pattern | Expected Behavior | Actual Behavior | Risk |
|--------------|-------------------|-----------------|------|
| **Old code (v0.4.x)** | Works (compatibility) | ✅ Works | Low (intended) |
| **New code + context** | Permission checks | ✅ Permission checks | Low |
| **New code WITHOUT context** | Permission denied | ❌ System bypass | **CRITICAL** |
| **RPC server (multi-user)** | Per-user permissions | ❌ All as system | **CRITICAL** |

---

## Real-World Impact

### Local Single-User App: ✅ OK

```python
# Personal filesystem browser
nx = NexusFS(backend, enforce_permissions=False)
nx.write("/my-notes.txt", data)  # Works fine, no context needed
```

### Production Multi-User RPC Server: 🚨 BROKEN

```python
# Production deployment
nx = NexusFS(backend, enforce_permissions=True)

# RPC server handles request from user Alice
def handle_write_request(path, content):
    # ⚠️ Doesn't pass context - uses _default_context!
    nx.write(path, content)  # System bypass - Alice can access EVERYTHING

# RPC server handles request from user Bob
def handle_read_request(path):
    # ⚠️ Doesn't pass context - uses _default_context!
    return nx.read(path)  # System bypass - Bob can read EVERYTHING
```

**Result:** All users have full system access, all permissions bypassed.

---

## Why Not Just Remove is_system=True?

**We should!** But there's a concern:

```python
# If we change to is_system=False:
self._default_context = OperationContext(
    user="system",
    is_system=False,  # ← No longer bypasses
)

# Old code that relied on no permission checks will break:
nx = NexusFS(backend)  # Old code, no enforce_permissions param
nx.write("/file.txt", data)  # ❌ May fail with permission errors
```

**Counter-argument:** Breaking old insecure code is GOOD! Forces migration to secure API.

---

## Recommended Fix

### Immediate (Breaking but Secure):

```python
# src/nexus/core/nexus_fs.py:202-207
self._default_context = OperationContext(
    user="anonymous",      # ← Change from "system"
    groups=[],
    is_admin=False,        # ← Never True
    is_system=False,       # ← CRITICAL FIX
)

# Add validation
def write(self, path, content, context=None):
    if self._enforce_permissions and context is None:
        raise ValueError(
            "context is required when enforce_permissions=True. "
            "Pass context=OperationContext(user='...', groups=[], is_admin=False)"
        )
    ...
```

### Migration Guide:

```python
# For old code without permissions:
nx = NexusFS(backend, enforce_permissions=False)  # ← Explicitly disable
nx.write("/file.txt", data)  # Works without context

# For new code with permissions:
nx = NexusFS(backend, enforce_permissions=True)   # ← Enable security
ctx = OperationContext(user="alice", groups=[], is_admin=False)
nx.write("/file.txt", data, context=ctx)  # ← Must pass context
```

---

## Conclusion

### Why _default_context Exists:

**Backward compatibility** for v0.4.x code that doesn't pass `context` parameter.

### Why is_system=True:

To preserve old behavior (no permission checks) for compatibility code.

### Why This is Bad:

1. ❌ Can't distinguish old code from new code that forgot context
2. ❌ Both get system bypass (fail-open instead of fail-closed)
3. ❌ RPC server uses it for ALL requests (security disaster)
4. ❌ `enforce_permissions=True` has no effect without context

### What Should Happen:

1. **Change `is_system=False`** - fail-closed by default
2. **Require context when `enforce_permissions=True`** - force explicit usage
3. **Add deprecation warning** - guide migration
4. **Fix RPC server** - always create context from JWT
5. **Update docs** - clarify security implications

**TL;DR:** _default_context was added for backward compatibility but created a critical security vulnerability by using `is_system=True`. It should be changed to `is_system=False` immediately.
