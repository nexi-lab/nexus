# Adding deprovision_user Test to Docker Integration

## Current Status

✅ **Test is now docker-ready!**

The `test_deprovision_user.py` script now supports:
- Environment variables for configuration
- Docker service URLs
- Custom API keys
- Both local and Docker environments

## How to Add to docker-integration.yml

Add this step after the "User Permission Grant Test" (around line 663):

```yaml
      - name: User Deprovision Integration Test
        run: |
          echo "🗑️ Running User Deprovision integration test..."
          echo ""

          # Use known dummy API key (set in .env)
          API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
          echo "Using API key: ${API_KEY:0:20}..."
          echo ""

          # Install Python dependencies
          python3 -m pip install sqlalchemy psycopg2-binary --quiet || true

          # Run deprovision test with Docker database
          # Note: Test connects directly to PostgreSQL container
          python3 tests/manual/test_deprovision_user.py \
            --db "postgresql://postgres:nexus@localhost:5432/nexus" \
            --base-url "http://localhost:2026" \
            --backend-path "./nexus-data-local"

          if [ $? -eq 0 ]; then
            echo "✅ User deprovision integration test completed successfully!"
          else
            echo "❌ User deprovision integration test failed"
            exit 1
          fi
```

## Environment Variables

The test automatically reads from:
- `NEXUS_DATABASE_URL` - Database connection string
- `NEXUS_API_KEY` - Admin API key
- `NEXUS_BASE_URL` - Nexus server URL

## Usage Examples

### Local Testing
```bash
# Default (local PostgreSQL)
python3 tests/manual/test_deprovision_user.py

# With SQLite
python3 tests/manual/test_deprovision_user.py --sqlite

# Custom database
python3 tests/manual/test_deprovision_user.py --db postgresql://user/pass@host:port/dbname
```

### Docker Environment
```bash
# Using environment variables
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
export NEXUS_API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
python3 tests/manual/test_deprovision_user.py

# Or with command-line arguments
python3 tests/manual/test_deprovision_user.py \
  --db "postgresql://postgres:nexus@localhost:5432/nexus" \
  --api-key "sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" \
  --base-url "http://localhost:2026"
```

### CI/CD Environment
```bash
# GitHub Actions automatically sets these from secrets
python3 tests/manual/test_deprovision_user.py \
  --db "${{ env.NEXUS_DATABASE_URL }}" \
  --base-url "http://localhost:${{ env.NEXUS_PORT }}"
```

## What It Tests

The integration test verifies the complete user lifecycle:

1. **Cleanup**: Removes any existing test user
2. **Provision**: Creates new user with API keys and directories
3. **Verification**: Confirms all resources were created
4. **Deprovision**: Removes user and all resources
5. **Validation**: Ensures complete cleanup:
   - User record soft-deleted (is_active=0, deleted_at set)
   - All API keys revoked
   - All directories empty
   - Physical directories removed
   - Metadata entries deleted
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
Step 8: Verifying resources are deleted/empty...
  ✓ workspace: empty
  ✓ memory: empty
  ✓ skill: empty
  ✓ agent: empty
  ✓ connector: empty
  ✓ resource: empty

================================================================================
✓ TEST PASSED!
  deprovision_user successfully removed all user data
================================================================================
```

## Integration with Existing Tests

This test complements the existing docker-integration tests:
- **User Permission Grant Test** (line 474) - Tests permission granting
- **Agent Permission Management Test** (line 664) - Tests agent permissions
- **PostgreSQL Auth Integration Test** (line 1626) - Tests authentication

The deprovision test specifically verifies that user cleanup is thorough and complete, ensuring no data leaks when users are removed.
