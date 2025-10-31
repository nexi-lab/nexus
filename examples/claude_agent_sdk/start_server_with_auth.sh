#!/bin/bash
# Start Nexus server with proper database authentication
# This is the CORRECT way - creates real user API keys

set -e

echo "🚀 Starting Nexus Server with Database Authentication"
echo ""

# Configuration
export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://$(whoami)@localhost/nexus_claude}"
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-demo-data}"
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
PORT="${NEXUS_PORT:-8080}"
HOST="${NEXUS_HOST:-0.0.0.0}"

echo "Configuration:"
echo "  Database: $NEXUS_DATABASE_URL"
echo "  Data dir: $NEXUS_DATA_DIR"
echo "  Admin:    $ADMIN_USER"
echo "  Server:   http://$HOST:$PORT"
echo ""

# ============================================
# Check if server is already initialized
# ============================================

if [ -f ".nexus-admin-env" ]; then
    echo "✓ Found existing admin environment"
    echo ""
    echo "To use the existing admin key:"
    echo "  source .nexus-admin-env"
    echo ""
    echo "To create a Claude agent key:"
    echo "  source .nexus-admin-env"
    echo "  python3 ../../scripts/create-api-key.py claude-agent \"Claude Agent\" --days 365"
    echo ""
    echo "Starting server with existing setup..."
    echo ""

    # Start server
    nexus serve --host $HOST --port $PORT --auth-type database
    exit 0
fi

# ============================================
# Fresh Setup
# ============================================

echo "📦 Initializing fresh Nexus server..."
echo ""

# Create database if needed
if command -v createdb &> /dev/null; then
    DB_NAME=$(echo "$NEXUS_DATABASE_URL" | sed 's|.*/||')
    if createdb "$DB_NAME" 2>/dev/null; then
        echo "✓ Created database: $DB_NAME"
    else
        echo "✓ Database already exists: $DB_NAME"
    fi
fi

# Run the official init script
if [ -f "../../scripts/init-nexus-with-auth.sh" ]; then
    echo ""
    echo "Running official setup script..."
    echo ""

    # Run init script (it will start the server)
    NEXUS_ADMIN_USER="$ADMIN_USER" \
    NEXUS_DATABASE_URL="$NEXUS_DATABASE_URL" \
    NEXUS_DATA_DIR="$NEXUS_DATA_DIR" \
    NEXUS_PORT="$PORT" \
    NEXUS_HOST="$HOST" \
    ../../scripts/init-nexus-with-auth.sh
else
    echo "❌ Cannot find scripts/init-nexus-with-auth.sh"
    echo ""
    echo "Please run from the nexus repository root, or use:"
    echo "  cd /path/to/nexus"
    echo "  ./scripts/init-nexus-with-auth.sh"
    exit 1
fi
