# Nexus Server Authentication

**Version:** 0.7.0

## Overview

Nexus server supports multiple authentication methods to secure your deployment. This guide covers all available authentication types and their configuration.

## Authentication Types

### 1. No Authentication (Development Only)

**Use case:** Local development, testing

```bash
# Start server without authentication
nexus serve

# OR explicitly
nexus serve --auth-type none
```

**⚠️ Warning:** Not suitable for production! Anyone can access the server.

### 2. Database Authentication (Recommended)

**Use case:** Production deployments, multi-user systems

**Features:**
- API key management in database
- User accounts with permissions
- Admin operations (create/revoke keys)
- Key expiry and revocation
- Secure key hashing (HMAC-SHA256)

**Setup:**

```bash
# Set database URL
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"

# First-time setup (creates admin user & API key)
nexus serve --auth-type database --init

# Clean setup for testing/demos (resets DB + init)
nexus serve --auth-type database --init --reset

# Restart server (already initialized)
nexus serve --auth-type database
```

**Environment Variables:**
- `NEXUS_DATABASE_URL` (required) - PostgreSQL connection string

**Initial Setup Output:**
```
🔧 Initializing Nexus Server

Creating admin user 'admin'...
✓ Admin user created
✓ Admin API key created: sk_a1b2c3d4e5f6...

Save this API key securely - it won't be shown again!
```

**Usage:**
```python
from nexus.remote import RemoteNexusFS

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key="sk_a1b2c3d4e5f6..."  # From init output
)
```

### 3. Local Authentication (Username/Password + JWT)

**Use case:** Web applications, mobile apps with login flow

**Features:**
- Username/password authentication
- JWT tokens for session management
- Refresh token support
- User registration and management

**Setup:**

```bash
# Set database URL and JWT secret
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
export NEXUS_JWT_SECRET="your-secret-key-here"  # Generate with: openssl rand -base64 32

# Start server
nexus serve --auth-type local
```

**Environment Variables:**
- `NEXUS_DATABASE_URL` (required) - PostgreSQL connection string
- `NEXUS_JWT_SECRET` (optional) - JWT signing secret (auto-generated if not set)

**Usage:**
```python
from nexus.remote import RemoteNexusFS

# Login with username/password
nx = RemoteNexusFS(server_url="http://localhost:8080")
jwt_token = nx.login(username="alice", password="password123")

# Use JWT token for requests
nx.set_token(jwt_token)
```

### 4. OIDC Authentication (Single Provider)

**Use case:** Enterprise SSO with Google, Microsoft, Okta, etc.

**Features:**
- OpenID Connect (OIDC) support
- JWT token validation
- Single provider (e.g., Google only)
- No password storage

**Setup:**

```bash
# Configure OIDC provider
export NEXUS_OIDC_ISSUER="https://accounts.google.com"
export NEXUS_OIDC_AUDIENCE="123456.apps.googleusercontent.com"

# Start server
nexus serve --auth-type oidc
```

**Environment Variables:**
- `NEXUS_OIDC_ISSUER` (required) - OIDC provider issuer URL
- `NEXUS_OIDC_AUDIENCE` (required) - OAuth client ID

**Supported Providers:**
- Google: `https://accounts.google.com`
- Microsoft: `https://login.microsoftonline.com/common/v2.0`
- GitHub: `https://token.actions.githubusercontent.com`
- Okta: `https://your-domain.okta.com`
- Auth0: `https://your-domain.auth0.com`

**Usage:**
```python
from nexus.remote import RemoteNexusFS

# Get ID token from your OAuth flow
id_token = "eyJhbGciOiJSUzI1NiIs..."  # From Google/Microsoft/etc.

nx = RemoteNexusFS(
    server_url="http://localhost:8080",
    api_key=id_token  # Pass OIDC token as API key
)
```

### 5. Multi-OIDC Authentication (Multiple Providers)

**Use case:** Support multiple SSO providers (Google + Microsoft + GitHub)

**Features:**
- Multiple OIDC providers simultaneously
- Users can log in via any configured provider
- Automatic provider detection from token

**Setup:**

```bash
# Configure multiple OIDC providers
export NEXUS_OIDC_PROVIDERS='{
  "google": {
    "issuer": "https://accounts.google.com",
    "audience": "123456.apps.googleusercontent.com"
  },
  "microsoft": {
    "issuer": "https://login.microsoftonline.com/common/v2.0",
    "audience": "abcdef-1234-5678-90ab-cdefghijklmn"
  },
  "github": {
    "issuer": "https://token.actions.githubusercontent.com",
    "audience": "your-repo-or-org"
  }
}'

# Start server
nexus serve --auth-type multi-oidc
```

**Environment Variables:**
- `NEXUS_OIDC_PROVIDERS` (required) - JSON object with provider configurations

**Usage:**
```python
from nexus.remote import RemoteNexusFS

# Users can log in via any configured provider
google_token = "eyJ..."  # From Google OAuth
microsoft_token = "eyJ..."  # From Microsoft OAuth

# Both work!
nx1 = RemoteNexusFS(server_url="http://localhost:8080", api_key=google_token)
nx2 = RemoteNexusFS(server_url="http://localhost:8080", api_key=microsoft_token)
```

### 6. Static API Key (Deprecated)

**Use case:** Quick testing only (deprecated)

**⚠️ Deprecated:** Use `--auth-type database` instead for production.

**Setup:**

```bash
# Legacy method (deprecated)
nexus serve --api-key "your-static-key"

# OR explicitly
nexus serve --auth-type static --api-key "your-static-key"
```

**⚠️ Limitations:**
- Single API key for all users
- No key rotation
- No expiry
- No admin operations
- Not suitable for production

## Comparison Table

| Feature | None | Database | Local | OIDC | Multi-OIDC | Static |
|---------|------|----------|-------|------|------------|--------|
| **Security** | ❌ | ✅ | ✅ | ✅ | ✅ | ⚠️ |
| **Multi-user** | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ |
| **SSO Support** | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ |
| **Key Rotation** | N/A | ✅ | N/A | N/A | N/A | ❌ |
| **Key Expiry** | N/A | ✅ | ✅ | ✅ | ✅ | ❌ |
| **Admin Operations** | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ |
| **Password Storage** | N/A | N/A | ✅ | ❌ | ❌ | N/A |
| **Production Ready** | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ |

## Best Practices

### Development

```bash
# No auth for quick local testing
nexus serve

# OR with database auth for realistic testing
nexus serve --auth-type database --init --reset
```

### Production

```bash
# Option 1: Database auth (recommended for most cases)
export NEXUS_DATABASE_URL="postgresql://..."
nexus serve --auth-type database --init

# Option 2: OIDC for enterprise SSO
export NEXUS_OIDC_ISSUER="https://..."
export NEXUS_OIDC_AUDIENCE="..."
nexus serve --auth-type oidc

# Option 3: Multi-OIDC for multiple SSO providers
export NEXUS_OIDC_PROVIDERS='{"google":{...},"microsoft":{...}}'
nexus serve --auth-type multi-oidc
```

### Security Recommendations

1. **Always use authentication in production** - Never deploy with `--auth-type none`
2. **Use database auth** - Most flexible, supports key rotation and expiry
3. **Set strong JWT secrets** - For local auth: `NEXUS_JWT_SECRET=$(openssl rand -base64 32)`
4. **Use HTTPS** - Always use TLS in production (via reverse proxy)
5. **Rotate keys regularly** - Database auth supports key rotation
6. **Monitor access** - Enable audit logging for compliance

## OAuth Backend Integration

For user-scoped backends (Google Drive, OneDrive), authentication works differently:

```bash
# Setup OAuth credentials (separate from server auth)
nexus oauth setup-gdrive \
    --client-id "123.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..." \
    --user-email "alice@example.com"

# Start server with database auth
nexus serve --auth-type database --init

# OAuth tokens are managed separately by TokenManager
# Server auth validates API requests
# OAuth auth validates backend access (Drive, OneDrive, etc.)
```

**Key Difference:**
- **Server auth** - Validates HTTP requests to Nexus API
- **OAuth auth** - Validates access to user's cloud storage (Drive, OneDrive)

## Troubleshooting

### "Database authentication requires NEXUS_DATABASE_URL"

**Solution:**
```bash
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
nexus serve --auth-type database --init
```

### "OIDC authentication requires: NEXUS_OIDC_ISSUER"

**Solution:**
```bash
export NEXUS_OIDC_ISSUER="https://accounts.google.com"
export NEXUS_OIDC_AUDIENCE="your-client-id"
nexus serve --auth-type oidc
```

### "Invalid NEXUS_OIDC_PROVIDERS JSON"

**Solution:** Check JSON syntax:
```bash
# Valid JSON (use single quotes for shell string)
export NEXUS_OIDC_PROVIDERS='{"google":{"issuer":"...","audience":"..."}}'

# Invalid: missing quotes, commas, braces
```

### "Failed to connect to database"

**Solutions:**
1. Check PostgreSQL is running: `docker ps | grep postgres`
2. Verify connection string: `psql "postgresql://postgres:nexus@localhost:5432/nexus"`
3. Start database: `docker-compose up -d postgres`

## Examples

### Example 1: Development Setup

```bash
# Quick start (no auth)
nexus serve

# Test writes
nexus write /test.txt "Hello World"
nexus cat /test.txt
```

### Example 2: Production Setup (Database Auth)

```bash
# Setup database
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@prod-db:5432/nexus"

# Initialize (first time only)
nexus serve --auth-type database --init --port 8080

# Save the admin API key from output
# Then restart without --init
nexus serve --auth-type database --port 8080
```

### Example 3: Enterprise SSO (Google)

```bash
# Configure Google OIDC
export NEXUS_OIDC_ISSUER="https://accounts.google.com"
export NEXUS_OIDC_AUDIENCE="123456.apps.googleusercontent.com"

# Start server
nexus serve --auth-type oidc --port 8080

# Users authenticate via Google OAuth
# Pass Google ID token as API key
```

### Example 4: Multi-Provider SSO

```bash
# Support Google + Microsoft + GitHub
export NEXUS_OIDC_PROVIDERS='{
  "google": {
    "issuer": "https://accounts.google.com",
    "audience": "123456.apps.googleusercontent.com"
  },
  "microsoft": {
    "issuer": "https://login.microsoftonline.com/common/v2.0",
    "audience": "abcdef-1234-5678"
  }
}'

nexus serve --auth-type multi-oidc --port 8080
```

## Related Documentation

- [Authentication System Design](../docs/design/auth-system.md)
- [OAuth Token Management](./oauth.md)
- [API Key Management](./admin-api.md)
- [Google Drive Backend](./google-drive-backend.md)
