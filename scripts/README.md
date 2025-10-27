# Nexus Setup Scripts

## Quick Start

### Development (No Auth)
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus.sh
```
- No authentication
- Uses `NEXUS_SUBJECT` header (insecure!)
- Good for: Local development, learning, demos

### Production (With Auth)
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus-with-auth.sh
```
- Database-backed API keys
- Secure authentication
- Good for: Production, multi-user, public servers

## Scripts

### `init-nexus.sh`
Starts Nexus server **without authentication**.

**What it does:**
1. Creates `/workspace` directory
2. Grants admin user ownership
3. Starts server on port 8080
4. Accepts unauthenticated requests (uses `X-Nexus-Subject` header)

**Security:** ⚠️ **INSECURE** - Anyone can impersonate any user

**Use when:**
- Local development
- Testing/demos
- Learning the system

### `init-nexus-with-auth.sh`
Starts Nexus server **with database-backed API key authentication**.

**What it does:**
1. Creates `/workspace` directory
2. Grants admin user ownership
3. **Creates admin API key** (90 day expiry)
4. Saves API key to `.nexus-admin-env`
5. Starts server with `--auth-type database`

**Security:** ✅ **SECURE** - Validates API keys, can't impersonate

**Use when:**
- Production deployments
- Multi-user environments
- Public-facing servers

**Output:**
```
Admin API Key: sk-admin_a1b2c3_d4e5f6789...

Saved to .nexus-admin-env (source this file)
```

### `create-api-key.py`
Creates API keys for users.

**Usage:**
```bash
# Regular user with 90 day expiry
python3 scripts/create-api-key.py alice "Alice's laptop" --days 90

# Admin user with no expiry
python3 scripts/create-api-key.py admin "Admin key" --admin

# Custom tenant
python3 scripts/create-api-key.py bob "Bob's key" --tenant-id org-acme --days 365
```

**Parameters:**
- `user_id` - User identifier (e.g., alice, bob)
- `name` - Human-readable key name
- `--admin` - Grant admin privileges
- `--days N` - Expiry in N days (optional)
- `--tenant-id` - Tenant ID (default: "default")

**Output:**
```
✓ Created API key for user 'alice'
  Name: Alice's laptop
  Admin: False
  Expires: 2025-01-15

IMPORTANT: Save this key - it will not be shown again!

  API Key: sk-alice_12ab34cd_56ef7890...

Use with:
  export NEXUS_API_KEY='sk-alice_12ab34cd_56ef7890...'
  nexus ls /workspace --remote-url http://localhost:8080
```

## Comparison

| Feature | `init-nexus.sh` | `init-nexus-with-auth.sh` |
|---------|----------------|--------------------------|
| **Authentication** | ❌ None | ✅ API Keys |
| **Security** | ⚠️ Insecure | ✅ Secure |
| **User identity** | Client claims (`NEXUS_SUBJECT`) | Server verifies (API key) |
| **Can impersonate** | ✅ Yes | ❌ No |
| **Key management** | N/A | `create-api-key.py` |
| **Production ready** | ❌ No | ✅ Yes |
| **Setup time** | Fast | +1 minute |

## Environment Variables

Both scripts support:

```bash
# Required
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# Optional
export NEXUS_DATA_DIR="./nexus-data"  # Default: ./nexus-data
export NEXUS_ADMIN_USER="alice"       # Default: admin
export NEXUS_PORT="9000"              # Default: 8080
export NEXUS_HOST="127.0.0.1"         # Default: 0.0.0.0
```

## Full Example: Production Setup

```bash
# 1. Set database URL
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# 2. Run authenticated setup
./scripts/init-nexus-with-auth.sh

# Output:
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IMPORTANT: Save this API key securely!
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Admin API Key: sk-admin_a1b2c3d4_e5f6g7h8...
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 3. Use the admin key
source .nexus-admin-env
nexus ls /workspace --remote-url http://localhost:8080

# 4. Create keys for other users
python3 scripts/create-api-key.py alice "Alice's laptop" --days 90
python3 scripts/create-api-key.py bob "Bob's server" --admin --days 365

# 5. Give users their API keys (securely!)
# Users set: export NEXUS_API_KEY='sk-alice_...'
```

## Troubleshooting

### "Cannot connect to database"
```bash
# Check PostgreSQL is running
docker ps | grep postgres

# Verify database exists
psql $NEXUS_DATABASE_URL -c "SELECT 1"

# Create database if needed
createdb nexus
```

### "Failed to create admin API key"
```bash
# Ensure python3 is available
which python3

# Check database permissions
psql $NEXUS_DATABASE_URL -c "SELECT 1"
```

### "Server already running"
```bash
# Stop existing server
pkill -f "nexus serve"

# Verify stopped
ps aux | grep "nexus serve"
```

## See Also

- Main guide: `docs/QUICKSTART_GUIDE.md`
- Authentication details: `examples/auth_demo/CLI_AUTH_GUIDE.md`
- Database auth: `examples/auth_demo/database_auth_demo.sh`
