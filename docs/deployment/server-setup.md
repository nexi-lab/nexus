# Nexus Server Setup Guide - Pure CLI Version

Complete step-by-step guide for setting up a Nexus server using **only command-line tools** - no Python scripts required!

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Verifying Permissions](#verifying-permissions)
4. [Production Setup](#production-setup)
5. [Managing Permissions](#managing-permissions)
6. [Client Access](#client-access)
7. [Remote Administration (Multi-Tenant)](#remote-administration-multi-tenant)
8. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### 1. Install Nexus

```bash
pip install nexus-ai-fs

# Verify installation
nexus --version
```

### 2. Set Up PostgreSQL (Recommended)

```bash
# macOS
brew install postgresql
brew services start postgresql

# Create database and user
createdb nexus

# Set environment variable (no password for local development)
export NEXUS_DATABASE_URL="postgresql://$(whoami)@localhost/nexus"

# OR with password (production)
export NEXUS_DATABASE_URL="postgresql://nexus:your-password@localhost/nexus"
```

**Linux (Ubuntu/Debian):**

```bash
# Install PostgreSQL
sudo apt-get install postgresql
sudo systemctl start postgresql

# Create user and database
sudo -u postgres psql << EOF
CREATE USER nexus WITH PASSWORD 'your-password';
CREATE DATABASE nexus OWNER nexus;
GRANT ALL PRIVILEGES ON DATABASE nexus TO nexus;
EOF

# Set environment variable
export NEXUS_DATABASE_URL="postgresql://nexus:your-password@localhost/nexus"
```

**Fix Authentication Issues:**

If you get "password authentication failed":

```bash
# Option 1: Use peer authentication (local development)
export NEXUS_DATABASE_URL="postgresql://$(whoami)@localhost/nexus"

# Option 2: Create PostgreSQL user with password
sudo -u postgres psql -c "CREATE USER nexus WITH PASSWORD 'secure-password';"
sudo -u postgres psql -c "CREATE DATABASE nexus OWNER nexus;"
export NEXUS_DATABASE_URL="postgresql://nexus:secure-password@localhost/nexus"

# Option 3: Use trust authentication (LOCAL ONLY - INSECURE)
# Edit /etc/postgresql/*/main/pg_hba.conf
# Change: local all all peer
# To:     local all all trust
# Then: sudo systemctl restart postgresql
```

### 3. Choose a Data Directory

```bash
export NEXUS_DATA_DIR="/var/lib/nexus"
mkdir -p $NEXUS_DATA_DIR
```

---

## Quick Start

### One-Command Setup (Easiest)

**Download the ready-made script:**

```bash
# Download from repository
curl -O https://raw.githubusercontent.com/anthropics/nexus/main/scripts/init-nexus.sh
chmod +x init-nexus.sh

# Run it
./init-nexus.sh

# Or with custom settings:
NEXUS_ADMIN_USER=alice NEXUS_PORT=9000 ./init-nexus.sh
```

**Or create your own** `init-nexus.sh`:

```bash
#!/bin/bash
# init-nexus.sh - Initialize and start Nexus server

# Configuration
export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://$(whoami)@localhost/nexus}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"

echo "🚀 Initializing Nexus Server..."

# Create database if needed
createdb nexus 2>/dev/null || echo "✓ Database exists"

# Bootstrap (permissions disabled)
export NEXUS_ENFORCE_PERMISSIONS=false

# Create admin workspace
nexus mkdir /workspace 2>/dev/null || echo "✓ /workspace exists"

# Grant admin full access
nexus rebac create user $ADMIN_USER direct_owner file /workspace --tenant-id default
echo "✓ Admin user '$ADMIN_USER' configured"

# Start server (permissions enabled)
export NEXUS_ENFORCE_PERMISSIONS=true
echo ""
echo "✅ Starting Nexus server..."
echo "   Admin user: $ADMIN_USER"
echo "   Database: $NEXUS_DATABASE_URL"
echo "   Data dir: $NEXUS_DATA_DIR"
echo ""

nexus serve --host 0.0.0.0 --port 8080
```

**Run it:**
```bash
chmod +x init-nexus.sh
./init-nexus.sh

# Or with custom admin user:
NEXUS_ADMIN_USER=myname ./init-nexus.sh
```

**That's it!** Server is running with admin user configured.

---

### Step 1: Bootstrap Server (Setup Initial Permissions)

```bash
# Disable permissions for initial setup
export NEXUS_ENFORCE_PERMISSIONS=false

# Create workspace directory
nexus mkdir /workspace
echo "✓ Created /workspace"

# Grant alice ownership of /workspace
nexus rebac create \
  --subject-type user \
  --subject-id alice \
  --relation direct_owner \
  --object-type file \
  --object-id /workspace \
  --tenant-id default

echo "✓ Granted alice ownership of /workspace"

# Grant bob editor access
nexus rebac create \
  --subject-type user \
  --subject-id bob \
  --relation direct_editor \
  --object-type file \
  --object-id /workspace \
  --tenant-id default

echo "✓ Granted bob editor access to /workspace"

# Verify permissions were set correctly
echo ""
echo "=== Verifying Permissions ==="

# Check if alice has write permission
nexus rebac check user alice write file /workspace
# Output: ✓ GRANTED - user:alice has write on file:/workspace

# Explain why alice has permission
nexus rebac explain user alice write file /workspace
# Shows the permission path

# List who has write access
nexus rebac expand write file /workspace
# Shows all users with write permission

# Re-enable permissions
export NEXUS_ENFORCE_PERMISSIONS=true
echo "✓ Bootstrap complete!"
```

### Step 2: Start the Server

```bash
# Start server (permissions enabled by default)
nexus serve --host 0.0.0.0 --port 8080
```

Server is now running at `http://0.0.0.0:8080`!

### Step 3: Verify Permissions (Before Starting Server)

```bash
# Check if alice can write to /workspace
nexus rebac check user alice write file /workspace
# Output: ✓ GRANTED - user:alice has write on file:/workspace

# Explain why alice has permission
nexus rebac explain user alice write file /workspace
# Output: Shows permission path (direct_owner → owner → write)

# See all users who can write to /workspace
nexus rebac expand write file /workspace
# Output: Lists all users with write permission

# Check bob's access
nexus rebac check user bob write file /workspace
# Output: ✓ GRANTED - user:bob has write on file:/workspace
```

### Step 4: Test Access (After Starting Server)

```bash
# As alice (has ownership)
export USER=alice
nexus write /workspace/test.txt "Hello from Alice" \
  --remote-url http://localhost:8080

nexus read /workspace/test.txt \
  --remote-url http://localhost:8080

nexus ls /workspace \
  --remote-url http://localhost:8080

echo "✓ Alice can access /workspace"
```

---

## Verifying Permissions

Complete guide for verifying that permissions are set correctly.

### Three Methods to Verify Permissions

#### Method 1: **Check** - Does user X have permission Y?

```bash
nexus rebac check user alice write file /workspace
```

**Output:**
```
✓ GRANTED
  user:alice has write on file:/workspace
```

**Use when:** You want a quick yes/no answer

---

#### Method 2: **Explain** - WHY does user X have (or not have) permission Y?

```bash
nexus rebac explain user alice write file /workspace
```

**Output:**
```
✓ GRANTED

Reason: user:alice has 'write' on file:/workspace
Path: direct_owner → owner → write

Permission chain:
  1. user:alice has direct_owner on file:/workspace (tuple created)
  2. direct_owner is part of 'owner' relation (schema union)
  3. 'owner' grants 'write' permission (schema permissions)
```

**Use when:** You need to understand the permission path or debug why access was denied

---

#### Method 3: **Expand** - WHO has permission Y on object Z?

```bash
nexus rebac expand write file /workspace
```

**Output:**
```
Subjects with write on file:/workspace:
  - user:alice
  - user:bob
  - group:engineering#member
```

**Use when:** You want to see all users/groups who have access

---

### Common Verification Scenarios

#### Verify Initial Bootstrap

After running your bootstrap script:

```bash
# 1. Check alice has ownership
nexus rebac check user alice write file /workspace
# Expected: ✓ GRANTED

# 2. See everyone who can access /workspace
nexus rebac expand write file /workspace
# Expected: Shows alice, bob, etc.

# 3. Verify bob has editor access
nexus rebac check user bob write file /workspace
# Expected: ✓ GRANTED
```

#### Verify Parent Inheritance

Check if permissions cascade to child files:

```bash
# Alice owns /workspace
nexus rebac check user alice write file /workspace
# ✓ GRANTED

# Create a file (or just check a hypothetical path)
# Does alice automatically have access to child files?
nexus rebac check user alice write file /workspace/project/file.txt
# ✓ GRANTED (via parent inheritance)

# Explain how inheritance works
nexus rebac explain user alice write file /workspace/project/file.txt
# Shows: parent tuple → parent_owner → owner → write
```

#### Debug "Permission Denied" Errors

When a user gets permission denied:

```bash
# Step 1: Check if they have permission
nexus rebac check user charlie write file /workspace/secret.txt
# ✗ DENIED

# Step 2: Explain WHY they don't have permission
nexus rebac explain user charlie write file /workspace/secret.txt
# Shows: No direct relation, no parent inheritance, no group membership

# Step 3: Check parent directory access
nexus rebac check user charlie write file /workspace
# ✗ DENIED - This is the root cause!

# Step 4: Grant access to parent
nexus rebac create user charlie direct_viewer file /workspace --tenant-id default

# Step 5: Verify access now works
nexus rebac check user charlie read file /workspace/secret.txt
# ✓ GRANTED (viewer can read via parent inheritance)
```

#### Verify Group Access

Check if group-based permissions work:

```bash
# Step 1: Verify user is in group
nexus rebac check user alice member group engineering
# ✓ GRANTED

# Step 2: Verify group has access to resource
nexus rebac explain user alice write file /workspace/code
# Shows: user:alice → member → group:engineering#member → direct_editor → editor → write

# Step 3: See all group members who can access
nexus rebac expand write file /workspace/code
# Shows all users in engineering group
```

---

### Quick Reference Table

| Command | Purpose | Example |
|---------|---------|---------|
| `rebac check` | Quick yes/no | `nexus rebac check user alice write file /workspace` |
| `rebac explain` | Detailed path | `nexus rebac explain user alice write file /workspace` |
| `rebac expand` | List all subjects | `nexus rebac expand write file /workspace` |
| `rebac create` | Grant permission | `nexus rebac create user alice direct_owner file /workspace --tenant-id default` |
| `rebac delete` | Revoke permission | `nexus rebac delete <tuple-id>` |

---

### Command Syntax

#### Check Command
```bash
nexus rebac check <subject-type> <subject-id> <permission> <object-type> <object-id>

# Examples:
nexus rebac check user alice write file /workspace
nexus rebac check user bob read file /workspace/file.txt
nexus rebac check group engineering write file /workspace/code
```

#### Explain Command
```bash
nexus rebac explain <subject-type> <subject-id> <permission> <object-type> <object-id>

# Examples:
nexus rebac explain user alice write file /workspace
nexus rebac explain user charlie read file /workspace/secret.txt
```

#### Expand Command
```bash
nexus rebac expand <permission> <object-type> <object-id>

# Examples:
nexus rebac expand write file /workspace
nexus rebac expand read file /workspace/file.txt
nexus rebac expand owner file /workspace/project
```

---

### Verification Best Practices

1. **Always verify after bootstrap**
   ```bash
   # After creating permissions, verify they work
   nexus rebac check user alice write file /workspace
   ```

2. **Use explain for debugging**
   ```bash
   # Don't guess why access is denied - use explain
   nexus rebac explain user charlie write file /workspace/secret.txt
   ```

3. **Check parent inheritance**
   ```bash
   # Verify parent directory has permissions before checking child files
   nexus rebac check user alice write file /workspace  # parent
   nexus rebac check user alice write file /workspace/file.txt  # child
   ```

4. **Test both granted and denied cases**
   ```bash
   # Positive test
   nexus rebac check user alice write file /workspace
   # Expected: GRANTED

   # Negative test
   nexus rebac check user nobody write file /workspace
   # Expected: DENIED
   ```

---

## Production Setup

### Complete Setup Script

Create `setup_nexus_server.sh`:

```bash
#!/bin/bash
# Complete Nexus server setup - Pure CLI version

set -e  # Exit on error

echo "=== Nexus Server Setup ==="

# 1. Configuration
export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://$(whoami)@localhost/nexus}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-/var/lib/nexus}"
export NEXUS_PORT="${NEXUS_PORT:-8080}"
export NEXUS_HOST="${NEXUS_HOST:-0.0.0.0}"

echo "Database: $NEXUS_DATABASE_URL"
echo "Data Dir: $NEXUS_DATA_DIR"
echo "Server: $NEXUS_HOST:$NEXUS_PORT"

# 2. Create data directory
mkdir -p $NEXUS_DATA_DIR
echo "✓ Created data directory"

# 3. Bootstrap (permissions disabled)
echo ""
echo "=== Bootstrapping ==="
export NEXUS_ENFORCE_PERMISSIONS=false

# Create root workspace
nexus mkdir /workspace 2>/dev/null || echo "✓ /workspace exists"

# Create team directories
for team in engineering marketing sales; do
    nexus mkdir /workspace/$team 2>/dev/null || true
    echo "✓ Created /workspace/$team"
done

# 4. Set up permissions
echo ""
echo "=== Setting up permissions ==="

# Admin user
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
nexus rebac create \
  --subject-type user \
  --subject-id $ADMIN_USER \
  --relation direct_owner \
  --object-type file \
  --object-id /workspace \
  --tenant-id default
echo "✓ Granted $ADMIN_USER ownership of /workspace"

# Engineering team
for user in alice bob; do
    nexus rebac create \
      --subject-type user \
      --subject-id $user \
      --relation direct_editor \
      --object-type file \
      --object-id /workspace/engineering \
      --tenant-id default
    echo "✓ Granted $user editor access to /workspace/engineering"
done

# Marketing team
for user in charlie diana; do
    nexus rebac create \
      --subject-type user \
      --subject-id $user \
      --relation direct_editor \
      --object-type file \
      --object-id /workspace/marketing \
      --tenant-id default
    echo "✓ Granted $user editor access to /workspace/marketing"
done

# 5. Re-enable permissions
export NEXUS_ENFORCE_PERMISSIONS=true

echo ""
echo "=== Bootstrap Complete! ==="
echo ""
echo "Start server with:"
echo "  nexus serve --host $NEXUS_HOST --port $NEXUS_PORT"
echo ""
echo "Or run in background:"
echo "  nohup nexus serve --host $NEXUS_HOST --port $NEXUS_PORT > /tmp/nexus.log 2>&1 &"
```

### Run the Setup

```bash
chmod +x setup_nexus_server.sh
./setup_nexus_server.sh
```

### Start the Server

```bash
# Foreground
nexus serve --host 0.0.0.0 --port 8080

# Background
nohup nexus serve --host 0.0.0.0 --port 8080 > /tmp/nexus.log 2>&1 &

# Save PID for later
echo $! > /tmp/nexus.pid
```

### Stop the Server

```bash
# If running in background
kill $(cat /tmp/nexus.pid)

# Or find and kill
pkill -f "nexus serve"
```

---

## Managing Permissions

> **Note:** All `nexus rebac` commands work with the **local database** directly.
> They don't require a running server and use `$NEXUS_DATABASE_URL`.
> Changes are immediately visible to both local CLI and remote server (they share the same database).

### Grant User Access

```bash
# Grant ownership
nexus rebac create \
  --subject-type user \
  --subject-id alice \
  --relation direct_owner \
  --object-type file \
  --object-id /workspace/project \
  --tenant-id default

# Grant editor access
nexus rebac create \
  --subject-type user \
  --subject-id bob \
  --relation direct_editor \
  --object-type file \
  --object-id /workspace/project \
  --tenant-id default

# Grant viewer access
nexus rebac create \
  --subject-type user \
  --subject-id charlie \
  --relation direct_viewer \
  --object-type file \
  --object-id /workspace/project \
  --tenant-id default
```

### Grant Group Access

```bash
# Add user to group
nexus rebac create \
  --subject-type user \
  --subject-id alice \
  --relation member \
  --object-type group \
  --object-id engineering \
  --tenant-id default

# Grant group access to directory (userset-as-subject)
nexus rebac create \
  --subject-type group \
  --subject-id engineering \
  --subject-relation member \
  --relation direct_editor \
  --object-type file \
  --object-id /workspace/code \
  --tenant-id default
```

### Check Permissions

> **Important:** `nexus rebac` commands connect to the **local database** (not a remote server).
> They use `$NEXUS_DATABASE_URL` to query the `rebac_tuples` table directly.
> No `--remote-url` needed for ReBAC commands!

**Method 1: Check if a user has permission**
```bash
# Simple check (returns GRANTED or DENIED)
nexus rebac check user alice write file /workspace

# Example output:
# ✓ GRANTED
#   user:alice has write on file:/workspace
```

**Method 2: Explain why user has permission**
```bash
# Get detailed explanation
nexus rebac explain user alice write file /workspace

# Example output:
# ✓ GRANTED
# Reason: user:alice has 'write' on file:/workspace
# Path: direct_owner → owner → write
```

**Method 3: See all users with permission**
```bash
# Find everyone who can write to /workspace
nexus rebac expand write file /workspace

# Example output:
# Subjects with write on file:/workspace:
#   - user:alice (via direct_owner)
#   - user:bob (via direct_editor)
```

### Common Verification Scenarios

**Verify ownership**
```bash
# Check if alice owns /workspace
nexus rebac check user alice write file /workspace

# If GRANTED, alice is owner (owners can write)
```

**Verify inheritance works**
```bash
# Alice owns /workspace
nexus rebac check user alice write file /workspace
# Output: ✓ GRANTED

# Check if alice can write to files under /workspace (via parent inheritance)
nexus rebac check user alice write file /workspace/project/file.txt
# Output: ✓ GRANTED (inherited from parent /workspace)
```

**Debug permission issues**
```bash
# Why can't charlie access this file?
nexus rebac explain user charlie write file /workspace/secret.txt

# Example output:
# ✗ DENIED
# Reason: user:charlie does not have write on file:/workspace/secret.txt
# - No direct relation found
# - No parent inheritance (parent /workspace denies access)
```

### Revoke Access

```bash
# Remove a permission
nexus rebac delete \
  --subject-type user \
  --subject-id bob \
  --relation direct_editor \
  --object-type file \
  --object-id /workspace/project \
  --tenant-id default
```

### Namespace Management

> **Note:** Namespace commands also use the **local database**.
> Namespaces registered via CLI are immediately available to the server.

```bash
# List all registered namespaces (from local database)
nexus rebac namespace-list

# View a specific namespace
nexus rebac namespace-get file

# Register a custom namespace
nexus rebac namespace-create \
  --object-type document \
  --config '{
    "relations": {
      "owner": {},
      "editor": {"union": ["owner"]},
      "viewer": {"union": ["editor"]}
    },
    "permissions": {
      "read": ["viewer"],
      "write": ["editor"],
      "delete": ["owner"]
    }
  }'

# List all namespaces
nexus rebac namespace-list

# Delete a namespace
nexus rebac namespace-delete --object-type document
```

---

## Client Access

### Understanding Local vs Remote Commands

**Important distinction:**

| Command Type | Connects To | Example | Needs Server? |
|-------------|-------------|---------|---------------|
| `nexus rebac ...` | Local database | `nexus rebac check user alice write file /workspace` | ❌ No |
| `nexus write/read/ls ...` (without --remote-url) | Local filesystem | `nexus write /workspace/test.txt "hello"` | ❌ No |
| `nexus write/read/ls ... --remote-url` | Remote server | `nexus write /workspace/test.txt "hello" --remote-url http://localhost:8080` | ✅ Yes |

**Key Points:**
- `nexus rebac` commands **always** use the local database (PostgreSQL/SQLite)
- File operations (`write`, `read`, `ls`) default to local, but support `--remote-url` for remote access
- Both local CLI and remote server share the **same database**, so permissions are synchronized

---

### CLI Client (Remote Mode)

```bash
# Set remote URL
export NEXUS_REMOTE_URL="http://localhost:8080"

# Write a file
nexus write /workspace/test.txt "Hello World" --remote-url $NEXUS_REMOTE_URL

# Read a file
nexus read /workspace/test.txt --remote-url $NEXUS_REMOTE_URL

# List files
nexus ls /workspace --remote-url $NEXUS_REMOTE_URL

# Search files
nexus search "*.txt" /workspace --remote-url $NEXUS_REMOTE_URL

# File metadata
nexus metadata /workspace/test.txt --remote-url $NEXUS_REMOTE_URL
```

### FUSE Mount (Advanced)

```bash
# Create mount point
mkdir -p /mnt/nexus

# Mount remote server
nexus mount /mnt/nexus --remote-url http://localhost:8080

# Now use regular filesystem commands
echo "Hello" > /mnt/nexus/workspace/test.txt
cat /mnt/nexus/workspace/test.txt
ls -la /mnt/nexus/workspace

# Unmount
nexus unmount /mnt/nexus
```

### Bash Scripting Example

```bash
#!/bin/bash
# Example: Batch file upload with permissions

REMOTE_URL="http://localhost:8080"
USER="alice"
WORKSPACE="/workspace/uploads"

# Create upload directory
nexus mkdir $WORKSPACE --remote-url $REMOTE_URL

# Upload files
for file in *.txt; do
    echo "Uploading $file..."
    nexus write "$WORKSPACE/$file" < "$file" --remote-url $REMOTE_URL
done

# Grant bob access to all uploaded files
# (Parent inheritance handles this automatically!)
nexus rebac create \
  --subject-type user \
  --subject-id bob \
  --relation direct_viewer \
  --object-type file \
  --object-id $WORKSPACE \
  --tenant-id default

echo "✓ Uploaded ${#files[@]} files and granted bob viewer access"
```

---

## Systemd Service Setup

### Create Service File

```bash
sudo tee /etc/systemd/system/nexus.service > /dev/null << 'EOF'
[Unit]
Description=Nexus AI Filesystem Server
After=network.target postgresql.service

[Service]
Type=simple
User=nexus
Group=nexus
WorkingDirectory=/opt/nexus

# Environment
Environment="NEXUS_DATABASE_URL=postgresql://nexus:password@localhost/nexus"
Environment="NEXUS_DATA_DIR=/var/lib/nexus"
Environment="NEXUS_ENFORCE_PERMISSIONS=true"

# Start command
ExecStart=/usr/local/bin/nexus serve --host 0.0.0.0 --port 8080

# Restart on failure
Restart=always
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### Enable and Start

```bash
# Create nexus user
sudo useradd -r -s /bin/false nexus
sudo mkdir -p /var/lib/nexus
sudo chown nexus:nexus /var/lib/nexus

# Reload systemd
sudo systemctl daemon-reload

# Enable (start on boot)
sudo systemctl enable nexus

# Start now
sudo systemctl start nexus

# Check status
sudo systemctl status nexus

# View logs
sudo journalctl -u nexus -f
```

### Manage Service

```bash
# Stop
sudo systemctl stop nexus

# Restart
sudo systemctl restart nexus

# Disable
sudo systemctl disable nexus

# Check if running
systemctl is-active nexus
```

---

## Environment Variables Reference

```bash
# Database
export NEXUS_DATABASE_URL="postgresql://user:pass@host/db"

# Data Directory
export NEXUS_DATA_DIR="/var/lib/nexus"

# Server Settings
export NEXUS_HOST="0.0.0.0"
export NEXUS_PORT="8080"

# Security
export NEXUS_ENFORCE_PERMISSIONS="true"   # Enable permission checks
export NEXUS_ALLOW_ADMIN_BYPASS="false"   # Disable admin bypass

# User Context (for CLI operations)
export USER="alice"                       # Subject for operations
export NEXUS_TENANT_ID="default"          # Tenant isolation

# Remote Access
export NEXUS_REMOTE_URL="http://localhost:8080"
```

---

## Troubleshooting

### Check Server Status

```bash
# Is server running?
curl http://localhost:8080/health

# Should return: {"status": "healthy"}
```

### View Server Logs

```bash
# If started with nohup
tail -f /tmp/nexus.log

# If using systemd
sudo journalctl -u nexus -f

# Search for errors
sudo journalctl -u nexus | grep ERROR
```

### Test Permissions

```bash
# Disable enforcement temporarily
export NEXUS_ENFORCE_PERMISSIONS=false

# Create test file
nexus write /workspace/test.txt "test"

# Grant alice permission
nexus rebac create \
  --subject-type user \
  --subject-id alice \
  --relation direct_owner \
  --object-type file \
  --object-id /workspace \
  --tenant-id default

# Re-enable enforcement
export NEXUS_ENFORCE_PERMISSIONS=true

# Test access
export USER=alice
nexus read /workspace/test.txt
```

### Debug Permission Issues

**Use the three verification methods:**

```bash
# Method 1: Quick check
nexus rebac check user alice write file /workspace/test.txt
# Shows: GRANTED or DENIED

# Method 2: Detailed explanation (BEST for debugging)
nexus rebac explain user alice write file /workspace/test.txt
# Shows WHY permission was granted/denied

# Method 3: See who has access
nexus rebac expand write file /workspace/test.txt
# Shows all users with write permission
```

**Common debugging scenarios:**

```bash
# Check if tuple exists
nexus rebac expand write file /workspace
# If user not listed, tuple doesn't exist

# Verify parent inheritance
nexus rebac explain user alice write file /workspace/project/file.txt
# Should show: parent tuple → parent_owner → owner → write
```

### Common Issues

#### 1. Permission Denied

```bash
# Problem: User can't access file
# Solution: Grant access to parent directory

nexus rebac create \
  --subject-type user \
  --subject-id alice \
  --relation direct_owner \
  --object-type file \
  --object-id /workspace \
  --tenant-id default

# Parent inheritance will grant access to all files under /workspace
```

#### 2. PostgreSQL Authentication Failed

```bash
# Problem: "password authentication failed for user postgres"
# OR: "connection refused" on port 5432

# Solution 1: Check PostgreSQL is running
sudo systemctl status postgresql  # Linux
brew services list | grep postgresql  # macOS

# Solution 2: Create dedicated nexus user with password
sudo -u postgres psql << EOF
CREATE USER nexus WITH PASSWORD 'your-password';
CREATE DATABASE nexus OWNER nexus;
GRANT ALL PRIVILEGES ON DATABASE nexus TO nexus;
EOF

export NEXUS_DATABASE_URL="postgresql://nexus:your-password@localhost/nexus"

# Solution 3: Use peer authentication (your OS username)
export NEXUS_DATABASE_URL="postgresql://$(whoami)@localhost/nexus"

# Verify connection works
psql $NEXUS_DATABASE_URL -c "SELECT 1;"
```

#### 3. Database Locked

```bash
# Problem: SQLite locking
# Solution: Use PostgreSQL

export NEXUS_DATABASE_URL="postgresql://nexus:password@localhost/nexus"
```

#### 4. Server Won't Start

```bash
# Check if port is in use
lsof -i :8080

# Kill existing process
pkill -f "nexus serve"

# Try different port
nexus serve --port 8081
```

#### 5. Files Not Found

```bash
# Check data directory
ls -la $NEXUS_DATA_DIR

# Check backend
nexus ls / --remote-url http://localhost:8080

# Verify database connection
echo "SELECT COUNT(*) FROM file_paths;" | psql $NEXUS_DATABASE_URL
```

#### 6. Permission Check Returns "DENIED" but Should Be "GRANTED"

**Possible causes:**

1. **Tuple not created** - Verify tuple exists:
   ```bash
   nexus rebac expand write file /workspace
   # If user is not listed, tuple doesn't exist
   ```

2. **Wrong tenant_id** - Check you're using the same tenant_id:
   ```bash
   # Create: --tenant-id default
   # Check: defaults to "default" if not specified
   ```

3. **Schema not registered** - Check namespace exists:
   ```bash
   nexus rebac namespace-get file
   # Should show parent inheritance relations
   ```

4. **Parent tuples missing** - For new files, check parent relationship:
   ```bash
   nexus rebac explain user alice write file /workspace/file.txt
   # Should show parent inheritance path
   ```

#### 7. Expand Shows Nobody Has Access

If `nexus rebac expand write file /workspace` returns empty:

1. **No tuples created** - Create initial permissions:
   ```bash
   nexus rebac create user alice direct_owner file /workspace --tenant-id default
   ```

2. **Wrong permission name** - Use exact permission from schema:
   ```bash
   # Correct: write, read, execute
   # Wrong: edit, view (these are relations, not permissions)
   ```

3. **Cached result** - Restart server or wait for cache to expire

#### 8. Explain Shows "Cycle Detected"

This indicates a schema error (circular dependency):

```bash
# BAD SCHEMA (causes cycles):
"relations": {
  "owner": {"union": ["direct_owner"]},
},
"permissions": {
  "owner": ["owner"],  # ← CIRCULAR!
}

# GOOD SCHEMA (no cycles):
"relations": {
  "owner": {"union": ["direct_owner", "parent_owner"]},
},
"permissions": {
  "write": ["editor", "owner"],  # ← Relations only, not permissions
  "read": ["viewer", "editor", "owner"],
}
```

**Fix:** Remove permission entries that conflict with relation names. See the fixed schema in the ReBAC section above.

---

## Remote Administration (Multi-Tenant)

For **production multi-tenant** scenarios where you manage multiple tenants remotely, use this workflow:

### Setup: Start Server First

```bash
# On server machine (via SSH or console)
export NEXUS_DATABASE_URL="postgresql://postgres:password@localhost/nexus"
export NEXUS_DATA_DIR="/var/lib/nexus"

# Start server
nexus serve --host 0.0.0.0 --port 8080

# Server is now running and accessible
```

### Manage Everything Remotely via CLI

Use the CLI with `--remote-url` to manage tenants from anywhere:

```bash
#!/bin/bash
# Remote admin script - manage tenants from your laptop

SERVER_URL="http://your-server.com:8080"

# Set admin context
export NEXUS_SUBJECT="user:admin"

# ============================================
# Create Tenant 1
# ============================================

# Create tenant workspace
nexus mkdir /tenant1 --remote-url $SERVER_URL
echo "✓ Created /tenant1 workspace"

# Grant owner to tenant1_admin
nexus rebac create user tenant1_admin direct_owner file /tenant1 \
  --tenant-id tenant1 \
  --remote-url $SERVER_URL
echo "✓ Granted tenant1_admin ownership"

# Grant editor to tenant1_user
nexus rebac create user tenant1_user direct_editor file /tenant1 \
  --tenant-id tenant1 \
  --remote-url $SERVER_URL
echo "✓ Granted tenant1_user editor access"

# ============================================
# Create Tenant 2
# ============================================

nexus mkdir /tenant2 --remote-url $SERVER_URL

nexus rebac create user tenant2_admin direct_owner file /tenant2 \
  --tenant-id tenant2 \
  --remote-url $SERVER_URL
echo "✓ Set up tenant2"

# ============================================
# Verify Permissions
# ============================================

# Check tenant1_admin can write to tenant1
nexus rebac check user tenant1_admin write file /tenant1 \
  --tenant-id tenant1 \
  --remote-url $SERVER_URL
echo "✓ Verified tenant1_admin access"

# Verify tenant isolation - tenant1_admin CANNOT access tenant2
if ! nexus rebac check user tenant1_admin write file /tenant2 \
  --tenant-id tenant2 \
  --remote-url $SERVER_URL 2>&1 | grep -q GRANTED; then
  echo "✓ Confirmed tenant isolation works"
fi

echo ""
echo "🎉 Multi-tenant setup complete - all managed remotely!"
```

### Key Advantages of Remote Administration:

✅ **No SSH needed** - Manage everything from your admin workstation
✅ **Multi-tenant ready** - Each tenant isolated by `tenant_id`
✅ **Scalable** - Add tenants without server access
✅ **Pure CLI** - No Python required
✅ **Secure** - Server runs continuously with permissions enabled

### Alternative: Using curl for Automation

```bash
# Create permission remotely
curl -X POST http://your-server.com:8080/api/nfs/rebac_create \
  -H "Content-Type: application/json" \
  -H "X-Nexus-Subject: user:admin" \
  -d '{
    "subject": ["user", "alice"],
    "relation": "direct_owner",
    "object": ["file", "/tenant1"],
    "tenant_id": "tenant1"
  }'

# Check permission remotely
curl -X POST http://your-server.com:8080/api/nfs/rebac_check \
  -H "Content-Type: application/json" \
  -H "X-Nexus-Subject: user:admin" \
  -d '{
    "subject": ["user", "alice"],
    "permission": "write",
    "object": ["file", "/tenant1"],
    "tenant_id": "tenant1"
  }'
```

### When to Use Each Approach:

| Approach | Best For | Pros | Cons |
|----------|----------|------|------|
| **Local Setup** (SSH to server) | Single-tenant, self-hosted | Simple, direct database access | Requires server access |
| **Remote Admin** (CLI --remote-url) | Multi-tenant SaaS | Scalable, pure CLI, no SSH needed | Requires running server |
| **Remote Admin** (curl) | Automation, CI/CD | Language-agnostic, scriptable | More verbose |

---

## Complete Example: End-to-End Setup

```bash
#!/bin/bash
# complete_cli_setup.sh - Complete Nexus setup using only CLI

set -e

echo "=== Nexus Server Complete Setup (CLI Only) ==="

# 1. Install PostgreSQL
if ! command -v psql &> /dev/null; then
    echo "Installing PostgreSQL..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install postgresql
        brew services start postgresql
    else
        sudo apt-get update
        sudo apt-get install -y postgresql
        sudo systemctl start postgresql
    fi
fi

# 2. Create database
createdb nexus 2>/dev/null || echo "Database already exists"

# 3. Set environment
export NEXUS_DATABASE_URL="postgresql://$(whoami)@localhost/nexus"
export NEXUS_DATA_DIR="/tmp/nexus-demo"
export NEXUS_ENFORCE_PERMISSIONS=false

mkdir -p $NEXUS_DATA_DIR

# 4. Bootstrap
echo ""
echo "=== Bootstrapping ==="

nexus mkdir /workspace
nexus mkdir /workspace/engineering
nexus mkdir /workspace/marketing

# Grant permissions
nexus rebac create \
  --subject-type user --subject-id alice \
  --relation direct_owner \
  --object-type file --object-id /workspace/engineering \
  --tenant-id default

nexus rebac create \
  --subject-type user --subject-id bob \
  --relation direct_editor \
  --object-type file --object-id /workspace/engineering \
  --tenant-id default

echo "✓ Bootstrap complete"

# 5. Start server in background
export NEXUS_ENFORCE_PERMISSIONS=true
nohup nexus serve --host 127.0.0.1 --port 8080 > /tmp/nexus.log 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > /tmp/nexus.pid

# Wait for server to start
sleep 2
echo "✓ Server started (PID: $SERVER_PID)"

# 6. Test access
echo ""
echo "=== Testing Access ==="

export NEXUS_REMOTE_URL="http://127.0.0.1:8080"

# Wait for server
for i in {1..10}; do
    if curl -s http://127.0.0.1:8080/health > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

# Alice writes file
export USER=alice
nexus write /workspace/engineering/alice.txt "Hello from Alice" --remote-url $NEXUS_REMOTE_URL
echo "✓ Alice wrote file"

# Alice reads back
content=$(nexus read /workspace/engineering/alice.txt --remote-url $NEXUS_REMOTE_URL)
echo "✓ Alice read: $content"

# Bob reads file (editor access)
export USER=bob
bob_content=$(nexus read /workspace/engineering/alice.txt --remote-url $NEXUS_REMOTE_URL)
echo "✓ Bob read: $bob_content"

echo ""
echo "=== Setup Complete! ==="
echo "Server running at: http://127.0.0.1:8080"
echo "PID: $SERVER_PID"
echo "Logs: tail -f /tmp/nexus.log"
echo "Stop: kill $SERVER_PID"
```

Run it:

```bash
chmod +x complete_cli_setup.sh
./complete_cli_setup.sh
```

---

## Quick Reference

```bash
# Server
nexus serve --host 0.0.0.0 --port 8080

# Permissions
nexus rebac create --subject-type user --subject-id alice --relation direct_owner --object-type file --object-id /workspace --tenant-id default
nexus rebac check --subject-type user --subject-id alice --permission write --object-type file --object-id /workspace --tenant-id default
nexus rebac list --object-type file --object-id /workspace --tenant-id default
nexus rebac delete --subject-type user --subject-id alice --relation direct_owner --object-type file --object-id /workspace --tenant-id default

# Files (Remote)
nexus write /path/file.txt "content" --remote-url http://localhost:8080
nexus read /path/file.txt --remote-url http://localhost:8080
nexus ls /path --remote-url http://localhost:8080
nexus delete /path/file.txt --remote-url http://localhost:8080

# Files (Local)
nexus write /path/file.txt "content"
nexus read /path/file.txt
nexus ls /path
```

---

For more information:
- Full Guide (with Python): `docs/SERVER_SETUP_GUIDE.md`
- API Documentation: `docs/api/`
- Permission System: `docs/PERMISSION_SYSTEM.md`
