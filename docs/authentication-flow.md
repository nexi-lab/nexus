# Authentication Flow: Server Auth + OAuth Backends

## Overview

Nexus has **two independent but correlated authentication systems**:

1. **Server Authentication** - Who can access Nexus API?
2. **OAuth Authentication** - Which cloud storage can they access?

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         User Request                            │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
         ┌────────────────────────┐
         │  Server Authentication │  ← API Key / JWT / OIDC
         │  (Who are you?)        │
         └────────────┬───────────┘
                      │
                      ▼ Identifies user_id
         ┌────────────────────────┐
         │  Authorization Check   │  ← ReBAC permissions
         │  (Can you do this?)    │
         └────────────┬───────────┘
                      │
                      ▼ Allowed
         ┌────────────────────────┐
         │  Backend Routing       │  ← Path router
         │  (Which backend?)      │
         └────────────┬───────────┘
                      │
        ┌─────────────┴─────────────┐
        │                           │
        ▼                           ▼
  ┌──────────┐              ┌──────────────┐
  │  Local   │              │ Google Drive │
  │ Backend  │              │  Connector   │
  └──────────┘              └──────┬───────┘
                                   │
                                   ▼
                      ┌────────────────────────┐
                      │  OAuth Token Lookup    │  ← user_id → OAuth token
                      │  (TokenManager)        │
                      └────────────┬───────────┘
                                   │
                                   ▼ Get/refresh token
                      ┌────────────────────────┐
                      │  Google Drive API      │
                      │  (User's personal      │
                      │   Google Drive)        │
                      └────────────────────────┘
```

## User-ID Correlation

The key is that **user_id correlates both systems**:

```python
# 1. Server Auth identifies user
user_id = "alice@example.com"  # From API key lookup

# 2. OAuth Token Manager uses same user_id
oauth_token = token_manager.get_valid_token(
    provider="google",
    user_email=user_id,  # Same user_id!
    tenant_id=context.tenant_id
)

# 3. Auto-refresh happens transparently
# If token expired, TokenManager refreshes it automatically
```

## Complete Workflow Example

### Setup (One-Time)

```bash
# 1. Start Nexus server with database auth
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
nexus serve --auth-type database --init --port 8080

# Output:
# ✓ Admin user created
# ✓ Admin API key created: sk_a1b2c3d4e5f6...
# Save this API key!

# 2. Setup OAuth for Alice's Google Drive (one-time per user)
export GOOGLE_CLIENT_ID="123.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="GOCSPX-..."

nexus oauth setup-gdrive \
    --client-id "$GOOGLE_CLIENT_ID" \
    --client-secret "$GOOGLE_CLIENT_SECRET" \
    --user-email "alice@example.com"

# Browser opens → Alice grants permission → Tokens stored encrypted

# 3. Create API key for Alice (admin operation)
nexus admin create-user alice@example.com --name "Alice's Key"

# Output:
# ✓ API key created: sk_alice_xyz123...
```

### Database State After Setup

```sql
-- api_keys table (Server Auth)
key_id | key_hash           | user_id           | name
-------|--------------------|--------------------|-------------
uuid1  | hmac_hash_of_key1  | admin             | Admin Key
uuid2  | hmac_hash_of_key2  | alice@example.com | Alice's Key

-- oauth_credentials table (OAuth Auth)
credential_id | provider | user_email        | encrypted_access_token | encrypted_refresh_token
--------------|----------|-------------------|------------------------|------------------------
uuid3         | google   | alice@example.com | encrypted_token_abc    | encrypted_refresh_xyz

-- CORRELATION: user_id (alice@example.com) links both tables!
```

### Usage (Every Request)

```python
from nexus.remote import RemoteNexusFS
from nexus.core.permissions import OperationContext

# Alice connects with her API key
nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk_alice_xyz123..."  # Alice's API key
)

# Server authenticates the request:
# 1. Validates API key → identifies user_id = "alice@example.com"
# 2. Creates OperationContext with user_id

# Alice writes to Google Drive mount
nx.write("/gdrive/workspace/file.txt", b"Hello from Alice's Drive!")

# Backend flow:
# 1. Router: /gdrive → GoogleDriveConnectorBackend
# 2. Backend: Needs OAuth token for user_id = "alice@example.com"
# 3. TokenManager.get_valid_token("google", "alice@example.com")
#    - Lookup in oauth_credentials table
#    - Check if expired
#    - If expired: Auto-refresh using refresh_token
#    - Return valid access_token
# 4. Use access_token to call Google Drive API
# 5. File written to Alice's personal Google Drive

# ✅ No manual OAuth flow needed!
# ✅ Tokens refresh automatically!
# ✅ Alice only needs her API key!
```

## Key Points

### ✅ One-Time OAuth Setup

```bash
# Alice does this ONCE (ever)
nexus oauth setup-gdrive \
    --client-id "$GOOGLE_CLIENT_ID" \
    --client-secret "$GOOGLE_CLIENT_SECRET" \
    --user-email "alice@example.com"
```

After this:
- OAuth tokens stored encrypted in database
- Linked to user_email = "alice@example.com"
- **Never need to re-authenticate** (tokens refresh automatically)

### ✅ Every Request Uses API Key

```python
# Alice uses her API key for every request
nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk_alice_xyz123..."  # This identifies Alice
)

# Server knows: "This is Alice (alice@example.com)"
# Backend looks up: "Get OAuth token for alice@example.com"
# TokenManager: Auto-refreshes if needed
# Google Drive: Accessed with fresh token
```

### ✅ Automatic Token Refresh

```python
# In GoogleDriveConnectorBackend._get_drive_service():

access_token = await self.token_manager.get_valid_token(
    provider="google",
    user_email=context.user_id,  # "alice@example.com"
    tenant_id=context.tenant_id,
)

# Inside TokenManager.get_valid_token():
# 1. Retrieve credential from database
# 2. Decrypt tokens
# 3. Check if expired
# 4. If expired and refresh_token exists:
#    - Call Google OAuth refresh endpoint
#    - Get new access_token
#    - Update database
#    - Log audit event
# 5. Return valid access_token

# ✅ All automatic - no user interaction needed!
```

## Multi-User Example

### Setup

```bash
# Setup OAuth for multiple users
nexus oauth setup-gdrive --user-email "alice@example.com" ...
nexus oauth setup-gdrive --user-email "bob@example.com" ...

# Create API keys
nexus admin create-user alice@example.com
nexus admin create-user bob@example.com
```

### Database State

```sql
-- API Keys
user_id           | key_hash
------------------|---------
alice@example.com | hash_a
bob@example.com   | hash_b

-- OAuth Credentials
user_email        | provider | encrypted_access_token | encrypted_refresh_token
------------------|----------|------------------------|------------------------
alice@example.com | google   | encrypted_token_a      | encrypted_refresh_a
bob@example.com   | google   | encrypted_token_b      | encrypted_refresh_b

-- Each user has:
-- 1. Their own API key (for server auth)
-- 2. Their own OAuth tokens (for Drive access)
```

### Usage

```python
# Alice's session
alice_nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk_alice_..."  # Alice's API key
)

# Writes to ALICE'S Google Drive
alice_nx.write("/gdrive/my-file.txt", b"Alice's file")

# Bob's session
bob_nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk_bob_..."  # Bob's API key
)

# Writes to BOB'S Google Drive
bob_nx.write("/gdrive/my-file.txt", b"Bob's file")

# ✅ Completely isolated!
# ✅ Each user accesses their own Drive!
# ✅ Automatic token refresh for both!
```

## Token Lifecycle

### Initial OAuth Setup

```
User → nexus oauth setup-gdrive
  ↓
Browser opens → User grants permission
  ↓
Authorization code received
  ↓
Exchange code for tokens
  ↓
Store in database (encrypted):
  - access_token (expires in ~1 hour)
  - refresh_token (never expires*)
  - expires_at timestamp
  - user_email
```

### Every API Request

```
User sends request with API key
  ↓
Server validates API key → Identifies user_id
  ↓
Request routes to Google Drive backend
  ↓
Backend calls TokenManager.get_valid_token(user_id)
  ↓
TokenManager checks expiry:
  - If valid: Return access_token
  - If expired:
    ↓
    Call Google OAuth refresh endpoint with refresh_token
      ↓
    Receive new access_token (and possibly new refresh_token)
      ↓
    Update database with new tokens
      ↓
    Return new access_token
  ↓
Backend uses access_token to call Google Drive API
  ↓
Success!
```

**\*Note:** Refresh tokens can be revoked if:
- User revokes access in Google Account settings
- Not used for 6 months (Google's policy)
- User changes password (sometimes)

If refresh fails → User must re-run `nexus oauth setup-gdrive`

## Configuration Example

### Nexus Configuration File

```yaml
# nexus.yaml

# Server authentication
auth:
  type: database
  database_url: postgresql://postgres:nexus@localhost:5432/nexus

# Backend mounts
backends:
  - type: gdrive_connector
    mount_point: /gdrive
    config:
      token_manager_db: /var/lib/nexus/nexus.db
      root_folder: nexus-data
      # ❌ No user_email here! It's determined per-request from context
    priority: 10
    readonly: false

  - type: local
    mount_point: /local
    config:
      data_dir: /var/lib/nexus/data
    priority: 0
```

### Why No user_email in Config?

The Google Drive connector is **user-scoped**, meaning:

```python
# Each request provides user context
context = OperationContext(
    user_id="alice@example.com",  # From API key auth
    tenant_id="org_acme",
    backend_path="/workspace/file.txt",
)

# Backend uses context.user_id to lookup OAuth token
token = token_manager.get_valid_token(
    provider="google",
    user_email=context.user_id,  # Different per user!
    tenant_id=context.tenant_id,
)
```

## Security Implications

### ✅ Secure Token Storage

```
API Keys (Server Auth):
  - Stored as HMAC-SHA256 hash in database
  - Never stored in plain text
  - Salt per key

OAuth Tokens (Backend Auth):
  - Encrypted with Fernet (AES-128 + HMAC-SHA256)
  - Encryption key: NEXUS_OAUTH_ENCRYPTION_KEY env var
  - Decrypted only when needed
  - Never logged
```

### ✅ Tenant Isolation

```sql
-- Alice (Tenant A) can't access Bob's (Tenant B) tokens
SELECT * FROM oauth_credentials
WHERE user_email = 'alice@example.com'
  AND tenant_id = 'tenant_a';

-- Returns only Alice's tokens for Tenant A
-- Bob's tokens (Tenant B) are invisible
```

### ✅ Audit Trail

```
AUDIT: token_refreshed | provider=google | user=alice@example.com | tenant=org_acme
AUDIT: credential_created | provider=google | user=bob@example.com | tenant=org_acme
AUDIT: credential_revoked | provider=google | user=alice@example.com | tenant=org_acme
```

## FAQ

### Q: Do users need to re-authenticate every time?

**A: No!** OAuth setup is one-time. After that:
- Access tokens refresh automatically
- Users only need their API key
- No browser redirects or manual OAuth flows

### Q: What if the refresh token expires?

**A: Rare, but possible.** If refresh fails:
```bash
# User must re-run OAuth setup
nexus oauth setup-gdrive \
    --client-id "$GOOGLE_CLIENT_ID" \
    --client-secret "$GOOGLE_CLIENT_SECRET" \
    --user-email "alice@example.com"
```

### Q: Can multiple users share the same Google Drive?

**A: No.** Each user has their own Google Drive. For shared access:
- Use Shared Drives (Google Workspace)
- Configure backend with `use_shared_drives: true`
- Each user still has their own OAuth tokens

### Q: How do I revoke a user's access?

**A: Two steps:**
```bash
# 1. Revoke OAuth tokens
nexus oauth revoke google alice@example.com

# 2. Revoke API key
nexus admin revoke-key <key_id>
```

### Q: Can I use the same OAuth tokens for multiple Nexus servers?

**A: No.** OAuth tokens are stored in the database. Each Nexus instance has its own database, so each needs its own OAuth setup.

### Q: What happens if I delete the database?

**A: You lose everything:**
- API keys
- OAuth tokens
- User data
- Permissions

**Solution:** Always backup your database!

## Related Documentation

- [OAuth Token Management](./oauth.md)
- [Server Authentication](./server-authentication.md)
- [Google Drive Backend](./google-drive-backend.md)
- [Google OAuth Setup](./google-oauth-setup.md)
