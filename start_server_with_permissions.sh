#!/bin/bash
# Start Nexus server with permissions enabled for E2E testing

echo "Starting Nexus server with permissions enabled..."
echo "=============================================="

# Set environment variables for permissions
export NEXUS_PERMISSIONS_ENABLED=true
export NEXUS_PERMISSIONS_ENFORCE=true
export NEXUS_PERMISSIONS_AUDIT_STRICT_MODE=true

# Database configuration
export NEXUS_DATABASE_URL="${NEXUS_DATABASE_URL:-postgresql://localhost/nexus_test}"

# Server configuration
export NEXUS_HOST="${NEXUS_HOST:-0.0.0.0}"
export NEXUS_PORT="${NEXUS_PORT:-8765}"
export NEXUS_API_KEY="${NEXUS_API_KEY:-test-api-key-12345}"

# Enable Memory brick
export NEXUS_MEMORY_ENABLED=true

echo "Configuration:"
echo "  Permissions: ENABLED"
echo "  Database: $NEXUS_DATABASE_URL"
echo "  Server: http://$NEXUS_HOST:$NEXUS_PORT"
echo "  Memory Brick: ENABLED"
echo ""

# Start server using Python module
cd "$(dirname "$0")"
PYTHONPATH=src python -m uvicorn nexus.server.fastapi_server:app \
    --host "$NEXUS_HOST" \
    --port "$NEXUS_PORT" \
    --log-level info \
    --reload
