#!/bin/bash
# Test MCP Server with Authenticated Remote Nexus
#
# This script demonstrates:
# 1. Starting Nexus server with authentication
# 2. Creating users and API keys
# 3. Starting MCP server pointing to authenticated Nexus
# 4. Testing operations with different user permissions

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== MCP Server with Authentication Test ===${NC}\n"

# Cleanup function
cleanup() {
    echo -e "\n${YELLOW}Cleaning up...${NC}"

    # Kill Nexus server
    if [ ! -z "$NEXUS_PID" ]; then
        echo "Stopping Nexus server (PID: $NEXUS_PID)..."
        kill $NEXUS_PID 2>/dev/null || true
        wait $NEXUS_PID 2>/dev/null || true
    fi

    # Kill MCP server
    if [ ! -z "$MCP_PID" ]; then
        echo "Stopping MCP server (PID: $MCP_PID)..."
        kill $MCP_PID 2>/dev/null || true
        wait $MCP_PID 2>/dev/null || true
    fi

    # Clean up test data
    if [ "$KEEP" != "1" ]; then
        echo "Removing test database..."
        rm -f /tmp/nexus-mcp-auth-test.db
        rm -f /tmp/admin_key.txt
        rm -f /tmp/test_mcp_auth.py
        rm -f /tmp/nexus-server.log
    else
        echo "Keeping test database at: /tmp/nexus-mcp-auth-test.db"
        echo "Admin key saved at: /tmp/admin_key.txt"
    fi

    echo -e "${GREEN}Cleanup complete${NC}"
}

trap cleanup EXIT

# Test directory
TEST_DB="/tmp/nexus-mcp-auth-test.db"
rm -f "$TEST_DB"

echo -e "${BLUE}Step 1: Start Nexus server with authentication${NC}"
echo "Database: $TEST_DB"
echo ""

# Start Nexus server in background
export NEXUS_DATABASE_URL="sqlite:///$TEST_DB"
nexus serve \
    --host 127.0.0.1 \
    --port 8765 \
    --auth-type database \
    > /tmp/nexus-server.log 2>&1 &

NEXUS_PID=$!
echo "Nexus server started (PID: $NEXUS_PID)"
echo "Logs: /tmp/nexus-server.log"

# Wait for server to be ready
echo -n "Waiting for server to be ready"
for i in {1..30}; do
    if curl -s http://127.0.0.1:8765/health > /dev/null 2>&1; then
        echo -e " ${GREEN}✓${NC}"
        break
    fi
    echo -n "."
    sleep 1
done

# Check if server is up
if ! curl -s http://127.0.0.1:8765/health > /dev/null 2>&1; then
    echo -e "${RED}Failed to start Nexus server${NC}"
    cat /tmp/nexus-server.log
    exit 1
fi

echo ""
echo -e "${BLUE}Step 2: Create users and API keys${NC}"
echo ""

# Create admin user first (direct database access for bootstrap)
echo "Creating admin user (bootstrap)..."
python3 << 'PYTHON_BOOTSTRAP'
import sqlite3
import secrets
import hashlib
import uuid
from datetime import datetime, timezone

db_path = "/tmp/nexus-mcp-auth-test.db"
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Create admin API key
admin_key = f"nxk_{secrets.token_urlsafe(32)}"
admin_key_hash = hashlib.sha256(admin_key.encode()).hexdigest()
key_id = str(uuid.uuid4())

cursor.execute("""
    INSERT INTO api_keys (
        key_id, key_hash, user_id, subject_type, subject_id,
        tenant_id, is_admin, name, created_at, expires_at,
        revoked, revoked_at, last_used_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL)
""", (
    key_id,
    admin_key_hash,
    "admin",
    "user",
    "admin",
    "default",
    1,  # is_admin
    "Admin bootstrap key",
    datetime.now(timezone.utc).isoformat()
))

conn.commit()
conn.close()

# Save admin key to file
with open("/tmp/admin_key.txt", "w") as f:
    f.write(admin_key)

print(f"Admin key created: {admin_key}")
PYTHON_BOOTSTRAP

ADMIN_KEY=$(cat /tmp/admin_key.txt)
echo -e "  Admin API Key: ${GREEN}${ADMIN_KEY}${NC}"

# Now use admin API to create regular users
export NEXUS_URL=http://127.0.0.1:8765
export NEXUS_API_KEY=$ADMIN_KEY

echo ""
echo "Creating user: alice"
ALICE_OUTPUT=$(nexus admin create-user alice --name "Alice User" --json-output 2>&1)
ALICE_KEY=$(echo "$ALICE_OUTPUT" | grep -o '"api_key": "[^"]*"' | cut -d'"' -f4)

if [ -z "$ALICE_KEY" ]; then
    echo -e "${RED}Failed to create Alice's API key${NC}"
    echo "$ALICE_OUTPUT"
    exit 1
fi
echo -e "  API Key: ${GREEN}${ALICE_KEY}${NC}"

echo "Creating user: bob"
BOB_OUTPUT=$(nexus admin create-user bob --name "Bob User" --json-output 2>&1)
BOB_KEY=$(echo "$BOB_OUTPUT" | grep -o '"api_key": "[^"]*"' | cut -d'"' -f4)

if [ -z "$BOB_KEY" ]; then
    echo -e "${RED}Failed to create Bob's API key${NC}"
    echo "$BOB_OUTPUT"
    exit 1
fi
echo -e "  API Key: ${GREEN}${BOB_KEY}${NC}"

echo ""
echo -e "${BLUE}Step 3: Set up test files with permissions${NC}"
echo ""

# Alice creates her file
echo "Alice creates /workspace/alice-private.txt"
curl -s -X POST http://127.0.0.1:8765/api/nfs/write \
    -H "X-API-Key: $ALICE_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path": "/workspace/alice-private.txt", "content": "QWxpY2UncyBwcml2YXRlIGRhdGE="}' \
    > /dev/null
echo -e "  ${GREEN}✓${NC} Created (owner: alice)"

# Bob creates his file
echo "Bob creates /workspace/bob-private.txt"
curl -s -X POST http://127.0.0.1:8765/api/nfs/write \
    -H "X-API-Key: $BOB_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path": "/workspace/bob-private.txt", "content": "Qm9iJ3MgcHJpdmF0ZSBkYXRh"}' \
    > /dev/null
echo -e "  ${GREEN}✓${NC} Created (owner: bob)"

# Alice creates a shared file
echo "Alice creates /workspace/shared.txt (public read)"
curl -s -X POST http://127.0.0.1:8765/api/nfs/write \
    -H "X-API-Key: $ALICE_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path": "/workspace/shared.txt", "content": "U2hhcmVkIGRhdGE="}' \
    > /dev/null
echo -e "  ${GREEN}✓${NC} Created"

# Grant public read access to shared file
echo "Granting public read access to shared.txt"
NEXUS_DATABASE_URL="sqlite:///$TEST_DB" nexus rebac grant \
    --subject "user:*" \
    --relation reader \
    --object "file:/workspace/shared.txt" \
    > /dev/null 2>&1
echo -e "  ${GREEN}✓${NC} Public read granted"

echo ""
echo -e "${BLUE}Step 4: Test MCP operations with authentication${NC}"
echo ""

# Create a Python test script
cat > /tmp/test_mcp_auth.py << 'PYTHON_SCRIPT'
import sys
import httpx
import json

NEXUS_URL = "http://127.0.0.1:8765"
ALICE_KEY = sys.argv[1]
BOB_KEY = sys.argv[2]

def test_read(api_key, path, should_succeed=True):
    """Test reading a file via Nexus API."""
    try:
        response = httpx.post(
            f"{NEXUS_URL}/api/nfs/read",
            headers={"X-API-Key": api_key},
            json={"path": path},
            timeout=10.0
        )

        if should_succeed:
            if response.status_code == 200:
                return True, response.json()
            else:
                return False, f"Expected success but got: {response.status_code}"
        else:
            if response.status_code != 200:
                return True, "Correctly denied"
            else:
                return False, "Expected denial but succeeded"
    except Exception as e:
        return False, str(e)

print("Test 1: Alice reads her own file")
success, result = test_read(ALICE_KEY, "/workspace/alice-private.txt", should_succeed=True)
if success:
    print(f"  ✓ Alice can read her own file")
else:
    print(f"  ✗ FAILED: {result}")
    sys.exit(1)

print("\nTest 2: Bob tries to read Alice's file (should fail)")
success, result = test_read(BOB_KEY, "/workspace/alice-private.txt", should_succeed=False)
if success:
    print(f"  ✓ Bob correctly denied access to Alice's file")
else:
    print(f"  ✗ FAILED: {result}")
    sys.exit(1)

print("\nTest 3: Bob reads his own file")
success, result = test_read(BOB_KEY, "/workspace/bob-private.txt", should_succeed=True)
if success:
    print(f"  ✓ Bob can read his own file")
else:
    print(f"  ✗ FAILED: {result}")
    sys.exit(1)

print("\nTest 4: Alice tries to read Bob's file (should fail)")
success, result = test_read(ALICE_KEY, "/workspace/bob-private.txt", should_succeed=False)
if success:
    print(f"  ✓ Alice correctly denied access to Bob's file")
else:
    print(f"  ✗ FAILED: {result}")
    sys.exit(1)

print("\nTest 5: Bob reads shared file (public)")
success, result = test_read(BOB_KEY, "/workspace/shared.txt", should_succeed=True)
if success:
    print(f"  ✓ Bob can read shared file")
else:
    print(f"  ✗ FAILED: {result}")
    sys.exit(1)

print("\nTest 6: No API key (should fail)")
try:
    response = httpx.post(
        f"{NEXUS_URL}/api/nfs/read",
        json={"path": "/workspace/shared.txt"},
        timeout=10.0
    )
    if response.status_code != 200:
        print(f"  ✓ Request without API key correctly rejected")
    else:
        print(f"  ✗ FAILED: Request without API key should be rejected")
        sys.exit(1)
except Exception as e:
    print(f"  ✗ FAILED: {e}")
    sys.exit(1)

print("\n✅ All authentication tests passed!")
PYTHON_SCRIPT

# Run the tests
python3 /tmp/test_mcp_auth.py "$ALICE_KEY" "$BOB_KEY"

echo ""
echo -e "${BLUE}Step 5: Test MCP server integration${NC}"
echo ""

# Note: For full MCP testing, we'd need to:
# 1. Update MCP server to accept api_key parameter in tools
# 2. Start MCP server pointing to authenticated Nexus
# 3. Call MCP tools with api_key parameter

echo "Current MCP implementation status:"
echo "  - Remote Nexus connection: ✓ Implemented"
echo "  - Per-tool api_key parameter: ⚠️  Needs implementation"
echo ""
echo "To test manually:"
echo ""
echo -e "${YELLOW}# Start MCP server (in another terminal):${NC}"
echo "NEXUS_URL=http://127.0.0.1:8765 nexus mcp serve --transport http --port 8081"
echo ""
echo -e "${YELLOW}# Test with Alice's key:${NC}"
echo "curl -X POST http://localhost:8081/... (when api_key parameter is added)"
echo ""

echo -e "${BLUE}Server Information:${NC}"
echo "  Nexus Server: http://127.0.0.1:8765"
echo "  Health Check: curl http://127.0.0.1:8765/health"
echo "  Database: $TEST_DB"
echo ""
echo "  Alice's API Key: $ALICE_KEY"
echo "  Bob's API Key: $BOB_KEY"
echo ""
echo -e "${YELLOW}Press Enter to stop servers and clean up...${NC}"
read

echo ""
echo -e "${GREEN}=== Test Complete ===${NC}"
echo ""
echo "Summary:"
echo "  ✅ Nexus server with authentication: Working"
echo "  ✅ User isolation and permissions: Working"
echo "  ✅ API key authentication: Working"
echo "  ⚠️  MCP server with per-request auth: Needs implementation"
echo ""
echo "Next steps:"
echo "  1. Add api_key parameter to MCP tools"
echo "  2. Pass api_key to RemoteNexusFS"
echo "  3. Test MCP tools with different user keys"
