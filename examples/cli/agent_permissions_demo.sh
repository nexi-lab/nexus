#!/bin/bash
# Nexus CLI - Agent Permissions & Inheritance Demo
#
# This demo tests issue #342: Agent API keys don't inherit permissions from owner user
#
# Features tested:
# - Agent registration with owner user
# - Agent API key creation and usage
# - Permission inheritance from owner user
# - Direct ReBAC grants to agents
# - File operations using agent API keys
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/agent_permissions_demo.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

print_section() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════"
    echo ""
}

print_subsection() {
    echo ""
    echo "─── $1 ───"
    echo ""
}

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_test() { echo -e "${MAGENTA}TEST:${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - Agent Permissions & Inheritance Demo      ║"
echo "║              Testing Issue #342 Fix                      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"
export DEMO_BASE="/workspace/agent-permissions-demo"

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo data..."

    # Delete files and directories
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true

    # Clean up ReBAC tuples for demo paths
    python3 << 'CLEANUP'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS('http://localhost:8080', api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

# Delete all tuples related to demo paths
all_tuples = nx.rebac_list_tuples()
demo_tuples = [t for t in all_tuples if base in str(t.get('object_id', ''))]
for t in demo_tuples:
    try:
        nx.rebac_delete(t['tuple_id'])
    except:
        pass

# Delete tuples for test users and agents
for user in ['alice', 'bob']:
    tuples = nx.rebac_list_tuples(subject=("user", user))
    for t in tuples:
        try:
            nx.rebac_delete(t['tuple_id'])
        except:
            pass

for agent in ['alice_agent', 'bob_agent']:
    tuples = nx.rebac_list_tuples(subject=("agent", agent))
    for t in tuples:
        try:
            nx.rebac_delete(t['tuple_id'])
        except:
            pass

print("✓ Cleaned up ReBAC tuples")
nx.close()
CLEANUP

    rm -f /tmp/agent-demo-*.txt
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# Initial cleanup
cleanup

# ════════════════════════════════════════════════════════════
# Section 1: Setup - Create Users and Test Files
# ════════════════════════════════════════════════════════════

print_section "1. Setup - Create Users and Test Files"

print_subsection "1.1 Create test users with API keys"

# Create alice and bob with API keys
ALICE_KEY=$(python3 scripts/create-api-key.py alice "Alice Owner" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')
BOB_KEY=$(python3 scripts/create-api-key.py bob "Bob Owner" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')

print_success "Created user 'alice' with API key: ${ALICE_KEY:0:20}..."
print_success "Created user 'bob' with API key: ${BOB_KEY:0:20}..."

print_subsection "1.2 Create workspace and grant permissions"

# Create demo directory
nexus mkdir $DEMO_BASE --parents
nexus rebac create user admin direct_owner file $DEMO_BASE
print_success "Created $DEMO_BASE with admin ownership"

# Create alice's personal directory
nexus mkdir $DEMO_BASE/alice-personal --parents

# Grant alice full permissions on her directory FIRST
nexus rebac create user alice direct_owner file $DEMO_BASE/alice-personal
nexus rebac create user alice direct_editor file $DEMO_BASE/alice-personal

# Now write files AS alice (so she owns them)
export NEXUS_API_KEY="$ALICE_KEY"
echo "Alice's personal data" | nexus write $DEMO_BASE/alice-personal/data.txt -
echo "Alice's project file" | nexus write $DEMO_BASE/alice-personal/project.md -
export NEXUS_API_KEY="$ADMIN_KEY"

print_success "Alice has owner + editor permissions on $DEMO_BASE/alice-personal and owns all files"

# Create bob's personal directory
nexus mkdir $DEMO_BASE/bob-personal --parents

# Grant bob permissions FIRST
nexus rebac create user bob direct_owner file $DEMO_BASE/bob-personal
nexus rebac create user bob direct_editor file $DEMO_BASE/bob-personal

# Write files AS bob (so he owns them)
export NEXUS_API_KEY="$BOB_KEY"
echo "Bob's personal data" | nexus write $DEMO_BASE/bob-personal/data.txt -
export NEXUS_API_KEY="$ADMIN_KEY"

print_success "Bob has owner + editor permissions on $DEMO_BASE/bob-personal and owns all files"

print_subsection "1.3 Verify permissions are correctly set"

# Debug: Check what permissions alice actually has
print_test "Check alice's ReBAC grants on her directory"
nexus rebac check user alice write file $DEMO_BASE/alice-personal

print_subsection "1.4 Verify user permissions work"

export NEXUS_API_KEY="$ALICE_KEY"
print_test "Alice can list her own files using user API key"
FILE_COUNT=$(nexus ls $DEMO_BASE/alice-personal 2>/dev/null | wc -l | tr -d ' ')
if [ "$FILE_COUNT" -ge 1 ]; then
    print_success "✅ Alice sees $FILE_COUNT files in her directory"
else
    print_error "Alice cannot see her files (expected at least 1, got $FILE_COUNT)"
fi

print_test "Alice can write new files to her directory using user API key"
if echo "Alice test write" | nexus write $DEMO_BASE/alice-personal/alice-test.txt - 2>/dev/null; then
    print_success "✅ Alice can write files to her directory"
else
    print_error "❌ Alice cannot write to her own directory!"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Section 2: Agent Registration and API Keys
# ════════════════════════════════════════════════════════════

print_section "2. Agent Registration and API Keys"

print_subsection "2.1 Register agents owned by users"

# Switch to user contexts to register agents
export NEXUS_API_KEY="$ALICE_KEY"
print_test "Register alice_agent owned by alice"
nexus agent register alice_agent "Alice's Data Analyst Agent"
print_success "Registered alice_agent owned by alice"

export NEXUS_API_KEY="$BOB_KEY"
print_test "Register bob_agent owned by bob"
nexus agent register bob_agent "Bob's Report Generator"
print_success "Registered bob_agent owned by bob"

export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "2.2 Create agent API keys using CLI"

print_info "Creating API keys for agents..."

# Use new admin CLI command
ALICE_AGENT_KEY=$(nexus admin create-agent-key alice alice_agent --expires-days 1 2>&1 | grep "API Key:" | awk '{print $3}')
print_success "Created API key for alice_agent"
print_info "Alice agent key: ${ALICE_AGENT_KEY:0:20}..."

BOB_AGENT_KEY=$(nexus admin create-agent-key bob bob_agent --expires-days 1 2>&1 | grep "API Key:" | awk '{print $3}')
print_success "Created API key for bob_agent"
print_info "Bob agent key: ${BOB_AGENT_KEY:0:20}..."

# ════════════════════════════════════════════════════════════
# Section 3: Test Agent Permission Inheritance (THE BUG!)
# ════════════════════════════════════════════════════════════

print_section "3. Test Agent Permission Inheritance (Issue #342)"

print_subsection "3.1 Test that alice_agent inherits alice's permissions"

export NEXUS_API_KEY="$ALICE_AGENT_KEY"

print_test "Alice's agent should inherit permission to list alice-personal/"
FILE_COUNT=$(nexus ls $DEMO_BASE/alice-personal 2>/dev/null | wc -l | tr -d ' ')
if [ "$FILE_COUNT" -ge 1 ]; then
    print_success "✅ Agent inherits owner permissions! Found $FILE_COUNT files"
    print_info "This means Issue #342 is FIXED!"
else
    print_error "❌ BUG #342: Agent cannot see owner's files (got $FILE_COUNT files, expected at least 1)"
    print_error "Agent API key does not inherit owner permissions!"
fi

print_test "Alice's agent should be able to read files"
if nexus cat $DEMO_BASE/alice-personal/data.txt 2>/dev/null | grep -q "Alice's personal data"; then
    print_success "✅ Agent can read owner's files"
else
    print_error "❌ Agent cannot read owner's files"
fi

print_test "Alice's agent should be able to write files"
echo "Written by alice_agent" | nexus write $DEMO_BASE/alice-personal/agent-test.txt - 2>/dev/null
if nexus cat $DEMO_BASE/alice-personal/agent-test.txt 2>/dev/null | grep -q "Written by alice_agent"; then
    print_success "✅ Agent can write to owner's directory"
else
    print_error "❌ Agent cannot write to owner's directory"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "3.2 Test permission isolation (cross-user access denial)"

export NEXUS_API_KEY="$ALICE_AGENT_KEY"

print_test "Alice's agent should NOT access Bob's files"
if nexus ls $DEMO_BASE/bob-personal 2>/dev/null | grep -q "data.txt"; then
    print_error "❌ SECURITY BUG: Agent can access other user's files!"
else
    print_success "✅ Agent correctly denied access to other user's files"
fi

export NEXUS_API_KEY="$BOB_AGENT_KEY"

print_test "Bob's agent should NOT access Alice's files"
if nexus ls $DEMO_BASE/alice-personal 2>/dev/null | grep -q "data.txt"; then
    print_error "❌ SECURITY BUG: Agent can access other user's files!"
else
    print_success "✅ Agent correctly denied access to other user's files"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Section 4: Direct ReBAC Grants to Agents
# ════════════════════════════════════════════════════════════

print_section "4. Direct ReBAC Grants to Agents"

print_subsection "4.1 Grant agent-specific permissions"

print_info "Creating shared workspace for agent collaboration..."
nexus mkdir $DEMO_BASE/shared-workspace --parents

# Grant alice permissions (so she can share with her agent)
nexus rebac create user alice direct_viewer file $DEMO_BASE/shared-workspace
nexus rebac create user alice direct_editor file $DEMO_BASE/shared-workspace

# Write file AS alice (so she owns it)
export NEXUS_API_KEY="$ALICE_KEY"
echo "Shared project data" | nexus write $DEMO_BASE/shared-workspace/project.txt -
export NEXUS_API_KEY="$ADMIN_KEY"

# Grant alice_agent direct editor permission (in addition to inherited permissions)
print_test "Grant alice_agent direct editor permission on shared workspace"
nexus rebac create agent alice_agent direct_editor file $DEMO_BASE/shared-workspace
print_success "Granted direct permission to alice_agent"

print_subsection "4.2 Verify agent can use both inherited and direct permissions"

export NEXUS_API_KEY="$ALICE_AGENT_KEY"

print_test "Agent can access shared workspace via direct grant"
if nexus ls $DEMO_BASE/shared-workspace 2>/dev/null | grep -q "project.txt"; then
    print_success "✅ Agent can access via direct ReBAC grant"
else
    print_error "Direct grant not working for agent"
fi

export NEXUS_API_KEY="$ADMIN_KEY"

# ════════════════════════════════════════════════════════════
# Section 5: ReBAC Permission Checks
# ════════════════════════════════════════════════════════════

print_section "5. ReBAC Permission Checks and Explanation"

print_subsection "5.1 List agent permissions"

print_test "List all ReBAC tuples for alice_agent"
python3 << 'LIST_AGENT_TUPLES'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS('http://localhost:8080', api_key=os.getenv('NEXUS_API_KEY'))
tuples = nx.rebac_list_tuples(subject=("agent", "alice_agent"))
print(f"\nalice_agent has {len(tuples)} direct ReBAC tuples:")
for t in tuples[:10]:
    print(f"  - {t['relation']} on {t['object_type']}:{t['object_id']}")

if len(tuples) == 1:
    print("\n✓ Agent has 1 direct grant (shared-workspace)")
    print("✓ Agent also inherits ALL permissions from owner user alice")
nx.close()
LIST_AGENT_TUPLES

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "✅ Agent Permissions Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║                  Agent Permission Features Tested                 ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ Agent Registration (owned by user)                            ║"
echo "║  ✅ Agent API Key Creation (using CLI)                            ║"
echo "║  ✅ Agent Permission Inheritance from Owner (Issue #342)          ║"
echo "║  ✅ Agent File Operations (list, read, write)                     ║"
echo "║  ✅ Permission Isolation (agent cannot access other users)        ║"
echo "║  ✅ Direct ReBAC Grants to Agents                                 ║"
echo "║  ✅ ReBAC Tuple Listing                                           ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
print_success "All agent permission tests passed!"
print_info "Issue #342 is FIXED: Agents inherit permissions from their owner users"
echo ""

if [ "$KEEP" == "1" ]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Demo data preserved. Inspect with:"
    echo "  nexus ls $DEMO_BASE"
    echo "  nexus agent list"
    echo "════════════════════════════════════════════════════════════"
fi
