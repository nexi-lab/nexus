#!/bin/bash
# init-nexus-with-auth.sh - Initialize Nexus server with authentication
#
# Usage:
#   ./init-nexus-with-auth.sh                    # Use default admin user
#   NEXUS_ADMIN_USER=alice ./init-nexus-with-auth.sh  # Custom admin user

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
║   Nexus Server Setup (With Auth)     ║
╚═══════════════════════════════════════╝
EOF

echo ""
echo "Configuration:"
echo "  Admin user:  $ADMIN_USER"
echo "  Database:    $NEXUS_DATABASE_URL"
echo "  Data dir:    $NEXUS_DATA_DIR"
echo "  Server:      http://$HOST:$PORT"
echo "  Auth:        Database-backed API keys"
echo ""

# ============================================
# Prerequisites Check
# ============================================

if ! command -v nexus &> /dev/null; then
    echo "❌ Error: 'nexus' command not found"
    echo "   Install with: pip install nexus-ai-fs"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "❌ Error: 'python3' not found (needed for API key creation)"
    exit 1
fi

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

if ! nexus ls / 2>/tmp/nexus-init-error.log >/dev/null; then
    echo ""
    echo "❌ Cannot connect to database!"
    echo ""
    echo "Error details:"
    cat /tmp/nexus-init-error.log
    echo ""
    echo "Please check docs/QUICKSTART_GUIDE.md for database setup instructions."
    exit 1
fi

echo "✓ Database connection successful"

# ============================================
# Bootstrap (With Admin API Key)
# ============================================

echo ""
echo "🔧 Bootstrapping server..."

# ============================================
# Create Admin API Key
# ============================================

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
EOF

echo "✓ Saved to .nexus-admin-env (source this file to use the API key)"
echo ""

# ============================================
# Setup Workspace (Direct Database Access)
# ============================================

echo "🔧 Setting up workspace..."

# Since we're not running a server yet, use direct database access
# (permissions disabled for initial setup)
export NEXUS_ENFORCE_PERMISSIONS=false

# Create workspace directory
nexus mkdir /workspace 2>/dev/null && echo "✓ Created /workspace" || echo "✓ /workspace exists"

# Grant admin user full ownership
nexus rebac create user $ADMIN_USER direct_owner file /workspace --tenant-id default >/dev/null 2>&1
echo "✓ Granted '$ADMIN_USER' ownership of /workspace"

# Re-enable permissions for server
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

# ============================================
# Start Server (With Authentication)
# ============================================

echo ""
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

# Start server with database auth
nexus serve --host $HOST --port $PORT --auth-type database
