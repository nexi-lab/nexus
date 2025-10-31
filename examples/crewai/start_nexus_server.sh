#!/bin/bash
# Start Nexus server for CrewAI demo
#
# This script starts a local Nexus server that the CrewAI agents
# will connect to via MCP protocol.
#
# Usage:
#   ./start_nexus_server.sh
#
# The server will run on http://localhost:8080 by default.
# Set NEXUS_PORT to use a different port.

set -e

# Configuration
PORT="${NEXUS_PORT:-8080}"
DATA_DIR="${NEXUS_DATA_DIR:-./nexus-data}"
DB_URL="${NEXUS_DATABASE_URL:-}"

echo "========================================================================"
echo "Starting Nexus Server for CrewAI Demo"
echo "========================================================================"
echo ""
echo "Port:      $PORT"
echo "Data dir:  $DATA_DIR"
echo ""

# Check if nexus CLI is available
if ! command -v nexus &> /dev/null; then
    echo "✗ Error: 'nexus' command not found"
    echo ""
    echo "Please install Nexus first:"
    echo "  pip install nexus-ai-fs"
    echo ""
    echo "Or install from source:"
    echo "  cd /path/to/nexus"
    echo "  pip install -e ."
    exit 1
fi

# Check if port is already in use
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "✗ Error: Port $PORT is already in use"
    echo ""
    echo "Either:"
    echo "  1. Stop the process using port $PORT"
    echo "  2. Use a different port: NEXUS_PORT=8081 ./start_nexus_server.sh"
    exit 1
fi

# Setup database (SQLite by default for simplicity)
if [ -z "$DB_URL" ]; then
    echo "Using SQLite database (embedded mode)"
    DB_URL="sqlite:///$DATA_DIR/nexus.db"
    export NEXUS_DATABASE_URL="$DB_URL"
else
    echo "Using database: $DB_URL"
fi

# Create data directory
mkdir -p "$DATA_DIR"

echo ""
echo "Starting server..."
echo ""
echo "✓ Server will be available at: http://localhost:$PORT"
echo "✓ Health check: curl http://localhost:$PORT/health"
echo ""
echo "Press Ctrl+C to stop the server"
echo "========================================================================"
echo ""

# Start the server
# Note: In production, you'd use --host 0.0.0.0 for external access
exec nexus serve \
    --host localhost \
    --port "$PORT" \
    --data-dir "$DATA_DIR"
