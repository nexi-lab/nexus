# Memory Brick Migration Guide

## Overview

The Memory service has been extracted from `/services/memory/` to `/bricks/memory/` following the NEXUS-LEGO-ARCHITECTURE design principles. This guide explains the changes and how to use the new Memory brick.

**Related Issues:** #2128 (Memory brick extraction), #2123 (Search primitives migration)

## What Changed

### Architecture

**Before:**
```
/services/memory/
├── memory_api.py (2,160 LOC - monolithic)
├── enrichment.py
├── state.py
├── versioning.py
└── ...
```

**After:**
```
/bricks/memory/
├── __init__.py (public API)
├── service.py (MemoryBrick facade, 540 LOC)
├── crud.py (681 LOC)
├── query.py (551 LOC)
├── lifecycle.py (230 LOC)
├── versioning_ops.py (613 LOC)
├── response_models.py (351 LOC - mixin-based)
├── enrichment/ (pipeline + stages)
└── tests/ (centralized fixtures)
```

### Key Improvements

1. **Modular Structure**: Single 2,160 LOC file split into 4 domain-focused files (200-600 LOC each)
2. **DRY Refactoring**: Eliminated 1,847 LOC through consolidation
   - Response models: 168 LOC saved via mixin composition
   - Test fixtures: 1,275 LOC saved via centralization
   - Import consolidation: 144 LOC saved
3. **Protocol-Based**: Implements `MemoryProtocol` with zero cross-brick imports
4. **Constructor DI**: All dependencies injected, no hardcoded imports
5. **Performance**: Batch entity loading APIs (7-10x potential improvement)

## Usage

### Basic Usage (No Code Changes Required)

The Memory brick is backward compatible. Existing code continues to work:

```python
# Via NexusFS (existing code)
nx = nexus.connect()
memory_id = nx.memory.store("User prefers Python", scope="user")
results = nx.memory.query(memory_type="preference")

# Via REST API (existing endpoints)
POST /api/v2/memories/
GET /api/v2/memories/{id}
POST /api/v2/memories/search
```

### Direct Brick Usage (New Pattern)

```python
from nexus.bricks.memory import MemoryBrick, RetentionPolicy
from nexus.core.permissions import OperationContext

# Create brick with dependency injection
brick = MemoryBrick(
    memory_router=memory_router,
    permission_enforcer=permission_enforcer,
    backend=backend,
    context=OperationContext(user_id="test", groups=[], is_admin=False),
    session_factory=session_factory,
    retention_policy=RetentionPolicy(
        keep_last_n=10,
        keep_versions_days=90,
        enabled=True
    ),
)

# Use MemoryProtocol methods
memory_id = brick.store(content="Test", scope="user")
memory = brick.get(memory_id=memory_id)
results = brick.query(scope="user", limit=10)
```

### Testing with New Fixtures

```python
# tests/unit/test_my_memory_feature.py
from conftest import memory_api_mock, sample_memories

def test_my_feature(memory_api_mock, sample_memories):
    """Use centralized fixtures."""
    memory_api_mock.store.return_value = "mem_test_123"

    result = my_function(memory_api_mock)

    assert result is not None
    memory_api_mock.store.assert_called_once()
```

## Search Primitives Migration

Search utilities moved from kernel tier (`/core/`) to brick tier (`/search/primitives/`):

### Update Imports

**Old (deprecated, 6-month warning):**
```python
from nexus.core import grep_fast, glob_fast, trigram_fast
```

**New (correct):**
```python
from nexus.search.primitives import grep_fast, glob_fast, trigram_fast
```

### Affected Files (Already Updated)

All imports have been updated in:
- factory.py
- services/sync_service.py
- services/search_service.py
- rebac/enforcer.py
- backends/sync_pipeline.py
- services/search_grep_mixin.py
- core/filters.py
- backends/x_connector.py

## Performance Improvements

### Batch Entity Loading

GraphStore now supports batch operations to avoid N+1 queries:

```python
from nexus.search.graph_store import GraphStore

# Old (N+1 queries)
entities = []
for entity_id in entity_ids:
    entity = await graph_store.get_entity(entity_id)
    entities.append(entity)

# New (single query)
entities = await graph_store.get_entities_batch(entity_ids)

# Relationships batch loading
relationships_map = await graph_store.get_relationships_batch(
    entity_ids=entity_ids,
    direction="both"
)
```

**Performance:** 10 entities = 21 queries → 2-3 queries (7-10x improvement)

### Version Retention Policy

Automatic garbage collection prevents unbounded version growth:

```python
from nexus.bricks.memory import RetentionPolicy

policy = RetentionPolicy(
    keep_last_n=10,           # Always keep 10 most recent
    keep_versions_days=90,    # Keep versions < 90 days
    gc_interval_hours=24,     # Run GC daily
    enabled=True
)

# Scheduled GC runs automatically via factory.py
```

## Breaking Changes

### None (Fully Backward Compatible)

- All REST API endpoints unchanged
- NexusFS.memory property works as before
- Search primitives have 6-month deprecation period
- Existing tests continue to work

## Migration Checklist

For new code using Memory brick:

- [ ] Import from `nexus.bricks.memory` instead of `nexus.services.memory`
- [ ] Use `MemoryBrick` class instead of `Memory` or `MemoryAPI`
- [ ] Inject dependencies via constructor (don't instantiate internally)
- [ ] Use centralized test fixtures from `conftest.py`
- [ ] Update search primitive imports to `nexus.search.primitives`
- [ ] Use batch entity loading APIs when fetching multiple entities
- [ ] Configure RetentionPolicy for version management

## Troubleshooting

### Import Errors

**Error:** `ImportError: cannot import name 'EnrichmentPipeline' from 'nexus.services.memory.enrichment'`

**Solution:** Update import:
```python
# Old
from nexus.services.memory.enrichment import EnrichmentPipeline

# New
from nexus.bricks.memory.enrichment import EnrichmentPipeline
```

### Memory Brick Not Available

**Error:** `HTTPException: Memory brick not enabled on this server`

**Solution:** Ensure brick is enabled in factory.py boot sequence. Check server logs for initialization errors.

### Test Fixture Not Found

**Error:** `NameError: name 'sample_memories' is not defined`

**Solution:** Ensure `conftest.py` is in the test directory path and imports are correct.

## Additional Resources

- [NEXUS-LEGO-ARCHITECTURE.md](../NEXUS-LEGO-ARCHITECTURE.md) - Design principles
- [MemoryProtocol API Reference](../api/memory-protocol.md)
- [Memory Brick Tests](../../src/nexus/bricks/memory/tests/) - Usage examples
- [Issue #2128](https://github.com/yourorg/nexus/issues/2128) - Full implementation details

## Support

For questions or issues:
1. Check this migration guide
2. Review memory brick tests for usage patterns
3. Search existing GitHub issues
4. Create new issue with `brick:memory` label
