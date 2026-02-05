# Manual Test: test_deprovision_user.py

## Purpose

This script provides a manual end-to-end test for the `deprovision_user()` function. It's useful for:
- Debugging deprovision issues
- Testing with different database backends (PostgreSQL, SQLite)
- Verifying complete resource cleanup
- Manual testing during development

## Usage

### Basic Test (PostgreSQL)
```bash
python3 tests/manual/test_deprovision_user.py
```

### With SQLite
```bash
python3 tests/manual/test_deprovision_user.py --sqlite
```

### Custom Database URL
```bash
python3 tests/manual/test_deprovision_user.py --db postgresql://user:pass@host:port/dbname
```

### Custom Backend Path
```bash
python3 tests/manual/test_deprovision_user.py --backend-path ./my-data-dir
```

## What It Tests

The script performs the following steps:

1. **Cleanup**: Checks for and removes any existing test user
2. **Provision**: Creates a new test user with API keys and directories
3. **Verify Resources**: Confirms all user resources were created
4. **Deprovision**: Removes the user and all their resources
5. **Verify Deletion**: Confirms complete cleanup including:
   - User record soft-deleted (is_active=0, deleted_at set)
   - All API keys revoked
   - All directories empty (workspace, memory, skill, agent, connector, resource)
   - Physical directories removed from filesystem
   - Metadata entries deleted from database
   - Permission tuples cleaned up

## Expected Output

```
================================================================================
Deprovision User Test
================================================================================

Database: postgresql://postgres:nexus@localhost:5432/nexus
Backend path: ./nexus-data-local

✓ NexusFS initialized

Step 1: Checking for existing test user...
Step 2: Provisioning test user...
✓ User provisioned successfully!
...

================================================================================
✓ TEST PASSED!
  deprovision_user successfully removed all user data
================================================================================
```

## Troubleshooting

### Test Fails with "⚠️ workspace: still has N items"

This indicates ghost entries remain in the database. Check:
1. Physical directory is actually deleted: `ls -la ./nexus-data-local/dirs/zone/test_zone/user:test_deprovision_user/`
2. Metadata entries in database: Query `file_paths` table for the path
3. ReBAC tuples: Query `rebac_tuples` table for the resource

### Database Connection Issues

- **PostgreSQL**: Ensure PostgreSQL is running and accessible at `localhost:5432`
- **SQLite**: Database file will be created automatically in the backend path

## Related Files

- **Implementation**: `src/nexus/core/nexus_fs.py` - `deprovision_user()` and `_delete_directory_recursive()`
- **Unit Tests**: `tests/unit/core/test_deprovision_user.py` - 12 comprehensive unit tests
- **RPC Client**: `src/nexus/remote/client.py` - Remote client implementation
