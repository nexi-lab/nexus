# Proposal: Enforce kernel minimality in core/ — move ~25 non-kernel files out

## Problem

The architecture doc ([KERNEL-ARCHITECTURE.md](../design/KERNEL-ARCHITECTURE.md)) defines the kernel as **"the minimal compilable unit with zero services loaded"** — following Linux-inspired layering: Services → Kernel → Drivers.

In practice, `src/nexus/core/` currently has **65+ files**, roughly 25 of which are service-layer, infrastructure, or utility concerns that violate the kernel boundary. This makes it hard to reason about what the kernel actually is, and impossible to enforce the dependency invariant (services depend on core, never the reverse).

## Proposed Reorganization

Keep the name `core/` (it's a Python convention and already in every import path), but **slim it down** to ~15 kernel-essential files.

### What stays in `core/` (the true kernel)

| File | Role |
|------|------|
| `nexus_fs.py` | Syscall dispatch (main kernel class) |
| `nexus_fs_core.py` | Core file operation mixin |
| `metastore.py` | MetastoreABC |
| `object_store.py` | ObjectStoreABC |
| `cache_store.py` | CacheStoreABC |
| `config.py` | Frozen config dataclasses |
| `metadata.py` | FileMetadata |
| `permissions.py` | OperationContext |
| `router.py` | PathRouter (VFS impl) |
| `read_set.py` | ReadSet |
| `types.py` | Core types |
| `exceptions.py` | Core exceptions |
| `operation_types.py` | Operation types |
| `path_utils.py` | Path utilities |
| `zone_helpers.py` | Zone helpers |
| `protocols/` | Kernel protocols (vfs_router, vfs_core, etc.) |

### What moves out — Phase 1: Service-layer files → `services/`

| File | Reason |
|------|--------|
| `agents.py` | Agent infra = service |
| `rebac.py` | ReBAC = service protocol |
| `edit_engine.py` | Content editing = service logic |
| `export_import.py` | Not kernel-minimal |
| `reactive_subscriptions.py` | Notification = service |
| `virtual_views.py` | Views = service |
| `coref_resolver.py` | NLP = service |
| `temporal.py`, `temporal_resolver.py` | Time-travel = service |
| `brick_container.py` | Brick lifecycle = Tier 1.5 service |
| `workspace_manifest.py` | Workspace = service |
| `revision_notifier.py` | Notifications = service |
| `filters.py` | Filtering = service logic |
| `scoped_filesystem.py`, `async_scoped_filesystem.py` | Scoped FS = service wrapper |

### What moves out — Phase 2: Consolidate drivers under `drivers/`

Rename `backends/` → `drivers/objectstore/`, and group all driver implementations by pillar:

```
src/nexus/drivers/
├── metastore/       # raft_metadata_store, etc. (from storage/)
├── objectstore/     # current backends/ contents
├── recordstore/     # RecordStoreABC + SQLAlchemy impl (from storage/)
└── cachestore/      # Dragonfly, InMemory, Null
```

This matches the four-pillar model from the architecture doc exactly.

### What moves out — Phase 3: Infrastructure → `infra/`

| File | Reason |
|------|--------|
| `event_bus.py`, `event_bus_nats.py` | User-space messaging tier |
| `distributed_lock.py` | Coordination = infra |
| `rpc_codec.py`, `rpc_decorator.py`, `rpc_transport.py`, `rpc_types.py` | Transport layer |
| `heartbeat_buffer.py` | Heartbeat = infra |
| `resiliency.py` | Infrastructure |
| `adaptive_ttl.py` | Infrastructure |
| `cache_invalidation.py` | Cache management = infra |
| `io_profile.py`, `deployment_profile.py` | Profiling/config = infra |
| `glob_fast.py`, `grep_fast.py`, `trigram_fast.py`, `hash_fast.py` | Performance utilities |

## Benefits

1. **Kernel minimality enforced structurally** — if it's in `core/`, it's kernel. ~15 files, not 65.
2. **Driver grouping by pillar** — matches the four-pillar model, replaces the confusing `backends/` + `storage/` split.
3. **Clean infra layer** — RPC, event bus, locks, resilience get their own home.
4. **Dependency invariant becomes enforceable** — services depend on `core/`, never the reverse. Can be checked with import linting.

## Migration Strategy

- **Phased approach** (Phase 1 → 2 → 3) to avoid a big-bang refactor.
- Each phase is independently valuable and merge-able.
- No backward-compatibility hacks — obsoleted re-exports are deleted once all internal callers are updated.

## Open Questions

- Any files in the "stays in core" list that should also move out?
- Any files in the "moves out" lists that should actually stay?
- Should `pagination.py`, `response.py`, `context_utils.py` stay in core or move to a `utils/` package?
- Should `registry.py` stay in core (it's used for service discovery, arguably infra)?
