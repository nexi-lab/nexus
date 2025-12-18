#!/bin/bash
# Script to reproduce the database corruption and crash issue
#
# This script:
# 1. Starts the Nexus server with a fresh data directory
# 2. Runs a sequence of concurrent API calls that trigger the crash
#
# Usage:
#   ./scripts/reproduce_crash.sh [DATA_DIR]
#
# Example:
#   ./scripts/reproduce_crash.sh /tmp/nexus-data-crash-test-1

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
cd "$SCRIPT_DIR"

# Default data directory with timestamp
DEFAULT_DATA_DIR="/tmp/nexus-data-crash-$(date +%s)"
DATA_DIR="${1:-$DEFAULT_DATA_DIR}"

echo "═══════════════════════════════════════════════════════"
echo "  Nexus Crash Reproduction Script"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "Data directory: $DATA_DIR"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check if Python reproduction script exists
if [ ! -f "$SCRIPT_DIR/scripts/reproduce_crash_api_calls.py" ]; then
    echo -e "${RED}Error: Python script not found at scripts/reproduce_crash_api_calls.py${NC}"
    echo "Please ensure the script is in the correct location."
    exit 1
fi

# Start the server in the background
echo -e "${YELLOW}Step 1: Starting server with fresh data directory...${NC}"
./local-demo.sh --start --data-dir "$DATA_DIR" --no-ui --no-langgraph &
SERVER_PID=$!

# Wait for server to be ready
echo -e "${YELLOW}Step 2: Waiting for server to be ready...${NC}"
MAX_WAIT=60
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Server is ready!${NC}"
        break
    fi
    sleep 1
    ELAPSED=$((ELAPSED + 1))
    if [ $((ELAPSED % 10)) -eq 0 ]; then
        echo "  Still waiting... ($ELAPSED seconds)"
    fi
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo -e "${RED}✗ Server failed to start within $MAX_WAIT seconds${NC}"
    kill $SERVER_PID 2>/dev/null || true
    exit 1
fi

# Give it a few more seconds to finish provisioning
sleep 5

echo ""
echo -e "${YELLOW}Step 3: Running concurrent API calls to trigger crash...${NC}"
echo ""

# Activate the Python venv and run the reproduction script
source "$SCRIPT_DIR/.venv/bin/activate"
python3 "$SCRIPT_DIR/scripts/reproduce_crash_api_calls.py"

# The script should crash the server before we get here
# If we do get here, the bug might be fixed or we didn't trigger it
echo ""
echo -e "${GREEN}Script completed without crash.${NC}"
echo "The server may still be running. Check the logs above for any warnings."
echo ""

# Kill the server
echo "Stopping server..."
kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true
echo -e "${GREEN}✓ Server stopped${NC}"
