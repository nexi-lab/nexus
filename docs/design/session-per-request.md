# Session-Per-Request Implementation Plan

## Overview

Refactor database session management to follow industry best practice: **one session per HTTP request**. This ensures predictable connection pool usage, atomic transactions per request, and consistent reads.

## Problem Statement

Current architecture creates multiple database sessions per request:
- Each method call to `EntityRegistry`, `ReBACManager`, `Memory`, etc. opens its own session
- Nested operations compound this (e.g., `register_entity` opens 2 sessions)
- Under concurrent load, connection pool exhaustion occurs (25-30 sessions per provisioning call)

### Current Session Creation Points

| Component | Session Creations | Pattern |
|-----------|------------------|---------|
| `nexus_fs.py` | 15+ | `self.metadata.SessionLocal()` |
| `nexus_fs_core.py` | 12+ | `self.metadata.SessionLocal()` |
| `entity_registry.py` | 6+ | `self._get_session()` |
| `rebac_manager.py` | Multiple | `self.SessionLocal()` |
| `workspace_manager.py` | 4 | `self.metadata.SessionLocal()` |
| Others | 30+ | Various |

## Solution Design

### Architecture

```
HTTP Request
    │
    ▼
┌─────────────────────────────────────┐
│  FastAPI Middleware                  │
│  - Create session at request start   │
│  - Store in request.state.db_session │
│  - Commit on success / Rollback on error │
│  - Close in finally block            │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  get_db_session() Dependency         │
│  - Extract session from request.state│
│  - Pass to RPC handler               │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  OperationContext                    │
│  - Add _db_session field             │
│  - Thread through all method calls   │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│  NexusFS / Managers / Registries     │
│  - Check context._db_session first   │
│  - Fall back to creating own session │
│  - Backward compatible               │
└─────────────────────────────────────┘
```

### Key Components

#### 1. Middleware (fastapi_server.py)

```python
from starlette.middleware.base import BaseHTTPMiddleware

class DBSessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, session_factory):
        super().__init__(app)
        self.session_factory = session_factory

    async def dispatch(self, request: Request, call_next):
        session = self.session_factory()
        request.state.db_session = session
        try:
            response = await call_next(request)
            session.commit()
            return response
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
```

#### 2. Dependency Injection

```python
def get_db_session(request: Request) -> Session:
    """Get database session from request state."""
    return request.state.db_session

# Usage in endpoints
@app.post("/api/nfs/{method}")
async def rpc_endpoint(
    method: str,
    request: Request,
    db: Session = Depends(get_db_session),
    auth: AuthResult = Depends(get_auth_result),
):
    context = build_context(auth, db_session=db)
    return await dispatch_method(method, context)
```

#### 3. OperationContext Enhancement

```python
@dataclass
class OperationContext:
    user: str
    groups: list[str]
    is_admin: bool = False
    tenant_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    _db_session: Session | None = None  # NEW: Request-scoped session

    def get_session(self, session_factory) -> Session:
        """Get session from context or create new one."""
        if self._db_session is not None:
            return self._db_session
        return session_factory()
```

#### 4. Method Signature Updates

Methods should prefer context session over creating new ones:

```python
# Before
def some_method(self, path: str, context: OperationContext | None = None):
    with self.metadata.SessionLocal() as session:
        # ... operations

# After
def some_method(self, path: str, context: OperationContext | None = None):
    session = context._db_session if context else None
    if session:
        # Use provided session (no commit - caller manages)
        return self._some_method_impl(session, path)
    else:
        # Backward compatible: create own session
        with self.metadata.SessionLocal() as session:
            result = self._some_method_impl(session, path)
            session.commit()
            return result
```

## Implementation Phases

### Phase 1: Infrastructure (2-3 days)

**Goal**: Add middleware and context support without breaking existing code.

**Files to modify**:
- `src/nexus/server/fastapi_server.py` - Add middleware
- `src/nexus/core/permissions.py` - Add `_db_session` to OperationContext

**Tasks**:
1. [ ] Create `DBSessionMiddleware` class
2. [ ] Add middleware to FastAPI app in lifespan
3. [ ] Add `_db_session` field to `OperationContext`
4. [ ] Add `get_db_session()` dependency function
5. [ ] Update `/api/nfs/{method}` endpoint to pass session in context
6. [ ] Add integration test for session lifecycle

### Phase 2: Core NexusFS Methods (3-5 days)

**Goal**: Update high-traffic methods to use context session.

**Files to modify**:
- `src/nexus/core/nexus_fs.py`
- `src/nexus/core/nexus_fs_core.py`

**Tasks**:
1. [ ] Update `_dispatch_method()` to pass context with session
2. [ ] Update read/write/delete operations
3. [ ] Update mkdir/rmdir/rename operations
4. [ ] Update list/glob operations
5. [ ] Update search operations
6. [ ] Ensure backward compatibility for embedded usage

### Phase 3: Secondary Managers (2-3 days)

**Goal**: Propagate session through manager classes.

**Files to modify**:
- `src/nexus/core/entity_registry.py` (partially done)
- `src/nexus/core/rebac_manager.py`
- `src/nexus/core/workspace_manager.py`
- `src/nexus/core/mount_manager.py`
- `src/nexus/core/workspace_registry.py`

**Tasks**:
1. [ ] Complete EntityRegistry session passthrough
2. [ ] Update ReBACManager to accept session
3. [ ] Update WorkspaceManager session handling
4. [ ] Update MountManager session handling
5. [ ] Update WorkspaceRegistry session handling

### Phase 4: Mixin Classes (2-3 days)

**Goal**: Update all mixin classes.

**Files to modify**:
- `src/nexus/core/nexus_fs_rebac.py`
- `src/nexus/core/nexus_fs_search.py`
- `src/nexus/core/nexus_fs_versions.py`
- `src/nexus/core/nexus_fs_mounts.py`
- `src/nexus/core/nexus_fs_skills.py`

**Tasks**:
1. [ ] Update each mixin to use context session
2. [ ] Ensure RPC-exposed methods pass session correctly
3. [ ] Update Memory API integration

### Phase 5: Testing & Validation (2-3 days)

**Goal**: Comprehensive testing under load.

**Tasks**:
1. [ ] Add unit tests for session lifecycle
2. [ ] Add integration tests for concurrent requests
3. [ ] Load test with connection pool limits
4. [ ] Verify no session leaks under stress
5. [ ] Performance benchmark (before/after)

## Migration Strategy

### Backward Compatibility

All changes must be backward compatible for:
1. **Embedded usage** (NexusFS used directly without FastAPI)
2. **CLI usage** (nexus-cli commands)
3. **Existing tests** (no breaking changes)

Pattern for backward compatibility:
```python
def method(self, ..., context: OperationContext | None = None):
    # Use context session if available (server mode)
    # Otherwise create own session (embedded/CLI mode)
    session = context._db_session if context else None
    ...
```

### Rollback Plan

If issues are discovered:
1. Middleware can be disabled by removing from app
2. Context `_db_session` field is optional (None = old behavior)
3. Each phase is independently deployable

## Success Metrics

| Metric | Before | Target |
|--------|--------|--------|
| Sessions per provision_user() | 25-30 | 1 |
| Max concurrent connections | Limited by pool | 1 per request |
| Session leak risk | High | None |
| Transaction atomicity | Partial | Full per-request |

## Dependencies

- SQLAlchemy 2.0+
- FastAPI 0.100+
- Starlette middleware support

## References

- [SQLAlchemy Session Basics](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [FastAPI Dependencies](https://fastapi.tiangolo.com/tutorial/dependencies/)
- [Starlette Middleware](https://www.starlette.io/middleware/)
- Commit `8bef8b0`: Initial session passthrough fix
