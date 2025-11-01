# Setup Guide for LangGraph + Nexus Demo

This guide explains how to set up the auth-enabled Nexus server required for the permission demo.

## Why Auth-Enabled Server?

The Nexus version of this demo showcases **permission-based access control** using ReBAC (Relationship-Based Access Control). This requires:

1. **Authentication** - Each agent needs an identity
2. **PostgreSQL** - Stores users, API keys, and permission relationships
3. **ReBAC** - Enforces fine-grained permissions

A plain `nexus serve` server doesn't have these features - it's for simple local development only.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  LangGraph Multi-Agent Workflow                     │
│                                                      │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐        │
│  │Researcher│──>│  Coder   │──>│ Reviewer │        │
│  └──────────┘   └──────────┘   └──────────┘        │
│       │              │              │               │
│       └──────────────┴──────────────┘               │
│                      │                              │
└──────────────────────┼──────────────────────────────┘
                       │ HTTP + Auth
                       ↓
          ┌────────────────────────┐
          │  Nexus Server          │
          │  (Auth-Enabled)        │
          │                        │
          │  ✓ User Management     │
          │  ✓ API Key Auth        │
          │  ✓ ReBAC Permissions   │
          └────────────────────────┘
                       │
                       ↓
          ┌────────────────────────┐
          │  PostgreSQL Database   │
          │                        │
          │  • Users & API Keys    │
          │  • Permission Tuples   │
          │  • File Metadata       │
          └────────────────────────┘
```

## One-Time Setup

### 1. Install PostgreSQL

**macOS (Homebrew):**
```bash
brew install postgresql
brew services start postgresql
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

**Verify:**
```bash
psql --version
# Should output: psql (PostgreSQL) 14.x or higher
```

### 2. Create Nexus Database

```bash
# Create database
createdb nexus

# Set postgres password (required by Nexus)
psql nexus -c "ALTER USER postgres WITH PASSWORD 'nexus';"

# Test connection
psql nexus -c "SELECT version();"
```

**Expected connection string:**
```
postgresql://postgres:nexus@localhost/nexus
```

### 3. Initialize Nexus with Auth

```bash
# From the nexus repository root
./scripts/init-nexus-with-auth.sh --init
```

**What this does:**
1. Creates database schema (users, api_keys, rebac_tuples, etc.)
2. Creates admin user (username: `admin`)
3. Generates admin API key with full permissions
4. Starts Nexus server on `http://localhost:8080`
5. Saves credentials to `.nexus-admin-env`

**You will see:**
```
╔═══════════════════════════════════════╗
║   Nexus Server Init (With Auth)      ║
╚═══════════════════════════════════════╝

⚠️  WARNING: This will DELETE ALL existing data!

Are you sure you want to continue? (yes/no):
```

**Type `yes` to proceed.**

### 4. Load Admin Credentials

```bash
# This sets NEXUS_URL and NEXUS_API_KEY
source .nexus-admin-env
```

**Check it worked:**
```bash
echo $NEXUS_URL
# Should output: http://localhost:8080

echo $NEXUS_API_KEY
# Should output: your-api-key

# Test API access
curl -H "Authorization: Bearer $NEXUS_API_KEY" $NEXUS_URL/api/auth/me
```

## Running the Demo

```bash
# 1. Make sure server is running and credentials loaded
source .nexus-admin-env

# 2. Set OpenAI key
export OPENAI_API_KEY="sk-..."

# 3. Navigate to example
cd examples/langgraph_integration

# 4. Run demo
./run_nexus_demo.sh
```

## Subsequent Runs

After the initial setup, you only need to:

```bash
# Restart server (keeps all data)
./scripts/init-nexus-with-auth.sh

# Load credentials
source .nexus-admin-env

# Run demo
cd examples/langgraph_integration
./run_nexus_demo.sh
```

**No need to re-initialize unless you want to wipe all data.**

## How Permissions Work

The demo sets up these permissions:

```python
# Researcher can write to /workspace/research/
admin_nx.rebac_create(
    subject=("agent", "researcher"),
    relation="direct_editor",
    object=("file", "/workspace/research")
)

# Coder can read /workspace/research/ (needs requirements)
admin_nx.rebac_create(
    subject=("agent", "coder"),
    relation="direct_viewer",
    object=("file", "/workspace/research")
)

# Coder can write to /workspace/code/
admin_nx.rebac_create(
    subject=("agent", "coder"),
    relation="direct_editor",
    object=("file", "/workspace/code")
)

# ... etc for reviewer
```

**Each agent connects with its identity:**
```python
nexus = RemoteNexusFS(
    server_url=os.getenv("NEXUS_URL"),
    api_key=os.getenv("NEXUS_API_KEY")
)
nexus.agent_id = "researcher"  # Identity for permission checks
```

When the agent tries to access a file, Nexus checks:
1. Who is this agent? (`nexus.agent_id`)
2. What file are they accessing? (`/workspace/research/requirements.txt`)
3. Do they have permission? (Check ReBAC tuples)
4. Grant or deny access

## Troubleshooting

### PostgreSQL not running

```bash
# macOS
brew services start postgresql

# Linux
sudo systemctl start postgresql

# Check status
brew services list | grep postgresql  # macOS
sudo systemctl status postgresql      # Linux
```

### Database doesn't exist

```bash
createdb nexus
```

### Connection refused

Check that PostgreSQL is listening:
```bash
netstat -an | grep 5432
# Should show: tcp4  0  0  127.0.0.1.5432  *.*  LISTEN
```

### Wrong password

Reset postgres password:
```bash
psql postgres -c "ALTER USER postgres WITH PASSWORD 'nexus';"
```

### API key not working

Regenerate by re-initializing (WARNING: deletes all data):
```bash
./scripts/init-nexus-with-auth.sh --init
source .nexus-admin-env
```

### Permission errors when running demo

**This is expected!** The demo includes a section that intentionally tests unauthorized access:

```
❌ Test: Can reviewer write to /code/? (Should be denied)
  ✓ Access denied: Permission denied
```

This proves permissions are working correctly.

## Database Schema

The auth-enabled server creates these key tables:

- **users** - User accounts
- **api_keys** - API keys for authentication
- **rebac_tuples** - Permission relationships (subject, relation, object)
- **rebac_namespaces** - Permission schema definitions
- **file_metadata** - File ownership and attributes
- **trajectories** - Agent execution tracking
- **workflow_runs** - Workflow execution history

## Advanced: Custom Database URL

Override the default connection:

```bash
export NEXUS_DATABASE_URL="postgresql://user:pass@host:5432/dbname"
./scripts/init-nexus-with-auth.sh --init
```

## Advanced: Custom Admin User

```bash
NEXUS_ADMIN_USER=alice ./scripts/init-nexus-with-auth.sh --init
```

## Cleaning Up

To completely reset:

```bash
# Stop server
pkill -f "nexus.*serve"

# Drop database
dropdb nexus

# Remove data directory
rm -rf ./nexus-data

# Remove credentials
rm .nexus-admin-env

# Start fresh
createdb nexus
./scripts/init-nexus-with-auth.sh --init
```

## Comparison: Auth vs Non-Auth Server

| Feature | `nexus serve` | `init-nexus-with-auth.sh` |
|---------|---------------|---------------------------|
| **Authentication** | None | API key based |
| **Permissions** | None | ReBAC (fine-grained) |
| **Database** | SQLite | PostgreSQL |
| **Multi-user** | No | Yes |
| **Production-ready** | No | Yes |
| **Setup complexity** | Simple | Moderate |
| **Use case** | Local dev | Enterprise/demo |

## Next Steps

Once you have the server running:

1. **Run the demo** - See permissions in action
2. **Modify permissions** - Edit `setup_nexus_permissions()` in `multi_agent_nexus.py`
3. **Add agents** - Create tester, deployer, etc. with different permissions
4. **Explore ReBAC** - Try `nexus rebac check`, `nexus rebac explain`
5. **Read comparison** - Check `COMPARISON.md` for code-level details

## Questions?

- Check `QUICKSTART.md` for a condensed version
- Check `README.md` for full documentation
- Check `COMPARISON.md` for code comparison
- Read Nexus docs at https://docs.nexus.ai (if available)
