# Service Wiring Evolution Plan (Issue #1410)

> Originally "`__getattr__` Retirement Plan". Renamed because `__getattr__` was
> already removed. The real goal is evolving from **setattr-based God object**
> to **Linux kernel-inspired ServiceRegistry with LKM lifecycle**.

## Status Summary (2026-03-09)

| Milestone | Status |
|-----------|--------|
| `__getattr__` removed from NexusFS | **Done** |
| `SERVICE_METHODS` / `SERVICE_ALIASES` / `resolve_service_attr()` deleted | **Done** (zero callers, deleted as dead code) |
| SearchService callers migrated to `search_service.glob()` | ~75% done |
| ServiceRegistry with LKM lifecycle (Issue #1452) | Not started |
| EXPORT_SYMBOL pattern (Issue #1455) | Not started |
| `bind_wired_services()` retired | Not started |

---

## Problem: The God Object Remains

`__getattr__` is gone, but the God object pattern persists through a different
mechanism: **`bind_wired_services()` stuffs 21 service references onto NexusFS
via `setattr()`**.

```
Factory → WiredServices dataclass → bind_wired_services() → setattr(nx, "search_service", svc)
                                                           → setattr(nx, "rebac_service", svc)
                                                           → setattr(nx, "mcp_service", svc)
                                                           → ... (21 services total)

Caller → nx.search_service.glob(pattern)   # still going through the kernel object
```

NexusFS still carries 21+ service attributes, 15+ system service attributes, and
6+ brick service attributes. Callers reach services **through the kernel**, not
from the factory directly. This violates kernel purity.

---

## Moving Direction: Linux Kernel Service Model

### Linux Kernel Analogy

| Linux | Nexus Current | Nexus Target |
|-------|--------------|--------------|
| `vmlinuz` core (VFS, scheduler, MM) | NexusFS `sys_*` syscalls + KernelDispatch | Same (no change) |
| Compiled-in drivers (`=y`) | MetastoreABC, ObjectStoreABC (injected at init) | Same (no change) |
| `insmod` / `rmmod` | `bind_wired_services()` + setattr | **ServiceRegistry** with LKM lifecycle |
| `EXPORT_SYMBOL()` | N/A (callers reach through kernel) | Service exposes symbols; callers import directly |
| `/proc/modules` | N/A | `ServiceRegistry.list()` |
| `request_module()` | N/A | On-demand service resolution |
| Module `init()` / `exit()` | Implicit (constructor / GC) | Explicit lifecycle protocol |
| Module dependency (`MODULE_DEPENDS`) | Implicit (boot ordering) | Explicit dependency graph |

### Target Architecture

```
                        ┌─────────────────────────┐
                        │    ServiceRegistry       │
                        │  (kernel symbol table)   │
                        ├─────────────────────────┤
                        │  register(name, svc)     │
                        │  resolve(name) -> svc    │
                        │  unregister(name)        │
                        │  list() -> [ServiceInfo] │
                        └──────────┬──────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
         ┌────▼────┐        ┌─────▼─────┐        ┌─────▼─────┐
         │ Search  │        │  ReBAC    │        │  Events   │
         │ Service │        │  Service  │        │  Service  │
         └────┬────┘        └─────┬─────┘        └─────┬─────┘
              │                   │                     │
    Callers import directly:      │                     │
    search = registry.resolve("search")                 │
    search.glob(pattern)                                │
```

**Key properties:**

1. **Kernel knows nothing about services** — NexusFS has zero service attributes
2. **Factory registers, callers resolve** — no God object intermediary
3. **LKM lifecycle** — `init() → start() → stop() → cleanup()` with dependency ordering
4. **Hot-swap capable** — replace a service implementation at runtime (Phase 2)
5. **Reference counting** — prevent unload while callers hold references
6. **Hook auto-management** — VFS hooks registered at `init()`, unregistered at `cleanup()`

---

## What Exists Today

### Already Implemented

1. **KernelDispatch** (`core/kernel_dispatch.py`) — three-phase VFS dispatch
   - PRE-DISPATCH (path resolvers) ≈ Linux VFS `f_op` dispatch
   - INTERCEPT (ordered hooks) ≈ Linux LSM `call_void_hook()` chain
   - OBSERVE (fire-and-forget) ≈ Linux `fsnotify()` / `notifier_call_chain()`
   - This is the kernel's own hook system — services plug into it, not replace it

2. **Brick auto-discovery** (`factory/_bricks.py`) — convention-based plugin loading
   - Scans `nexus/bricks/*/brick_factory.py` for `BrickFactoryDescriptor`
   - Profile gating via `brick_on` callback per `DeploymentProfile`
   - Two tiers: "independent" (no deps) and "dependent" (needs other bricks)

3. **Three-phase lifecycle** (`factory/_lifecycle.py`)
   - `link()` — pure memory wiring, no I/O
   - `initialize()` — one-time side effects (hook registration, BLM)
   - `bootstrap()` — background threads, deferred I/O

4. **WiredServices frozen dataclass** (`core/config.py`) — typed DI container
   - 21 optional service fields, all defaulting to None
   - Created by `_boot_wired_services()`, consumed by `bind_wired_services()`

### Dead Code Removed (2026-03-09)

- `SERVICE_METHODS` dict (66 entries) — was never consulted by any runtime code
- `SERVICE_ALIASES` dict (63 entries) — was never consulted by any runtime code
- `resolve_service_attr()` function — had zero callers across entire codebase

---

## Evolution Phases

### Phase 1: Caller Migration (Current Focus)

Migrate callers from `nx.service.method()` to directly holding service references.
The service reference comes from the factory, not from the kernel.

**Pattern:**
```python
# Before (God object — reaches through kernel)
nx = get_nexus_fs()
results = nx.search_service.glob(pattern)

# After (direct injection — factory gives service to caller)
search = get_search_service()  # from DI context, not from nx
results = search.glob(pattern)
```

**SearchService migration (~75% done):**

| File | Status |
|------|--------|
| `server/rpc/handlers/filesystem.py` | Migrated |
| `server/cache_warmer.py` | Migrated |
| `cli/commands/search.py` | Migrated |
| `bricks/llm/llm_document_reader.py` | Migrated |
| `system_services/agent_runtime/tool_dispatcher.py` | Migrated |
| `bricks/mcp/server.py` | Partial (fallback pattern) |
| `bricks/tools/langgraph/nexus_tools.py` | Not migrated |

**Other services:** Not started. Priority order:
1. Low-caller services (MCPService, LLMService, OAuthService — 1-2 files each)
2. EventsService + MountServices (moderate blast radius)
3. ReBACService (35+ methods, 60+ files, ~400+ call sites — largest)

### Phase 2: ServiceRegistry (Issue #1452)

Implement the kernel-side registry that replaces `bind_wired_services()`:

```python
class ServiceRegistry:
    """Kernel symbol table for services (LKM pattern)."""

    def register(self, name: str, service: Any, *,
                 deps: list[str] | None = None,
                 hooks: list[VFSHook] | None = None) -> None:
        """Register a service (insmod).

        - Validates dependency graph (all deps must be registered first)
        - Auto-registers VFS hooks if provided
        - Increments reference count
        """

    def resolve(self, name: str) -> Any:
        """Resolve a service by name (EXPORT_SYMBOL lookup)."""

    def unregister(self, name: str) -> None:
        """Unregister a service (rmmod).

        - Checks reference count (refuse if > 0)
        - Auto-unregisters VFS hooks
        - Runs service cleanup() if defined
        """

    def list(self) -> list[ServiceInfo]:
        """List registered services (/proc/modules)."""
```

ServiceRegistry lives on NexusFS as the single kernel-provided infrastructure
(like Linux's module subsystem), replacing the 21 individual service attributes.

### Phase 3: EXPORT_SYMBOL Pattern (Issue #1455)

Services declare their exported API; callers resolve by symbol name:

```python
# Service side (in brick_factory.py)
EXPORTS = ["glob", "grep", "glob_batch", "semantic_search"]

# Caller side
search = nx.registry.resolve("search")
search.glob(pattern)

# Or via convenience
glob = nx.registry.symbol("search.glob")
glob(pattern)
```

### Phase 4: Retire `bind_wired_services()`

Once ServiceRegistry is in place and all callers use `registry.resolve()`:

1. Delete `bind_wired_services()` from `factory/service_routing.py`
2. Delete `WiredServices` dataclass (registry replaces it)
3. Remove all `self.*_service = None` declarations from NexusFS
4. Factory calls `registry.register()` instead of `bind_wired_services()`
5. `factory/service_routing.py` either deleted or contains only ServiceRegistry helpers

### Phase 5: Hot-Swap (Future)

With ServiceRegistry + lifecycle protocol:

```python
# Runtime service replacement (like rmmod + insmod)
nx.registry.unregister("search")  # calls cleanup(), unregisters hooks
nx.registry.register("search", BetterSearchService(), hooks=[...])  # calls init()
```

Enables: A/B testing, graceful degradation, plugin marketplace.

---

## Scope Analysis (Reference)

| Service | Methods | Files | Call Sites | Priority |
|---------|---------|-------|------------|----------|
| ReBACService | 35+ | 60+ | ~400+ | P3 (huge, defer) |
| MountServices | 10 | 9 | ~70 | P2 |
| SearchService | 6 | 17 | ~30 | **P1** |
| EventsService | 4 | 7 | ~30 | P2 |
| WorkspaceRPCService | 17 | 2 | ~20 | P2 |
| SandboxRPCService | 11 | 2 | ~15 | P2 |
| OAuthService | 6 | 1 | ~6 | P2 |
| AgentRPCService | 5 | 2 | ~7 | P2 |
| SyncService | 5 | 2 | ~7 | P2 |
| UserProvisioningService | 2 | 4 | ~5 | P2 |
| VersionService | 4 | 0 prod | tests only | P3 |
| MetadataExportService | 2 | 0 prod | tests only | P3 |
| MCPService | 1 | 1 | 1 | P2 |
| LLMService | 1 | 1 | 1 | P2 |

**Total: ~110 methods, ~90 files, ~600+ call sites**

---

## Related Issues

| Issue | Title | Status |
|-------|-------|--------|
| #1410 | Service wiring evolution (this plan) | In progress |
| #1443 | Delete `_service_extras()` legacy routing | Pending |
| #1452 | Implement ServiceRegistry with LKM lifecycle | Pending |
| #1453 | Close 'Standard Plug' gaps — declarative brick hooks | Pending |
| #1454 | Replace static SERVICE_METHODS with declarative registration | **Done** (tables deleted) |
| #1455 | Eliminate Tier 2b wired services via EXPORT_SYMBOL pattern | Pending |

## Non-Goals

- Changing kernel `sys_*` syscalls (those are kernel-native, not services)
- Removing NexusFS (it's the kernel — VFS layer stays)
- Changing the RPC wire protocol (callers change, server API stays)
- Removing KernelDispatch (it's the kernel's own hook system, orthogonal to services)

## Success Criteria (Updated)

1. ~~`SERVICE_METHODS` dict is empty~~ **Done** — deleted
2. ~~`SERVICE_ALIASES` dict is empty~~ **Done** — deleted
3. ~~`resolve_service_attr()` deleted~~ **Done** — deleted
4. `ServiceRegistry` implemented and used by factory
5. `bind_wired_services()` deleted
6. NexusFS has zero service attributes (only `registry: ServiceRegistry`)
7. All callers use `registry.resolve()` or direct DI
8. All tests pass
