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
# Ensure Skills Directory Exists
# ============================================
# The docker-compose mounts ./nexus-data:/app/data, which should contain
# the skills. The docker-demo.sh script ensures skills are copied there.
echo ""
echo "📦 Checking for default skills..."

SKILLS_DIR="/app/data/skills"
mkdir -p "$SKILLS_DIR"

# Check if skills exist (they should be copied by docker-demo.sh)
if [ "$(ls -A $SKILLS_DIR/*.skill 2>/dev/null | wc -l)" -gt 0 ]; then
    SKILL_COUNT=$(ls -1 $SKILLS_DIR/*.skill 2>/dev/null | wc -l)
    echo -e "${GREEN}✓ Found $SKILL_COUNT skill file(s)${NC}"
else
    echo -e "${YELLOW}⚠ No skill files found in $SKILLS_DIR${NC}"
    echo "  Skills will be imported if available during provisioning"
fi

# ============================================
# Initialize Database Schema & Migrations
# ============================================
echo ""
echo "📊 Initializing database..."

# Use new migration-based initialization script
# This script intelligently handles:
# - Fresh databases: Creates schema + stamps with latest migration
# - Existing databases: Runs pending migrations
# - Legacy databases: Stamps existing schema + runs future migrations
cd /app
python3 scripts/init_database.py

if [ $? -ne 0 ]; then
    echo -e "${RED}✗ Database initialization failed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Database initialized${NC}"

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
# Initialize Semantic Search (Optional)
# ============================================
# Check if semantic search is enabled in config file
CONFIG_FILE="${NEXUS_CONFIG_FILE:-/app/configs/config.demo.yaml}"
if [ -f "$CONFIG_FILE" ]; then
    # Extract semantic_search value from YAML config
    SEMANTIC_SEARCH_ENABLED=$(python3 -c "
import sys
import yaml
try:
    with open('$CONFIG_FILE', 'r') as f:
        config = yaml.safe_load(f)
    enabled = config.get('features', {}).get('semantic_search', False)
    print('true' if enabled else 'false')
except Exception as e:
    print('false')
    print(f'Warning: Could not read semantic_search config: {e}', file=sys.stderr)
" 2>&1)

    if [ "$SEMANTIC_SEARCH_ENABLED" = "true" ]; then
        echo ""
        echo "🔍 Initializing semantic search (from config)..."

        python3 << 'PYTHON_SEMANTIC_INIT'
import os
import sys
import asyncio

try:
    # Add src to path
    sys.path.insert(0, '/app/src')

    from nexus.core.nexus_fs import NexusFS
    from nexus.backends.local import LocalBackend

    data_dir = os.getenv('NEXUS_DATA_DIR', '/app/data')
    database_url = os.getenv('NEXUS_DATABASE_URL')

    async def init_semantic_search():
        """Initialize semantic search (defaults to keyword-only for safety)."""
        backend = LocalBackend(data_dir)
        nx = NexusFS(backend, db_path=database_url)

        # Check if explicitly requested to use vector embeddings
        # Default: keyword-only mode (safer, more stable)
        semantic_mode = os.getenv('NEXUS_SEMANTIC_MODE', 'keyword')

        if semantic_mode == 'semantic':
            # Only use embeddings if explicitly requested
            openai_api_key = os.getenv('OPENAI_API_KEY')
            if openai_api_key and openai_api_key != 'your-openai-api-key':
                await nx.initialize_semantic_search(
                    embedding_provider="openai",
                    api_key=openai_api_key,
                    chunk_size=512,
                    chunk_strategy="semantic"
                )
                print("✓ Semantic search initialized (OpenAI embeddings - experimental)")
            else:
                print("WARNING: NEXUS_SEMANTIC_MODE=semantic but no valid OPENAI_API_KEY found")
                print("Falling back to keyword-only mode")
                await nx.initialize_semantic_search(
                    embedding_provider=None,
                    chunk_size=512,
                    chunk_strategy="semantic"
                )
                print("✓ Semantic search initialized (keyword-only mode)")
        else:
            # Default: keyword-only mode (PostgreSQL FTS)
            await nx.initialize_semantic_search(
                embedding_provider=None,
                chunk_size=512,
                chunk_strategy="semantic"
            )
            print("✓ Semantic search initialized (keyword-only mode)")

        nx.close()

    asyncio.run(init_semantic_search())

except Exception as e:
    print(f"ERROR: Failed to initialize semantic search: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
PYTHON_SEMANTIC_INIT

        if [ $? -eq 0 ]; then
            echo -e "${GREEN}✓ Semantic search initialized${NC}"
        else
            echo -e "${RED}✗ Semantic search initialization failed${NC}"
            exit 1
        fi
    else
        echo ""
        echo "ℹ️  Semantic search not enabled in config (features.semantic_search: false)"
    fi
else
    echo ""
    echo "ℹ️  No config file found, skipping semantic search initialization"
fi

# ============================================
# Start Nexus Server
# ============================================
echo ""
echo "🚀 Starting Nexus server..."
echo ""
echo "  Host: ${NEXUS_HOST:-0.0.0.0}"
echo "  Port: ${NEXUS_PORT:-8080}"
echo "  Backend: ${NEXUS_BACKEND:-local}"

# Check if config file is specified
CONFIG_FILE="${NEXUS_CONFIG_FILE:-/app/configs/config.demo.yaml}"
if [ -f "$CONFIG_FILE" ]; then
    echo "  Config: $CONFIG_FILE"
    echo ""
    echo -e "${GREEN}✓ Using configuration file${NC}"
    CMD="nexus serve --config $CONFIG_FILE --auth-type database --async"
else
    echo "  Config: Not found (using CLI options)"
    echo ""

    # Build command based on backend type (legacy CLI mode)
    CMD="nexus serve --host ${NEXUS_HOST:-0.0.0.0} --port ${NEXUS_PORT:-8080} --auth-type database --async"

    if [ "${NEXUS_BACKEND}" = "gcs" ]; then
        CMD="$CMD --backend gcs --gcs-bucket ${NEXUS_GCS_BUCKET}"
        if [ -n "${NEXUS_GCS_PROJECT}" ]; then
            CMD="$CMD --gcs-project ${NEXUS_GCS_PROJECT}"
        fi
    fi
fi

# Start server in background to load saved mounts
echo "Starting server..."
$CMD &
SERVER_PID=$!

# Wait for server to be ready
echo "Waiting for server to start..."
for i in {1..30}; do
    if curl -sf http://localhost:${NEXUS_PORT:-8080}/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Server is ready${NC}"
        break
    fi
    sleep 1
    if [ $i -eq 30 ]; then
        echo -e "${YELLOW}⚠ Server health check timeout (but continuing...)${NC}"
    fi
done
sleep 2

# Load saved mounts (only if not using config file)
# Config-based backends are auto-loaded by the server
if [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    echo "🔄 Loading saved mounts from database..."

    # Call list_saved_mounts API
    SAVED_MOUNTS=$(curl -sf http://localhost:${NEXUS_PORT:-8080}/api/nfs/list_saved_mounts \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${ADMIN_API_KEY}" \
        -d '{"jsonrpc": "2.0", "id": 1, "method": "list_saved_mounts"}' 2>/dev/null)

    if [ $? -eq 0 ]; then
        # Count mounts
        MOUNT_COUNT=$(echo "$SAVED_MOUNTS" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('result', [])))" 2>/dev/null || echo "0")

        if [ "$MOUNT_COUNT" -gt 0 ]; then
            echo "Found $MOUNT_COUNT saved mount(s)"

            # Extract mount points and load each one
            echo "$SAVED_MOUNTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
mounts = data.get('result', [])
for mount in mounts:
    print(mount['mount_point'])
" 2>/dev/null | while read -r MOUNT_POINT; do
                if [ -n "$MOUNT_POINT" ]; then
                    echo "  Loading mount: $MOUNT_POINT"
                    LOAD_RESULT=$(curl -sf http://localhost:${NEXUS_PORT:-8080}/api/nfs/load_mount \
                        -H "Content-Type: application/json" \
                        -H "Authorization: Bearer ${ADMIN_API_KEY}" \
                        -d "{\"jsonrpc\": \"2.0\", \"id\": 2, \"method\": \"load_mount\", \"params\": {\"mount_point\": \"$MOUNT_POINT\"}}" 2>/dev/null)

                    if [ $? -eq 0 ]; then
                        echo -e "    ${GREEN}✓${NC} Loaded: $MOUNT_POINT"
                    else
                        echo -e "    ${YELLOW}⚠${NC} Failed to load: $MOUNT_POINT"
                    fi
                fi
            done
        else
            echo "No saved mounts found"
        fi
    else
        echo -e "${YELLOW}⚠ Could not check for saved mounts (API not ready)${NC}"
    fi
else
    echo ""
    echo -e "${GREEN}✓ Backends loaded from configuration file${NC}"
fi

echo ""
echo -e "${GREEN}✓ Server initialization complete${NC}"
echo ""

# Wait for server process (bring to foreground)
wait $SERVER_PID
