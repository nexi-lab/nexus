# Admin API Documentation

**Version:** v0.5.1
**Issues:** [#322](https://github.com/nexi-lab/nexus/issues/322) (API), [#266](https://github.com/nexi-lab/nexus/issues/266) (CLI)

## Overview

The Admin API provides secure, remote management of API keys without requiring SSH access to the server. This solves a critical security and operational gap in production deployments.

**Key Benefits:**
- ✅ No SSH access required for user provisioning
- ✅ Remote API key management via HTTP or CLI
- ✅ Secure admin-only endpoints
- ✅ Production-ready security (HMAC-SHA256, expiry, revocation)
- ✅ Beautiful CLI with tables and JSON output

## Two Ways to Use Admin API

### 1. CLI Commands (Recommended) - Issue #266

User-friendly command-line interface with formatted output:

```bash
# Set environment variables
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=<your_admin_key>

# Create a user
nexus admin create-user alice --name "Alice Smith" --expires-days 90

# List users (beautiful table output)
nexus admin list-users
```

**Best for:**
- Interactive use
- Automation scripts
- Day-to-day operations
- Beautiful formatted output

### 2. JSON-RPC API (Lower-level) - Issue #322

Direct HTTP API calls for custom integrations:

```bash
curl -X POST http://localhost:8080/api/nfs/admin_create_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"params":{"user_id":"alice",...}}'
```

**Best for:**
- Custom integrations
- Non-CLI environments
- Programmatic access

---

## Prerequisites

1. **Database-backed authentication** must be enabled:
   ```bash
   export NEXUS_DATABASE_URL="postgresql://postgres:password@localhost/nexus"
   # or SQLite:
   # export NEXUS_DATABASE_URL="sqlite:///path/to/nexus.db"
   ```

2. **Admin API key** must exist:
   ```bash
   python scripts/create-api-key.py admin "Admin Key" --admin --days 365
   ```

3. **Server must be running with database authentication**:
   ```bash
   nexus serve --host 0.0.0.0 --port 8080 --auth-type=database
   ```

   **Note:** The `--auth-type=database` flag is required for admin API functionality.

---

## API Endpoints

All endpoints use JSON-RPC 2.0 protocol:

```
POST /api/nfs/{method_name}
Authorization: Bearer <admin_api_key>
Content-Type: application/json

{
  "jsonrpc": "2.0",
  "id": "request-id",
  "params": { ... }
}
```

### 1. Create API Key

**Endpoint:** `POST /api/nfs/admin_create_key`

**Admin Only:** ✅

**Description:** Create a new API key for a user without SSH access.

**Parameters:**
- `user_id` (string, required): User identifier (e.g., "alice")
- `name` (string, required): Human-readable key name (e.g., "Alice's Laptop")
- `is_admin` (boolean, optional): Grant admin privileges (default: false)
- `expires_days` (integer, optional): Expiry in days from now (default: no expiry)
- `tenant_id` (string, optional): Tenant identifier (default: "default")
- `subject_type` (string, optional): "user" or "agent" (default: "user")
- `subject_id` (string, optional): Custom subject ID (defaults to user_id)

**Example Request:**
```bash
curl -X POST http://localhost:8080/api/nfs/admin_create_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "params": {
      "user_id": "alice",
      "name": "Alice Laptop",
      "is_admin": false,
      "expires_days": 90
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1",
    "api_key": "sk-default_alice_cd01ee6c_...",
    "user_id": "alice",
    "name": "Alice Laptop",
    "subject_type": "user",
    "subject_id": "alice",
    "tenant_id": "default",
    "is_admin": false,
    "expires_at": "2026-01-27T18:39:29Z"
  }
}
```

**⚠️ IMPORTANT:** The `api_key` field is **only returned once** and cannot be retrieved again. Save it immediately!

---

### 2. List API Keys

**Endpoint:** `POST /api/nfs/admin_list_keys`

**Admin Only:** ✅

**Description:** List API keys with optional filtering and pagination.

**Parameters:**
- `user_id` (string, optional): Filter by user
- `tenant_id` (string, optional): Filter by tenant
- `is_admin` (boolean, optional): Filter by admin status
- `include_revoked` (boolean, optional): Include revoked keys (default: false)
- `include_expired` (boolean, optional): Include expired keys (default: false)
- `limit` (integer, optional): Max results (default: 100)
- `offset` (integer, optional): Pagination offset (default: 0)

**Example Request:**
```bash
curl -X POST http://localhost:8080/api/nfs/admin_list_keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "params": {
      "user_id": "alice"
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "keys": [
      {
        "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1",
        "user_id": "alice",
        "subject_type": "user",
        "subject_id": "alice",
        "name": "Alice Laptop",
        "tenant_id": "default",
        "is_admin": false,
        "created_at": "2025-10-29T18:39:29Z",
        "expires_at": "2026-01-27T18:39:29Z",
        "revoked": false,
        "revoked_at": null,
        "last_used_at": "2025-10-29T20:15:00Z"
      }
    ],
    "total": 1
  }
}
```

---

### 3. Get API Key Details

**Endpoint:** `POST /api/nfs/admin_get_key`

**Admin Only:** ✅

**Description:** Get detailed information about a specific API key.

**Parameters:**
- `key_id` (string, required): Key ID to retrieve

**Example Request:**
```bash
curl -X POST http://localhost:8080/api/nfs/admin_get_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "params": {
      "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1"
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1",
    "user_id": "alice",
    "subject_type": "user",
    "subject_id": "alice",
    "name": "Alice Laptop",
    "tenant_id": "default",
    "is_admin": false,
    "created_at": "2025-10-29T18:39:29Z",
    "expires_at": "2026-01-27T18:39:29Z",
    "revoked": false,
    "revoked_at": null,
    "last_used_at": "2025-10-29T20:15:00Z"
  }
}
```

---

### 4. Update API Key

**Endpoint:** `POST /api/nfs/admin_update_key`

**Admin Only:** ✅

**Description:** Update API key properties (expiry, admin status, name).

**Parameters:**
- `key_id` (string, required): Key ID to update
- `expires_days` (integer, optional): New expiry in days from now
- `is_admin` (boolean, optional): Change admin status
- `name` (string, optional): Update key name

**Safety Features:**
- ✅ Prevents removing admin from last admin key
- ✅ Atomic updates with transaction rollback on error

**Example Request:**
```bash
curl -X POST http://localhost:8080/api/nfs/admin_update_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 4,
    "params": {
      "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1",
      "expires_days": 180,
      "name": "Alice Updated Key"
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1",
    "user_id": "alice",
    "name": "Alice Updated Key",
    "expires_at": "2026-04-27T18:39:29Z",
    ...
  }
}
```

---

### 5. Revoke API Key

**Endpoint:** `POST /api/nfs/admin_revoke_key`

**Admin Only:** ✅

**Description:** Immediately revoke an API key (cannot be undone).

**Parameters:**
- `key_id` (string, required): Key ID to revoke

**Example Request:**
```bash
curl -X POST http://localhost:8080/api/nfs/admin_revoke_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 5,
    "params": {
      "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1"
    }
  }'
```

**Example Response:**
```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "success": true,
    "key_id": "d6f5e137-5fce-4e06-9432-6e30324dfad1"
  }
}
```

---

## CLI Commands (v0.5.1+)

### Overview

The Nexus CLI provides user-friendly commands that wrap the Admin API endpoints. All commands support both interactive use and JSON output for automation.

**Command Group:** `nexus admin`

**Global Options:**
- `--remote-url <url>` - Server URL (or set `NEXUS_URL`)
- `--remote-api-key <key>` - Admin API key (or set `NEXUS_API_KEY`)
- `--json-output` - Output as JSON instead of formatted tables

### Setup

```bash
# Set environment variables (recommended)
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=<your_admin_key>

# Or use flags with each command
nexus admin list-users --remote-url http://localhost:8080 --remote-api-key <key>
```

### Command Reference

#### 1. Create User

**Command:** `nexus admin create-user <user_id> --name <name> [options]`

**Description:** Create a new user and generate their API key.

**Options:**
- `--name <text>` (required) - Human-readable key name
- `--email <text>` - User email (for documentation)
- `--is-admin` - Grant admin privileges
- `--expires-days <int>` - Key expiry in days
- `--tenant-id <text>` - Tenant ID (default: "default")
- `--subject-type <text>` - Subject type: "user" or "agent"
- `--json-output` - Output as JSON

**Examples:**

```bash
# Create regular user with 90-day expiry
nexus admin create-user alice --name "Alice Smith" --expires-days 90

# Create admin user
nexus admin create-user admin --name "Admin Key" --is-admin

# Create agent key
nexus admin create-user bot1 --name "Bot Agent" --subject-type agent

# JSON output for automation
nexus admin create-user charlie --name "Charlie" --json-output
```

**Output:**

```
✓ User created successfully

⚠ Save this API key - it will only be shown once!

User ID:     alice
Key ID:      d6f5e137-5fce-4e06-9432-6e30324dfad1
API Key:     sk-default_alice_cd01ee6c_...
Tenant:      default
Admin:       False
Expires:     2026-01-28T18:39:29Z
```

#### 2. List Users

**Command:** `nexus admin list-users [options]`

**Description:** List all users with their API keys.

**Options:**
- `--user-id <text>` - Filter by user ID
- `--tenant-id <text>` - Filter by tenant ID
- `--is-admin` - Show only admin keys
- `--include-revoked` - Include revoked keys
- `--include-expired` - Include expired keys
- `--limit <int>` - Max results (default: 100)
- `--json-output` - Output as JSON

**Examples:**

```bash
# List all active users
nexus admin list-users

# List keys for specific user
nexus admin list-users --user-id alice

# List admin keys only
nexus admin list-users --is-admin

# Include revoked and expired keys
nexus admin list-users --include-revoked --include-expired

# JSON output
nexus admin list-users --json-output
```

**Output (Table):**

```
                               API Keys (3 total)
┏━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┓
┃ User ID ┃ Name       ┃ Key ID     ┃ Admin ┃ Created    ┃ Expires    ┃ Status ┃
┡━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━┩
│ admin   │ Admin Key  │ 480d353f-… │ ✓     │ 2025-10-30 │ 2026-10-30 │ Active │
│ alice   │ Alice      │ e4b8f69e-… │       │ 2025-10-30 │ 2026-01-28 │ Active │
│ bob     │ Bob Admin  │ 7b1e2f75-… │ ✓     │ 2025-10-30 │ Never      │ Active │
└─────────┴────────────┴────────────┴───────┴────────────┴────────────┴────────┘
```

#### 3. Get User Details

**Command:** `nexus admin get-user --user-id <id> | --key-id <id> [options]`

**Description:** Get detailed information about a user or API key.

**Options:**
- `--user-id <text>` - Look up by user ID
- `--key-id <text>` - Look up by key ID
- `--json-output` - Output as JSON

**Examples:**

```bash
# Get by user ID
nexus admin get-user --user-id alice

# Get by key ID
nexus admin get-user --key-id d6f5e137-5fce-4e06-9432-6e30324dfad1

# JSON output
nexus admin get-user --user-id alice --json-output
```

**Output:**

```
User Information

User ID:      alice
Key ID:       d6f5e137-5fce-4e06-9432-6e30324dfad1
Name:         Alice Smith
Tenant:       default
Admin:        False
Created:      2025-10-30T08:08:28.289236
Expires:      2026-01-28T08:08:28.288762
Last Used:    2025-10-30T10:15:00.123456
Revoked:      False
Subject Type: user
Subject ID:   alice
```

#### 4. Create Additional Key

**Command:** `nexus admin create-key <user_id> --name <name> [options]`

**Description:** Create an additional API key for an existing user.

**Options:**
- `--name <text>` (required) - Human-readable key name
- `--expires-days <int>` - Key expiry in days
- `--json-output` - Output as JSON

**Examples:**

```bash
# Create second key for alice
nexus admin create-key alice --name "Alice's Laptop" --expires-days 90

# Create key with no expiry
nexus admin create-key alice --name "Alice's Server"
```

**Output:**

```
✓ API key created successfully

⚠ Save this API key - it will only be shown once!

User ID:     alice
Key ID:      5f511ba4-0dad-4695-a50d-e9bb2866150f
API Key:     sk-default_alice_c2fca930_...
Expires:     2025-11-29T08:08:32.638821
```

#### 5. Revoke Key

**Command:** `nexus admin revoke-key <key_id> [options]`

**Description:** Revoke an API key immediately (cannot be undone).

**Options:**
- `--json-output` - Output as JSON

**Examples:**

```bash
# Revoke a key
nexus admin revoke-key d6f5e137-5fce-4e06-9432-6e30324dfad1

# With JSON output
nexus admin revoke-key d6f5e137-5fce-4e06-9432-6e30324dfad1 --json-output
```

**Output:**

```
✓ API key revoked successfully
Key ID: d6f5e137-5fce-4e06-9432-6e30324dfad1
```

#### 6. Update Key

**Command:** `nexus admin update-key <key_id> [options]`

**Description:** Update API key settings (expiry, admin status).

**Options:**
- `--expires-days <int>` - Extend expiry by days from now
- `--is-admin <bool>` - Change admin status (true/false)
- `--json-output` - Output as JSON

**Examples:**

```bash
# Extend expiry by 180 days
nexus admin update-key d6f5e137-5fce-4e06-9432-6e30324dfad1 --expires-days 180

# Grant admin privileges
nexus admin update-key d6f5e137-5fce-4e06-9432-6e30324dfad1 --is-admin true

# Revoke admin privileges
nexus admin update-key d6f5e137-5fce-4e06-9432-6e30324dfad1 --is-admin false
```

**Output:**

```
✓ API key updated successfully
Key ID: d6f5e137-5fce-4e06-9432-6e30324dfad1
New expiry: 2026-04-28T18:39:29Z
Admin: True
```

### CLI Automation Example

The CLI commands work great in shell scripts:

```bash
#!/bin/bash
# Automated user provisioning script

export NEXUS_URL="http://nexus-server:8080"
export NEXUS_API_KEY="<admin_key>"

# Create users from CSV
while IFS=',' read -r username fullname email; do
  echo "Creating user: $username"

  # Create user and capture JSON output
  result=$(nexus admin create-user "$username" \
    --name "$fullname" \
    --email "$email" \
    --expires-days 90 \
    --json-output)

  # Extract API key from JSON
  api_key=$(echo "$result" | jq -r '.api_key')

  # Send key to user via secure channel
  echo "User: $username, Key: $api_key" >> keys.txt
done < users.csv

echo "✓ All users created"
```

### CLI Testing

Run the comprehensive demo to see all commands in action:

```bash
# Run full CLI demo with automatic cleanup
./examples/cli/admin_cli_demo.sh

# Keep resources for manual testing
KEEP=1 ./examples/cli/admin_cli_demo.sh
```

See [examples/cli/README_ADMIN_CLI.md](../examples/cli/README_ADMIN_CLI.md) for detailed CLI documentation.

---

## Security Considerations

### ✅ What's Secure

1. **Admin-only enforcement**: All endpoints check `is_admin=true` flag
2. **One-time key display**: Raw API keys never shown after creation
3. **No hash exposure**: `key_hash` never returned in responses
4. **Secure key generation**: HMAC-SHA256 with salt (not raw SHA-256)
5. **Atomic operations**: Database transactions ensure consistency
6. **Last admin protection**: Cannot remove admin from last admin key

### ⚠️ Best Practices

1. **Use expiry dates**: Set `expires_days` for all keys (recommended: 90 days)
2. **Rotate admin keys**: Create new admin keys periodically
3. **Monitor usage**: Check `last_used_at` timestamps regularly
4. **Revoke unused keys**: Revoke keys that haven't been used in 90+ days
5. **Secure admin keys**: Store admin keys in secrets manager (Vault, 1Password, etc.)

### 🚫 Security Warnings

- **Never commit admin keys to git**
- **Never log or expose admin keys in error messages**
- **Never share admin keys via email or Slack**
- **Rotate immediately if admin key is compromised**

---

## Error Handling

### Admin Permission Denied

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "error": {
    "code": -32004,
    "message": "Admin privileges required for this operation"
  }
}
```

### Key Not Found

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "error": {
    "code": -32000,
    "message": "API key not found: d6f5e137-..."
  }
}
```

### Last Admin Protection

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32005,
    "message": "Cannot remove admin privileges from the last admin key"
  }
}
```

---

## Migration Guide

### Before (SSH Required) ❌

The old way required SSH access to the production server:

```bash
# Step 1: SSH to server
ssh nexus-server

# Step 2: Switch to nexus user
sudo su - nexus

# Step 3: Run script directly on server
cd /opt/nexus
export NEXUS_DATABASE_URL="postgresql://..."
python scripts/create-api-key.py alice "Alice Key" --days 90
```

**Problems:**
- ❌ Requires SSH access (security risk)
- ❌ Requires server file system access
- ❌ Manual process, error-prone
- ❌ Difficult to audit
- ❌ Cannot be automated easily

### After (Remote CLI) ✅ Recommended

The new way uses the admin CLI remotely:

```bash
# One-time setup
export NEXUS_URL=http://nexus-server:8080
export NEXUS_API_KEY=<your_admin_key>

# Create user (no SSH needed!)
nexus admin create-user alice --name "Alice Key" --expires-days 90

# List users
nexus admin list-users

# Revoke key if compromised
nexus admin revoke-key <key_id>
```

**Benefits:**
- ✅ No SSH access required
- ✅ Works from any machine with CLI installed
- ✅ Beautiful formatted output
- ✅ Easy to automate
- ✅ Full audit trail via API logs

### After (Remote API) - For Custom Integrations

For custom scripts or non-CLI environments:

```bash
# Admin calls API remotely (no SSH)
curl -X POST http://nexus-server/api/nfs/admin_create_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "params": {
      "user_id": "alice",
      "name": "Alice Key",
      "expires_days": 90
    }
  }'
```

---

## Testing

### CLI Commands Demo (Recommended)

Test all CLI commands with beautiful output:

```bash
# Run comprehensive CLI demo with automatic cleanup
./examples/cli/admin_cli_demo.sh

# Keep resources for manual testing
KEEP=1 ./examples/cli/admin_cli_demo.sh
```

**What it tests:**
- ✅ All 6 CLI commands (`create-user`, `list-users`, `get-user`, `create-key`, `revoke-key`, `update-key`)
- ✅ Multiple user types (regular, admin, agent)
- ✅ Table and JSON output formats
- ✅ Filtering and pagination
- ✅ Key revocation workflow
- ✅ Key updates and expiry management

**Output includes:**
- Beautiful color-coded tables
- JSON output examples
- Command reference guide
- Migration examples
- Comprehensive test summary

See the [CLI Demo README](../examples/cli/README_ADMIN_CLI.md) for details.

### API Endpoints Demo

For testing the underlying JSON-RPC API:

```bash
# Run complete API demo with automatic cleanup
./examples/cli/admin_api_demo.sh

# Keep resources for manual inspection
KEEP=1 ./examples/cli/admin_api_demo.sh
```

**What it tests:**
- All 5 Admin API endpoints via `curl`
- Authentication verification
- Key revocation testing
- Before/after comparisons
- Error handling

### Manual Testing

#### With CLI

If you have an existing server:

```bash
# Set up environment
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=<your_admin_key>

# Test commands
nexus admin create-user testuser --name "Test User"
nexus admin list-users
nexus admin get-user --user-id testuser
```

#### With API

```bash
# With existing server and admin key
ADMIN_KEY="sk-default_admin_..." curl -X POST http://localhost:8080/api/nfs/admin_list_keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"params":{}}'
```

#### From Scratch

```bash
# Start server
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"
nexus serve --host 0.0.0.0 --port 8080 --auth-type=database

# In another terminal: create admin key
python scripts/create-api-key.py admin "Admin Key" --admin

# Use the key
export NEXUS_API_KEY="<generated_key>"
nexus admin list-users
```

---

## Roadmap

### Phase 1: Admin-Managed (✅ v0.5.1 - This Release)
- Admin creates all users via API
- Default: 90-day expiry keys
- Username-based user IDs
- Single-tenant (hard-coded "default")

### Phase 2: Invitation-Based (Future)
- Admin creates invitation tokens
- Users self-register with invite code
- Email delivery of invitations

### Phase 3: SSO/OAuth (Future)
- Auto-provisioning on first login
- Support Google, GitHub, etc.

### Phase 4: Multi-Tenant (Future)
- Tenant-scoped user management
- Cross-tenant admin permissions

---

## Troubleshooting

### "Server cannot use RemoteNexusFS (circular dependency detected)"

**Problem:** You have `NEXUS_URL` environment variable set.

**Solution:**
```bash
unset NEXUS_URL
nexus serve --host 0.0.0.0 --port 8080 --auth-type=database
```

The demo scripts automatically unset this for you.

### "Admin privileges required for this operation"

**Problem:** Using a non-admin API key.

**Solution:**
```bash
# Create a new admin key
python scripts/create-api-key.py admin "Admin Key" --admin

# Or update existing key to admin (if you have another admin key)
curl -X POST http://localhost:8080/api/nfs/admin_update_key \
  -H "Authorization: Bearer $OTHER_ADMIN_KEY" \
  -d '{"params": {"key_id": "<key_id>", "is_admin": true}}'
```

### "Database auth provider not configured"

**Problem:** Server not started with `--auth-type=database`.

**Solution:**
```bash
# Make sure to include --auth-type=database flag
nexus serve --host 0.0.0.0 --port 8080 --auth-type=database
```

### "Port already in use"

**Problem:** Previous server still running.

**Solution:**
```bash
pkill -f "nexus serve"
sleep 2
nexus serve ...
```

---

## Related Documentation

- [Authentication Guide](./authentication.md)
- [API Key Security Best Practices](./security.md)
- [Database Setup](./database.md)

---

## Support

- GitHub Issues:
  - [#322](https://github.com/nexi-lab/nexus/issues/322) - Admin API (Backend)
  - [#266](https://github.com/nexi-lab/nexus/issues/266) - Admin CLI Commands
- Demo Scripts:
  - [`admin_cli_demo.sh`](../examples/cli/admin_cli_demo.sh) - CLI Commands Demo
  - [`admin_api_demo.sh`](../examples/cli/admin_api_demo.sh) - API Endpoints Demo
- Documentation:
  - [CLI Demo README](../examples/cli/README_ADMIN_CLI.md)
  - Main docs: https://docs.nexus.ai
