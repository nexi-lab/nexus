# Quick Fix: Database Lock / Crash Issues

## Problem

Server crashes or returns 500 errors with messages like:
- `sqlite3.OperationalError: database is locked`
- `database disk image is malformed`
- `bad parameter or other API misuse`
- Segmentation fault

## Quick Fix: Use PostgreSQL

### Option 1: With Docker (Recommended)

```bash
# Use the Docker demo which includes PostgreSQL
./scripts/dev/docker-demo.sh
```

### Option 2: Local PostgreSQL

```bash
# Start with local PostgreSQL (it will auto-start the container)
./local-demo.sh --start --nosqlite
```

### Option 3: Custom PostgreSQL URL

```bash
./local-demo.sh --start --postgres-url 'postgresql://user:pass@localhost:5432/mydb'
```

## Why This Happens

**Root Cause**: SQLite cannot handle concurrent writes properly.

When multiple API calls happen simultaneously (like when the frontend loads):
1. Multiple permission checks are triggered
2. Each tries to write to SQLite cache
3. SQLite locks the database
4. Some writes fail → database corruption
5. Server crashes or becomes unresponsive

## Quick Test

Run the reproduction script to verify the issue:

```bash
# This should trigger the error with SQLite
./scripts/reproduce_crash.sh /tmp/nexus-crash-test-$(date +%s)
```

## Verify Fix

After switching to PostgreSQL, the same operations should work:

```bash
# Start with PostgreSQL
./local-demo.sh --start --nosqlite

# In another terminal, run the reproduction script
./scripts/reproduce_crash.sh /tmp/nexus-postgres-test

# Should complete without errors ✓
```

## Temporary Workaround (SQLite)

If you must use SQLite, reduce concurrency:

1. **Reduce frontend refresh rate**
2. **Avoid parallel operations**
3. **Add delays between API calls**

But this is **not recommended** for production.

## More Information

- [CRASH_REPRODUCTION_GUIDE.md](CRASH_REPRODUCTION_GUIDE.md) - Full reproduction guide
- [CRASH_REPRODUCTION_RESULTS.md](CRASH_REPRODUCTION_RESULTS.md) - Detailed analysis
- [local-demo.sh](local-demo.sh) - Server startup script

## Summary

| Issue | Solution |
|-------|----------|
| Database locked | Use PostgreSQL |
| Database malformed | Use PostgreSQL |
| Segmentation fault | Use PostgreSQL |
| 500 errors under load | Use PostgreSQL |

**Bottom line**: Use PostgreSQL for anything beyond single-user development.
