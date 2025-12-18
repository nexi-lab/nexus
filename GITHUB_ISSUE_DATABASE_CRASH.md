# SQLite Database Lock Causes Server Crash Under Concurrent Load

## Description

The Nexus server crashes or becomes unresponsive when handling concurrent API requests due to SQLite database locking issues. This occurs during normal frontend operations when multiple API calls are made simultaneously.

## Issue Type

- [x] Bug
- [ ] Feature Request
- [ ] Documentation

## Severity

**High** - Causes server crashes and data corruption in production-like scenarios

## Environment

- **OS**: macOS (Darwin 25.1.0) - Also reproducible on Linux
- **Python Version**: 3.13
- **Database**: SQLite (embedded mode)
- **Server Mode**: FastAPI async server (`--async` flag)
- **Config**: `configs/config.demo.yaml`
- **Nexus Version**: Current main branch

## Steps to Reproduce

We've created an automated reproduction script that consistently triggers this issue:

### Quick Reproduction

```bash
# Clone and setup (if not already done)
cd nexus

# Run the automated reproduction script
./scripts/reproduce_crash.sh /tmp/nexus-crash-test-$(date +%s)
```

The script will:
1. Start a fresh Nexus server with SQLite
2. Wait for server to be ready
3. Execute concurrent API calls that simulate frontend behavior
4. Trigger the database lock/crash

### Manual Reproduction

1. Start the server with SQLite:
   ```bash
   ./local-demo.sh --start --data-dir /tmp/nexus-test
   ```

2. Make concurrent API calls (e.g., by refreshing the frontend multiple times or running):
   ```bash
   python scripts/reproduce_crash_api_calls.py
   ```

3. Observe server logs for errors and 500 responses

## Expected Behavior

- All API calls should succeed with 200 OK responses
- Server should handle concurrent requests gracefully
- No database locking or corruption errors
- Server remains responsive under concurrent load

## Actual Behavior

### Phase 1: Initial Success (Wave 1-3)
```
✓ POST /api/nfs/list_mounts - 200 OK
✓ POST /api/nfs/list - 200 OK
✓ POST /api/nfs/skills_list (x5 concurrent) - 200 OK
```

### Phase 2: Failure Cascade (Wave 4+)
```
✗ POST /api/nfs/list - 500 Internal Server Error
✗ POST /api/nfs/skills_list - 500 Internal Server Error
✗ All subsequent operations fail
```

### Server Logs

```
sqlite3.OperationalError: database is locked

Traceback (most recent call last):
  File "/nexus/src/nexus/core/rebac_manager_tenant_aware.py", line 908, in _cache_check_result_tenant_aware
    cursor.execute(
        self._fix_sql_placeholders(...),
    )
sqlite3.OperationalError: database is locked
```

Additional warnings observed:
```
2025-12-17 22:53:05,660 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/docx/SKILL.md: database disk image is malformed
2025-12-17 22:53:05,661 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/pdf/SKILL.md: bad parameter or other API misuse
./local-demo.sh: line 959: 74347 Segmentation fault: 11  nexus serve --config ./configs/config.demo.yaml --auth-type database --async
```

## Root Cause Analysis

### Primary Issue: SQLite Concurrency Limitations

SQLite has well-known limitations with concurrent writes:
1. **Single Writer Lock** - Only one write transaction at a time
2. **Write Serialization** - Concurrent writes cause `database is locked` errors
3. **Corruption Risk** - Continued writes during lock can corrupt the database

### Trigger Point

The issue is triggered in the ReBAC permission caching layer:

**File**: [`src/nexus/core/rebac_manager_tenant_aware.py:908`](../src/nexus/core/rebac_manager_tenant_aware.py#L908)

**Method**: `_cache_check_result_tenant_aware()`

**Scenario**:
1. Multiple concurrent `skills_list` API calls arrive
2. Each triggers multiple permission checks
3. Each permission check attempts to write cache results to SQLite
4. SQLite locks on concurrent write attempts
5. Subsequent operations fail with 500 errors
6. Database may become corrupted → segmentation fault

### Why FastAPI Async Makes It Worse

The `--async` flag with uvloop enables true concurrent request handling:
- Multiple requests processed simultaneously
- Multiple async tasks can interleave
- All trying to write to SQLite at once
- SQLite can't handle this concurrency model

## Impact

- **User Experience**: Frontend becomes unresponsive, requires server restart
- **Data Integrity**: Risk of database corruption
- **Production Readiness**: Blocks production deployments with SQLite
- **Development**: Frequent crashes during testing with concurrent operations

## Reproduction Scripts

We've created comprehensive test scripts:

### 1. Main Reproduction Script
**File**: [`scripts/reproduce_crash.sh`](../scripts/reproduce_crash.sh)

Automated script that:
- Starts server with fresh data directory
- Waits for server readiness
- Executes concurrent API calls
- Captures crash/errors

### 2. API Call Simulator
**File**: [`scripts/reproduce_crash_api_calls.py`](../scripts/reproduce_crash_api_calls.py)

Python script that simulates frontend behavior:
- 7 waves of progressively concurrent API calls
- Targets specific endpoints that trigger the issue
- Reports success/failure for each wave

### 3. Documentation
- **[CRASH_REPRODUCTION_GUIDE.md](../CRASH_REPRODUCTION_GUIDE.md)** - Detailed reproduction guide
- **[CRASH_REPRODUCTION_RESULTS.md](../CRASH_REPRODUCTION_RESULTS.md)** - Analysis of findings
- **[QUICKFIX_DATABASE_CRASH.md](../QUICKFIX_DATABASE_CRASH.md)** - Quick reference for fixes

## Proposed Solutions

### Solution 1: Use PostgreSQL (Recommended) ⭐

**Priority**: HIGH
**Effort**: LOW
**Impact**: Complete fix

PostgreSQL properly handles concurrent writes with MVCC (Multi-Version Concurrency Control).

```bash
# Quick fix - switch to PostgreSQL
./local-demo.sh --start --nosqlite
```

**Pros**:
- ✅ Completely solves the issue
- ✅ Better performance under load
- ✅ Production-ready
- ✅ No code changes needed
- ✅ Recommended for all non-development scenarios

**Cons**:
- Requires PostgreSQL installation (but script auto-starts Docker container)

### Solution 2: Enable SQLite WAL Mode

**Priority**: MEDIUM
**Effort**: LOW
**Impact**: Partial improvement

Enable Write-Ahead Logging for better concurrency:

```python
# In database initialization
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA busy_timeout=30000")  # 30 seconds
connection.execute("PRAGMA synchronous=NORMAL")
```

**Pros**:
- ✅ Improves concurrency
- ✅ Simple to implement
- ✅ Better for single-user dev scenarios

**Cons**:
- ⚠️ Doesn't completely solve high-concurrency scenarios
- ⚠️ Still susceptible under heavy load
- ⚠️ Not recommended for production

### Solution 3: Use aiosqlite

**Priority**: MEDIUM
**Effort**: MEDIUM
**Impact**: Partial improvement

Replace sync sqlite3 with async aiosqlite:

```python
import aiosqlite

async with aiosqlite.connect(db_path) as db:
    await db.execute(query, params)
    await db.commit()
```

**Pros**:
- ✅ Better async/await support
- ✅ Reduces blocking

**Cons**:
- ⚠️ Requires code changes throughout
- ⚠️ Still has SQLite fundamental concurrency limits
- ⚠️ Complex migration

### Solution 4: Serialize Writes with Lock

**Priority**: LOW
**Effort**: HIGH
**Impact**: Reduces throughput

Implement write serialization:

```python
class SQLiteConnectionPool:
    def __init__(self):
        self._write_lock = asyncio.Lock()

    async def execute_write(self, query, params):
        async with self._write_lock:
            # Serialize all writes
            conn.execute(query, params)
```

**Pros**:
- ✅ Prevents database locks
- ✅ Works with existing SQLite

**Cons**:
- ⚠️ Complex implementation
- ⚠️ Reduces throughput significantly
- ⚠️ Still limited by SQLite
- ⚠️ Not addressing root cause

## Recommendation

**Use PostgreSQL for production and any multi-user scenarios.**

SQLite should only be used for:
- ✅ Single-user development
- ✅ Automated testing
- ✅ Low-concurrency workloads

SQLite is **NOT suitable** for:
- ❌ Production deployments
- ❌ Multi-user web servers
- ❌ High-concurrency APIs
- ❌ Concurrent write-heavy workloads

## Additional Context

### Frequency
- **Reproducibility**: 100% with provided scripts
- **Trigger**: Any scenario with concurrent API calls (normal frontend usage)
- **Time to failure**: Usually within 5-10 concurrent operations

### Workarounds

Temporary workarounds if PostgreSQL cannot be used immediately:
1. Reduce frontend concurrent requests (not practical)
2. Add artificial delays between operations (poor UX)
3. Restart server frequently (not acceptable)

None of these are recommended - switch to PostgreSQL instead.

## Testing

### Verify the Issue
```bash
./scripts/reproduce_crash.sh /tmp/crash-test-$(date +%s)
# Should show database lock errors and 500 responses
```

### Verify the Fix (PostgreSQL)
```bash
# Start with PostgreSQL
./local-demo.sh --start --nosqlite

# Run same test
./scripts/reproduce_crash.sh /tmp/postgres-test-$(date +%s)
# Should complete without errors ✓
```

## Related Issues

- Relates to concurrency handling
- Relates to production deployment requirements
- May affect other SQLite operations under load

## Checklist

- [x] Issue is reproducible
- [x] Reproduction scripts provided
- [x] Root cause identified
- [x] Solutions proposed
- [x] Documentation created
- [ ] Fix implemented
- [ ] Tests added
- [ ] Documentation updated

## Files Changed/Added

- `scripts/reproduce_crash.sh` - Automated reproduction script
- `scripts/reproduce_crash_api_calls.py` - API call simulator
- `CRASH_REPRODUCTION_GUIDE.md` - Detailed guide
- `CRASH_REPRODUCTION_RESULTS.md` - Analysis results
- `QUICKFIX_DATABASE_CRASH.md` - Quick reference

---

## For Maintainers

This issue is ready for implementation with:
1. Full reproduction scripts
2. Root cause analysis
3. Recommended solution (PostgreSQL)
4. Test verification strategy

**Recommended Action**:
1. Document SQLite limitations in README
2. Default to PostgreSQL in production configs
3. Consider adding WAL mode for SQLite dev scenarios
4. Add warning when starting server with SQLite in non-dev environments
