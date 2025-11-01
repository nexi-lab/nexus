#!/bin/bash
set -e

# Agent Permission Inheritance Demo
# Tests that agents inherit permissions from their parent users

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Agent Permission Inheritance Demo                        ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo
echo "This demo shows:"
echo "  1. Users can access directories based on their permissions"
echo "  2. Agents registered under users inherit their permissions"
echo "  3. Empty directories are visible when you have subdirectory access"
echo

# Check environment
if [ -z "$NEXUS_URL" ]; then
    echo "Error: NEXUS_URL not set. Please run:"
    echo "  export NEXUS_URL=http://localhost:8080"
    exit 1
fi

if [ -z "$NEXUS_API_KEY" ]; then
    echo "Error: NEXUS_API_KEY not set. Please run:"
    echo "  source .nexus-admin-env"
    exit 1
fi

echo "Environment:"
echo "  NEXUS_URL: $NEXUS_URL"
echo "  Admin key: ${NEXUS_API_KEY:0:30}..."
echo

# Cleanup function
cleanup() {
    echo
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Cleaning up..."
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    # Clean up files
    nexus rmdir -r -f /workspace/alice 2>/dev/null || true

    # Clean up users (delete alice's keys and user)
    echo "Deleting alice's API keys..."
    nexus admin list-keys --json-output 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for key in data.get('keys', []):
        if key.get('user_id') == 'alice':
            print(key['key_id'])
except: pass
" | while read key_id; do
        [ -n "$key_id" ] && nexus admin delete-key "$key_id" 2>/dev/null || true
    done

    echo "Deleting alice user..."
    nexus admin list-users --json-output 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for user in data.get('users', []):
        if user.get('user_id') == 'alice':
            print(user['user_id'])
except: pass
" | while read user_id; do
        [ -n "$user_id" ] && nexus admin delete-user "$user_id" 2>/dev/null || true
    done

    echo "✓ Cleanup complete"
}

# Set up cleanup on exit
if [ -z "$KEEP" ]; then
    trap cleanup EXIT
fi

# ============================================================
# Test 1: Create user and grant permissions
# ============================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 1: Setting up user alice with directory permissions"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "1.1 Creating user alice..."
nexus admin create-user alice --name "Alice User" 2>&1 | grep -v "UserWarning" | head -5

echo
echo "1.2 Creating empty directory /workspace/alice..."
nexus mkdir /workspace/alice

echo
echo "1.3 Granting alice direct_owner permission on /workspace/alice..."
nexus rebac create user alice direct_owner file /workspace/alice 2>&1 | head -5

echo
echo "1.4 Verifying alice can read /workspace/alice..."
if nexus rebac check user alice read file /workspace/alice 2>&1 | grep -q "GRANTED"; then
    echo "✓ Permission check passed"
else
    echo "✗ Permission check failed"
    exit 1
fi

# ============================================================
# Test 2: User can see parent directories
# ============================================================
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 2: Testing user directory visibility"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "2.1 Creating USER API key for alice..."
ALICE_KEY_JSON=$(nexus admin create-key alice --name "Alice User Key" --json-output 2>&1 | grep -v "UserWarning")
ALICE_KEY=$(echo "$ALICE_KEY_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('api_key', ''))" 2>/dev/null)

if [ -z "$ALICE_KEY" ]; then
    echo "✗ Failed to create API key for alice"
    exit 1
fi

echo "✓ Created key: ${ALICE_KEY:0:40}..."

echo
echo "2.2 Listing root (/) as user alice..."
ALICE_FILES=$(NEXUS_API_KEY="$ALICE_KEY" nexus ls / 2>&1 | grep -v "UserWarning" | grep "/workspace" || echo "")

if [ -n "$ALICE_FILES" ]; then
    echo "✓ SUCCESS: User alice can see /workspace"
    echo "  (even though she only has permission on /workspace/alice)"
else
    echo "✗ FAIL: User alice cannot see /workspace"
    echo "  This is a bug - directory inference should show parent directories"
    exit 1
fi

# ============================================================
# Test 3: Register agent under user
# ============================================================
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 3: Registering agent under user alice"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "3.1 Registering agent 'alice_agent' as user alice..."
AGENT_RESULT=$(NEXUS_API_KEY="$ALICE_KEY" python3 << 'EOPYTHON'
import nexus
import json

try:
    nx = nexus.connect()

    # Register agent - will be owned by authenticated user (alice)
    result = nx.register_agent(
        agent_id="alice_agent",
        name="Alice's Agent",
        description="Agent for alice"
    )

    print(json.dumps(result))
    nx.close()
except Exception as e:
    print(json.dumps({"error": str(e)}))
EOPYTHON
)

AGENT_USER=$(echo "$AGENT_RESULT" | python3 -c "import sys, json; print(json.load(sys.stdin).get('user_id', ''))" 2>/dev/null)

if [ "$AGENT_USER" = "alice" ]; then
    echo "✓ Agent registered: alice_agent owned by alice"
else
    echo "✗ Agent registration failed or wrong owner: $AGENT_USER"
    echo "  Result: $AGENT_RESULT"
    exit 1
fi

# ============================================================
# Test 4: Agent inherits user permissions
# ============================================================
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Test 4: Testing agent permission inheritance"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "4.1 Creating AGENT API key for alice_agent..."
# Save alice key and restore admin key
SAVED_ALICE_KEY="$ALICE_KEY"
export NEXUS_API_KEY="${NEXUS_API_KEY_ADMIN:-$NEXUS_API_KEY}"  # Use admin key for this
AGENT_KEY_JSON=$(nexus admin create-agent-key alice alice_agent --name "Alice Agent Key" --json-output 2>&1 | grep -v "UserWarning")
AGENT_KEY=$(echo "$AGENT_KEY_JSON" | python3 -c "import sys, json; print(json.load(sys.stdin).get('api_key', ''))" 2>/dev/null)

if [ -z "$AGENT_KEY" ]; then
    echo "✗ Failed to create agent API key"
    exit 1
fi

echo "✓ Created agent key: ${AGENT_KEY:0:40}..."

echo
echo "4.2 Listing root (/) as agent alice_agent..."
AGENT_FILES=$(NEXUS_API_KEY="$AGENT_KEY" nexus ls / 2>&1 | grep -v "UserWarning" | grep "/workspace" || echo "")

if [ -n "$AGENT_FILES" ]; then
    echo "✓ SUCCESS: Agent alice_agent can see /workspace"
    echo "  Agent successfully inherits permissions from user alice!"
else
    echo "✗ FAIL: Agent alice_agent cannot see /workspace"
    echo "  Permission inheritance is not working"
    exit 1
fi

echo
echo "4.3 Verifying agent can access /workspace/alice..."
AGENT_WORKSPACE=$(NEXUS_API_KEY="$AGENT_KEY" nexus ls /workspace 2>&1 | grep -v "UserWarning" | grep "/workspace/alice" || echo "")

if [ -n "$AGENT_WORKSPACE" ]; then
    echo "✓ Agent can list /workspace and see /workspace/alice"
else
    echo "✗ Agent cannot access /workspace contents"
    exit 1
fi

# ============================================================
# Summary
# ============================================================
echo
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ All Tests Passed!                                      ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo
echo "Summary:"
echo "  ✓ Users see parent directories when they have subdirectory permissions"
echo "  ✓ Agents inherit permissions from their parent users"
echo "  ✓ Empty directories are visible with proper permission inference"
echo
echo "Key Findings:"
echo "  • Directory inference works correctly for empty directories"
echo "  • Agent entities must be registered to enable inheritance"
echo "  • Agent registration creates parent→user relationships automatically"
echo "  • Permission checks use ReBAC for fine-grained access control"
echo

if [ -n "$KEEP" ]; then
    echo "Note: KEEP=1 set, data not cleaned up for inspection"
    echo "  Alice user key: ${ALICE_KEY:0:40}..."
    echo "  Agent key: ${AGENT_KEY:0:40}..."
fi
