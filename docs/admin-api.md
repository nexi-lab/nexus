# Admin API Documentation

**Version:** v0.5.1
**Issue:** [#322](https://github.com/nexi-lab/nexus/issues/322)

## Overview

The Admin API provides secure, remote management of API keys without requiring SSH access to the server. This solves a critical security and operational gap in production deployments.

**Key Benefits:**
- No SSH access required for user provisioning
- Remote API key management via HTTP
- Secure admin-only endpoints
- Production-ready security (HMAC-SHA256, expiry, revocation)

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

3. **Server must be running**:
   ```bash
   nexus serve --host 0.0.0.0 --port 8080
   ```

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

### Before (SSH Required)

```bash
# Admin must SSH to server
ssh nexus-server

# Run script directly on server
export NEXUS_DATABASE_URL="postgresql://..."
python scripts/create-api-key.py alice "Alice Key" --days 90
```

### After (Remote API)

```bash
# Admin calls API remotely (no SSH)
curl -X POST http://nexus-server/api/nfs/admin_create_key \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"params": {"user_id": "alice", "name": "Alice Key", "expires_days": 90}}'
```

---

## Testing

### Quick Start (One Command!)

```bash
# Automated testing - sets up everything and runs tests
./examples/cli/test_admin_api.sh --auto
```

This automatically:
1. Creates a temporary SQLite database
2. Starts the Nexus server
3. Creates an admin API key
4. Tests `admin_create_key` and `admin_list_keys`
5. Cleans up resources

### Comprehensive Demo

For a full demonstration of all 5 endpoints with detailed output:

```bash
# Run complete demo with automatic cleanup
./examples/cli/admin_api_demo.sh

# Keep resources for manual inspection
KEEP=1 ./examples/cli/admin_api_demo.sh
```

This script demonstrates:
- All 5 Admin API endpoints
- Authentication verification
- Key revocation testing
- Before/after comparisons

### Manual Testing

If you have an existing server:

```bash
# With existing server and admin key
ADMIN_KEY="sk-default_admin_..." ./examples/cli/test_admin_api.sh

# Or set up from scratch
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"
nexus serve --host 0.0.0.0 --port 8080 --auth-type=database
python scripts/create-api-key.py admin "Admin Key" --admin
ADMIN_KEY="<key>" ./examples/cli/test_admin_api.sh
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

- GitHub Issue: https://github.com/nexi-lab/nexus/issues/322
- Documentation: https://docs.nexus.ai
