# Nexus Quickstart Guide

A practical step-by-step guide to set up and use Nexus with permissions and authentication.

**💡 Pro Tip:** This guide uses environment variables (`NEXUS_URL` and `NEXUS_API_KEY`) so you don't need to pass `--remote-url` or `--remote-api-key` flags with every command. Set them once and all commands work automatically!

## Prerequisites

- PostgreSQL running (Docker or Homebrew)
- Nexus installed: `pip install nexus-ai-fs`
- Python 3 (for API key generation)

## Choose Your Setup

### For Learning/Development (Insecure)
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus.sh
```
⚠️ **No authentication** - Anyone can claim any identity. Use `NEXUS_SUBJECT` to impersonate users.

### For Production (Secure) ✅ Recommended
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus-with-auth.sh
```
✅ **With authentication** - Uses secure API keys. Server validates user identity.

**This guide uses the production setup** for realistic examples.

## Step 1: Start Nexus Server with Authentication

```bash
# Set database URL
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# Run the init script with auth (creates admin API key)
./scripts/init-nexus-with-auth.sh
```

**Output:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: Save this API key securely!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Admin API Key: sk-admin_a1b2c3d4_e5f6g7h8...

Add to your ~/.bashrc or ~/.zshrc:
  export NEXUS_API_KEY='sk-admin_a1b2c3d4_e5f6g7h8...'
  export NEXUS_URL='http://localhost:8080'
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

**Save your admin API key!** You'll need it for all commands.

```bash
# Load admin credentials (sets NEXUS_API_KEY and NEXUS_URL)
source .nexus-admin-env

# Or set manually
export NEXUS_API_KEY='sk-admin_...'  # Use your actual key
export NEXUS_URL='http://localhost:8080'
```

**💡 Tip:** Once you set `NEXUS_URL` and `NEXUS_API_KEY`, all Nexus CLI commands will use them automatically - no need to pass `--remote-url` or `--remote-api-key` flags!

Server is now running on `http://localhost:8080` with:
- Admin user: `admin` (has API key)
- Workspace: `/workspace` (admin owns it)
- Default namespaces: `file`, `group`, `memory` (auto-registered)
- Authentication: Database-backed API keys

## Step 2: Verify Setup

**Open a NEW terminal** (keep the server running in the first terminal).

```bash
# IMPORTANT: Load admin credentials (sets NEXUS_API_KEY and NEXUS_URL)
source .nexus-admin-env

# Verify the environment variables are set
echo $NEXUS_API_KEY   # Should show: sk-admin_...
echo $NEXUS_URL       # Should show: http://localhost:8080

# Verify server is running
curl -s http://localhost:8080/health
# Output: {"status": "healthy", "service": "nexus-rpc"}

# Test authenticated access (automatically uses NEXUS_API_KEY and NEXUS_URL from environment)
nexus rebac namespace-list
# Shows: file, group, memory
```

**Important:** All subsequent commands automatically use `NEXUS_API_KEY` and `NEXUS_URL` from your environment - no need to pass flags!

## Step 3: Create Directory Structure

```bash
# IMPORTANT: Load admin credentials first!
source .nexus-admin-env

# Verify environment variables are set
echo $NEXUS_API_KEY  # Should show: sk-admin_... or sk-default_admin_...
echo $NEXUS_URL      # Should show: http://localhost:8080

# The CLI automatically uses NEXUS_API_KEY and NEXUS_URL from environment
# No need to pass --remote-url or --remote-api-key flags!

# Create project directories (automatically uses admin API key from environment)
nexus mkdir /workspace/project1
nexus mkdir /workspace/project2
nexus mkdir /workspace/shared

echo "✓ Created project directories"
```

**How authentication works:**
1. The `NEXUS_API_KEY` and `NEXUS_URL` environment variables are **automatically** used by all CLI commands
   - No need to pass `--remote-api-key` or `--remote-url` flags
   - The CLI adds the API key to the `Authorization: Bearer <key>` header
2. Server validates the API key against the database
3. Server extracts your identity from the API key (e.g., "user:admin")
4. Admin users have `is_admin=true`, which bypasses permission checks
5. **Without** `NEXUS_API_KEY` set, you're treated as "anonymous" → Access denied!

## Step 4: Create Users and Grant Permissions

### Create API Keys for Users

```bash
# Create API key for alice (regular user, 90 days)
python3 scripts/create-api-key.py alice "Alice's key" --days 90
# Output: API Key: sk-alice_abc123_...

# Create API key for bob (regular user, 90 days)
python3 scripts/create-api-key.py bob "Bob's key" --days 90
# Output: API Key: sk-bob_def456_...

# Create API key for charlie (regular user, 90 days)
python3 scripts/create-api-key.py charlie "Charlie's key" --days 90
# Output: API Key: sk-charlie_ghi789_...

# Create API key for dave (regular user, 90 days)
python3 scripts/create-api-key.py dave "Dave's key" --days 90
# Output: API Key: sk-dave_jkl012_...
```

**Save these keys!** Give each user their API key securely (don't share in plaintext).

### Grant Permissions to Users

Now use your admin API key to grant permissions:

```bash
# Make sure you're using admin key
source .nexus-admin-env

# Alice - owner of project1
nexus rebac create user alice direct_owner file /workspace/project1 --tenant-id default
echo "✓ Alice owns /workspace/project1"

# Bob - editor of project1 (can read/write, but not manage permissions)
nexus rebac create user bob direct_editor file /workspace/project1 --tenant-id default
echo "✓ Bob is editor of /workspace/project1"

# Charlie - viewer of project1 (read-only)
nexus rebac create user charlie direct_viewer file /workspace/project1 --tenant-id default
echo "✓ Charlie can view /workspace/project1"

# Dave - owner of project2 (completely isolated from project1)
nexus rebac create user dave direct_owner file /workspace/project2 --tenant-id default
echo "✓ Dave owns /workspace/project2"
```

## Step 5: Demonstrate Permission Checks

### Alice (Owner) - Full Access

```bash
# Check alice's write permission
nexus rebac check user alice write file /workspace/project1
# Output: ✓ GRANTED

# Explain why
nexus rebac explain user alice write file /workspace/project1
# Shows: direct_owner → owner → write
```

### Bob (Editor) - Can Read/Write

```bash
# Check bob's write permission
nexus rebac check user bob write file /workspace/project1
# Output: ✓ GRANTED

# Check bob's execute permission (owner-only)
nexus rebac check user bob execute file /workspace/project1
# Output: ✗ DENIED
```

### Charlie (Viewer) - Read Only

```bash
# Check charlie's read permission
nexus rebac check user charlie read file /workspace/project1
# Output: ✓ GRANTED

# Check charlie's write permission
nexus rebac check user charlie write file /workspace/project1
# Output: ✗ DENIED
```

### Dave - Isolated (Cannot Access project1)

```bash
# Check dave's access to project1
nexus rebac check user dave read file /workspace/project1
# Output: ✗ DENIED

# But dave owns project2
nexus rebac check user dave write file /workspace/project2
# Output: ✓ GRANTED
```

## Step 6: Demonstrate Access Denied

```bash
# First, create a test file as alice (owner)
export NEXUS_API_KEY='sk-alice_...'  # Use alice's actual key
nexus write /workspace/project1/test.txt "Alice's data"

# Try to write as charlie (viewer - should fail)
export NEXUS_API_KEY='sk-charlie_...'  # Use charlie's actual key
nexus write /workspace/project1/test.txt "Charlie's attempt"
# Output: Error: Permission denied

# Charlie can read (has viewer permission)
nexus cat /workspace/project1/test.txt
# Output: Alice's data

# Try to read project1 as dave (should fail - dave has no access to project1)
export NEXUS_API_KEY='sk-dave_...'  # Use dave's actual key
nexus cat /workspace/project1/test.txt
# Output: Error: Permission denied
```

## Step 7: Demonstrate Permission Inheritance

```bash
# Create subdirectory under project1
nexus mkdir /workspace/project1/docs

# Add parent relation (so permissions inherit)
nexus rebac create file /workspace/project1/docs parent file /workspace/project1 --tenant-id default

# Now bob (editor of project1) can also write to docs/
nexus rebac check user bob write file /workspace/project1/docs
# Output: ✓ GRANTED (inherited through parent_editor)

# Explain the inheritance
nexus rebac explain user bob write file /workspace/project1/docs
# Shows: parent → parent_editor → editor → write
```

## Step 8: Update Permissions

```bash
# Promote charlie from viewer to editor
# First, remove viewer permission
nexus rebac delete user charlie direct_viewer file /workspace/project1 --tenant-id default

# Then grant editor permission
nexus rebac create user charlie direct_editor file /workspace/project1 --tenant-id default

# Verify charlie can now write
nexus rebac check user charlie write file /workspace/project1
# Output: ✓ GRANTED

echo "✓ Charlie promoted to editor"
```

## Step 9: List All Permissions

```bash
# See everyone who has write access to project1
nexus rebac expand write file /workspace/project1 --tenant-id default
# Output: Lists alice, bob, charlie

# List all tuples for project1
nexus rebac list --object file:/workspace/project1 --tenant-id default
# Shows all permission tuples
```

## Understanding the Security Model

This guide uses **API key authentication** which is production-ready:

### How It Works

1. **Admin creates user API keys**
   ```bash
   python3 scripts/create-api-key.py alice "Alice's key" --days 90
   # Output: sk-alice_abc123_...
   ```

2. **User sets their API key**
   ```bash
   export NEXUS_API_KEY='sk-alice_abc123_...'
   ```

3. **Server validates identity**
   - Client sends API key in request
   - Server looks up key in database
   - Server resolves to user identity (e.g., "user:alice")
   - Server checks permissions for that user

### Alternative: Development Mode (Insecure)

If you used `./scripts/init-nexus.sh` instead (no auth):
- Uses `NEXUS_SUBJECT` header
- ⚠️ Anyone can claim any identity
- ❌ Never use in production

```bash
# Insecure mode example
export NEXUS_SUBJECT="user:alice"
export NEXUS_URL="http://localhost:8080"
nexus ls /workspace
# Server blindly trusts you're alice!
```

### Security Comparison

| Aspect | Development Mode | Production Mode (This Guide) |
|--------|-----------------|------------------------------|
| **Setup** | `init-nexus.sh` | `init-nexus-with-auth.sh` ✅ |
| **User identity** | `NEXUS_SUBJECT="user:alice"` | `NEXUS_API_KEY="sk-alice_..."` |
| **Server validates** | ❌ No | ✅ Yes |
| **Can impersonate** | ✅ Yes | ❌ No |
| **Production ready** | ❌ Never | ✅ Yes |

## Summary

You've now learned:
- ✅ Start Nexus server with admin user (`init-nexus.sh`)
- ✅ Register ReBAC namespace with inheritance
- ✅ Create users with different permission levels (owner, editor, viewer)
- ✅ Check permissions (`rebac check`)
- ✅ Understand permission paths (`rebac explain`)
- ✅ See access denied cases
- ✅ Use permission inheritance (parent relations)
- ✅ Update permissions (promote/demote users)
- ✅ List who has access (`rebac expand`)
- ✅ Add API key authentication for production

## Next Steps

- **Authentication deep dive**: See `examples/auth_demo/CLI_AUTH_GUIDE.md`
- **Database-backed keys**: See `examples/auth_demo/database_auth_demo.sh`
- Mount external backends: See `docs/api/mounts.md`
- Multi-tenant setup: See `docs/SERVER_SETUP_CLI_GUIDE.md`
- API reference: See `docs/api/permissions.md`
