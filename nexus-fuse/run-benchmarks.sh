#!/bin/bash
# Run Nexus FUSE benchmarks with automatic server management

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Nexus FUSE Performance Benchmarks${NC}"
echo -e "${GREEN}========================================${NC}"
echo

# Check if server is already running
if lsof -i:2026 >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Nexus server already running on port 2026"
    STARTED_SERVER=false
else
    echo -e "${YELLOW}⚠${NC}  Starting Nexus test server..."

    # Start server in background
    cd ..
    uv run nexus serve --port 2026 --api-key sk-test-key-123 --auth-type static >/dev/null 2>&1 &
    SERVER_PID=$!
    cd "$SCRIPT_DIR"

    # Wait for server to be ready
    echo -n "  Waiting for server"
    for i in {1..30}; do
        if lsof -i:2026 >/dev/null 2>&1; then
            echo
            echo -e "${GREEN}✓${NC} Server started (PID: $SERVER_PID)"
            STARTED_SERVER=true
            break
        fi
        echo -n "."
        sleep 0.5
    done
    echo

    if ! lsof -i:2026 >/dev/null 2>&1; then
        echo -e "${RED}✗${NC} Failed to start server"
        exit 1
    fi
fi

# Cleanup function
cleanup() {
    if [ "$STARTED_SERVER" = true ]; then
        echo
        echo -e "${YELLOW}Stopping test server (PID: $SERVER_PID)...${NC}"
        kill $SERVER_PID 2>/dev/null || true
        wait $SERVER_PID 2>/dev/null || true
        echo -e "${GREEN}✓${NC} Server stopped"
    fi
}

# Register cleanup on exit
trap cleanup EXIT INT TERM

# Run benchmarks
echo
echo -e "${GREEN}Running benchmarks...${NC}"
echo

if [ "$1" = "--quick" ]; then
    echo -e "${YELLOW}Running in quick mode (fewer samples)${NC}"
    cargo bench --bench fuse_operations -- --quick
elif [ -n "$1" ]; then
    # Run specific benchmark
    echo -e "${YELLOW}Running specific benchmark: $1${NC}"
    cargo bench --bench fuse_operations "$1"
else
    # Run all benchmarks
    cargo bench --bench fuse_operations
fi

echo
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Benchmarks Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo "View detailed results:"
echo "  open target/criterion/report/index.html"
echo
echo "Compare with baseline:"
echo "  cargo bench --bench fuse_operations -- --baseline <name>"
echo
