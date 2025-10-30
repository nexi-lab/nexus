#!/bin/bash
# Admin CLI Commands Demo Script
#
# Demonstrates the new Admin CLI commands for user/API key management (issue #266)
# This script automatically:
# 1. Sets up a test database and server
# 2. Creates an admin API key
# 3. Tests all Admin CLI commands
# 4. Shows beautiful output with tables
# 5. Cleans up resources
#
# Usage:
#   ./examples/cli/admin_cli_demo.sh
#   KEEP=1 ./examples/cli/admin_cli_demo.sh  # Skip cleanup

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Configuration
DEMO_DIR="/tmp/nexus-admin-cli-demo-$$"
DB_PATH="$DEMO_DIR/nexus.db"
SERVER_PORT=19080
SERVER_URL="http://localhost:$SERVER_PORT"
KEEP="${KEEP:-0}"  # Set KEEP=1 to skip cleanup

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Nexus Admin CLI Commands Demo${NC}"
echo -e "${BLUE}Issue: #266${NC}"
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
echo -e "${YELLOW}[1/7] Setting up demo environment${NC}"
mkdir -p "$DEMO_DIR"
export NEXUS_DATABASE_URL="sqlite:///$DB_PATH"

# IMPORTANT: Unset NEXUS_URL to prevent circular dependency
unset NEXUS_URL

echo "  Database: $DB_PATH"
echo "  Server URL: $SERVER_URL"
echo

# Start Nexus server (this will initialize the database schema)
echo -e "${YELLOW}[2/7] Starting Nexus server${NC}"
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
echo -e "${YELLOW}[3/7] Creating admin API key${NC}"
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

# Create admin API key (server is stopped, database exists with tables)
ADMIN_KEY_OUTPUT=$(python scripts/create-api-key.py admin "Admin CLI Demo" --admin --days 365 2>&1)
ADMIN_KEY=$(echo "$ADMIN_KEY_OUTPUT" | grep "API Key:" | awk '{print $3}')

if [ -z "$ADMIN_KEY" ]; then
    echo -e "${RED}✗ Failed to create admin key${NC}"
    echo "Output:"
    echo "$ADMIN_KEY_OUTPUT"
    exit 1
fi

echo -e "  ${GREEN}✓ Admin key created${NC}"
echo "  Key: ${ADMIN_KEY:0:30}..."

# Restart server
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
    exit 1
fi
echo

# Set environment variables for CLI commands
export NEXUS_URL="$SERVER_URL"
export NEXUS_API_KEY="$ADMIN_KEY"

# Test CLI commands
echo -e "${YELLOW}[4/7] Testing Admin CLI commands${NC}"
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 1: Create regular user 'alice'${NC}"
echo -e "${CYAN}Command: nexus admin create-user alice --name \"Alice Smith\" --expires-days 90${NC}"
echo
nexus admin create-user alice --name "Alice Smith" --expires-days 90
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 2: Create admin user 'bob'${NC}"
echo -e "${CYAN}Command: nexus admin create-user bob --name \"Bob Admin\" --is-admin${NC}"
echo
nexus admin create-user bob --name "Bob Admin" --is-admin
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 3: Create agent key${NC}"
echo -e "${CYAN}Command: nexus admin create-user agent1 --name \"Test Agent\" --subject-type agent${NC}"
echo
nexus admin create-user agent1 --name "Test Agent" --subject-type agent
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 4: List all users (table view)${NC}"
echo -e "${CYAN}Command: nexus admin list-users${NC}"
echo
nexus admin list-users
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 5: List users for specific user${NC}"
echo -e "${CYAN}Command: nexus admin list-users --user-id alice${NC}"
echo
nexus admin list-users --user-id alice
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 6: Get user details (JSON output)${NC}"
echo -e "${CYAN}Command: nexus admin get-user --user-id alice --json-output${NC}"
echo
nexus admin get-user --user-id alice --json-output | python3 -m json.tool
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 7: Create additional key for alice${NC}"
echo -e "${CYAN}Command: nexus admin create-key alice --name \"Alice's Second Device\" --expires-days 30${NC}"
echo
nexus admin create-key alice --name "Alice's Second Device" --expires-days 30
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 8: List all keys including alice's new key${NC}"
echo -e "${CYAN}Command: nexus admin list-users${NC}"
echo
nexus admin list-users
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 9: Get key ID for revocation test${NC}"
echo
# Get the key_id for alice's first key
ALICE_KEY_ID=$(nexus admin list-users --user-id alice --json-output | python3 -c "import sys,json; keys=json.load(sys.stdin); print([k['key_id'] for k in keys if 'Second Device' not in k['name']][0])" 2>/dev/null)
echo "  Alice's first key ID: $ALICE_KEY_ID"
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 10: Revoke alice's first key${NC}"
echo -e "${CYAN}Command: nexus admin revoke-key $ALICE_KEY_ID${NC}"
echo
nexus admin revoke-key "$ALICE_KEY_ID"
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 11: List keys including revoked${NC}"
echo -e "${CYAN}Command: nexus admin list-users --include-revoked${NC}"
echo
nexus admin list-users --include-revoked
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 12: Update bob's key (extend expiry)${NC}"
BOB_KEY_ID=$(nexus admin list-users --user-id bob --json-output | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['key_id'])" 2>/dev/null)
echo -e "${CYAN}Command: nexus admin update-key $BOB_KEY_ID --expires-days 180${NC}"
echo
nexus admin update-key "$BOB_KEY_ID" --expires-days 180
echo

echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}Test 13: Verify bob's key was updated${NC}"
echo -e "${CYAN}Command: nexus admin get-user --key-id $BOB_KEY_ID${NC}"
echo
nexus admin get-user --key-id "$BOB_KEY_ID"
echo

# Summary
echo -e "${YELLOW}[5/7] Command Reference${NC}"
echo
echo -e "${CYAN}Available Commands:${NC}"
echo "  nexus admin create-user <user_id> --name <name> [options]"
echo "  nexus admin create-key <user_id> --name <name> [options]"
echo "  nexus admin list-users [--user-id <id>] [--is-admin] [options]"
echo "  nexus admin get-user --user-id <id> | --key-id <id>"
echo "  nexus admin revoke-key <key_id>"
echo "  nexus admin update-key <key_id> [--expires-days <days>] [--is-admin <bool>]"
echo
echo -e "${CYAN}Common Options:${NC}"
echo "  --json-output          Output as JSON instead of formatted tables"
echo "  --remote-url <url>     Server URL (or set NEXUS_URL)"
echo "  --remote-api-key <key> Admin API key (or set NEXUS_API_KEY)"
echo

# Migration example
echo -e "${YELLOW}[6/7] Migration Example${NC}"
echo
echo -e "${RED}Before (Required SSH access):${NC}"
echo "  ssh nexus-server"
echo "  cd /opt/nexus"
echo "  python scripts/create-api-key.py alice \"Alice's Key\" --days 90"
echo
echo -e "${GREEN}After (Remote admin CLI, no SSH):${NC}"
echo "  export NEXUS_URL=http://nexus-server:8080"
echo "  export NEXUS_API_KEY=<your_admin_key>"
echo "  nexus admin create-user alice --name \"Alice's Key\" --expires-days 90"
echo

# Test summary
echo -e "${YELLOW}[7/7] Test Summary${NC}"
echo
echo "Users created:"
echo "  • admin   (demo admin key)"
echo "  • alice   (regular user, 2 keys, 1 revoked)"
echo "  • bob     (admin user, expiry extended)"
echo "  • agent1  (agent type)"
echo
echo "Commands tested:"
echo "  ✓ create-user (3 users created)"
echo "  ✓ create-key (additional key for alice)"
echo "  ✓ list-users (with various filters)"
echo "  ✓ get-user (by user_id and key_id)"
echo "  ✓ revoke-key (alice's first key)"
echo "  ✓ update-key (extended bob's expiry)"
echo

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✓ All CLI commands tested successfully!${NC}"
echo -e "${BLUE}========================================${NC}"
echo
echo -e "${CYAN}Try it yourself:${NC}"
echo "  export NEXUS_URL=$SERVER_URL"
echo "  export NEXUS_API_KEY=$ADMIN_KEY"
echo "  nexus admin list-users"
echo
