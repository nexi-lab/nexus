# ADR: Delete ScopedHookEngine — Use KernelDispatch for All Notifications

**Issue**: [#907](https://github.com/nexi-lab/nexus/issues/907)
**Status**: Accepted
**Date**: 2026-02-23

## Summary

The three-layer hook engine stack (PluginHooks → AsyncHookEngine →
ScopedHookEngine) is deleted. KernelDispatch's two-phase model
(INTERCEPT + OBSERVE) is the single notification mechanism for all
kernel and service-layer events.

## Background

The codebase had two parallel notification systems:

### Hook Engine (deleted)

A three-layer "onion" built before KernelDispatch existed:

| Layer | Class | Role |
|-------|-------|------|
| 1 | `PluginHooks` | Sync hook registry with timeout |
| 2 | `AsyncHookEngine` | Async adapter wrapping Layer 1 |
| 3 | `ScopedHookEngine` | Per-agent scope filtering + veto |

~1,200 lines of implementation across 6 files, plus a protocol
definition. Designed for plugin extensibility that never materialized.

### KernelDispatch (kept)

Two-phase dispatch built as part of the kernel notification unification
(Issue #900):

| Phase | Semantics | Use Case |
|-------|-----------|----------|
| **INTERCEPT** | Sync, ordered, can veto | Permission checks, write validation |
| **OBSERVE** | Async, fire-and-forget | Indexing, cache invalidation, notifications |

### Why the hook engine was redundant

An audit revealed the hook engine was almost entirely dead code:

- **3 total registrations** existed across the entire codebase
- **1 was dead code**: IPC POST_WRITE hook was defined but its
  `register_ipc_hooks()` was never called
- **2 were live**: artifact indexing (CREATE + UPDATE) — migrated to a
  simple callback list
- **6 fire sites in BrickLifecycleManager** (PRE/POST_MOUNT,
  PRE/POST_UNMOUNT, PRE/POST_UNREGISTER) had **zero registered
  handlers** — every fire returned `proceed=True` (no-op)

The hook engine added ~6,300 lines (impl + tests) for functionality
that KernelDispatch already provides.

## Decision

**Delete the hook engine entirely. KernelDispatch is the single
notification mechanism.**

### Rules

1. **New event notifications** must use KernelDispatch:
   - `register_intercept(op)` — when you need sync veto/validation
   - `register_observe(op)` — when you need async fire-and-forget

2. **Simple callback lists** are acceptable for brick-internal
   observer patterns that don't need kernel-level dispatch (e.g.,
   artifact indexing uses `list[ArtifactCallback]` on TaskManager).

3. **Do not rebuild a hook engine.** If a use case seems to need
   scoped/per-agent filtering, add scope awareness to the
   `register_observe()` callback instead.

### Migration examples

**Before (hook engine)**:
```python
# 1. Define handler
async def on_artifact_create(context: HookContext) -> HookResult:
    artifact = context.payload["artifact"]
    await index(artifact)
    return HookResult(proceed=True)

# 2. Register via ceremony
hook_engine.register_hook(
    HookType.ARTIFACT_CREATE,
    on_artifact_create,
    priority=10,
)

# 3. Fire from business logic
result = await hook_engine.fire(
    HookType.ARTIFACT_CREATE,
    HookContext(payload={"artifact": a, "task_id": t}),
)
if not result.proceed:
    raise PermissionError("Vetoed")
```

**After (simple callback)**:
```python
# 1. Define callback type
ArtifactCallback = Callable[[Any, str, str], Awaitable[None]]

# 2. Pass callbacks at construction
manager = SomeManager(observers=[on_index])

# 3. Call directly
for observer in self._observers:
    await observer(artifact, task_id, zone_id)
```

**After (KernelDispatch, for kernel-level events)**:
```python
# Register an observer for write operations
dispatch.register_observe("write", on_write_callback)

# Register an interceptor that can veto
dispatch.register_intercept("delete", permission_check)
```

### What about brick lifecycle events?

BrickLifecycleManager previously fired 6 hook phases (PRE_MOUNT,
POST_MOUNT, etc.) but no code ever registered handlers for them.
They were pure dead fires.

If future brick lifecycle notifications are needed:
- Use `dispatch.register_observe("brick.mounted")` for async
  notification
- Use `dispatch.register_intercept("brick.mounting")` if veto
  capability is required
- Do **not** rebuild a hook engine for this

## Consequences

### Positive

- **-6,300 lines** deleted (6 impl files, 10 test files, 1 protocol)
- **Single notification model**: developers only learn KernelDispatch
- **No dead code**: every remaining notification path has active consumers
- **Simpler factory bootstrap**: removed 3-layer hook engine construction

### Negative

- **Plugin extensibility deferred**: if third-party plugins need hook
  points, we'll need to design a KernelDispatch-based plugin API
- **Existing plugin authors** (if any) would need to migrate — but
  audit confirmed zero external registrations exist

## References

- Issue #907: Delete ScopedHookEngine three-layer onion
- Issue #900: Unify kernel notification mechanisms (KernelDispatch)
- PR #2544: Implementation
- `rust/nexus_runtime/src/dispatch.rs`: Rust Kernel dispatch (HookRegistry, ObserverRegistry, PathTrie)
- `src/nexus/core/nexus_fs_dispatch.py`: DispatchMixin (Python-side registration API)
- `docs/rfcs/adr-nexus-fs-method-freeze.md`: Related NexusFS method freeze decision
