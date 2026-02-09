# Memory Paging Server Integration Guide

## Status

**Python API:** ✅ Complete (tests passing)
**FastAPI Server:** ⚠️ Requires integration

## The Gap

Currently:
- `NexusFilesystem` creates default `Memory` instance
- FastAPI uses `nx.memory` which doesn't have paging
- Need to swap to `MemoryWithPaging`

## How to Enable Paging in Server

### Option 1: Configuration Flag (Recommended)

Add to server startup code:

```python
# src/nexus/core/nexus_fs.py or wherever NexusFilesystem initializes Memory

from nexus.core.memory_with_paging import MemoryWithPaging

class NexusFilesystem:
    def __init__(self, ..., enable_memory_paging: bool = False):
        ...
        if enable_memory_paging:
            self.memory = MemoryWithPaging(
                session=self.session,
                backend=self.backend,
                zone_id=self.zone_id,
                user_id=self.user_id,
                agent_id=self.agent_id,
                enable_paging=True,
                main_capacity=100,
                recall_max_age_hours=24.0,
            )
        else:
            self.memory = Memory(...)  # Existing code
```

Then add CLI flag:
```bash
nexus serve --enable-memory-paging --auth-type database
```

### Option 2: Environment Variable

```python
import os

ENABLE_MEMORY_PAGING = os.getenv("NEXUS_MEMORY_PAGING", "false").lower() == "true"

if ENABLE_MEMORY_PAGING:
    self.memory = MemoryWithPaging(...)
```

Usage:
```bash
NEXUS_MEMORY_PAGING=true nexus serve --auth-type database
```

### Option 3: Always On (Breaking Change)

Simply replace Memory with MemoryWithPaging everywhere:

```python
# Replace all occurrences
from nexus.core.memory_api import Memory
# With:
from nexus.core.memory_with_paging import MemoryWithPaging as Memory
```

**Risk:** Changes default behavior (but backward compatible API)

## Testing with Server

### 1. Manual Test

```bash
# Terminal 1: Start server with paging
NEXUS_MEMORY_PAGING=true nexus serve --auth-type database --init --port 2026

# Terminal 2: Test via HTTP
export API_KEY=$(cat ~/.nexus/api_key.txt)

# Store many memories to trigger paging
for i in {1..20}; do
  curl -X POST http://localhost:2026/api/v2/memories \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"content\": \"Memory $i\", \"scope\": \"user\", \"memory_type\": \"fact\"}"
done

# Check paging stats (need new endpoint)
curl http://localhost:2026/api/v2/memories/stats \
  -H "Authorization: Bearer $API_KEY"
```

### 2. Add Stats Endpoint

Add to `src/nexus/server/api/v2/routers/memories.py`:

```python
@router.get("/stats", response_model=dict)
async def get_memory_stats(
    auth_result: dict[str, Any] = Depends(_get_require_auth()),
) -> dict:
    """Get memory paging statistics."""
    app_state = _get_app_state()
    if not app_state.nexus_fs:
        raise HTTPException(status_code=503, detail="NexusFS not initialized")

    # Check if paging is enabled
    if hasattr(app_state.nexus_fs.memory, 'get_paging_stats'):
        return app_state.nexus_fs.memory.get_paging_stats()
    else:
        return {"paging_enabled": False, "message": "Memory paging not enabled"}
```

## Current Workaround

For now, paging works at Python API level:

```python
from nexus.core.memory_with_paging import MemoryWithPaging
from nexus.backends.local import LocalBackend
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Create engine & session
engine = create_engine("sqlite:///nexus.db")
Session = sessionmaker(bind=engine)
session = Session()

# Create backend
backend = LocalBackend("/tmp/nexus")

# Use paging directly
memory = MemoryWithPaging(
    session=session,
    backend=backend,
    zone_id="default",
    user_id="alice",
    agent_id="assistant",
    enable_paging=True,
)

# Store memories
for i in range(100):
    memory.store(f"Memory {i}", memory_type="fact")

# Check distribution
stats = memory.get_paging_stats()
print(f"Main: {stats['main']['count']}, Recall: {stats['recall']['count']}")
```

## TODO for Full Integration

- [ ] Add `enable_memory_paging` flag to NexusFilesystem init
- [ ] Add `--enable-memory-paging` CLI flag to `nexus serve`
- [ ] Add `/api/v2/memories/stats` endpoint
- [ ] Add paging config to server settings (capacity, recall age, etc.)
- [ ] Update server startup docs
- [ ] Integration test with real FastAPI server

## Estimated Effort

- **Simple flag integration:** 1-2 hours
- **Stats endpoint:** 30 minutes
- **Testing & docs:** 1 hour

**Total:** 2-3 hours to complete server integration
