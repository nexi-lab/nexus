# `__getattr__` Retirement Plan (Issue #1410)

## Problem

NexusFS kernel proxies **110+ service methods** via `__getattr__` + `SERVICE_METHODS`/`SERVICE_ALIASES` routing tables in `factory/service_routing.py`. Callers do `nx.glob()`, `nx.rebac_check()`, etc. — treating the kernel as a God object facade for every service.

This violates kernel purity: the kernel should only expose `sys_*` syscalls. Service methods should be accessed via direct service references.

## Current Architecture (Bad)

```
Caller → nx.glob() → NexusFS.__getattr__ → SERVICE_METHODS["glob"] → search_service.glob()
```

- NexusFS acts as a transparent proxy for 110+ service methods
- Callers don't know (or care) which service they're actually calling
- Factory stuffs 20+ service references onto the kernel object via `bind_wired_services()`
- `__getattr__` is a compatibility shim that hides the God object anti-pattern

## Target Architecture (Good)

```
Caller → search_service.glob()
```

- Callers hold direct service references, injected by factory
- NexusFS exposes only `sys_*` syscalls (Tier 1) and convenience methods (Tier 2)
- `SERVICE_METHODS`/`SERVICE_ALIASES` tables are empty and deleted
- `__getattr__` only raises `AttributeError`
- `bind_wired_services()` deleted — factory gives services to callers, not to kernel

## Scope Analysis

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

## Migration Strategy

### Phase 1: SearchService (P1 — smallest blast radius, proves the pattern)

**Why first**: Only 6 methods, 17 files. `glob`/`grep` are already routed via `SERVICE_METHODS` (not on ABC). Clean migration target.

**Pattern**: Each caller that does `nx.glob(...)` gets `search_service` injected instead.

Example — `bricks/mcp/server.py`:
```python
# Before
nx_instance = _get_nexus_instance(ctx)
all_matches = nx_instance.glob(pattern, path)

# After
search_service = _get_search_service(ctx)
all_matches = search_service.glob(pattern, path)
```

**Files to migrate**:
- `server/rpc/handlers/filesystem.py` — handle_glob, handle_grep, handle_semantic_search_index
- `server/cache_warmer.py` — glob
- `cli/commands/search.py` — glob, grep
- `cli/commands/file_ops.py` — glob
- `bricks/llm/llm_document_reader.py` — glob
- `bricks/mcp/server.py` — glob, grep
- `bricks/context_manifest/executors/file_glob.py` — glob
- `bricks/workflows/loader.py` — glob
- `bricks/tools/langgraph/nexus_tools.py` — grep, glob
- `bricks/filesystem/scoped_filesystem.py` — glob, grep
- `sdk/__init__.py` — glob

**After Phase 1**: Remove `glob`, `grep`, `glob_batch`, `asemantic_search*` from `SERVICE_METHODS`/`SERVICE_ALIASES`. ~6 fewer methods routed.

### Phase 2: Low-caller services (batch)

Migrate services with 1-2 caller files each:
- MCPService (1 file)
- LLMService (1 file)
- OAuthService (1 file)
- AgentRPCService (2 files)
- SyncService (2 files)
- UserProvisioningService (4 files)
- SandboxRPCService (2 files)
- WorkspaceRPCService (2 files)

**Pattern**: Factory creates service, passes reference to caller. Caller calls service directly.

**After Phase 2**: ~55 fewer methods routed. Routing tables shrink by ~50%.

### Phase 3: EventsService + MountServices

- EventsService: 4 methods, 7 files
- MountCoreService + MountPersistService: 10 methods, 9 files

These have moderate blast radius. MountService has bidirectional coupling with NexusFS that needs careful decoupling.

**After Phase 3**: ~70 fewer methods. Only ReBACService remains.

### Phase 4: ReBACService (largest, last)

35+ methods across 60+ files with ~400+ call sites. This is the hardest migration because:
- Deep coupling between rebac brick and NexusFS
- Many sync/async method pairs
- Used in kernel-adjacent code (permission enforcement)

Approach: Migrate incrementally by sub-group:
1. Namespace methods (5 methods)
2. Share/consent methods (9 methods)
3. Core CRUD methods (7 sync + 7 async)
4. Query/expand methods (7 methods)

**After Phase 4**: All routing tables empty. Delete `SERVICE_METHODS`, `SERVICE_ALIASES`, `resolve_service_attr()`, simplify `__getattr__` to just `raise AttributeError`.

### Phase 5: Cleanup

- Delete `bind_wired_services()` — no more service attrs on NexusFS
- Delete `factory/service_routing.py` entirely
- Remove all service instance attributes from NexusFS (`search_service`, `rebac_service`, etc.)
- `__getattr__` becomes a simple `raise AttributeError`

## DI Pattern for Callers

Factory already creates all services. The change is: **who receives them**.

**Current**: Factory → `bind_wired_services(nx, services)` → NexusFS holds all → callers use `nx.method()`

**Target**: Factory → gives each caller its needed services directly

For CLI commands:
```python
# Factory creates CLI context with needed services
ctx.search_service = search_service
ctx.rebac_service = rebac_service
```

For RPC handlers:
```python
# Handler receives service via dependency injection
def handle_glob(request, search_service: SearchService):
    return search_service.glob(request.pattern)
```

For bricks:
```python
# Brick constructor takes service dependency
class LLMDocumentReader:
    def __init__(self, nx: NexusFilesystemABC, search_service: SearchService):
        self.nx = nx
        self.search = search_service
```

## Non-Goals

- Changing how NexusFS `sys_*` methods work (those are kernel methods, not routed)
- Removing NexusFS entirely (it's the kernel — VFS layer stays)
- Changing the RPC wire protocol (callers change, but RPC handlers still expose same API)

## Success Criteria

1. `SERVICE_METHODS` dict is empty
2. `SERVICE_ALIASES` dict is empty
3. `factory/service_routing.py` deleted
4. NexusFS has zero service attributes (`search_service`, `rebac_service`, etc.)
5. `__getattr__` only raises `AttributeError`
6. All callers use direct service references
7. All tests pass
