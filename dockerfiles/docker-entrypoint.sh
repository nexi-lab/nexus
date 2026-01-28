#!/bin/bash
# docker-entrypoint.sh - Nexus Docker container entrypoint
# Handles initialization and starts the Nexus server.
# Refactored for maintainability: functions, extracted Python scripts, signal handling.

set -e
set -o pipefail

# Load helpers (same directory as this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=dockerfiles/entrypoint-helpers.sh
if [ -f "${SCRIPT_DIR}/entrypoint-helpers.sh" ]; then
    # When copied to /usr/local/bin, helpers may be alongside
    source "${SCRIPT_DIR}/entrypoint-helpers.sh"
elif [ -f "/app/dockerfiles/entrypoint-helpers.sh" ]; then
    source /app/dockerfiles/entrypoint-helpers.sh
else
    # Fallback: define minimal colors and no-op cleanup
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
    cleanup() { [ -n "${SERVER_PID:-}" ] && kill -TERM "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null; exit 0; }
    ensure_dir() { mkdir -p "$1"; }
    validate_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
    run_python() { python3 "$@"; }
fi

# Configuration
ADMIN_USER="${NEXUS_ADMIN_USER:-admin}"
API_KEY_FILE="${NEXUS_API_KEY_FILE:-/app/data/.admin-api-key}"
CONFIG_FILE="${NEXUS_CONFIG_FILE:-/app/configs/config.demo.yaml}"
APP_DIR="${APP_DIR:-/app}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/app/scripts}"
NEXUS_PORT="${NEXUS_PORT:-2026}"

# Optional: set to "true" to redact API key from startup banner (logs)
REDACT_API_KEY_IN_LOGS="${REDACT_API_KEY_IN_LOGS:-false}"

# PIDs for cleanup
SERVER_PID=""
ZOEKT_PID=""

# -----------------------------------------------------------------------------
# Functions
# -----------------------------------------------------------------------------
print_banner() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║        Nexus Server - Docker Init        ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""
}

check_permissions_flags() {
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
}

wait_for_postgres() {
    [ -z "${NEXUS_DATABASE_URL:-}" ] && return 0

    echo "🔌 Waiting for PostgreSQL..."

    DB_HOST="$(python3 "$SCRIPTS_DIR/parse_db_url.py" "$NEXUS_DATABASE_URL" host 2>/dev/null)" || true
    DB_PORT="$(python3 "$SCRIPTS_DIR/parse_db_url.py" "$NEXUS_DATABASE_URL" port 2>/dev/null)" || true
    DB_PORT="${DB_PORT:-5432}"

    if [ -z "$DB_HOST" ]; then
        echo -e "${YELLOW}Could not parse DB host from NEXUS_DATABASE_URL, skipping wait${NC}"
        return 0
    fi

    MAX_TRIES=30
    COUNT=0
    while [ $COUNT -lt $MAX_TRIES ]; do
        if nc -z "$DB_HOST" "$DB_PORT" 2>/dev/null; then
            echo -e "${GREEN}✓ PostgreSQL is ready${NC}"
            return 0
        fi
        COUNT=$((COUNT + 1))
        if [ $COUNT -eq $MAX_TRIES ]; then
            echo -e "${RED}✗ PostgreSQL is not available after ${MAX_TRIES}s${NC}"
            exit 1
        fi
        sleep 1
    done
}

ensure_skills_directory() {
    echo ""
    echo "📦 Checking for default skills..."

    SKILLS_DIR="/app/data/skills"
    ensure_dir "$SKILLS_DIR" || exit 1

    SKILL_COUNT=0
    for f in "$SKILLS_DIR"/*.skill; do
        [ -e "$f" ] && SKILL_COUNT=$((SKILL_COUNT + 1))
    done 2>/dev/null || true

    if [ "$SKILL_COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ Found $SKILL_COUNT skill file(s)${NC}"
    else
        echo -e "${YELLOW}⚠ No skill files found in $SKILLS_DIR${NC}"
        echo "  Skills will be imported if available during provisioning"
    fi
}

init_database() {
    echo ""
    echo "📊 Initializing database..."

    if [ ! -f "$SCRIPTS_DIR/init_database.py" ]; then
        echo -e "${RED}✗ init_database.py not found${NC}"
        exit 1
    fi

    cd "$APP_DIR" || exit 1
    if ! python3 "$SCRIPTS_DIR/init_database.py"; then
        echo -e "${RED}✗ Database initialization failed${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Database initialized${NC}"
}

setup_admin_api_key() {
    local need_create=1
    ADMIN_API_KEY=""

    if [ -f "$API_KEY_FILE" ]; then
        ADMIN_API_KEY="$(cat "$API_KEY_FILE")"
        if [ -n "$ADMIN_API_KEY" ] && [ -n "${NEXUS_DATABASE_URL:-}" ]; then
            local key_status
            key_status="$(python3 "$SCRIPTS_DIR/check_api_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_API_KEY" 2>/dev/null)" || true
            if [ "$key_status" = "EXISTS" ]; then
                echo ""
                echo "🔑 Using existing admin API key (registered in database)"
                need_create=0
            else
                echo ""
                echo "⚠️  API key file exists but key not registered in database"
                echo "   Re-registering key..."
                ADMIN_API_KEY=""
            fi
        else
            ADMIN_API_KEY=""
        fi
    fi

    if [ "$need_create" -eq 1 ] && [ -z "$ADMIN_API_KEY" ]; then
        echo ""
        if [ -n "${NEXUS_API_KEY:-}" ]; then
            echo "🔑 Registering custom API key from environment..."
        else
            echo "🔑 Creating admin API key..."
        fi

        local skip_perm
        skip_perm="false"
        [ "${NEXUS_SKIP_PERMISSIONS:-false}" = "true" ] && skip_perm="true"

        local custom_key="${NEXUS_API_KEY:-}"
        local out
        out="$(python3 "$SCRIPTS_DIR/create_admin_key.py" "$NEXUS_DATABASE_URL" "$ADMIN_USER" "$custom_key" "$skip_perm" 2>&1)" || {
            echo -e "${RED}✗ Failed to create admin API key${NC}"
            echo "$out"
            exit 1
        }

        ADMIN_API_KEY="$(echo "$out" | grep "API Key:" | sed -n 's/.*API Key: *//p')"
        if [ -z "$ADMIN_API_KEY" ]; then
            echo -e "${RED}✗ Failed to extract API key${NC}"
            echo "$out"
            exit 1
        fi
        echo "$ADMIN_API_KEY" > "$API_KEY_FILE"
        echo -e "${GREEN}✓ Admin API key created and saved${NC}"
    else
        echo ""
        echo "🔑 Using existing admin API key"
    fi

    # Display API key info (set REDACT_API_KEY_IN_LOGS=true to hide key in logs)
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e "${YELLOW}ADMIN API KEY${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo -e "  User:    ${BLUE}${ADMIN_USER}${NC}"
    if [ "$REDACT_API_KEY_IN_LOGS" = "true" ]; then
        echo -e "  API Key: ${GREEN}(redacted - set REDACT_API_KEY_IN_LOGS=false to show)${NC}"
    else
        echo -e "  API Key: ${GREEN}${ADMIN_API_KEY}${NC}"
        echo ""
        echo "  To use this key:"
        echo "    export NEXUS_API_KEY='${ADMIN_API_KEY}'"
        echo "    export NEXUS_URL='http://localhost:${NEXUS_PORT:-2026}'"
        echo ""
        echo "  Or retrieve from container:"
        echo "    docker logs <container-name> | grep 'API Key:'"
    fi
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
}

init_semantic_search_if_enabled() {
    if [ ! -f "$CONFIG_FILE" ]; then
        echo ""
        echo "ℹ️  No config file found, skipping semantic search initialization"
        return 0
    fi

    local enabled
    enabled="$(python3 "$SCRIPTS_DIR/check_semantic_search_config.py" "$CONFIG_FILE" 2>/dev/null)" || enabled="false"

    if [ "$enabled" != "true" ]; then
        echo ""
        echo "ℹ️  Semantic search not enabled in config (features.semantic_search: false)"
        return 0
    fi

    echo ""
    echo "🔍 Initializing semantic search (from config)..."

    if ! python3 "$SCRIPTS_DIR/init_semantic_search.py"; then
        echo -e "${RED}✗ Semantic search initialization failed${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Semantic search initialized${NC}"
}

start_zoekt_if_enabled() {
    if [ "${ZOEKT_ENABLED:-false}" != "true" ]; then
        echo ""
        echo "ℹ️  Zoekt search disabled (set ZOEKT_ENABLED=true to enable)"
        echo ""
        return 0
    fi

    echo ""
    echo "🔍 Starting Zoekt search sidecar..."

    ZOEKT_INDEX_DIR="${ZOEKT_INDEX_DIR:-/app/data/.zoekt-index}"
    ZOEKT_DATA_DIR="${ZOEKT_DATA_DIR:-/app/data}"
    ZOEKT_PORT="${ZOEKT_PORT:-6070}"

    ensure_dir "$ZOEKT_INDEX_DIR" || exit 1

    if [ ! -f "$ZOEKT_INDEX_DIR/compound-0.zoekt" ]; then
        echo "  Building initial Zoekt index..."
        if ! zoekt-index -index "$ZOEKT_INDEX_DIR" "$ZOEKT_DATA_DIR" 2>&1 | head -5; then
            echo -e "${YELLOW}⚠ Index build had warnings or partial failure; continuing${NC}"
        fi
        echo -e "${GREEN}✓ Initial index built${NC}"
    fi

    echo "  Starting Zoekt webserver on port $ZOEKT_PORT..."
    zoekt-webserver -index "$ZOEKT_INDEX_DIR" -listen ":$ZOEKT_PORT" &
    ZOEKT_PID=$!

    local i
    for i in $(seq 1 10); do
        if curl -sf "http://localhost:$ZOEKT_PORT/" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Zoekt search ready at http://localhost:$ZOEKT_PORT${NC}"
            break
        fi
        sleep 0.5
    done
    echo ""
}

build_serve_cmd() {
    if [ -f "$CONFIG_FILE" ]; then
        echo "nexus serve --config $CONFIG_FILE --auth-type database --async"
    else
        local cmd="nexus serve --host ${NEXUS_HOST:-0.0.0.0} --port ${NEXUS_PORT:-2026} --auth-type database --async"
        if [ "${NEXUS_BACKEND:-}" = "gcs" ]; then
            cmd="$cmd --backend gcs --gcs-bucket ${NEXUS_GCS_BUCKET:-}"
            [ -n "${NEXUS_GCS_PROJECT:-}" ] && cmd="$cmd --gcs-project ${NEXUS_GCS_PROJECT}"
        fi
        echo "$cmd"
    fi
}

wait_for_health() {
    local port="${1:-$NEXUS_PORT}"
    local max="${2:-30}"
    local i
    for i in $(seq 1 "$max"); do
        if curl -sf "http://localhost:${port}/health" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Server is ready${NC}"
            return 0
        fi
        sleep 5
    done
    echo -e "${YELLOW}⚠ Server health check timeout after ${max}s (continuing anyway)${NC}"
    return 1
}

load_saved_mounts_if_needed() {
    if [ -f "$CONFIG_FILE" ]; then
        echo ""
        echo -e "${GREEN}✓ Backends loaded from configuration file${NC}"
        return 0
    fi

    echo ""
    echo "🔄 Loading saved mounts from database..."

    local base_url="http://localhost:${NEXUS_PORT:-2026}"
    if python3 "$SCRIPTS_DIR/load_saved_mounts.py" "$base_url" "$ADMIN_API_KEY" 2>/dev/null; then
        : # script prints result
    else
        echo -e "${YELLOW}⚠ Could not load saved mounts (API not ready or error)${NC}"
    fi
}

start_nexus_server() {
    echo ""
    echo "🚀 Starting Nexus server..."
    echo ""
    echo "  Host: ${NEXUS_HOST:-0.0.0.0}"
    echo "  Port: ${NEXUS_PORT:-2026}"
    echo "  Backend: ${NEXUS_BACKEND:-local}"

    if [ -f "$CONFIG_FILE" ]; then
        echo "  Config: $CONFIG_FILE"
        echo ""
        echo -e "${GREEN}✓ Using configuration file${NC}"
    else
        echo "  Config: Not found (using CLI options)"
        echo ""
    fi

    local cmd
    cmd="$(build_serve_cmd)"
    echo "Starting server..."
    eval "$cmd" &
    SERVER_PID=$!

    trap 'cleanup TERM' SIGTERM SIGINT
    # Allow more time for server initialization (GCS sync, cache population, etc.)
    wait_for_health "${NEXUS_PORT:-2026}" 60
    sleep 2

    load_saved_mounts_if_needed

    echo ""
    echo -e "${GREEN}✓ Server initialization complete${NC}"
    echo ""

    wait $SERVER_PID
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    print_banner
    check_permissions_flags
    wait_for_postgres
    ensure_skills_directory
    init_database
    setup_admin_api_key
    init_semantic_search_if_enabled
    start_zoekt_if_enabled
    start_nexus_server
}

main "$@"
