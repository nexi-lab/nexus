#!/bin/bash
# Group-Based Permissions Demo Script (Issue #338)
#
# Demonstrates group-based permission inheritance for files using ReBAC.
# This script shows how users can inherit permissions through group membership.
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Or run standalone (auto-starts server):
#   ./examples/cli/group_permissions_demo.sh
#   KEEP=1 ./examples/cli/group_permissions_demo.sh  # Skip cleanup
#
# Issue: #338

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Configuration
DEMO_DIR="/tmp/nexus-group-perms-demo-$$"
DB_PATH="$DEMO_DIR/nexus.db"
SERVER_PORT=18081
SERVER_URL="http://localhost:$SERVER_PORT"
KEEP="${KEEP:-0}"  # Set KEEP=1 to skip cleanup
STANDALONE_MODE=0

print_section() {
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════${NC}"
    echo ""
}

print_subsection() {
    echo ""
    echo -e "${CYAN}─── $1 ───${NC}"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }

echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║      Group-Based Permissions Demo (Issue #338)          ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if we need to start our own server
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_warning "NEXUS_URL/NEXUS_API_KEY not set - starting standalone server"
    STANDALONE_MODE=1
else
    print_info "Using existing server: $NEXUS_URL"
fi

# Cleanup function
cleanup() {
    if [ "$KEEP" = "1" ]; then
        echo ""
        print_warning "KEEP=1 set, skipping cleanup"
        if [ "$STANDALONE_MODE" = "1" ]; then
            print_info "Database: $DB_PATH"
            print_info "Server PID: $SERVER_PID"
            print_info "To cleanup manually: rm -rf $DEMO_DIR && kill $SERVER_PID"
        fi
        return
    fi

    echo ""
    print_info "Cleaning up..."

    # Clean up demo files (skip if permissions don't allow)
    if [ -n "$ADMIN_KEY" ] && [ "$STANDALONE_MODE" = "0" ]; then
        export NEXUS_API_KEY="$ADMIN_KEY"
        nexus rmdir -r -f /workspace/shared 2>/dev/null || true
    fi

    # Stop server if we started it
    if [ "$STANDALONE_MODE" = "1" ] && [ ! -z "$SERVER_PID" ]; then
        print_info "Stopping server (PID: $SERVER_PID)"
        kill $SERVER_PID 2>/dev/null || true
        wait $SERVER_PID 2>/dev/null || true
    fi

    # Remove demo directory
    if [ -d "$DEMO_DIR" ]; then
        print_info "Removing demo directory: $DEMO_DIR"
        rm -rf "$DEMO_DIR"
    fi

    print_success "Cleanup complete"
}

# Set trap for cleanup on exit
trap cleanup EXIT

# ════════════════════════════════════════════════════════════
# Standalone Mode Setup
# ════════════════════════════════════════════════════════════

if [ "$STANDALONE_MODE" = "1" ]; then
    print_section "Setting Up Standalone Server"

    # Create demo directory
    print_info "Creating demo environment"
    mkdir -p "$DEMO_DIR"
    export NEXUS_DATABASE_URL="sqlite:///$DB_PATH"
    unset NEXUS_URL  # Prevent server from using remote mode

    print_info "Database: $DB_PATH"
    print_info "Server URL: $SERVER_URL"

    # Start Nexus server with debug logging
    print_info "Starting Nexus server with debug logging..."
    NEXUS_LOG_LEVEL=INFO nexus serve --host 0.0.0.0 --port $SERVER_PORT --auth-type=database > "$DEMO_DIR/server.log" 2>&1 &
    SERVER_PID=$!
    print_info "Server PID: $SERVER_PID"

    # Wait for server to start
    echo -n "  Waiting for server to start"
    for i in {1..30}; do
        if curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
            echo -e " ${GREEN}✓${NC}"
            break
        fi
        echo -n "."
        sleep 0.5
    done

    if ! curl -s "$SERVER_URL/health" > /dev/null 2>&1; then
        print_error "Server failed to start!"
        cat "$DEMO_DIR/server.log"
        exit 1
    fi

    print_success "Server started successfully"

    # Give server time to initialize database schema
    sleep 2

    # Stop server temporarily to create admin key
    # (SQLite database locks in write mode)
    print_info "Creating admin API key..."
    print_info "Stopping server temporarily..."
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

    # Create admin API key using the script
    ADMIN_OUTPUT=$(python3 scripts/create-api-key.py admin "Admin Demo Key" --admin --days 365 2>&1)
    ADMIN_API_KEY_JSON=$(echo "$ADMIN_OUTPUT" | grep "API Key:" | awk '{print $3}')

    if [ -z "$ADMIN_API_KEY_JSON" ]; then
        print_error "Failed to create admin key"
        echo "$ADMIN_OUTPUT"
        exit 1
    fi

    print_success "Admin API key created"
    print_info "Admin API Key: ${ADMIN_API_KEY_JSON:0:20}..."

    # Restart server with debug logging
    print_info "Restarting server with debug logging..."
    NEXUS_LOG_LEVEL=INFO nexus serve --host 0.0.0.0 --port $SERVER_PORT --auth-type=database > "$DEMO_DIR/server.log" 2>&1 &
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
        print_error "Server failed to restart!"
        cat "$DEMO_DIR/server.log"
        exit 1
    fi

    # Export credentials
    export NEXUS_URL="$SERVER_URL"
    export NEXUS_API_KEY="$ADMIN_API_KEY_JSON"
    ADMIN_KEY="$ADMIN_API_KEY_JSON"

    print_success "Admin API key created"
    print_info "Admin API Key: ${ADMIN_API_KEY_JSON:0:20}..."
else
    ADMIN_KEY="$NEXUS_API_KEY"
fi

# ════════════════════════════════════════════════════════════
# Section 1: Create Users
# ════════════════════════════════════════════════════════════

print_section "1. Creating Users"

print_subsection "Creating test users (via API keys): joe and alice"

# Create API keys for users (this implicitly creates the users)
# Using curl to call admin_create_key RPC method

print_info "Creating API key for user 'joe'..."
JOE_RESPONSE=$(curl -s -X POST "$NEXUS_URL/api/nfs/admin_create_key" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEXUS_API_KEY" \
    -d '{
        "jsonrpc": "2.0",
        "id": 1,
        "params": {
            "user_id": "joe",
            "name": "Joe Demo Key",
            "is_admin": false,
            "expires_days": 1
        }
    }')

if echo "$JOE_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    print_error "Failed to create key for joe"
    echo "$JOE_RESPONSE" | jq '.error'
    exit 1
fi

print_success "Created user 'joe' with API key"

print_info "Creating API key for user 'alice'..."
ALICE_RESPONSE=$(curl -s -X POST "$NEXUS_URL/api/nfs/admin_create_key" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEXUS_API_KEY" \
    -d '{
        "jsonrpc": "2.0",
        "id": 1,
        "params": {
            "user_id": "alice",
            "name": "Alice Demo Key",
            "is_admin": false,
            "expires_days": 1
        }
    }')

if echo "$ALICE_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    print_error "Failed to create key for alice"
    echo "$ALICE_RESPONSE" | jq '.error'
    exit 1
fi

print_success "Created user 'alice' with API key"

# ════════════════════════════════════════════════════════════
# Section 2: Create Group and Add Members
# ════════════════════════════════════════════════════════════

print_section "2. Creating Group and Adding Members"

print_subsection "Creating group 'engineering-team'"

# Create group membership using ReBAC
# [user, joe] --[member]--> [group, engineering-team]
# Syntax: nexus rebac create subject_type subject_id relation object_type object_id
JOE_MEMBERSHIP_OUTPUT=$(nexus rebac create user joe member group engineering-team)
JOE_MEMBERSHIP_ID=$(echo "$JOE_MEMBERSHIP_OUTPUT" | grep "Tuple ID:" | awk '{print $NF}')

print_success "Added joe to engineering-team group"
print_info "Joe's membership tuple ID: ${JOE_MEMBERSHIP_ID:0:36}..."

# Add alice too
nexus rebac create user alice member group engineering-team

print_success "Added alice to engineering-team group"

# Note: nexus rebac doesn't have a list-tuples command in CLI
# The tuples are created and can be queried via check commands

# ════════════════════════════════════════════════════════════
# Section 3: Create Files
# ════════════════════════════════════════════════════════════

print_section "3. Creating Test Files"

print_subsection "Granting admin user permissions on /workspace"

# Grant admin user permissions first
nexus rebac create user admin direct_owner file /workspace

print_success "Admin user granted permissions"

# Create workspace directory
nexus mkdir -p /workspace/shared
nexus mkdir -p /workspace/public

# Create test files
echo "This is a shared file that the engineering-team can edit" | \
    nexus write /workspace/shared/team_doc.md --input -

echo "This is a public file that the engineering-team can view" | \
    nexus write /workspace/public/readme.md --input -

echo "This is a private file only for the engineering-team owners" | \
    nexus write /workspace/shared/private.md --input -

print_success "Created test files"

# ════════════════════════════════════════════════════════════
# Section 4: Grant Group Permissions on Files
# ════════════════════════════════════════════════════════════

print_section "4. Granting Group Permissions (Issue #338 Fix)"

print_subsection "Understanding the tuple direction"
print_info "IMPORTANT: For group permissions to work with tupleToUserset,"
print_info "tuples must be created as:"
echo -e "  ${GREEN}✓ [file] --[direct_editor]--> [group]${NC}"
echo -e "  ${RED}✗ [group] --[direct_editor]--> [file]${NC}"
echo ""

print_subsection "Granting editor permission to engineering-team on /workspace/shared/team_doc.md"

# Grant group editor permission on shared file
# CORRECT DIRECTION: [file] --[direct_editor]--> [group]
nexus rebac create file /workspace/shared/team_doc.md direct_editor group engineering-team

print_success "Granted editor permission"

print_subsection "Granting viewer permission to engineering-team on /workspace/public/readme.md"

# Grant group viewer permission on public file
nexus rebac create file /workspace/public/readme.md direct_viewer group engineering-team

print_success "Granted viewer permission"

print_subsection "Granting owner permission to engineering-team on /workspace/shared/private.md"

# Grant group owner permission on private file
nexus rebac create file /workspace/shared/private.md direct_owner group engineering-team

print_success "Granted owner permission"

# ════════════════════════════════════════════════════════════
# Section 5: Test Permission Inheritance
# ════════════════════════════════════════════════════════════

print_section "5. Testing Permission Inheritance (The Magic!)"

print_subsection "Test 1: Joe should inherit WRITE permission via group editor role"

print_test "Checking if joe has write permission on /workspace/shared/team_doc.md"
if nexus rebac check user joe write file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Joe has write permission via engineering-team group!"
else
    print_error "✗ FAIL: Joe should have write permission but doesn't"
    if [ "$STANDALONE_MODE" = "1" ]; then
        print_error "Server logs (last 100 lines):"
        tail -100 "$DEMO_DIR/server.log" | grep -A 5 -B 5 "REBAC\|rebac\|permission\|Permission" || tail -100 "$DEMO_DIR/server.log"
    fi
    exit 1
fi

print_subsection "Test 2: Joe should inherit READ permission (editor can read)"

print_test "Checking if joe has read permission on /workspace/shared/team_doc.md"
if nexus rebac check user joe read file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Joe has read permission!"
else
    print_error "✗ FAIL: Joe should have read permission but doesn't"
    exit 1
fi

print_subsection "Test 3: Joe should NOT have EXECUTE permission (editor != owner)"

print_test "Checking if joe has execute permission on /workspace/shared/team_doc.md"
if nexus rebac check user joe execute file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_error "✗ FAIL: Joe should not have execute permission (editor != owner)"
    exit 1
else
    print_success "✓ PASS: Joe correctly denied execute permission!"
fi

print_subsection "Test 4: Alice should also inherit group permissions"

print_test "Checking if alice has write permission on /workspace/shared/team_doc.md"
if nexus rebac check user alice write file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Alice has write permission via engineering-team group!"
else
    print_error "✗ FAIL: Alice should have write permission but doesn't"
    exit 1
fi

print_subsection "Test 5: Viewer permissions - Joe should read but not write"

print_test "Checking if joe has read permission on /workspace/public/readme.md"
if nexus rebac check user joe read file /workspace/public/readme.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Joe has read permission via group viewer role!"
else
    print_error "✗ FAIL: Joe should have read permission but doesn't"
    exit 1
fi

print_test "Checking if joe has write permission on /workspace/public/readme.md"
if nexus rebac check user joe write file /workspace/public/readme.md | grep -q "GRANTED"; then
    print_error "✗ FAIL: Joe should not have write permission (viewer != editor)"
    exit 1
else
    print_success "✓ PASS: Joe correctly denied write permission!"
fi

print_subsection "Test 6: Owner permissions - Joe should have all permissions"

print_test "Checking if joe has execute permission on /workspace/shared/private.md"
if nexus rebac check user joe execute file /workspace/shared/private.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Joe has execute permission via group owner role!"
else
    print_error "✗ FAIL: Joe should have execute permission but doesn't"
    exit 1
fi

print_test "Checking if joe has write permission on /workspace/shared/private.md"
if nexus rebac check user joe write file /workspace/shared/private.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Joe has write permission via group owner role!"
else
    print_error "✗ FAIL: Joe should have write permission but doesn't"
    exit 1
fi

# ════════════════════════════════════════════════════════════
# Section 6: Explain Permission Paths
# ════════════════════════════════════════════════════════════

print_section "6. Understanding Permission Paths"

print_subsection "Let's see how joe gets write permission on team_doc.md"

print_info "Running: nexus rebac explain user joe write file /workspace/shared/team_doc.md"
echo ""

nexus rebac explain user joe write file /workspace/shared/team_doc.md || true

# ════════════════════════════════════════════════════════════
# Section 7: Test Permission Revocation
# ════════════════════════════════════════════════════════════

print_section "7. Testing Permission Revocation"

print_subsection "Test: Removing group membership should revoke permissions"

print_info "Currently joe has write permission on /workspace/shared/team_doc.md"
print_info "Joe's membership tuple ID: ${JOE_MEMBERSHIP_ID:0:36}..."

# Delete joe's group membership using the API
print_info "Removing joe from engineering-team group..."
DELETE_RESPONSE=$(curl -s -X POST "$NEXUS_URL/api/nfs/rebac_delete" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $NEXUS_API_KEY" \
    -d "{
        \"jsonrpc\": \"2.0\",
        \"id\": 1,
        \"params\": {
            \"tuple_id\": \"$JOE_MEMBERSHIP_ID\"
        }
    }")

if echo "$DELETE_RESPONSE" | jq -e '.error' > /dev/null 2>&1; then
    print_error "Failed to delete joe's membership"
    echo "$DELETE_RESPONSE" | jq '.error'
    exit 1
fi

print_success "Removed joe from engineering-team group"

# Now verify joe lost permissions
print_test "Verifying joe no longer has write permission on /workspace/shared/team_doc.md"
if nexus rebac check user joe write file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_error "✗ FAIL: Joe should have lost write permission after group removal"
    exit 1
else
    print_success "✓ PASS: Joe correctly lost write permission after group removal!"
fi

print_test "Verifying joe no longer has read permission on /workspace/public/readme.md"
if nexus rebac check user joe read file /workspace/public/readme.md | grep -q "GRANTED"; then
    print_error "✗ FAIL: Joe should have lost read permission after group removal"
    exit 1
else
    print_success "✓ PASS: Joe correctly lost read permission after group removal!"
fi

print_test "Verifying joe no longer has execute permission on /workspace/shared/private.md"
if nexus rebac check user joe execute file /workspace/shared/private.md | grep -q "GRANTED"; then
    print_error "✗ FAIL: Joe should have lost execute permission after group removal"
    exit 1
else
    print_success "✓ PASS: Joe correctly lost execute permission after group removal!"
fi

# Verify alice still has permissions (she's still in the group)
print_test "Verifying alice still has write permission (still in group)"
if nexus rebac check user alice write file /workspace/shared/team_doc.md | grep -q "GRANTED"; then
    print_success "✓ PASS: Alice still has permissions (still in group)!"
else
    print_error "✗ FAIL: Alice should still have write permission"
    exit 1
fi

# ════════════════════════════════════════════════════════════
# Success!
# ════════════════════════════════════════════════════════════

print_section "✨ All Tests Passed! ✨"

echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                                                          ║${NC}"
echo -e "${GREEN}║  Issue #338 Fix Verified Successfully! 🎉               ║${NC}"
echo -e "${GREEN}║                                                          ║${NC}"
echo -e "${GREEN}║  Group-based permissions are working correctly:          ║${NC}"
echo -e "${GREEN}║  ✓ Users inherit permissions through group membership   ║${NC}"
echo -e "${GREEN}║  ✓ Editor, viewer, and owner roles all work             ║${NC}"
echo -e "${GREEN}║  ✓ Permission checks correctly grant/deny access        ║${NC}"
echo -e "${GREEN}║  ✓ Removing membership correctly revokes permissions    ║${NC}"
echo -e "${GREEN}║                                                          ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"

if [ "$KEEP" != "1" ]; then
    echo ""
    print_info "Cleanup will run automatically on exit"
    print_info "To keep demo data, run: KEEP=1 $0"
fi
