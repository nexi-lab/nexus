# Nexus Database Corruption and Crash - Reproduction Guide

## Issue Summary

The Nexus server crashes with a **segmentation fault** after running multiple operations. The crash is preceded by SQLite database corruption errors:

- `database disk image is malformed`
- `bad parameter or other API misuse`

The crash occurs when the server attempts to read skill files after concurrent API operations.

## Root Cause

This appears to be a **SQLite concurrency issue**. SQLite has known limitations with concurrent writes, and the issue manifests when multiple API calls (especially `skills_list`) are made concurrently, causing:

1. Database corruption
2. Segmentation fault when trying to read from the corrupted database

## API Call Sequence That Triggers the Crash

Based on the logs, here's the sequence of API calls that leads to the crash:

### Wave 1: Initial Page Load
```
POST /api/nfs/list_mounts
POST /api/nfs/list (path: "/")
POST /api/nfs/list (path: "/tenant:default/")
POST /api/nfs/list (path: "/tenant:default/user:admin/")
```

### Wave 2: Agents and Permissions
```
POST /api/nfs/list_agents
POST /api/nfs/rebac_list_tuples
POST /api/nfs/list_workspaces
```

### Wave 3: Skills List (CRASH TRIGGER)
```
POST /api/nfs/skills_list (multiple concurrent calls)
```

**This is where the database corruption begins.**

### Wave 4: File Operations
```
POST /api/nfs/list (path: "/tenant:default/user:admin/skill/")
POST /api/nfs/list (path: "/tenant:default/user:admin/agent/")
POST /api/nfs/list (path: "/tenant:default/user:admin/workspace/")
POST /api/nfs/list (path: "/tenant:default/user:admin/connector/")
POST /api/nfs/list (path: "/tenant:default/user:admin/memory/")
```

### Wave 5: More Skills Operations
```
POST /api/nfs/skills_list (more concurrent calls)
```

**This is where the crash occurs with:**
```
2025-12-17 22:53:05,660 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/docx/SKILL.md: database disk image is malformed
2025-12-17 22:53:05,660 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/internal-comms/SKILL.md: database disk image is malformed
2025-12-17 22:53:05,661 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/pdf/SKILL.md: bad parameter or other API misuse
./local-demo.sh: line 959: 74347 Segmentation fault: 11  nexus serve --config ./configs/config.demo.yaml --auth-type database --async
```

### Wave 6: Sandbox Operations
```
POST /api/nfs/sandbox_get_or_create (name: "admin,ImpersonatedUser")
POST /api/nfs/sandbox_get_or_create (name: "admin,UntrustedAgent")
GET /health
```

### Wave 7: Final Read Operations
```
POST /api/nfs/read (various skill files)
```

## Reproduction Steps

### Quick Reproduction

Use the provided scripts to reproduce the issue:

```bash
# Run with a fresh data directory
./scripts/reproduce_crash.sh /tmp/nexus-data-crash-test-1

# Or specify a different directory for each test
./scripts/reproduce_crash.sh /tmp/nexus-data-crash-test-2
```

### Manual Reproduction

If you want to reproduce manually:

1. Start the server with a fresh data directory:
   ```bash
   ./local-demo.sh --start --data-dir /tmp/nexus-data-fresh-1
   ```

2. Wait for the server to be fully provisioned (check logs)

3. Use the frontend or make concurrent API calls:
   - Open the frontend at `http://localhost:5173`
   - Navigate through different sections
   - Open the Skills page multiple times
   - Refresh the page several times

4. Observe the crash in the server logs

## Files Created

- **[scripts/reproduce_crash.sh](scripts/reproduce_crash.sh)** - Main reproduction script
- **[scripts/reproduce_crash_api_calls.py](scripts/reproduce_crash_api_calls.py)** - Python script that makes concurrent API calls

## Expected Behavior

When running the reproduction script, you should see:

1. Server starts successfully
2. Initial API calls succeed
3. Database corruption warnings appear
4. Server crashes with segmentation fault

Example output:
```
2025-12-17 22:53:05,660 - nexus.skills.registry - WARNING - Failed to read skill /tenant:default/user:admin/skill/docx/SKILL.md: database disk image is malformed
./local-demo.sh: line 959: 74347 Segmentation fault: 11  nexus serve --config ./configs/config.demo.yaml --auth-type database --async
```

## Potential Solutions

### 1. Use PostgreSQL Instead of SQLite

SQLite has known concurrency limitations. Using PostgreSQL should resolve this issue:

```bash
./local-demo.sh --start --nosqlite --postgres-url 'postgresql://user:pass@localhost:5432/db'
```

### 2. Add SQLite Write-Ahead Logging (WAL)

Enable WAL mode for SQLite to improve concurrency:

```python
# In database initialization
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA busy_timeout=5000")
```

### 3. Add Connection Pooling with Retries

Implement proper connection pooling and retry logic for SQLite operations.

### 4. Add Locking/Serialization for Skills Operations

Add a lock or serialize `skills_list` operations to prevent concurrent access.

## Environment Details

- **Database**: SQLite (embedded mode)
- **Server**: Nexus with `--async` flag
- **Config**: `configs/config.demo.yaml`
- **Auth**: Database auth type
- **API Key**: `sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee`

## Related Code Paths

The crash occurs in:
- [nexus/skills/registry.py](../nexus/skills/registry.py) - Skills registry that reads skill files
- [nexus/core/nexus_fs_search.py](../nexus/core/nexus_fs_search.py) - File system search operations
- SQLite database operations in the virtual file system

## Notes

- The crash is **deterministic** when following the reproduction steps
- The issue only occurs with **concurrent** API calls
- Using PostgreSQL instead of SQLite should prevent this issue
- The crash leaves the database in a corrupted state, requiring a fresh data directory
