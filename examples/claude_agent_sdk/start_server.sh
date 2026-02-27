#!/bin/bash
# Quick start Nexus server for Claude Agent SDK demos

set -e

echo "🚀 Starting Nexus Server for Claude Agent SDK Demos"
echo ""

# Configuration
export NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-./nexus-demo-data}"
SERVER_PORT="${NEXUS_PORT:-2026}"
API_KEY="${NEXUS_API_KEY:-demo-key-12345}"

echo "Configuration:"
echo "  Data directory: $NEXUS_DATA_DIR"
echo "  Server port: $SERVER_PORT"
echo "  API key: $API_KEY"
echo ""

# Create data directory
mkdir -p "$NEXUS_DATA_DIR"

# Check if PostgreSQL is available
if command -v psql &> /dev/null; then
    echo "✓ PostgreSQL detected"

    # Try to create database
    DB_NAME="nexus_claude_demo"
    createdb "$DB_NAME" 2>/dev/null && echo "  Created database: $DB_NAME" || echo "  Database already exists: $DB_NAME"

    # Use PostgreSQL
    export NEXUS_DATABASE_URL="postgresql://$(whoami)@localhost/$DB_NAME"
    echo "  Using database: $NEXUS_DATABASE_URL"
else
    echo "⚠️  PostgreSQL not found - using SQLite (limited features)"
    echo "  Install PostgreSQL for full functionality:"
    echo "    macOS: brew install postgresql && brew services start postgresql"
    echo "    Linux: sudo apt-get install postgresql"
fi

echo ""
echo "📡 Starting server..."
echo ""
echo "Connect from Python:"
echo "  import nexus"
echo "  nx = nexus.connect(config={\"mode\": \"remote\", \"url\": \"http://localhost:$SERVER_PORT\", \"api_key\": \"$API_KEY\"})"
echo ""
echo "Or set environment variables:"
echo "  export NEXUS_URL='http://localhost:$SERVER_PORT'"
echo "  export NEXUS_API_KEY='$API_KEY'"
echo ""
echo "Press Ctrl+C to stop"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Start server with API key authentication
nexus serve --host 0.0.0.0 --port "$SERVER_PORT" --api-key "$API_KEY"
