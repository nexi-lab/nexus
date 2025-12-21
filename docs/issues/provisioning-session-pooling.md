# Issue: Provisioning API Database Session Pool Exhaustion

## Problem

The provisioning API (`provision_user()` and `provision_tenant()`) opens 25-30 database sessions per call due to nested operations. While context managers properly close these sessions, a single API call can exhaust the connection pool (default: 30 connections).

## Root Cause

Each nested operation opens its own database session:

1. **Direct session opens in `provision_user()`:**
   - Check existing user: 1 session
   - Check/create tenant (personal accounts): 3 sessions
   - Create user record: 1 session
   - **Subtotal: 5 sessions**

2. **`_create_user_directories()` - 6 resource types:**
   - Each `rebac_create()` call opens a session
   - 6 resources × 1 session = **6 sessions**

3. **`_provision_default_agents()` - 2 agents:**
   - Each `register_agent()` calls `entity_registry.register_entity()`:
     - `get_entity()` check: 1 session
     - Entity create: 1 session
     - = 2 sessions per agent
   - 2 agents × 2 sessions = **4 sessions**
   - Each agent gets `rebac_create()` for permissions: **2 sessions**
   - **Subtotal: 6 sessions**

4. **`_provision_default_workspace()`:**
   - `register_workspace()`: ~2 sessions
   - `rebac_create()`: 1 session
   - **Subtotal: 3 sessions**

**Total: 20-30 sessions per provision_user() call**

## Impact

- Integration tests fail with "QueuePool limit reached" errors
- Production deployments need large connection pools (50+ connections)
- Cannot scale provisioning API horizontally without connection overhead

## Current Workaround

Increased database pool size in [`config.demo.yaml`](../../configs/config.demo.yaml):
```yaml
pool_size: 20  # Was 10
max_overflow: 30  # Was 20
```

**This is treating the symptom, not the cause.**

## Proper Fix

Refactor provisioning API to use shared sessions throughout the call stack:

### Option 1: Session-Aware API (Recommended)

Add optional `_session` parameter to all database operations:

```python
def _create_user_directories(
    self,
    tenant_id: str,
    user_id: str,
    context: OperationContext,
    _session: Session | None = None,  # NEW
) -> None:
    \"\"\"Create user directories, optionally reusing an existing session.\"\"\"
    # If session provided, use it; otherwise open new one
    ...
```

Then in `provision_user()`:

```python
def provision_user(...) -> dict:
    # Open ONE session for entire provisioning operation
    with self.metadata.SessionLocal() as session:
        # Pass session to all nested operations
        self._create_user_directories(..., _session=session)
        self._provision_default_agents(..., _session=session)
        ...
        session.commit()  # Single commit at end
```

### Option 2: Batch ReBAC API

Implement `rebac_create_batch()` to reduce session opens:

```python
# Instead of 6 separate rebac_create() calls:
for resource_type in ALL_RESOURCE_TYPES:
    self.rebac_create(...)  # 6 sessions

# Use batch API:
self.rebac_create_batch([
    (subject, relation, object, tenant_id)
    for resource_type in ALL_RESOURCE_TYPES
])  # 1 session
```

### Option 3: EntityRegistry Session Pooling

Refactor `EntityRegistry.register_entity()` to check for existing entity in the same session as creation:

```python
def register_entity(...) -> EntityRegistryModel:
    with self._get_session() as session:
        # Do both check AND create in same session
        existing = session.query(...).first()
        if existing:
            return existing

        entity = EntityRegistryModel(...)
        session.add(entity)
        session.commit()
        return entity
```

## Implementation Plan

1. [ ] Audit all database operations in provisioning flow
2. [ ] Add `_session` parameter support to:
   - [ ] `entity_registry.register_entity()`
   - [ ] `rebac_create()` / `rebac_manager` operations
   - [ ] `register_workspace()`, `register_agent()`, etc.
3. [ ] Refactor `provision_user()` to use shared session
4. [ ] Refactor `provision_tenant()` to use shared session
5. [ ] Update tests to verify session count
6. [ ] Revert `config.demo.yaml` pool size increase
7. [ ] Document new session-aware API patterns

## Related Files

- [`src/nexus/core/nexus_fs_provisioning.py`](../../src/nexus/core/nexus_fs_provisioning.py) - Provisioning API
- [`src/nexus/core/entity_registry.py`](../../src/nexus/core/entity_registry.py) - Entity registration
- [`src/nexus/core/rebac_manager.py`](../../src/nexus/core/rebac_manager.py) - ReBAC operations
- [`configs/config.demo.yaml`](../../configs/config.demo.yaml) - Database config

## Testing

Add test to verify session count:

```python
def test_provision_user_session_count(monkeypatch):
    \"\"\"Verify provision_user doesn't exhaust connection pool.\"\"\"
    session_count = 0

    original_session_local = nx.metadata.SessionLocal
    def counting_session():
        nonlocal session_count
        session_count += 1
        return original_session_local()

    monkeypatch.setattr(nx.metadata, "SessionLocal", counting_session)

    nx.provision_user("test_user", ...)

    # Should use <10 sessions, not 30
    assert session_count < 10, f"Too many sessions: {session_count}"
```

## Priority

**High** - This is a blocking issue for production scalability and CI reliability.

## Labels

- `bug`
- `performance`
- `database`
- `provisioning-api`
- `technical-debt`
