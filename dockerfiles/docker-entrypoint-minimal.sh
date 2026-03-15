#!/bin/bash
# docker-entrypoint-minimal.sh - Nexus Minimal Docker container entrypoint
# Lightweight standalone server: storage only, no Zoekt, no semantic search,
# no cluster join, no saved mounts.

set -e
set -o pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
NEXUS_HOST="${NEXUS_HOST:-0.0.0.0}"
NEXUS_PORT="${NEXUS_PORT:-2026}"
NEXUS_DATA_DIR="${NEXUS_DATA_DIR:-/app/data}"
SERVER_PID=""

# Signal handling for graceful shutdown
cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down Nexus server...${NC}"
    if [ -n "$SERVER_PID" ]; then
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    echo -e "${GREEN}Nexus server stopped.${NC}"
    exit 0
}

trap cleanup SIGTERM SIGINT

print_banner() {
    echo ""
    echo "╔═══════════════════════════════════════════╗"
    echo "║     Nexus Minimal Server - Docker Init    ║"
    echo "╚═══════════════════════════════════════════╝"
    echo ""
    echo "  Profile: minimal (storage only)"
    echo "  Host:    ${NEXUS_HOST}"
    echo "  Port:    ${NEXUS_PORT}"
    echo "  Data:    ${NEXUS_DATA_DIR}"
    echo ""
}

ensure_data_dir() {
    if [ ! -d "$NEXUS_DATA_DIR" ]; then
        echo "Creating data directory: ${NEXUS_DATA_DIR}"
        mkdir -p "$NEXUS_DATA_DIR"
    fi
    echo -e "${GREEN}✓ Data directory ready${NC}"
}

start_server() {
    echo ""
    echo "🚀 Starting Nexus server..."
    echo ""

    nexusd --host "$NEXUS_HOST" --port "$NEXUS_PORT" &
    SERVER_PID=$!

    # Wait for server to become healthy
    local i
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${NEXUS_PORT}/health" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Server is ready${NC}"
            break
        fi
        sleep 2
    done

    echo ""
    echo -e "${GREEN}✓ Nexus minimal server running on port ${NEXUS_PORT}${NC}"
    echo ""

    wait $SERVER_PID
}

# Main
print_banner
ensure_data_dir
start_server
