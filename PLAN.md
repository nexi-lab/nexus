# Mount Mixin Refactoring Plan

## Overview

Refactor `NexusFSMountsMixin` (2,065 lines) into AI-friendly, testable services using the Gateway pattern.

### Design Principles

1. **Gateway Pattern**: Single `NexusFSGateway` for all NexusFS access (`self._gw.`)
2. **Sync Services**: All services are synchronous - FastAPI auto-wraps with `to_thread`
3. **Thin Mixin**: `NexusFSMountsMixin` becomes a thin RPC facade (~100 lines)
4. **No Circular Dependencies**: Services depend on Gateway, not NexusFS directly

### Target Architecture

```
NexusFSMountsMixin (thin facade, ~100 lines)
    │
    ├── MountService (mount CRUD, ~300 lines)
    ├── SyncService (metadata sync, ~500 lines)
    └── MountPersistService (DB persistence, ~150 lines)
            │
            ▼
    NexusFSGateway (AI-friendly interface, ~100 lines)
            │
            ▼
        NexusFS
```

---

## Phase 1: Create NexusFSGateway

**File**: `nexus/services/gateway.py` (NEW)

### Tasks

1.1. Create `NexusFSGateway` class with explicit method delegation:
   - File ops: `mkdir()`, `write()`
   - Metadata ops: `metadata_get()`, `metadata_put()`, `metadata_list()`, `metadata_delete()`
   - Permission ops: `rebac_create()`, `rebac_check()`, `rebac_delete_object_tuples()`
   - Hierarchy ops: `ensure_parent_tuples_batch()`, `hierarchy_enabled` property
   - Router access: `router` property
   - Session factory: `session_factory` property

1.2. Add docstring documenting all exposed operations for AI discoverability

### Code Structure

```python
# nexus/services/gateway.py

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

class NexusFSGateway:
    """Gateway providing NexusFS operations to services.

    AI-Friendly Design:
    - Single object to grep: self._fs.
    - Explicit method delegation
    - No protocol hunting required

    Dependencies exposed:
    - File ops: mkdir(), write()
    - Metadata: metadata_get/put/list/delete
    - Permissions: rebac_create/check/delete_object_tuples
    - Hierarchy: ensure_parent_tuples_batch, hierarchy_enabled
    - Router: router property
    - Session: session_factory property
    """

    def __init__(self, fs: "NexusFS"):
        self._fs = fs

    # File Operations
    def mkdir(self, path, *, parents=False, exist_ok=False, context=None): ...
    def write(self, path, content, *, context=None): ...

    # Metadata Operations
    def metadata_get(self, path): ...
    def metadata_put(self, meta): ...
    def metadata_list(self, prefix, recursive=False): ...
    def metadata_delete(self, path): ...

    # Permission Operations
    def rebac_create(self, subject, relation, object, tenant_id=None): ...
    def rebac_check(self, subject, permission, object, tenant_id=None): ...
    def rebac_delete_object_tuples(self, object, tenant_id=None): ...

    # Hierarchy Operations
    @property
    def hierarchy_enabled(self) -> bool: ...
    def ensure_parent_tuples_batch(self, paths, tenant_id=None): ...

    # Router Access
    @property
    def router(self): ...

    # Session Factory
    @property
    def session_factory(self): ...
```

---

## Phase 2: Refactor MountService

**File**: `nexus/services/mount_service.py` (REFACTOR)

### Tasks

2.1. Remove async wrappers - make all methods sync
2.2. Replace `self.nexus_fs` with `self._gw` (NexusFSGateway)
2.3. Extract backend factory into `_create_backend()` method
2.4. Extract setup logic into `_setup_mount_point()` method
2.5. Remove sync_mount delegation (will be handled by SyncService)

### Methods (all sync)

| Method | Description |
|--------|-------------|
| `add_mount()` | Create backend, add to router, setup permissions |
| `remove_mount()` | Remove from router, cleanup permissions |
| `list_mounts()` | List with permission filtering |
| `get_mount()` | Get single mount details |
| `has_mount()` | Check existence |
| `list_connectors()` | List available connector types |
| `_create_backend()` | Factory for backend instantiation |
| `_setup_mount_point()` | Create dir, grant permissions, generate skill |
| `_grant_owner_permission()` | Grant direct_owner to creator |
| `_generate_skill()` | Generate SKILL.md for connector |

### Code Structure

```python
# nexus/services/mount_service.py

class MountService:
    """Core mount management operations (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.
    """

    def __init__(self, gateway: NexusFSGateway):
        self._gw = gateway  # AI-friendly: grep self._gw.

    def add_mount(self, mount_point, backend_type, backend_config,
                  priority=0, readonly=False, context=None) -> str:
        backend = self._create_backend(backend_type, backend_config)
        self._gw.router.add_mount(mount_point, backend, priority, readonly)
        self._setup_mount_point(mount_point, backend_type, context)
        return mount_point

    def remove_mount(self, mount_point, context=None) -> dict:
        # Sync implementation
        ...

    def list_mounts(self, context=None) -> list[dict]:
        # Permission-filtered listing
        ...

    # ... other methods
```

---

## Phase 3: Create SyncService

**File**: `nexus/services/sync_service.py` (NEW)

### Tasks

3.1. Create `SyncContext` dataclass for all sync parameters
3.2. Create `SyncResult` dataclass for sync results
3.3. Implement `sync_mount()` with clear step-by-step flow
3.4. Implement `_sync_metadata()` with BFS traversal (from current `_sync_mount_metadata`)
3.5. Implement `_sync_deletions()` (from current `_sync_mount_deletions`)
3.6. Implement `_sync_content()` (from current `_sync_mount_content_cache`)
3.7. Implement `_sync_all_mounts()` for mount_point=None case
3.8. Implement pattern matching helper `_matches_patterns()`

### Code Structure

```python
# nexus/services/sync_service.py

from collections import deque
from dataclasses import dataclass, field

@dataclass
class SyncContext:
    """All parameters for a sync operation."""
    mount_point: str
    path: str | None = None
    recursive: bool = True
    dry_run: bool = False
    sync_content: bool = True
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    generate_embeddings: bool = False
    context: OperationContext | None = None
    progress_callback: ProgressCallback | None = None

@dataclass
class SyncResult:
    """Result of a sync operation."""
    files_scanned: int = 0
    files_created: int = 0
    files_updated: int = 0
    files_deleted: int = 0
    cache_synced: int = 0
    cache_bytes: int = 0
    embeddings_generated: int = 0
    errors: list[str] = field(default_factory=list)

class SyncService:
    """Handles metadata and content synchronization (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.
    """

    def __init__(self, gateway: NexusFSGateway):
        self._gw = gateway

    def sync_mount(self, ctx: SyncContext) -> SyncResult:
        """Main sync entry point."""
        if ctx.mount_point is None:
            return self._sync_all_mounts(ctx)

        result = SyncResult()
        backend = self._validate_mount(ctx.mount_point)
        files_found = self._sync_metadata(ctx, backend, result)
        self._sync_deletions(ctx, files_found, result)
        self._sync_content(ctx, backend, result)
        return result

    def _sync_metadata(self, ctx, backend, result) -> set[str]:
        """BFS traversal of backend, updating metadata."""
        # Extracted from _sync_mount_metadata
        ...

    def _sync_deletions(self, ctx, files_found, result):
        """Remove files no longer in backend."""
        # Extracted from _sync_mount_deletions
        ...

    def _sync_content(self, ctx, backend, result):
        """Sync content to cache."""
        # Extracted from _sync_mount_content_cache
        ...
```

---

## Phase 4: Create SyncJobService

**File**: `nexus/services/sync_job_service.py` (NEW)

### Tasks

4.1. Create `SyncJobService` class for async job management
4.2. Implement `create_job()` - create job record
4.3. Implement `start_job()` - execute in background thread
4.4. Implement `get_job()`, `cancel_job()`, `list_jobs()` - job queries
4.5. Implement progress callback with cancellation check

### Code Structure

```python
# nexus/services/sync_job_service.py

import threading

class SyncJobService:
    """Manages async sync jobs (background execution)."""

    def __init__(self, gateway: NexusFSGateway, sync_service: SyncService):
        self._gw = gateway
        self._sync = sync_service
        self._manager = SyncJobManager(gateway.session_factory)

    def create_job(self, mount_point, params, user_id=None) -> str:
        """Create job record, return job_id."""
        return self._manager.create_job(mount_point, params, user_id)

    def start_job(self, job_id: str) -> None:
        """Start job in background thread."""
        def execute():
            try:
                self._manager.mark_running(job_id)
                job = self._manager.get_job(job_id)
                ctx = SyncContext(
                    mount_point=job["mount_point"],
                    progress_callback=lambda n, p: self._update_progress(job_id, n, p),
                    **job["params"],
                )
                result = self._sync.sync_mount(ctx)
                self._manager.complete_job(job_id, asdict(result))
            except SyncCancelled:
                self._manager.mark_cancelled(job_id)
            except Exception as e:
                self._manager.fail_job(job_id, str(e))

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()

    def get_job(self, job_id) -> dict | None: ...
    def cancel_job(self, job_id) -> bool: ...
    def list_jobs(self, mount_point=None, status=None, limit=50) -> list[dict]: ...
```

---

## Phase 5: Create MountPersistService

**File**: `nexus/services/mount_persist_service.py` (NEW)

### Tasks

5.1. Create `MountPersistService` class
5.2. Implement `save_mount()` - persist to database
5.3. Implement `load_mount()` - load and activate via MountService
5.4. Implement `load_all_mounts()` - load all saved mounts
5.5. Implement `list_saved_mounts()` - query saved configs
5.6. Implement `delete_saved_mount()` - remove from database

### Code Structure

```python
# nexus/services/mount_persist_service.py

class MountPersistService:
    """Handles mount configuration persistence (SYNC)."""

    def __init__(self, mount_manager: MountManager, mount_service: MountService):
        self._manager = mount_manager
        self._mounts = mount_service

    def save_mount(self, mount_point, backend_type, backend_config, **kwargs) -> str:
        """Persist mount config to database."""
        return self._manager.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            **kwargs,
        )

    def load_mount(self, mount_point, context=None) -> str:
        """Load saved config and activate mount."""
        config = self._manager.get_mount(mount_point)
        if not config:
            raise ValueError(f"Mount not found: {mount_point}")
        return self._mounts.add_mount(
            mount_point=config["mount_point"],
            backend_type=config["backend_type"],
            backend_config=config["backend_config"],
            priority=config["priority"],
            readonly=config["readonly"],
            context=context,
        )

    def load_all_mounts(self, auto_sync=False) -> dict:
        """Load all saved mounts on startup."""
        ...

    def list_saved_mounts(self, owner_user_id=None, tenant_id=None, context=None) -> list[dict]: ...
    def delete_saved_mount(self, mount_point) -> bool: ...
```

---

## Phase 6: Refactor NexusFSMountsMixin

**File**: `nexus/core/nexus_fs_mounts.py` (REFACTOR)

### Tasks

6.1. Remove all business logic (moved to services)
6.2. Add `cached_property` for service instantiation
6.3. Keep only RPC-decorated thin delegation methods
6.4. Keep `SyncMountContext` and `ProgressCallback` type definitions (for backward compat)

### Code Structure

```python
# nexus/core/nexus_fs_mounts.py (~100 lines)

from dataclasses import asdict
from functools import cached_property

from nexus.core.rpc_decorator import rpc_expose
from nexus.services.gateway import NexusFSGateway
from nexus.services.mount_service import MountService
from nexus.services.mount_persist_service import MountPersistService
from nexus.services.sync_service import SyncContext, SyncService
from nexus.services.sync_job_service import SyncJobService

# Keep for backward compatibility
ProgressCallback = Callable[[int, str], None]

class NexusFSMountsMixin:
    """Thin facade exposing mount operations via RPC.

    All logic delegated to services:
    - MountService: add/remove/list mounts
    - SyncService: sync operations
    - MountPersistService: persistence
    - SyncJobService: async job management
    """

    @cached_property
    def _gateway(self) -> NexusFSGateway:
        return NexusFSGateway(self)

    @cached_property
    def _mount_service(self) -> MountService:
        return MountService(self._gateway)

    @cached_property
    def _sync_service(self) -> SyncService:
        return SyncService(self._gateway)

    @cached_property
    def _persist_service(self) -> MountPersistService:
        return MountPersistService(self.mount_manager, self._mount_service)

    @cached_property
    def _sync_job_service(self) -> SyncJobService:
        return SyncJobService(self._gateway, self._sync_service)

    # =========================================================================
    # RPC Methods - Thin Delegation
    # =========================================================================

    @rpc_expose(description="Add dynamic backend mount")
    def add_mount(self, mount_point, backend_type, backend_config,
                  priority=0, readonly=False, context=None) -> str:
        return self._mount_service.add_mount(
            mount_point, backend_type, backend_config, priority, readonly, context
        )

    @rpc_expose(description="Remove backend mount")
    def remove_mount(self, mount_point, context=None) -> dict:
        return self._mount_service.remove_mount(mount_point, context)

    @rpc_expose(description="List all backend mounts")
    def list_mounts(self, context=None) -> list[dict]:
        return self._mount_service.list_mounts(context)

    @rpc_expose(description="Get mount details")
    def get_mount(self, mount_point) -> dict | None:
        return self._mount_service.get_mount(mount_point)

    @rpc_expose(description="Check if mount exists")
    def has_mount(self, mount_point) -> bool:
        return self._mount_service.has_mount(mount_point)

    @rpc_expose(description="List available connector types")
    def list_connectors(self, category=None) -> list[dict]:
        return self._mount_service.list_connectors(category)

    @rpc_expose(description="Sync metadata from connector backend")
    def sync_mount(self, mount_point=None, path=None, recursive=True,
                   dry_run=False, sync_content=True, include_patterns=None,
                   exclude_patterns=None, generate_embeddings=False,
                   context=None, progress_callback=None) -> dict:
        ctx = SyncContext(
            mount_point=mount_point, path=path, recursive=recursive,
            dry_run=dry_run, sync_content=sync_content,
            include_patterns=include_patterns, exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings, context=context,
            progress_callback=progress_callback,
        )
        result = self._sync_service.sync_mount(ctx)
        return asdict(result)

    @rpc_expose(description="Start async sync job")
    def sync_mount_async(self, mount_point, **kwargs) -> dict:
        job_id = self._sync_job_service.create_job(mount_point, kwargs)
        self._sync_job_service.start_job(job_id)
        return {"job_id": job_id, "status": "pending", "mount_point": mount_point}

    @rpc_expose(description="Get sync job status")
    def get_sync_job(self, job_id) -> dict | None:
        return self._sync_job_service.get_job(job_id)

    @rpc_expose(description="Cancel sync job")
    def cancel_sync_job(self, job_id) -> dict:
        success = self._sync_job_service.cancel_job(job_id)
        return {"success": success, "job_id": job_id}

    @rpc_expose(description="List sync jobs")
    def list_sync_jobs(self, mount_point=None, status=None, limit=50) -> list[dict]:
        return self._sync_job_service.list_jobs(mount_point, status, limit)

    # Persistence methods
    @rpc_expose(description="Save mount configuration")
    def save_mount(self, mount_point, backend_type, backend_config, **kwargs) -> str:
        return self._persist_service.save_mount(mount_point, backend_type, backend_config, **kwargs)

    @rpc_expose(description="List saved mounts")
    def list_saved_mounts(self, owner_user_id=None, tenant_id=None, context=None) -> list[dict]:
        return self._persist_service.list_saved_mounts(owner_user_id, tenant_id, context)

    @rpc_expose(description="Load saved mount")
    def load_mount(self, mount_point) -> str:
        return self._persist_service.load_mount(mount_point)

    @rpc_expose(description="Delete saved mount")
    def delete_saved_mount(self, mount_point) -> bool:
        return self._persist_service.delete_saved_mount(mount_point)

    def load_all_saved_mounts(self, auto_sync=False) -> dict:
        return self._persist_service.load_all_mounts(auto_sync)
```

---

## Phase 7: Update Existing MountService

**File**: `nexus/services/mount_service.py` (DELETE async code)

### Tasks

7.1. Remove all `async def` declarations
7.2. Remove all `asyncio.to_thread()` wrappers
7.3. Remove sync method delegation to NexusFS (move to SyncService)
7.4. Update to use NexusFSGateway instead of `self.nexus_fs`

---

## Phase 8: Testing & Validation

### Tasks

8.1. Create unit tests for `NexusFSGateway`
8.2. Create unit tests for `MountService` with mock gateway
8.3. Create unit tests for `SyncService` with mock gateway
8.4. Create unit tests for `SyncJobService`
8.5. Create unit tests for `MountPersistService`
8.6. Run existing integration tests to verify no regressions
8.7. Verify FastAPI auto-wrapping works correctly

---

## File Summary

| File | Action | Lines (Est.) |
|------|--------|--------------|
| `nexus/services/gateway.py` | NEW | ~100 |
| `nexus/services/mount_service.py` | REFACTOR | ~300 |
| `nexus/services/sync_service.py` | NEW | ~500 |
| `nexus/services/sync_job_service.py` | NEW | ~100 |
| `nexus/services/mount_persist_service.py` | NEW | ~150 |
| `nexus/core/nexus_fs_mounts.py` | REFACTOR | ~100 |
| **Total** | | ~1,250 |

**Original**: 2,065 lines in monolithic mixin
**New**: ~1,250 lines across 6 focused files

---

## Migration Notes

1. **Backward Compatibility**: All RPC method signatures remain unchanged
2. **No API Changes**: External callers see same interface
3. **Incremental**: Each phase can be merged separately
4. **Rollback**: Keep old mixin code commented until validation complete

---

## Dependencies

- Phase 1 (Gateway) must complete first
- Phase 2-5 can proceed in parallel after Phase 1
- Phase 6 (Mixin refactor) depends on all services
- Phase 7 (Cleanup) after Phase 6 validated
- Phase 8 (Testing) throughout

---

## Success Criteria

1. All existing tests pass
2. No async wrappers in service layer
3. Mixin is < 150 lines
4. Each service is independently testable with mock gateway
5. AI agents can grep `self._gw.` to find all NexusFS operations
