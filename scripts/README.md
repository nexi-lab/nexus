# Nexus Scripts

This directory contains scripts for setting up, deploying, and managing Nexus.

## Table of Contents

- [Setup Scripts](#setup-scripts) - Local development and testing
- [Deployment Scripts](#deployment-scripts) - Production deployment
- [Utility Scripts](#utility-scripts) - Maintenance and management

---

## Setup Scripts

### Quick Start

#### Development (No Auth)
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus.sh
```
- No authentication
- Uses `NEXUS_SUBJECT` header (insecure!)
- Good for: Local development, learning, demos

#### Production (With Auth)
```bash
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
./scripts/init-nexus-with-auth.sh
```
- Database-backed API keys
- Secure authentication
- Good for: Production, multi-user, public servers

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

### Comparison: Setup Scripts

| Feature | `init-nexus.sh` | `init-nexus-with-auth.sh` |
|---------|----------------|--------------------------|
| **Authentication** | ❌ None | ✅ API Keys |
| **Security** | ⚠️ Insecure | ✅ Secure |
| **User identity** | Client claims (`NEXUS_SUBJECT`) | Server verifies (API key) |
| **Can impersonate** | ✅ Yes | ❌ No |
| **Key management** | N/A | `create-api-key.py` |
| **Production ready** | ❌ No | ✅ Yes |
| **Setup time** | Fast | +1 minute |

---

## Deployment Scripts

### `deploy-docker-image.sh` - PyPI Production Deployment

Deploys the **released version** from PyPI to production.

**Use this for:**
- ✅ Production deployments after PyPI release
- ✅ Deploying stable, tested versions
- ✅ Official releases

**Example:**
```bash
./scripts/deploy-docker-image.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"
```

### `deploy-docker-local.sh` - Local Development Deployment

Builds from **local source code** and deploys for testing.

**Use this for:**
- ✅ Testing bug fixes before releasing
- ✅ Testing new features in production-like environment
- ✅ Rapid iteration during development
- ❌ NOT for production releases

**Example:**
```bash
# Build from current branch and deploy
./scripts/deploy-docker-local.sh \
  --cloud-sql-instance nexi-lab-888:us-west1:nexus-hub \
  --db-name nexus \
  --db-user postgres \
  --db-password "Nexus-Hub2025"

# Custom tag for this test
./scripts/deploy-docker-local.sh --tag fix/fuse-remote-metadata
```

### Deployment Options

Both scripts support:

- `--project-id` - GCP project (default: nexi-lab-888)
- `--instance-name` - VM name (default: nexus-server)
- `--zone` - GCP zone (default: us-west1-a)
- `--port` - Server port (default: 8080)
- `--cloud-sql-instance` - PostgreSQL Cloud SQL instance
- `--db-name` - Database name (default: nexus)
- `--db-user` - Database user (default: postgres)
- `--db-password` - Database password

Additional options for `deploy-docker-local.sh`:

- `--tag` - Custom Docker image tag
- `--skip-build` - Use existing image without rebuilding

### Deployment Workflow

#### Testing a Bug Fix

1. Create a feature branch with your fix
2. Test locally
3. Deploy to staging using local build:
   ```bash
   ./scripts/deploy-docker-local.sh --tag my-bugfix
   ```
4. Verify the fix works in production environment
5. Merge to main
6. Release to PyPI
7. Deploy to production:
   ```bash
   ./scripts/deploy-docker-image.sh
   ```

#### Production Release

1. Ensure all tests pass
2. Update version in `pyproject.toml`
3. Release to PyPI (see CLAUDE.md)
4. Deploy using PyPI script:
   ```bash
   ./scripts/deploy-docker-image.sh
   ```

---

## Utility Scripts

### `nexus-mount.sh` / `nexus-unmount.sh`
Mount and unmount Nexus filesystem via FUSE.

### `check_server.py`
Check server health and status.

### `cleanup_os_metadata.py`
Clean up metadata from older OS-based permission system.

### `run_benchmarks.sh`
Run performance benchmarks.

---

## Environment Variables

Setup scripts support:

```bash
# Required
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# Optional
export NEXUS_DATA_DIR="./nexus-data"  # Default: ./nexus-data
export NEXUS_ADMIN_USER="alice"       # Default: admin
export NEXUS_PORT="9000"              # Default: 8080
export NEXUS_HOST="127.0.0.1"         # Default: 0.0.0.0
```

---

## Full Example: Production Setup

```bash
# 1. Set database URL
export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"

# 2. Run authenticated setup
./scripts/init-nexus-with-auth.sh

# 3. Use the admin key
source .nexus-admin-env
nexus ls /workspace --remote-url http://localhost:8080

# 4. Create keys for other users
python3 scripts/create-api-key.py alice "Alice's laptop" --days 90
python3 scripts/create-api-key.py bob "Bob's server" --admin --days 365

# 5. Give users their API keys (securely!)
# Users set: export NEXUS_API_KEY='sk-alice_...'
```

---

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

---

## Architecture

Deployment scripts deploy with:

- **Storage Backend:** GCS (`nexi-hub` bucket)
- **Metadata Store:** PostgreSQL (Cloud SQL, optional)
- **Networking:** Host network mode
- **Ports:** 80 (public) → 8080 (container)
- **Authentication:** Via GCP metadata service

Docker images are stored in GCR:
- PyPI builds: `gcr.io/nexi-lab-888/nexus-server:latest`
- Local builds: `gcr.io/nexi-lab-888/nexus-server:local-TIMESTAMP`

---

## See Also

- Main guide: `docs/QUICKSTART_GUIDE.md`
- Authentication details: `examples/auth_demo/CLI_AUTH_GUIDE.md`
- Database auth: `examples/auth_demo/database_auth_demo.sh`
