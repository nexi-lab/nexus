#!/bin/bash
# docker-entrypoint.sh - Nexus Docker container entrypoint
# Handles initialization and starts the Nexus server

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
API_KEY_FILE="/app/data/.admin-api-key"

echo ""
echo "╔═══════════════════════════════════════════╗"
echo "║        Nexus Server - Docker Init        ║"
echo "╚═══════════════════════════════════════════╝"
echo ""

# ============================================
# Wait for PostgreSQL
# ============================================
if [ -n "$NEXUS_DATABASE_URL" ]; then
    echo "🔌 Waiting for PostgreSQL..."

    # Extract connection info from database URL
    # Format: postgresql://user:pass@host:port/dbname
    DB_HOST=$(echo "$NEXUS_DATABASE_URL" | sed -n 's/.*@\([^:]*\):.*/\1/p')
    DB_PORT=$(echo "$NEXUS_DATABASE_URL" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')

    if [ -n "$DB_HOST" ]; then
        MAX_TRIES=30
        COUNT=0

        while [ $COUNT -lt $MAX_TRIES ]; do
            if nc -z "$DB_HOST" "${DB_PORT:-5432}" 2>/dev/null; then
                echo -e "${GREEN}✓ PostgreSQL is ready${NC}"
                break
            fi
            COUNT=$((COUNT + 1))
            if [ $COUNT -eq $MAX_TRIES ]; then
                echo -e "${RED}✗ PostgreSQL is not available after ${MAX_TRIES}s${NC}"
                exit 1
            fi
            sleep 1
        done
    fi
fi

# ============================================
# Initialize Database Schema
# ============================================
echo ""
echo "📊 Initializing database schema..."

# Create schema by instantiating NexusFS (it auto-creates tables)
python3 << 'PYTHON_INIT'
import os
import sys
from sqlalchemy import create_engine, inspect

database_url = os.getenv('NEXUS_DATABASE_URL')
if not database_url:
    print("ERROR: NEXUS_DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

try:
    # Check if tables exist
    engine = create_engine(database_url)
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    if 'users' in tables:
        print("✓ Database schema already exists")
    else:
        print("Creating database schema...")
        # Import NexusFS to create tables
        from nexus.core.nexus_fs import NexusFS
        from nexus.backends.local import LocalBackend

        data_dir = os.getenv('NEXUS_DATA_DIR', '/app/data')
        backend = LocalBackend(data_dir)
        nfs = NexusFS(backend, db_path=database_url)
        nfs.close()
        print("✓ Database schema created")

except Exception as e:
    print(f"ERROR: Failed to initialize database: {e}", file=sys.stderr)
    sys.exit(1)
PYTHON_INIT

if [ $? -ne 0 ]; then
    echo -e "${RED}✗ Database initialization failed${NC}"
    exit 1
fi

# ============================================
# Create Admin API Key (First Run)
# ============================================

# Check if API key already exists (from previous run or env variable)
if [ -f "$API_KEY_FILE" ]; then
    echo ""
    echo "🔑 Using existing admin API key"
    ADMIN_API_KEY=$(cat "$API_KEY_FILE")
elif [ -n "$NEXUS_API_KEY" ]; then
    echo ""
    echo "🔑 Using API key from environment variable"
    ADMIN_API_KEY="$NEXUS_API_KEY"
    # Save for future runs
    echo "$ADMIN_API_KEY" > "$API_KEY_FILE"
else
    echo ""
    echo "🔑 Creating admin API key..."

    # Create admin API key using Python (matches create-api-key.py)
    API_KEY_OUTPUT=$(python3 << PYTHON_CREATE_KEY
import os
import sys
from datetime import UTC, datetime, timedelta

# Add src to path
sys.path.insert(0, '/app/src')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.core.entity_registry import EntityRegistry
from nexus.server.auth.database_key import DatabaseAPIKeyAuth

database_url = os.getenv('NEXUS_DATABASE_URL')
admin_user = '${ADMIN_USER}'

try:
    engine = create_engine(database_url)
    SessionFactory = sessionmaker(bind=engine)

    # Register user in entity registry (for agent permission inheritance)
    entity_registry = EntityRegistry(SessionFactory)
    entity_registry.register_entity(
        entity_type='user',
        entity_id=admin_user,
        parent_type='tenant',
        parent_id='default',
    )

    # Create API key (90 day expiry)
    with SessionFactory() as session:
        expires_at = datetime.now(UTC) + timedelta(days=90)
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id=admin_user,
            name='Admin key (Docker auto-generated)',
            tenant_id='default',
            is_admin=True,
            expires_at=expires_at,
        )
        session.commit()

        print(f"API Key: {raw_key}")
        print(f"Created admin API key for user: {admin_user}")
        print(f"Expires: {expires_at.isoformat()}")

except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
PYTHON_CREATE_KEY
)

    if [ $? -ne 0 ]; then
        echo -e "${RED}✗ Failed to create admin API key${NC}"
        echo "$API_KEY_OUTPUT"
        exit 1
    fi

    # Extract the API key from output
    ADMIN_API_KEY=$(echo "$API_KEY_OUTPUT" | grep "API Key:" | awk '{print $3}')

    if [ -z "$ADMIN_API_KEY" ]; then
        echo -e "${RED}✗ Failed to extract API key${NC}"
        echo "$API_KEY_OUTPUT"
        exit 1
    fi

    # Save API key for future runs
    echo "$ADMIN_API_KEY" > "$API_KEY_FILE"

    echo -e "${GREEN}✓ Admin API key created${NC}"
fi

# ============================================
# Display API Key Info
# ============================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${YELLOW}ADMIN API KEY${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "  User:    ${BLUE}${ADMIN_USER}${NC}"
echo -e "  API Key: ${GREEN}${ADMIN_API_KEY}${NC}"
echo ""
echo "  To use this key:"
echo "    export NEXUS_API_KEY='${ADMIN_API_KEY}'"
echo "    export NEXUS_URL='http://localhost:${NEXUS_PORT:-8080}'"
echo ""
echo "  Or retrieve from container:"
echo "    docker logs <container-name> | grep 'API Key:'"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ============================================
# Start Nexus Server
# ============================================
echo "🚀 Starting Nexus server..."
echo ""
echo "  Host: ${NEXUS_HOST:-0.0.0.0}"
echo "  Port: ${NEXUS_PORT:-8080}"
echo "  Backend: ${NEXUS_BACKEND:-local}"
echo ""

# Build command based on backend type
CMD="nexus serve --host ${NEXUS_HOST:-0.0.0.0} --port ${NEXUS_PORT:-8080} --auth-type database"

if [ "${NEXUS_BACKEND}" = "gcs" ]; then
    CMD="$CMD --backend gcs --gcs-bucket ${NEXUS_GCS_BUCKET}"
    if [ -n "${NEXUS_GCS_PROJECT}" ]; then
        CMD="$CMD --gcs-project ${NEXUS_GCS_PROJECT}"
    fi
fi

# Execute the server (replace shell with nexus process)
exec $CMD
