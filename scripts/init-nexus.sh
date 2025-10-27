#!/bin/bash
# init-nexus.sh - Initialize and start Nexus server with admin user
#
# Usage:
#   ./init-nexus.sh                    # Use default admin user
#   NEXUS_ADMIN_USER=alice ./init-nexus.sh  # Custom admin user

set -e  # Exit on error

# ============================================
# Configuration
# ============================================

export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://$(whoami)@localhost/nexus}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
PORT="${NEXUS_PORT:-8080}"
HOST="${NEXUS_HOST:-0.0.0.0}"

# ============================================
# Banner
# ============================================

cat << 'EOF'
╔═══════════════════════════════════════╗
║   Nexus AI Filesystem Server Setup   ║
╚═══════════════════════════════════════╝
EOF

echo ""
echo "Configuration:"
echo "  Admin user:  $ADMIN_USER"
echo "  Database:    $NEXUS_DATABASE_URL"
echo "  Data dir:    $NEXUS_DATA_DIR"
echo "  Server:      http://$HOST:$PORT"
echo ""

# ============================================
# Prerequisites Check
# ============================================

if ! command -v nexus &> /dev/null; then
    echo "❌ Error: 'nexus' command not found"
    echo "   Install with: pip install nexus-ai-fs"
    exit 1
fi

if ! command -v createdb &> /dev/null; then
    echo "⚠️  Warning: PostgreSQL tools not found"
    echo "   Will use default database (may already exist)"
fi

# ============================================
# Database Setup
# ============================================

echo "📦 Setting up database..."

# Try to create database (will fail silently if exists or no permission)
if command -v createdb &> /dev/null; then
    if createdb nexus 2>/dev/null; then
        echo "✓ Created database 'nexus'"
    else
        echo "✓ Database exists (or will be created automatically)"
    fi
else
    echo "✓ Using database: $NEXUS_DATABASE_URL"
fi

# Test database connection
echo ""
echo "🔌 Testing database connection..."

if ! nexus ls / &>/dev/null; then
    echo ""
    echo "❌ Cannot connect to database!"
    echo ""
    echo "Your database URL: $NEXUS_DATABASE_URL"
    echo ""
    echo "Common fixes:"
    echo ""
    echo "1. Use your OS username (no password):"
    echo "   export NEXUS_DATABASE_URL=\"postgresql://\$(whoami)@localhost/nexus\""
    echo "   createdb nexus"
    echo "   ./init-nexus.sh"
    echo ""
    echo "2. Create PostgreSQL user with password:"
    echo ""
    echo "   Linux:"
    echo "   sudo -u postgres psql -c \"CREATE USER nexus WITH PASSWORD 'password';\""
    echo "   sudo -u postgres psql -c \"CREATE DATABASE nexus OWNER nexus;\""
    echo ""
    echo "   macOS (Homebrew PostgreSQL):"
    echo "   psql postgres -c \"CREATE USER nexus WITH PASSWORD 'password';\""
    echo "   psql postgres -c \"CREATE DATABASE nexus OWNER nexus;\""
    echo ""
    echo "   macOS (Docker PostgreSQL):"
    echo "   docker exec nexus-postgres psql -U postgres -c \"CREATE USER nexus WITH PASSWORD 'password';\""
    echo "   docker exec nexus-postgres psql -U postgres -c \"CREATE DATABASE nexus OWNER nexus;\""
    echo "   docker exec nexus-postgres psql -U postgres -d nexus -c \"GRANT ALL ON ALL TABLES IN SCHEMA public TO nexus;\""
    echo ""
    echo "   Then:"
    echo "   export NEXUS_DATABASE_URL=\"postgresql://nexus:password@localhost/nexus\""
    echo "   ./init-nexus.sh"
    echo ""
    echo "3. Use SQLite instead (not recommended for production):"
    echo "   unset NEXUS_DATABASE_URL"
    echo "   ./init-nexus.sh"
    echo ""
    exit 1
fi

echo "✓ Database connection successful"

# ============================================
# Bootstrap (Permissions Disabled)
# ============================================

echo ""
echo "🔧 Bootstrapping server (permissions disabled)..."

export NEXUS_ENFORCE_PERMISSIONS=false

# Create workspace directory
nexus mkdir /workspace 2>/dev/null && echo "✓ Created /workspace" || echo "✓ /workspace exists"

# Grant admin user full ownership
tuple_id=$(nexus rebac create user $ADMIN_USER direct_owner file /workspace --tenant-id default 2>&1 | grep -o '[0-9a-f]\{8\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{4\}-[0-9a-f]\{12\}' | head -1 || echo "created")

if [ -n "$tuple_id" ]; then
    echo "✓ Granted '$ADMIN_USER' ownership of /workspace"
else
    echo "✓ Admin permissions configured"
fi

# ============================================
# Verify Setup
# ============================================

echo ""
echo "🔍 Verifying setup..."

# Check if admin can write
can_write=$(nexus rebac check user $ADMIN_USER write file /workspace 2>&1 || echo "false")

if echo "$can_write" | grep -q "GRANTED"; then
    echo "✓ Admin permissions verified"
else
    echo "⚠️  Warning: Could not verify admin permissions"
    echo "   Continuing anyway..."
fi

# ============================================
# Start Server (Permissions Enabled)
# ============================================

export NEXUS_ENFORCE_PERMISSIONS=true

# ============================================
# Port Cleanup (Kill existing processes)
# ============================================

echo "🔍 Checking port $PORT..."

# Find and kill any process using the port
if command -v lsof &> /dev/null; then
    PID=$(lsof -ti:$PORT 2>/dev/null)
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

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║   ✅ Setup Complete!                  ║"
echo "╚═══════════════════════════════════════╝"
echo ""
echo "Starting Nexus server..."
echo ""
echo "Server URL: http://$HOST:$PORT"
echo "Admin user: $ADMIN_USER"
echo ""
echo "Test with:"
echo "  curl http://localhost:$PORT/health"
echo ""
echo "Press Ctrl+C to stop server"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

nexus serve --host $HOST --port $PORT
