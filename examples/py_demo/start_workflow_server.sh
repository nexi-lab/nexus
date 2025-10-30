#!/bin/bash
# Start Nexus server for workflow demo
#
# This script starts a Nexus server on localhost:8080 with PostgreSQL backend
# for testing workflow auto-fire in remote mode.
#
# Supports both local and Docker PostgreSQL
#
# Usage:
#   ./start_workflow_server.sh

set -e

echo "🚀 Starting Nexus Server for Workflow Demo"
echo "=========================================="
echo ""

# PostgreSQL connection settings (adjust for your Docker setup)
PG_HOST="${POSTGRES_HOST:-localhost}"
PG_PORT="${POSTGRES_PORT:-5432}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_PASSWORD="${POSTGRES_PASSWORD:-nexus}"
PG_DB="${POSTGRES_DB:-nexus}"

echo "📍 PostgreSQL Configuration:"
echo "   Host: $PG_HOST"
echo "   Port: $PG_PORT"
echo "   User: $PG_USER"
echo "   Database: $PG_DB"
echo ""

# Check if PostgreSQL is running (works for Docker and local)
if ! pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" >/dev/null 2>&1; then
    echo "❌ Error: PostgreSQL is not accessible at $PG_HOST:$PG_PORT"
    echo ""
    echo "   For Docker PostgreSQL, make sure it's running:"
    echo "   docker ps | grep postgres"
    echo ""
    echo "   Or set connection parameters:"
    echo "   export POSTGRES_HOST=localhost"
    echo "   export POSTGRES_PORT=5432"
    echo "   export POSTGRES_USER=postgres"
    echo "   export POSTGRES_PASSWORD=nexus"
    echo ""
    exit 1
fi

echo "✅ PostgreSQL is accessible"

# Build connection string for psql (with password if set)
PSQL_CONN="postgresql://$PG_USER"
if [ -n "$PG_PASSWORD" ]; then
    PSQL_CONN="$PSQL_CONN:$PG_PASSWORD"
fi
PSQL_CONN="$PSQL_CONN@$PG_HOST:$PG_PORT"

# Check if database exists
if ! PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -lqt | cut -d \| -f 1 | grep -qw "$PG_DB"; then
    echo "📦 Creating database: $PG_DB"
    PGPASSWORD="$PG_PASSWORD" createdb -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" "$PG_DB"
else
    echo "✅ Database exists: $PG_DB"
fi

# Set environment variables
export NEXUS_DATABASE_URL="$PSQL_CONN/$PG_DB"
export NEXUS_DATA_DIR="./workflow-server-data"

# IMPORTANT: Unset NEXUS_URL to prevent circular dependency
# The server must use local NexusFS, not RemoteNexusFS
unset NEXUS_URL
unset NEXUS_API_KEY

echo "✅ Database URL: $NEXUS_DATABASE_URL"
echo "✅ Data directory: $NEXUS_DATA_DIR"
echo "✅ NEXUS_URL unset (server mode)"

# Note: Skipping migrations due to existing cycle issue in Alembic
# The server will auto-create tables on first run
echo ""
echo "ℹ️  Note: Skipping Alembic migrations (pre-existing cycle issue)"
echo "   Tables will be auto-created by SQLAlchemy on first run"

echo ""
echo "🌐 Starting Nexus server on http://localhost:8080"
echo "   Press Ctrl+C to stop the server"
echo ""

# Start the server (use nexus command directly)
cd ../../
nexus serve \
    --host 0.0.0.0 \
    --port 8080 \
    --data-dir "$NEXUS_DATA_DIR"
