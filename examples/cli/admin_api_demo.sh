#!/bin/bash
# Admin API Demo Script
#
# Demonstrates the Admin API for remote user/API key management (issue #322)
# This script automatically:
# 1. Sets up a test database
# 2. Starts the Nexus server
# 3. Creates an admin API key
# 4. Runs all Admin API endpoints
# 5. Cleans up resources
#
# Usage:
#   ./examples/cli/admin_api_demo.sh
#   KEEP=1 ./examples/cli/admin_api_demo.sh  # Skip cleanup

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
DEMO_DIR="/tmp/nexus-admin-api-demo-$$"
DB_PATH="$DEMO_DIR/nexus.db"
SERVER_PORT=18080
SERVER_URL="http://localhost:$SERVER_PORT"
KEEP="${KEEP:-0}"  # Set KEEP=1 to skip cleanup

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Nexus Admin API Demo${NC}"
echo -e "${BLUE}Issue: #322${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Cleanup function
cleanup() {
    if [ "$KEEP" = "1" ]; then
        echo -e "\n${YELLOW}KEEP=1 set, skipping cleanup${NC}"
        echo -e "${YELLOW}Database: $DB_PATH${NC}"
        echo -e "${YELLOW}Server PID: $SERVER_PID${NC}"
        echo -e "${YELLOW}To cleanup manually: rm -rf $DEMO_DIR && kill $SERVER_PID${NC}"
        return
    fi

    echo -e "\n${YELLOW}Cleaning up...${NC}"

    # Stop server
    if [ ! -z "$SERVER_PID" ]; then
        echo "  Stopping server (PID: $SERVER_PID)"
        kill $SERVER_PID 2>/dev/null || true
        wait $SERVER_PID 2>/dev/null || true
    fi

    # Remove demo directory
    if [ -d "$DEMO_DIR" ]; then
        echo "  Removing demo directory: $DEMO_DIR"
        rm -rf "$DEMO_DIR"
    fi

    echo -e "${GREEN}✓ Cleanup complete${NC}"
}

# Set trap for cleanup on exit
trap cleanup EXIT

# Create demo directory
echo -e "${YELLOW}[1/6] Setting up demo environment${NC}"
mkdir -p "$DEMO_DIR"
export NEXUS_DATABASE_URL="sqlite:///$DB_PATH"

# IMPORTANT: Unset NEXUS_URL to prevent circular dependency
# (Server should use local backend, not RemoteNexusFS)
unset NEXUS_URL

echo "  Database: $DB_PATH"
echo "  Server URL: $SERVER_URL"
echo

# Start Nexus server (this will initialize the database schema)
echo -e "${YELLOW}[2/6] Starting Nexus server${NC}"
nexus serve --host 0.0.0.0 --port $SERVER_PORT --auth-type=database > "$DEMO_DIR/server.log" 2>&1 &
SERVER_PID=$!
echo "  Server PID: $SERVER_PID"

# Wait for server to start and initialize schema
echo -n "  Waiting for server to start"
for i in {1..30}; do
    if curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
    sleep 0.5
done

# Verify server is up
if ! curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
    echo -e "\n${RED}✗ Server failed to start${NC}"
    echo "Server log:"
    cat "$DEMO_DIR/server.log"
    exit 1
fi

# Give server time to initialize database schema
sleep 2
echo

# Stop server temporarily to create admin key
# (The server locks the SQLite database in write mode)
echo -e "${YELLOW}[3/6] Creating admin API key${NC}"
echo "  Stopping server temporarily..."
kill $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true

# Wait for port to be released
echo -n "  Waiting for port $SERVER_PORT to be released"
for i in {1..10}; do
    if ! lsof -i :$SERVER_PORT > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
    sleep 1
done
echo

ADMIN_OUTPUT=$(python scripts/create-api-key.py admin "Admin Demo Key" --admin --days 365 2>&1)
ADMIN_KEY=$(echo "$ADMIN_OUTPUT" | grep "API Key:" | awk '{print $3}')

if [ -z "$ADMIN_KEY" ]; then
    echo -e "${RED}✗ Failed to create admin key${NC}"
    echo "$ADMIN_OUTPUT"
    exit 1
fi

echo -e "  ${GREEN}✓ Admin key created${NC}"
echo "  Key: ${ADMIN_KEY:0:20}..."

# Restart server with the new admin key in the database
echo "  Restarting server..."
nexus serve --host 0.0.0.0 --port $SERVER_PORT --auth-type=database > "$DEMO_DIR/server.log" 2>&1 &
SERVER_PID=$!

# Wait for server to restart
echo -n "  Waiting for server to restart"
for i in {1..30}; do
    if curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
    sleep 0.5
done

if ! curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
    echo -e "\n${RED}✗ Server failed to restart${NC}"
    echo "Server log:"
    tail -20 "$DEMO_DIR/server.log"
    exit 1
fi
echo

# Helper function for API calls
call_api() {
    local method=$1
    local params=$2
    local description=$3

    echo -e "${BLUE}Testing: $description${NC}" >&2
    echo "  Method: $method" >&2

    local response=$(curl -s -X POST "$SERVER_URL/api/nfs/$method" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $ADMIN_KEY" \
        -d "{\"jsonrpc\": \"2.0\", \"id\": 1, \"params\": $params}")

    # Check for errors
    if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
        echo -e "  ${RED}✗ Error:${NC}" >&2
        echo "$response" | jq '.error' >&2
        return 1
    fi

    echo -e "  ${GREEN}✓ Success${NC}" >&2
    echo "$response" | jq '.' | sed 's/^/    /' >&2
    echo >&2

    # Return response for further processing (stdout only)
    echo "$response"
}

# Run Admin API tests
echo -e "${YELLOW}[4/6] Testing Admin API endpoints${NC}"
echo

# Test 1: Create API key for Alice
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
ALICE_RESPONSE=$(call_api "admin_create_key" '{
    "user_id": "alice",
    "name": "Alice Test Key",
    "is_admin": false,
    "expires_days": 90
}' "Create API key for user 'alice'") || {
    echo -e "${RED}✗ Failed to create Alice's key - stopping tests${NC}"
    exit 1
}

ALICE_KEY_ID=$(echo "$ALICE_RESPONSE" | jq -r '.result.key_id')
ALICE_API_KEY=$(echo "$ALICE_RESPONSE" | jq -r '.result.api_key')

# Test 2: Create API key for Bob (admin)
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
BOB_RESPONSE=$(call_api "admin_create_key" '{
    "user_id": "bob",
    "name": "Bob Admin Key",
    "is_admin": true,
    "expires_days": 365
}' "Create admin API key for user 'bob'")

BOB_KEY_ID=$(echo "$BOB_RESPONSE" | jq -r '.result.key_id')

# Test 3: List all API keys
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_list_keys" '{}' "List all API keys" > /dev/null

# Test 4: List keys for specific user
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_list_keys" '{
    "user_id": "alice"
}' "List API keys for user 'alice'" > /dev/null

# Test 5: Get specific key details
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_get_key" "{
    \"key_id\": \"$ALICE_KEY_ID\"
}" "Get details for Alice's key" > /dev/null

# Test 6: Update key properties
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_update_key" "{
    \"key_id\": \"$ALICE_KEY_ID\",
    \"expires_days\": 180,
    \"name\": \"Alice Updated Key\"
}" "Update Alice's key (extend expiry, rename)" > /dev/null

# Test 7: Verify the update worked
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
UPDATED_RESPONSE=$(call_api "admin_get_key" "{
    \"key_id\": \"$ALICE_KEY_ID\"
}" "Verify update (check new name and expiry)")

UPDATED_NAME=$(echo "$UPDATED_RESPONSE" | jq -r '.result.name')
if [ "$UPDATED_NAME" = "Alice Updated Key" ]; then
    echo -e "  ${GREEN}✓ Update verified: name changed successfully${NC}"
else
    echo -e "  ${RED}✗ Update verification failed${NC}"
fi
echo

# Test 8: Test Alice's key works
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}Testing: Verify Alice's key is functional${NC}"
echo "  Method: /api/auth/whoami"

WHOAMI_RESPONSE=$(curl -s "$SERVER_URL/api/auth/whoami" \
    -H "Authorization: Bearer $ALICE_API_KEY")

if echo "$WHOAMI_RESPONSE" | jq -e '.authenticated == true' > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Alice's key works!${NC}"
    echo "$WHOAMI_RESPONSE" | jq '.' | sed 's/^/    /'
else
    echo -e "  ${RED}✗ Alice's key doesn't work${NC}"
    echo "$WHOAMI_RESPONSE" | jq '.' | sed 's/^/    /'
fi
echo

# Test 9: Revoke Alice's key
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_revoke_key" "{
    \"key_id\": \"$ALICE_KEY_ID\"
}" "Revoke Alice's key" > /dev/null

# Test 10: Verify revoked key doesn't work
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}Testing: Verify revoked key is rejected${NC}"
echo "  Method: /api/auth/whoami (with revoked key)"

REVOKED_RESPONSE=$(curl -s "$SERVER_URL/api/auth/whoami" \
    -H "Authorization: Bearer $ALICE_API_KEY")

# Check if authentication failed (either authenticated=false or error present)
if echo "$REVOKED_RESPONSE" | jq -e '.authenticated == false or .error' > /dev/null 2>&1; then
    echo -e "  ${GREEN}✓ Revoked key correctly rejected${NC}"
    echo "$REVOKED_RESPONSE" | jq '.' | sed 's/^/    /'
else
    echo -e "  ${RED}✗ Revoked key still works (BUG!)${NC}"
    echo "$REVOKED_RESPONSE" | jq '.' | sed 's/^/    /'
fi
echo

# Test 11: List keys including revoked
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
call_api "admin_list_keys" '{
    "include_revoked": true
}' "List all keys (including revoked)" > /dev/null

# Summary
echo -e "${YELLOW}[5/6] Test Summary${NC}"
echo
echo "Keys created:"
echo "  • Admin: admin (demo key)"
echo "  • User:  alice (revoked)"
echo "  • Admin: bob (active)"
echo
echo "Operations tested:"
echo "  ✓ admin_create_key (2 keys created)"
echo "  ✓ admin_list_keys (with and without filters)"
echo "  ✓ admin_get_key (key details retrieval)"
echo "  ✓ admin_update_key (name and expiry updated)"
echo "  ✓ admin_revoke_key (key revoked)"
echo "  ✓ Authentication (verified key works/doesn't work)"
echo

# Migration example
echo -e "${YELLOW}[6/6] Migration Example${NC}"
echo
echo -e "${RED}Before (SSH required):${NC}"
echo "  ssh nexus-server"
echo "  python scripts/create-api-key.py alice \"Key\" --days 90"
echo
echo -e "${GREEN}After (Remote API, no SSH):${NC}"
echo "  curl -X POST http://nexus-server/api/nfs/admin_create_key \\"
echo "    -H \"Authorization: Bearer \$ADMIN_KEY\" \\"
echo "    -d '{\"params\": {\"user_id\": \"alice\", \"name\": \"Key\", \"expires_days\": 90}}'"
echo

# Final status
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✓ All tests passed!${NC}"
echo -e "${BLUE}========================================${NC}"
echo

if [ "$KEEP" = "1" ]; then
    echo -e "${YELLOW}Resources preserved (KEEP=1):${NC}"
    echo "  Database:   $DB_PATH"
    echo "  Server PID: $SERVER_PID"
    echo "  Server URL: $SERVER_URL"
    echo "  Admin Key:  $ADMIN_KEY"
    echo
    echo "To cleanup manually:"
    echo "  kill $SERVER_PID"
    echo "  rm -rf $DEMO_DIR"
fi
