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

# Show if permissions are being skipped or disabled
if [ "${NEXUS_SKIP_PERMISSIONS:-false}" = "true" ]; then
    echo -e "${YELLOW}⚠️  NEXUS_SKIP_PERMISSIONS=true${NC}"
    echo -e "${YELLOW}   Entity registry and permission setup will be skipped${NC}"
    echo ""
fi

if [ "${NEXUS_ENFORCE_PERMISSIONS:-true}" = "false" ]; then
    echo -e "${YELLOW}⚠️  NEXUS_ENFORCE_PERMISSIONS=false${NC}"
    echo -e "${YELLOW}   Runtime permission checks are DISABLED${NC}"
    echo ""
fi

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
# Run Database Migrations
# ============================================
echo ""
echo "🔄 Running database migrations..."

# Run Alembic migrations to apply schema changes
# env.py automatically reads NEXUS_DATABASE_URL from environment
cd /app

# Run with timeout to avoid hanging
timeout 30 alembic upgrade head 2>&1 || true

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Database migrations applied${NC}"
else
    echo -e "${YELLOW}⚠ Database migrations timed out or failed${NC}"
    echo -e "${YELLOW}  This is expected for legacy databases - continuing startup${NC}"
fi

# ============================================
# Create Admin API Key (First Run)
# ============================================

# Check if API key already exists (from previous run)
if [ -f "$API_KEY_FILE" ]; then
    echo ""
    echo "🔑 Using existing admin API key"
    ADMIN_API_KEY=$(cat "$API_KEY_FILE")
else
    echo ""
    if [ -n "$NEXUS_API_KEY" ]; then
        echo "🔑 Registering custom API key from environment..."
        CUSTOM_KEY="$NEXUS_API_KEY"
    else
        echo "🔑 Creating admin API key..."
        CUSTOM_KEY=""
    fi

    # Create/register admin API key using Python
    API_KEY_OUTPUT=$(python3 << PYTHON_CREATE_KEY
import os
import sys
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

# Add src to path
sys.path.insert(0, '/app/src')

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.core.entity_registry import EntityRegistry
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.storage.models import APIKeyModel

database_url = os.getenv('NEXUS_DATABASE_URL')
admin_user = '${ADMIN_USER}'
custom_key = '${CUSTOM_KEY}'
skip_permissions = os.getenv('NEXUS_SKIP_PERMISSIONS', 'false').lower() == 'true'

try:
    engine = create_engine(database_url)
    SessionFactory = sessionmaker(bind=engine)

    # Register user in entity registry (for agent permission inheritance)
    # Skip if NEXUS_SKIP_PERMISSIONS is set to true
    if not skip_permissions:
        entity_registry = EntityRegistry(SessionFactory)
        entity_registry.register_entity(
            entity_type='user',
            entity_id=admin_user,
            parent_type='tenant',
            parent_id='default',
        )
    else:
        print("Skipping entity registry setup (NEXUS_SKIP_PERMISSIONS=true)")

    with SessionFactory() as session:
        expires_at = datetime.now(UTC) + timedelta(days=90)

        if custom_key:
            # Use custom API key from environment
            # Hash the key for storage (same as DatabaseAPIKeyAuth does)
            # Uses HMAC-SHA256 with salt (same as nexus.server.auth.database_key)
            HMAC_SALT = "nexus-api-key-v1"
            key_hash = hmac.new(HMAC_SALT.encode("utf-8"), custom_key.encode("utf-8"), hashlib.sha256).hexdigest()

            # Check if key already exists
            existing = session.query(APIKeyModel).filter_by(user_id=admin_user).first()
            if existing:
                print(f"API Key: {custom_key}")
                print(f"Custom API key already registered for user: {admin_user}")
            else:
                # Insert custom key into database
                api_key = APIKeyModel(
                    user_id=admin_user,
                    key_hash=key_hash,
                    name='Admin key (from environment)',
                    tenant_id='default',
                    is_admin=1,  # PostgreSQL expects integer, not boolean
                    created_at=datetime.now(UTC),
                    expires_at=expires_at,
                )
                session.add(api_key)
                session.commit()

                print(f"API Key: {custom_key}")
                print(f"Registered custom API key for user: {admin_user}")
                print(f"Expires: {expires_at.isoformat()}")

            raw_key = custom_key
        else:
            # Generate new API key
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
