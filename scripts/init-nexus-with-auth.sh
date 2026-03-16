#!/bin/bash
# init-nexus-with-auth.sh - Restart or initialize Nexus server with authentication
#
# Usage:
#   ./init-nexus-with-auth.sh                    # Restart server (default)
#   ./init-nexus-with-auth.sh --init             # Full initialization (clean DB, create admin key)
#   NEXUS_ADMIN_USER=alice ./init-nexus-with-auth.sh --init  # Custom admin user for init

set -e  # Exit on error

# ============================================
# Parse Arguments
# ============================================

INIT_MODE=false
if [ "$1" == "--init" ]; then
    INIT_MODE=true
fi

# If SKIP_CONFIRM is set, automatically enable INIT mode for clean database
if [ "$SKIP_CONFIRM" == "1" ]; then
    INIT_MODE=true
fi

# ============================================
# Configuration
# ============================================

export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://postgres:nexus@localhost/nexus}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
PORT="${NEXUS_PORT:-2026}"
HOST="${NEXUS_HOST:-0.0.0.0}"

# ============================================
# Banner
# ============================================

# Skip banner if QUIET mode is enabled
if [ "$QUIET" != "1" ]; then

if [ "$INIT_MODE" = true ]; then
    cat << 'EOF'
╔═══════════════════════════════════════╗
║   Nexus Server Init (With Auth)      ║
╚═══════════════════════════════════════╝
EOF
    echo ""
    echo "Mode: INITIALIZATION"
    echo ""

    # Show what will be deleted
    if [ "$CLEAN_CREDENTIALS" == "1" ]; then
        echo "⚠️  WARNING: This will DELETE ALL existing data AND credentials!"
        echo ""
        echo "The following will be cleared:"
        echo "  • All users and API keys (CREDENTIALS)"
        echo "  • All files and metadata"
        echo "  • All permissions and relationships"
        echo "  • All workspaces and configurations"
        echo "  • All operation logs and caches"
    else
        echo "⚠️  WARNING: This will DELETE data but PRESERVE credentials!"
        echo ""
        echo "The following will be cleared:"
        echo "  • All files and metadata"
        echo "  • All permissions and relationships"
        echo "  • All workspaces and configurations"
        echo "  • All operation logs and caches"
        echo ""
        echo "The following will be PRESERVED:"
        echo "  ✓ All users and API keys (existing credentials still work)"
    fi
    echo ""
    echo "Configuration:"
    echo "  Admin user:  $ADMIN_USER"
    echo "  Database:    $NEXUS_DATABASE_URL"
    echo "  Data dir:    $NEXUS_DATA_DIR"
    echo "  Server:      http://$HOST:$PORT"
    echo "  Auth:        Database-backed API keys"
    echo ""

    # Confirmation prompt (skip if AUTO_CONFIRM is set)
    if [ "$AUTO_CONFIRM" != "1" ]; then
        read -p "Are you sure you want to continue? (yes/no): " CONFIRM
        if [ "$CONFIRM" != "yes" ]; then
            echo ""
            echo "❌ Initialization cancelled"
            echo ""
            echo "To restart without initialization, run:"
            echo "  ./scripts/init-nexus-with-auth.sh"
            echo ""
            exit 0
        fi
        echo ""
    fi
    echo "✓ Confirmed - proceeding with initialization..."
    echo ""
else
    cat << 'EOF'
╔═══════════════════════════════════════╗
║   Nexus Server Restart (With Auth)   ║
╚═══════════════════════════════════════╝
EOF
    echo ""
    echo "Mode: RESTART (skip initialization)"
    echo ""
    echo "Configuration:"
    echo "  Server:      http://$HOST:$PORT"
    echo "  Auth:        Database-backed API keys"
    echo ""
    echo "Use --init flag for full initialization"
    echo ""
fi

fi  # End QUIET check

# ============================================
# Prerequisites Check
# ============================================

if ! command -v nexus &> /dev/null; then
    echo "❌ Error: 'nexus' command not found"
    echo "   Install with: pip install nexus-ai-fs"
    exit 1
fi

if [ "$INIT_MODE" = true ] && ! command -v python3 &> /dev/null; then
    echo "❌ Error: 'python3' not found (needed for API key creation)"
    exit 1
fi

# ============================================
# Initialization Steps (Only in --init mode)
# ============================================

if [ "$INIT_MODE" = true ]; then

# ============================================
# Database Setup
# ============================================

echo "📦 Setting up database..."

# Try to create database
if command -v createdb &> /dev/null; then
    if createdb nexus 2>/dev/null; then
        echo "✓ Created database 'nexus'"
    else
        echo "✓ Database exists"
    fi
fi

# Test database connection
echo ""
echo "🔌 Testing database connection..."

# Ensure we use embedded mode, not remote mode
unset NEXUS_URL
unset NEXUS_API_KEY

if ! python3 -c "from sqlalchemy import create_engine; engine = create_engine('$NEXUS_DATABASE_URL'); engine.connect().close()" 2>/tmp/nexus-init-error.log; then
    echo ""
    echo "❌ Cannot connect to database!"
    echo ""
    echo "Error details:"
    cat /tmp/nexus-init-error.log
    echo ""
    echo "Please check docs/deployment/postgresql.md for database setup instructions."
    exit 1
fi

echo "✓ Database connection successful"

# ============================================
# Create Database Schema
# ============================================

echo ""
echo "📊 Creating database schema..."

# Create tables via SQLAlchemy models (simpler than migrations for fresh install)
if ! python3 -c "
from nexus.core.nexus_fs import NexusFS
from nexus.backends.storage.cas_local import CASCASLocalBackend
backend = CASLocalBackend('$NEXUS_DATA_DIR')
nfs = NexusFS(backend, db_path='$NEXUS_DATABASE_URL')
nfs.close()
" 2>/tmp/nexus-schema-error.log; then
    echo "❌ Failed to create database schema!"
    echo ""
    echo "Error details:"
    cat /tmp/nexus-schema-error.log
    echo ""
    exit 1
fi

# Mark database as up-to-date with latest migration
if command -v alembic &> /dev/null; then
    LATEST_MIGRATION=$(alembic heads 2>/dev/null | head -1 | awk '{print $1}')
    if [ -n "$LATEST_MIGRATION" ]; then
        alembic stamp "$LATEST_MIGRATION" 2>/dev/null || true
    fi
fi

echo "✓ Database schema created"

# ============================================
# Clean Database (Fresh Start)
# ============================================

echo ""
echo "🧹 Clearing existing data for fresh start..."
echo ""
if [ "$CLEAN_CREDENTIALS" == "1" ]; then
    echo "This will remove:"
    echo "  • All users and their API keys"
    echo "  • All files, directories, and metadata"
    echo "  • All permissions and access control relationships"
    echo "  • All workspaces, memories, and workflows"
    echo "  • All operation logs and audit trails"
else
    echo "This will remove:"
    echo "  • All files, directories, and metadata"
    echo "  • All permissions and access control relationships"
    echo "  • All workspaces, memories, and workflows"
    echo "  • All operation logs and audit trails"
    echo ""
    echo "This will preserve:"
    echo "  ✓ All users and their API keys (credentials)"
fi
echo ""

# Clear filesystem data (to stay in sync with database)
if [ -d "$NEXUS_DATA_DIR" ]; then
    echo "Clearing filesystem data: $NEXUS_DATA_DIR"
    rm -rf "$NEXUS_DATA_DIR"/*
    echo "✓ Cleared filesystem data"
fi

# Clear all data from key tables
python3 << 'PYTHON_CLEANUP'
from sqlalchemy import create_engine, text
import os
import sys

db_url = os.environ.get('NEXUS_DATABASE_URL')
if not db_url:
    print("ERROR: NEXUS_DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

engine = create_engine(db_url)
deleted_counts = {}

# Helper to delete with proper transaction handling
def delete_table(table_name):
    """Delete all rows from a table, with proper error handling."""
    try:
        with engine.connect() as conn:
            # Start a new transaction for each delete
            trans = conn.begin()
            try:
                result = conn.execute(text(f"DELETE FROM {table_name}"))
                count = result.rowcount
                trans.commit()
                deleted_counts[table_name] = count
                if count > 0:
                    print(f"  Deleted {count} rows from {table_name}")
                return True
            except Exception as e:
                trans.rollback()
                # Only ignore "table doesn't exist" errors
                if "does not exist" in str(e).lower():
                    return False
                print(f"  WARNING: Failed to clear {table_name}: {e}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"  ERROR: Cannot connect to delete from {table_name}: {e}", file=sys.stderr)
        return False

# Clear in dependency order
print("Clearing database tables:")

# Clear auth-related tables first (due to foreign keys) - only if CLEAN_CREDENTIALS is set
clean_credentials = os.environ.get('CLEAN_CREDENTIALS') == '1'
if clean_credentials:
    print("\n🔑 Clearing authentication data...")
    delete_table("refresh_tokens")
    delete_table("api_keys")
    delete_table("users")  # Clear all users
else:
    print("\n🔑 Preserving authentication data (users and API keys)...")

# Clear ReBAC and audit tables
print("\n🔐 Clearing permissions and audit logs...")
delete_table("rebac_check_cache")
delete_table("rebac_changelog")
delete_table("admin_bypass_audit")
delete_table("operation_log")
delete_table("rebac_tuples")

# Clear entity registry (agents, sessions, etc.)
print("\n🤖 Clearing entity registry...")
delete_table("entity_registry")

# Clear file-related tables (dependencies: content_chunks -> file_metadata -> file_paths)
print("\n📁 Clearing file system data...")
delete_table("content_chunks")
delete_table("document_chunks")
delete_table("version_history")
delete_table("file_metadata")
delete_table("file_paths")

# Clear memory and workspace tables
print("\n🧠 Clearing workspaces and memories...")
delete_table("memories")
delete_table("memory_configs")
delete_table("workspace_snapshots")
delete_table("workspace_configs")

# Clear workflow tables
print("\n⚙️  Clearing workflows...")
delete_table("workflow_executions")
delete_table("workflows")

# Clear mount configs
print("\n🔌 Clearing mount configurations...")
delete_table("mount_configs")

# Now outside the function, print summary
total = sum(deleted_counts.values())
print("\n" + "="*50)
if total > 0:
    print(f"✅ Successfully cleared {total} total rows from {len(deleted_counts)} tables")
    print("\nDeleted data:")
    for table, count in sorted(deleted_counts.items()):
        if count > 0:
            print(f"  • {table}: {count} rows")
else:
    print("✅ Database was already empty")
print("="*50 + "\n")

PYTHON_CLEANUP

if [ $? -ne 0 ]; then
    echo "❌ Failed to clean database"
    exit 1
fi

# ============================================
# Bootstrap (With Admin API Key)
# ============================================

echo ""
echo "🔧 Bootstrapping server..."

# ============================================
# Create Admin API Key (Only if credentials were cleaned)
# ============================================

if [ "$CLEAN_CREDENTIALS" == "1" ]; then
    echo ""
    echo "🔑 Creating admin API key..."

    # Get script directory
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

    # Create admin API key (90 day expiry)
    ADMIN_KEY_OUTPUT=$(python3 "$SCRIPT_DIR/create-api-key.py" \
        "$ADMIN_USER" \
        "Admin key (created by init script)" \
        --admin \
        --days 90 \
        2>&1)

    # Extract the API key from output
    ADMIN_API_KEY=$(echo "$ADMIN_KEY_OUTPUT" | grep "API Key:" | awk '{print $3}')

    if [ -z "$ADMIN_API_KEY" ]; then
        echo "❌ Failed to create admin API key"
        echo "$ADMIN_KEY_OUTPUT"
        exit 1
    fi

    echo "✓ Created admin API key"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "IMPORTANT: Save this API key securely!"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Admin API Key: $ADMIN_API_KEY"
    echo ""
    echo "Add to your ~/.bashrc or ~/.zshrc:"
    echo "  export NEXUS_API_KEY='$ADMIN_API_KEY'"
    echo "  export NEXUS_URL='http://localhost:$PORT'"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""

    # Save to env file for this session
    cat > .nexus-admin-env << EOF
# Nexus Admin Environment
# Created: $(date)
# User: $ADMIN_USER
export NEXUS_API_KEY='$ADMIN_API_KEY'
export NEXUS_URL='http://localhost:$PORT'
export NEXUS_DATABASE_URL='$NEXUS_DATABASE_URL'
EOF

    echo "✓ Saved to .nexus-admin-env (source this file to use the API key)"
    echo ""
else
    echo ""
    echo "🔑 Preserving existing credentials..."
    echo ""
    echo "ℹ️  Existing users and API keys were preserved."
    echo "   Use your existing API key or create new keys with:"
    echo "     python3 scripts/create-api-key.py <username> \"Description\" --days 90"
    echo ""
    echo "   If you have .nexus-admin-env from before, source it:"
    echo "     source .nexus-admin-env"
    echo ""
fi

# ============================================
# Setup Workspace (Direct Database Access)
# ============================================

echo "🔧 Setting up workspace..."

# Since we're not running a server yet, use direct database access
# (permissions disabled for initial setup)
export NEXUS_ENFORCE_PERMISSIONS=false

# Create workspace directory
nexus mkdir /workspace 2>/dev/null && echo "✓ Created /workspace" || echo "✓ /workspace exists"

# Only grant ownership if we just created the admin user (credentials were cleaned)
if [ "$CLEAN_CREDENTIALS" == "1" ]; then
    # Grant admin user full ownership
    nexus rebac create user $ADMIN_USER direct_owner file /workspace --zone-id default >/dev/null 2>&1
    echo "✓ Granted '$ADMIN_USER' ownership of /workspace"
else
    echo "✓ Workspace permissions preserved (existing user permissions maintained)"
fi

# Re-enable permissions for server
export NEXUS_ENFORCE_PERMISSIONS=true

fi  # End of INIT_MODE

# ============================================
# Port Cleanup (Kill existing processes)
# ============================================

echo "🔍 Checking port $PORT..."

# Find and kill any process using the port
if command -v lsof &> /dev/null; then
    PID=$(lsof -ti:$PORT 2>/dev/null || true)
    if [ -n "$PID" ]; then
        echo "⚠️  Port $PORT is in use by process $PID"
        echo "   Killing process..."
        kill -9 $PID 2>/dev/null || true
        sleep 1
        echo "✓ Port $PORT is now available"
    else
        echo "✓ Port $PORT is available"
    fi
else
    # Fallback for systems without lsof (e.g., some Linux)
    if netstat -an 2>/dev/null | grep -q ":$PORT.*LISTEN"; then
        echo "⚠️  Port $PORT appears to be in use"
        echo "   Please manually stop the process using port $PORT"
        echo "   Or set NEXUS_PORT to a different port: export NEXUS_PORT=8081"
    fi
fi

# ============================================
# Start Server (With Authentication)
# ============================================

echo ""
if [ "$INIT_MODE" = true ]; then
    echo "╔═══════════════════════════════════════╗"
    echo "║   ✅ Setup Complete!                  ║"
    echo "╚═══════════════════════════════════════╝"
    echo ""
    echo "Starting Nexus server with authentication..."
    echo ""
    echo "Server URL: http://$HOST:$PORT"
    echo "Admin user: $ADMIN_USER"
    echo "Auth type:  Database-backed API keys"
    echo ""
    echo "Quick start:"
    echo "  source .nexus-admin-env"
    echo "  nexus ls /workspace"
    echo ""
    echo "Create more users with:"
    echo "  python3 scripts/create-api-key.py alice \"Alice's key\" --days 90"
    echo ""
    echo "Press Ctrl+C to stop server"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
else
    if [ "$QUIET" != "1" ]; then
        echo "╔═══════════════════════════════════════╗"
        echo "║   ✅ Restart Complete!                ║"
        echo "╚═══════════════════════════════════════╝"
        echo ""
        echo "Starting Nexus server with authentication..."
        echo ""
        echo "Server URL: http://$HOST:$PORT"
        echo "Auth type:  Database-backed API keys"
        echo ""
        echo "Quick start:"
        echo "  source .nexus-admin-env  # If you have it"
        echo "  nexus ls /workspace"
        echo ""
        echo "Press Ctrl+C to stop server"
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo ""
    fi
fi

# Unset NEXUS_URL to prevent server from using remote NexusFS (circular dependency)
unset NEXUS_URL

# Start server with database auth (redirect logs to file)
LOG_FILE="${NEXUS_DATA_DIR}/server.log"
if [ "$QUIET" != "1" ]; then
    echo "Server logs: $LOG_FILE"
    echo ""
fi

# Set logging level to ERROR in quiet mode to suppress INFO/WARNING logs
if [ "$QUIET" = "1" ]; then
    export NEXUS_LOG_LEVEL=ERROR
fi

# Enable admin bypass for dev/demo environments (allows admin users to bypass ReBAC checks)
# In production, set NEXUS_ALLOW_ADMIN_BYPASS=false or leave unset for security
export NEXUS_ALLOW_ADMIN_BYPASS="${NEXUS_ALLOW_ADMIN_BYPASS:-true}"

nexus serve --host $HOST --port $PORT --auth-type database --async > "$LOG_FILE" 2>&1
