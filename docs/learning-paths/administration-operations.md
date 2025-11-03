# Administration & Operations

**User management, API keys, monitoring, and operational tasks**

⏱️ **Time:** 25 minutes | 💡 **Difficulty:** Hard

## What You'll Learn

- Start Nexus in server mode with authentication
- Create and manage users via CLI and API
- Generate and manage API keys
- Configure ReBAC permissions for users and groups
- Monitor filesystem operations and health
- Perform backup and restore operations
- Manage database migrations
- Configure advanced server settings

## Prerequisites

✅ Python 3.8+ installed
✅ Nexus installed (`pip install nexus-ai-fs`)
✅ PostgreSQL database (for production server mode)
✅ Basic understanding of authentication and authorization
✅ Familiarity with command-line operations

## Overview

**Administration & Operations** covers everything needed to run Nexus in production:

- **👥 User Management** - Create users, agents, and manage identities
- **🔑 API Key Lifecycle** - Generate, rotate, and revoke keys
- **🔒 Permission Management** - Configure ReBAC policies
- **📊 Monitoring** - Health checks, metrics, and logging
- **💾 Backup & Recovery** - Data protection strategies
- **🔧 Maintenance** - Database migrations and upgrades

**Server Modes:**

```
┌─────────────────────────────────────────────────────────┐
│  Embedded Mode (Development)                            │
│  ✓ No server required, direct filesystem access        │
│  ✓ Single-user, no authentication                      │
│  ✓ Perfect for prototyping and testing                 │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  Server Mode (Production)                               │
│  ✓ Multi-user with authentication                      │
│  ✓ ReBAC permissions and access control                │
│  ✓ Database-backed user management                     │
│  ✓ API key lifecycle management                        │
│  ✓ Audit trails and monitoring                         │
└─────────────────────────────────────────────────────────┘
```

---

## Step 1: Start Nexus Server

Start Nexus server with database authentication:

### Quick Start with Script

```bash
# Initialize server with admin user (easiest method)
./scripts/init-nexus-with-auth.sh

# This will:
# 1. Start PostgreSQL (if not running)
# 2. Initialize database schema
# 3. Create admin user
# 4. Generate API key
# 5. Save credentials to .nexus-admin-env

# Load credentials
source .nexus-admin-env

# Verify
echo $NEXUS_URL      # http://localhost:8080
echo $NEXUS_API_KEY  # nxk_abc123...
```

### Manual Server Setup

```bash
# Start PostgreSQL
docker run -d --name nexus-postgres \
  -e POSTGRES_PASSWORD=nexus \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_DB=nexus \
  -p 5432:5432 \
  postgres:15

# Initialize database and start server
nexus serve --host 0.0.0.0 --port 8080 \
  --database-url "postgresql://postgres:nexus@localhost/nexus" \
  --auth-type database \
  --init

# Server will output:
# ✓ Database schema initialized
# ✓ Admin user created: admin
# ✓ API key: nxk_abc123...
# ✓ Server listening on http://0.0.0.0:8080
```

**Important:** Save the admin API key! You'll need it for all administrative operations.

### Verify Server Health

```bash
# Check health endpoint
curl http://localhost:8080/health

# Expected response:
# {"status":"ok","version":"0.5.2"}
```

---

## Step 2: User Management via CLI

Nexus provides CLI commands for user management:

### Create Users

```bash
# Create a regular user
nexus admin create-user alice \
  --name "Alice Johnson" \
  --subject-type user

# Expected output:
# ✓ User created: alice
# ✓ API key: nxk_def456...
# Save this API key for alice!

# Create an agent user (for autonomous agents)
nexus admin create-user bot-analyzer \
  --name "Data Analyzer Bot" \
  --subject-type agent

# Create user with custom tenant
nexus admin create-user bob \
  --name "Bob Smith" \
  --tenant-id acme-corp
```

### List Users

```bash
# List all users
nexus admin list-users

# Expected output:
# Users:
#   • admin (user) - tenant: default
#   • alice (user) - tenant: default
#   • bot-analyzer (agent) - tenant: default
#   • bob (user) - tenant: acme-corp
```

### Delete Users

```bash
# Delete user by username
nexus admin delete-user alice

# Delete user by ID
nexus admin delete-user-by-id user123
```

---

## Step 3: User Management via Python API

Manage users programmatically:

```python
# user_management.py
import nexus
import asyncio

async def main():
    # Connect as admin
    nx = nexus.connect(config={
        "url": "http://localhost:8080",
        "api_key": "your-admin-api-key"
    })

    # Create user (requires admin permissions)
    from nexus.core.nexus_fs import NexusFS
    if isinstance(nx, NexusFS):
        user_id, api_key = await nx.auth_manager.create_user(
            username="charlie",
            display_name="Charlie Brown",
            subject_type="user",
            tenant_id="default"
        )
        print(f"✓ Created user: charlie")
        print(f"  User ID: {user_id}")
        print(f"  API Key: {api_key}")

        # List all users
        users = await nx.auth_manager.list_users()
        print("\nAll users:")
        for user in users:
            print(f"  • {user['username']} ({user['subject_type']})")

asyncio.run(main())
```

---

## Step 4: API Key Management

### Create Agent API Keys

```bash
# Create API key for an agent
nexus admin create-agent-key analyzer-bot \
  --name "Analyzer Bot v2.0"

# Expected output:
# ✓ Agent key created
# ✓ API key: nxk_agent_xyz789...

# List all agents
nexus admin list-agents

# Expected output:
# Agents:
#   • analyzer-bot - Analyzer Bot v2.0
#   • data-processor - Data Processing Agent
```

### Key Rotation Strategy

```python
# rotate_keys.py
"""
API key rotation for security best practices
"""
import asyncio
import nexus

async def rotate_user_key(admin_nx, username):
    """Rotate API key for a user"""
    # Create new key
    user_id, new_key = await admin_nx.auth_manager.create_user(
        username=f"{username}-new",
        display_name=username,
        subject_type="user",
        tenant_id="default"
    )

    print(f"✓ New key created for {username}")
    print(f"  New API key: {new_key}")
    print(f"  Update your application with this key")
    print(f"  Old key will be revoked in 7 days")

    return new_key

async def main():
    admin_nx = nexus.connect(config={
        "url": "http://localhost:8080",
        "api_key": "admin-key"
    })

    # Rotate key
    new_key = await rotate_user_key(admin_nx, "alice")

asyncio.run(main())
```

---

## Step 5: Permission Management

Configure ReBAC permissions for users and groups:

### Grant User Permissions

```bash
# Grant alice read access to /workspace
nexus rebac create \
  --subject user:alice \
  --relation can_read \
  --object file:/workspace

# Grant write access
nexus rebac create \
  --subject user:alice \
  --relation can_write \
  --object file:/workspace/alice-files

# Grant admin role (all permissions)
nexus rebac create \
  --subject user:alice \
  --relation admin \
  --object namespace:default
```

### Create Groups and Assign Members

```bash
# Create engineering group with alice as member
nexus rebac create \
  --subject user:alice \
  --relation member \
  --object group:engineering

# Grant group permissions
nexus rebac create \
  --subject group:engineering \
  --relation can_read \
  --object file:/shared/engineering

nexus rebac create \
  --subject group:engineering \
  --relation can_write \
  --object file:/shared/engineering
```

### Verify Permissions

```bash
# Check if alice can read /workspace
nexus rebac check \
  --subject user:alice \
  --relation can_read \
  --object file:/workspace

# Expected output:
# ✓ Allowed: user:alice can_read file:/workspace

# List all permissions for alice
nexus rebac list-tuples --subject user:alice

# Expected output:
# Permissions for user:alice:
#   • can_read → file:/workspace
#   • can_write → file:/workspace/alice-files
#   • member → group:engineering
```

---

## Step 6: Monitoring and Health Checks

Monitor your Nexus server:

### Health Check Endpoint

```bash
# Basic health check
curl http://localhost:8080/health

# Response:
# {
#   "status": "ok",
#   "version": "0.5.2",
#   "database": "connected",
#   "uptime_seconds": 3600
# }
```

### Check Server Status

```python
# monitor.py
"""
Monitor Nexus server health
"""
import asyncio
import nexus
import time

async def check_health(nx):
    """Perform health checks"""
    try:
        # Test basic operations
        start = time.time()

        # Write test
        nx.write("/test/health-check.txt", b"test")

        # Read test
        content = nx.read("/test/health-check.txt")

        # Cleanup
        nx.rm("/test/health-check.txt")

        elapsed = time.time() - start

        print(f"✓ Health check passed")
        print(f"  Write/Read/Delete: {elapsed:.3f}s")
        return True

    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return False

async def main():
    nx = nexus.connect(config={
        "url": "http://localhost:8080",
        "api_key": "your-api-key"
    })

    # Run health check every 60 seconds
    while True:
        await check_health(nx)
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 7: Backup and Recovery

### Backup Strategy

```bash
# Backup PostgreSQL database
pg_dump -h localhost -U postgres nexus > nexus-backup-$(date +%Y%m%d).sql

# Backup with compression
pg_dump -h localhost -U postgres nexus | gzip > nexus-backup-$(date +%Y%m%d).sql.gz

# Backup to custom format (recommended for large databases)
pg_dump -h localhost -U postgres -Fc nexus > nexus-backup-$(date +%Y%m%d).dump
```

### Restore from Backup

```bash
# Restore from SQL dump
psql -h localhost -U postgres nexus < nexus-backup-20250103.sql

# Restore from compressed dump
gunzip -c nexus-backup-20250103.sql.gz | psql -h localhost -U postgres nexus

# Restore from custom format
pg_restore -h localhost -U postgres -d nexus nexus-backup-20250103.dump
```

### Automated Backup Script

```bash
#!/bin/bash
# backup-nexus.sh
# Automated daily backups with retention

BACKUP_DIR="/var/backups/nexus"
RETENTION_DAYS=30
DATE=$(date +%Y%m%d-%H%M%S)

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup database
pg_dump -h localhost -U postgres -Fc nexus > $BACKUP_DIR/nexus-$DATE.dump

# Compress old backups
find $BACKUP_DIR -name "*.dump" -mtime +7 -exec gzip {} \;

# Delete old backups
find $BACKUP_DIR -name "*.dump.gz" -mtime +$RETENTION_DAYS -delete

echo "✓ Backup complete: nexus-$DATE.dump"
```

```bash
# Make executable
chmod +x backup-nexus.sh

# Add to cron for daily backups at 2 AM
crontab -e
# Add: 0 2 * * * /path/to/backup-nexus.sh
```

---

## Step 8: Database Migrations

Manage database schema upgrades:

```bash
# Check current database version
alembic current

# Expected output:
# INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
# INFO  [alembic.runtime.migration] Will assume transactional DDL.
# 3c5f8d9e2a1b (head)

# Show migration history
alembic history

# Upgrade to latest version
alembic upgrade head

# Upgrade to specific version
alembic upgrade 3c5f8d9e2a1b

# Downgrade one version
alembic downgrade -1

# Show current heads
alembic heads
```

---

## Step 9: Advanced Server Configuration

Configure server settings for production:

### Environment Variables

```bash
# Database
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"

# Server
export NEXUS_HOST="0.0.0.0"
export NEXUS_PORT="8080"

# Storage
export NEXUS_DATA_DIR="/var/lib/nexus"

# Authentication
export NEXUS_AUTH_TYPE="database"  # or "none" for development

# Logging
export NEXUS_LOG_LEVEL="INFO"  # DEBUG, INFO, WARNING, ERROR

# CORS (for web applications)
export NEXUS_CORS_ORIGINS="https://app.example.com,https://admin.example.com"

# Start server
nexus serve
```

### Configuration File

```yaml
# nexus-config.yaml
server:
  host: 0.0.0.0
  port: 8080
  data_dir: /var/lib/nexus

database:
  url: postgresql://postgres:nexus@localhost/nexus

auth:
  type: database
  session_ttl: 86400  # 24 hours

storage:
  default_backend: local
  backends:
    local:
      type: local
      base_path: /var/lib/nexus/data

    s3:
      type: s3
      bucket: nexus-production
      region: us-west-2

logging:
  level: INFO
  format: json
  file: /var/log/nexus/server.log

cors:
  origins:
    - https://app.example.com
    - https://admin.example.com
  methods: [GET, POST, PUT, DELETE, PATCH]
  allow_credentials: true
```

```bash
# Start with config file
nexus serve --config nexus-config.yaml
```

---

## Step 10: Production Deployment Checklist

### Security Hardening

```bash
# ✅ Use strong database password
export NEXUS_DATABASE_URL="postgresql://postgres:strong-random-password@localhost/nexus"

# ✅ Enable TLS/SSL
nexus serve --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem

# ✅ Run as non-root user
sudo useradd -r -s /bin/false nexus
sudo -u nexus nexus serve

# ✅ Set file permissions
chmod 700 /var/lib/nexus
chown -R nexus:nexus /var/lib/nexus

# ✅ Enable firewall
sudo ufw allow 8080/tcp
sudo ufw enable
```

### Systemd Service

```ini
# /etc/systemd/system/nexus.service
[Unit]
Description=Nexus AI Filesystem Server
After=network.target postgresql.service

[Service]
Type=simple
User=nexus
Group=nexus
WorkingDirectory=/opt/nexus
Environment="NEXUS_DATABASE_URL=postgresql://postgres:password@localhost/nexus"
Environment="NEXUS_DATA_DIR=/var/lib/nexus"
ExecStart=/opt/nexus/venv/bin/nexus serve --host 0.0.0.0 --port 8080
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start service
sudo systemctl enable nexus
sudo systemctl start nexus

# Check status
sudo systemctl status nexus

# View logs
sudo journalctl -u nexus -f
```

---

## Troubleshooting

### Issue: Cannot Connect to Server

**Problem:** Connection refused or timeout

**Solution:**
```bash
# Check if server is running
curl http://localhost:8080/health

# Check server logs
sudo journalctl -u nexus -n 50

# Verify port is open
sudo netstat -tlnp | grep 8080

# Check firewall
sudo ufw status
```

---

### Issue: Permission Denied

**Problem:** Users cannot access files

**Solution:**
```bash
# Verify user exists
nexus admin list-users | grep alice

# Check permissions
nexus rebac list-tuples --subject user:alice

# Grant missing permissions
nexus rebac create \
  --subject user:alice \
  --relation can_read \
  --object file:/workspace
```

---

### Issue: Database Connection Failed

**Problem:** Cannot connect to PostgreSQL

**Solution:**
```bash
# Test database connection
psql -h localhost -U postgres -d nexus -c "SELECT 1"

# Check PostgreSQL is running
sudo systemctl status postgresql

# Verify connection string
echo $NEXUS_DATABASE_URL

# Check database exists
psql -h localhost -U postgres -l | grep nexus
```

---

## Best Practices

### 1. API Key Security

```bash
# ✅ Store keys in environment variables
export NEXUS_API_KEY="nxk_secret123"

# ✅ Use .env files (not committed to git)
echo "NEXUS_API_KEY=nxk_secret123" > .env
echo ".env" >> .gitignore

# ❌ Never hardcode keys in source code
# nx = nexus.connect(api_key="nxk_secret123")  # DON'T DO THIS!
```

### 2. Regular Backups

```bash
# Automated daily backups
# Retention: 30 days
# Test restore monthly
# Store off-site (S3, GCS)

# Example: Backup to S3
pg_dump -h localhost -U postgres -Fc nexus | \
  aws s3 cp - s3://backups/nexus/backup-$(date +%Y%m%d).dump
```

### 3. Monitoring

```python
# Set up health check monitoring
# Alert on:
#   - Server downtime
#   - Slow response times (>1s)
#   - Database connection failures
#   - Disk space low (<10% free)

import requests
import time

def health_check():
    try:
        response = requests.get("http://localhost:8080/health", timeout=5)
        return response.status_code == 200
    except:
        return False

# Run every 60 seconds
while True:
    if not health_check():
        # Send alert (email, Slack, PagerDuty)
        print("ALERT: Nexus server is down!")
    time.sleep(60)
```

### 4. Access Control

```bash
# Principle of least privilege
# Grant minimum necessary permissions

# ❌ Bad: Grant admin to everyone
nexus rebac create --subject user:alice --relation admin --object namespace:default

# ✅ Good: Grant specific permissions
nexus rebac create --subject user:alice --relation can_read --object file:/workspace/alice
nexus rebac create --subject user:alice --relation can_write --object file:/workspace/alice
```

---

## What's Next?

**Congratulations!** You've mastered Nexus administration and operations.

### 🔍 Recommended Next Steps

1. **[Building Plugins](building-plugins.md)** (30 min)
   Extend Nexus with custom functionality

2. **[Multi-Backend Storage](multi-backend-storage.md)** (20 min)
   Configure S3, GCS, and database backends

3. **[Production Deployment Guide](../production/deployment-patterns.md)**
   Deploy to cloud platforms (AWS, GCP, Azure)

### 📚 Related Concepts

- [ReBAC Explained](../concepts/rebac-explained.md)
- [Agent Permissions](../concepts/agent-permissions.md)
- [Security Checklist](../production/security-checklist.md)

### 🔧 Advanced Topics

- [Docker Deployment](../how-to/deploy/docker-compose.md)
- [Kubernetes Setup](../how-to/deploy/kubernetes.md)
- [Performance Tuning](../how-to/optimize/performance-tuning.md)

---

## Summary

🎉 **You've completed the Administration & Operations tutorial!**

**What you learned:**
- ✅ Start Nexus server with authentication
- ✅ Create and manage users via CLI and API
- ✅ Generate and rotate API keys
- ✅ Configure ReBAC permissions
- ✅ Monitor server health
- ✅ Backup and restore data
- ✅ Manage database migrations
- ✅ Deploy to production

**Key Takeaways:**
- Server mode enables multi-user collaboration
- CLI provides convenient admin operations
- ReBAC offers fine-grained access control
- Regular backups are critical for production
- Security hardening is essential
- Monitoring prevents downtime

---

**Next:** [Building Plugins →](building-plugins.md)

**Questions?** Check our [Production Guide](../production/deployment-patterns.md) or [GitHub Discussions](https://github.com/nexi-lab/nexus/discussions)
