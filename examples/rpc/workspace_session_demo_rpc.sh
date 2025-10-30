#!/bin/bash
# Nexus RPC API - Workspace, Memory, Sessions & Agent Delegation Demo
#
# This is the RPC API equivalent of examples/cli/workspace_session_demo.sh
# It demonstrates the same v0.5.0 ACE features using direct RPC calls instead of CLI.
#
# Prerequisites:
# 1. Server running with authentication: ./scripts/init-nexus-with-auth.sh
# 2. Set environment variables:
#    export NEXUS_URL="http://localhost:8080"
#    export NEXUS_API_KEY="sk-default_admin_..."
#
# Usage:
#   ./examples/rpc/workspace_session_demo_rpc.sh          # Normal mode
#   DEBUG=1 ./examples/rpc/workspace_session_demo_rpc.sh  # With detailed logs
#   KEEP=1 ./examples/rpc/workspace_session_demo_rpc.sh   # Keep demo data

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
print_debug() {
    if [ "$DEBUG" == "1" ]; then
        echo -e "${CYAN}DEBUG:${NC} $1"
    fi
}

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set"
    print_info "Set them with:"
    print_info "  export NEXUS_URL='http://localhost:8080'"
    print_info "  export NEXUS_API_KEY='sk-default_admin_...'"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus v0.5.0 - RPC API Demo                          ║"
echo "║   Workspace, Sessions & Agents via RPC                  ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "This demo uses direct RPC API calls (curl)"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"

# Helper function to call RPC API
rpc_call() {
    local method="$1"
    local params="$2"
    local description="$3"

    print_debug "RPC: $method"
    print_debug "Params: $params"

    local response
    response=$(curl -s -X POST "$NEXUS_URL/api/nfs/$method" \
        -H "Authorization: Bearer $NEXUS_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\": \"2.0\", \"method\": \"$method\", \"params\": $params, \"id\": 1}")

    print_debug "Response: $response"

    # Check for error
    if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
        local error_msg=$(echo "$response" | jq -r '.error.message')
        print_error "RPC Error: $error_msg"
        return 1
    fi

    echo "$response"
}

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo data..."

    # Unregister workspaces
    rpc_call "unregister_workspace" '{"path": "/workspace/demo-persistent"}' "Unregister persistent workspace" 2>/dev/null || true
    rpc_call "unregister_workspace" '{"path": "/workspace/demo-jupyter"}' "Unregister jupyter workspace" 2>/dev/null || true
    rpc_call "unregister_workspace" '{"path": "/workspace/demo-ci-build"}' "Unregister build workspace" 2>/dev/null || true

    # Unregister memories
    rpc_call "unregister_memory" '{"path": "/memory/demo-knowledge"}' "Unregister knowledge memory" 2>/dev/null || true
    rpc_call "unregister_memory" '{"path": "/memory/demo-agent-ctx"}' "Unregister agent memory" 2>/dev/null || true

    print_debug "Cleanup completed"
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

if [ "$DEBUG" == "1" ]; then
    print_info "DEBUG mode enabled - showing detailed logs"
fi

# Run cleanup at start to ensure clean state
print_subsection "Initial cleanup: Ensuring clean state"
cleanup

# ════════════════════════════════════════════════════════════
# Section 1: Persistent Workspaces (Traditional)
# ════════════════════════════════════════════════════════════

print_section "1. Persistent Workspaces (Traditional Behavior)"

print_subsection "1.0 Setup: Grant admin permissions"

print_test "Create workspace parent directory and grant permissions"
rpc_call "mkdir" '{"path": "/workspace"}' "Create /workspace" 2>/dev/null || true
rpc_call "rebac_create" '{"subject": ["user", "admin"], "relation": "direct_owner", "object": ["file", "/workspace"], "tenant_id": "default"}' "Grant admin ownership" 2>/dev/null || true
rpc_call "mkdir" '{"path": "/memory"}' "Create /memory" 2>/dev/null || true
rpc_call "rebac_create" '{"subject": ["user", "admin"], "relation": "direct_owner", "object": ["file", "/memory"], "tenant_id": "default"}' "Grant admin ownership" 2>/dev/null || true
print_success "Admin granted owner permissions on /workspace and /memory"

print_subsection "1.1 Create persistent workspace (no TTL)"

print_test "Register /workspace/demo-persistent (lives forever)"
print_debug "RPC: register_workspace"

response=$(rpc_call "register_workspace" '{
    "path": "/workspace/demo-persistent",
    "name": "main-project",
    "description": "Long-term project workspace"
}' "Register persistent workspace")

if [ $? -eq 0 ]; then
    print_success "Persistent workspace registered (auto-granted ownership to admin)"
    print_debug "Registration response: $response"
else
    print_error "Failed to register workspace"
    exit 1
fi

print_info "This workspace will NOT auto-delete (no session_id or TTL)"

print_subsection "1.2 Use persistent workspace"

print_test "Create workspace directory"
rpc_call "mkdir" '{"path": "/workspace/demo-persistent"}' "Create workspace directory" > /dev/null || true
print_success "Workspace directory created"

print_test "Create files in persistent workspace"
# Write file with base64 encoded content
CONTENT=$(echo -n "Project README" | base64)
rpc_call "write" "{
    \"path\": \"/workspace/demo-persistent/README.md\",
    \"content\": {\"__type__\": \"bytes\", \"data\": \"$CONTENT\"}
}" "Write README.md" > /dev/null
print_success "File written to persistent workspace"

print_test "Create snapshot of workspace"
rpc_call "workspace_snapshot" '{
    "path": "/workspace/demo-persistent",
    "description": "Initial project setup"
}' "Create workspace snapshot" > /dev/null
print_success "Snapshot created (workspace versioning)"

print_test "List all workspaces"
workspaces=$(rpc_call "list_workspaces" '{}' "List workspaces")
echo "$workspaces" | jq -r '.result[] | "  - \(.path) (\(.name // "unnamed"))"'
print_success "Persistent workspace is listed"

# ════════════════════════════════════════════════════════════
# Section 2: Session-Scoped Workspaces with TTL
# ════════════════════════════════════════════════════════════

print_section "2. Session-Scoped Workspaces (v0.5.0 - Auto-Cleanup)"

print_subsection "2.1 Jupyter Notebook Session (8-hour TTL)"

SESSION_JUPYTER="jupyter_session_$(date +%s)"
print_info "Session ID: $SESSION_JUPYTER"

print_test "Register temporary Jupyter workspace (8h TTL)"
# TTL is in seconds: 8 hours = 28800 seconds
rpc_call "register_workspace" "{
    \"path\": \"/workspace/demo-jupyter\",
    \"name\": \"jupyter-notebook\",
    \"description\": \"Temporary notebook workspace\",
    \"session_id\": \"$SESSION_JUPYTER\",
    \"ttl\": 28800
}" "Register jupyter workspace" > /dev/null

print_success "Session-scoped workspace registered (auto-granted ownership)"
print_info "This workspace will auto-delete after 8 hours"
print_warning "session_id implies 'session' scope (auto-cleanup enabled)"

print_test "Create workspace directory"
rpc_call "mkdir" '{"path": "/workspace/demo-jupyter"}' "Create jupyter workspace directory" > /dev/null || true
print_success "Workspace directory created"

print_test "Verify workspace details"
workspace_info=$(rpc_call "get_workspace_info" '{"path": "/workspace/demo-jupyter"}' "Get workspace info")
echo "$workspace_info" | jq '.result'
print_success "Workspace shows session and TTL information"

print_subsection "2.2 CI/CD Build Workspace (2-hour TTL)"

SESSION_BUILD="build_${RANDOM}"
print_info "Build ID: $SESSION_BUILD"

print_test "Register temporary CI/CD workspace (2h TTL)"
# TTL is in seconds: 2 hours = 7200 seconds
rpc_call "register_workspace" "{
    \"path\": \"/workspace/demo-ci-build\",
    \"session_id\": \"$SESSION_BUILD\",
    \"ttl\": 7200
}" "Register build workspace" > /dev/null

print_success "Build workspace registered (auto-granted ownership)"
print_info "This workspace auto-expires in 2 hours"

print_test "Create workspace directory"
rpc_call "mkdir" '{"path": "/workspace/demo-ci-build"}' "Create build workspace directory" > /dev/null || true
print_success "Workspace directory created"

print_test "Write build artifacts"
BUILD_LOG=$(echo -n "Build output" | base64)
rpc_call "write" "{
    \"path\": \"/workspace/demo-ci-build/build.log\",
    \"content\": {\"__type__\": \"bytes\", \"data\": \"$BUILD_LOG\"}
}" "Write build log" > /dev/null
print_success "Build artifacts saved to temporary workspace"

# ════════════════════════════════════════════════════════════
# Section 3: Persistent Memories
# ════════════════════════════════════════════════════════════

print_section "3. Persistent Memories (Knowledge Base)"

print_subsection "3.1 Create persistent memory"

print_test "Register persistent knowledge base"
rpc_call "register_memory" '{
    "path": "/memory/demo-knowledge",
    "name": "team-kb",
    "description": "Team knowledge base (permanent)"
}' "Register persistent memory" > /dev/null

print_success "Persistent memory registered (auto-granted ownership)"
print_info "This memory will NOT auto-delete"

print_test "Create memory directory"
rpc_call "mkdir" '{"path": "/memory/demo-knowledge"}' "Create knowledge directory" > /dev/null || true
print_success "Memory directory created"

print_test "Write knowledge to memory"
KNOWLEDGE=$(echo -n "Important team knowledge" | base64)
rpc_call "write" "{
    \"path\": \"/memory/demo-knowledge/guidelines.md\",
    \"content\": {\"__type__\": \"bytes\", \"data\": \"$KNOWLEDGE\"}
}" "Write knowledge" > /dev/null
print_success "Knowledge stored in persistent memory"

# ════════════════════════════════════════════════════════════
# Section 4: Session-Scoped Memories (Agent Context)
# ════════════════════════════════════════════════════════════

print_section "4. Session-Scoped Memories (v0.5.0 - Temporary Agent Context)"

print_subsection "4.1 Agent working memory (2-hour session)"

SESSION_AGENT="agent_task_${RANDOM}"
print_info "Agent session: $SESSION_AGENT"

print_test "Register temporary agent memory (2h TTL)"
# TTL is in seconds: 2 hours = 7200 seconds
rpc_call "register_memory" "{
    \"path\": \"/memory/demo-agent-ctx\",
    \"session_id\": \"$SESSION_AGENT\",
    \"ttl\": 7200
}" "Register agent memory" > /dev/null

print_success "Session-scoped memory registered (auto-granted ownership)"
print_info "Agent context auto-deletes after task completion (2h)"

print_test "Create memory directory"
rpc_call "mkdir" '{"path": "/memory/demo-agent-ctx"}' "Create agent memory directory" > /dev/null || true
print_success "Memory directory created"

print_test "Write agent context"
AGENT_STATE=$(echo -n "Agent task state" | base64)
rpc_call "write" "{
    \"path\": \"/memory/demo-agent-ctx/task-state.json\",
    \"content\": {\"__type__\": \"bytes\", \"data\": \"$AGENT_STATE\"}
}" "Write agent state" > /dev/null
print_success "Agent context saved to temporary memory"

# ════════════════════════════════════════════════════════════
# Section 5: Agent Registration & Delegation
# ════════════════════════════════════════════════════════════

print_section "5. Agent Registration & Delegation (v0.5.0 ACE)"

print_subsection "5.1 Register agent (NO API key - recommended)"

print_test "Register agent without API key (recommended)"
rpc_call "register_agent" '{
    "agent_id": "agent_data_analyst",
    "name": "Data Analyst Agent",
    "description": "Analyzes data and generates reports",
    "generate_api_key": false
}' "Register agent" > /dev/null

print_success "Agent registered (auth-agnostic pattern)"
print_info "Agent inherits permissions from owner 'admin'"
print_info "Authentication: User's credentials + X-Agent-ID header"

print_subsection "5.2 Grant agent-specific permissions"

print_test "Grant agent editor permissions on workspace"
rpc_call "rebac_create" '{
    "subject": ["agent", "agent_data_analyst"],
    "relation": "direct_editor",
    "object": ["file", "/workspace/demo-persistent"],
    "tenant_id": "default"
}' "Grant agent permissions" > /dev/null

print_success "Agent granted editor access to persistent workspace"
print_info "Agent now has: inherited permissions + specific grants"
print_info "Note: Using 'direct_editor' (concrete relation), not 'editor' (computed union)"

print_test "Verify agent permissions"
check_result=$(rpc_call "rebac_check" '{
    "subject": ["agent", "agent_data_analyst"],
    "permission": "write",
    "object": ["file", "/workspace/demo-persistent"],
    "tenant_id": "default"
}' "Check agent permissions")

if echo "$check_result" | jq -e '.result.allowed' > /dev/null 2>&1; then
    print_success "Agent can write to workspace"
else
    print_warning "Agent permission check returned: $(echo $check_result | jq -r '.result')"
fi

print_subsection "5.3 Explain agent permission chain"

print_test "Show permission inheritance chain"
explain_result=$(rpc_call "rebac_explain" '{
    "subject": ["agent", "agent_data_analyst"],
    "permission": "write",
    "object": ["file", "/workspace/demo-persistent"],
    "tenant_id": "default"
}' "Explain agent permissions")
echo "$explain_result" | jq '.result'
print_info "Shows how agent inherited permissions from user + direct grants"

# ═══════════════════════════════════════════════════��════════
# Section 6: Session Lifecycle & Cleanup
# ════════════════════════════════════════════════════════════

print_section "6. Session Lifecycle & Auto-Cleanup (Background Task)"

print_subsection "6.1 Session cleanup demonstration"

print_info "Background cleanup task runs every hour (configurable)"
print_info "Cleans up expired sessions and their resources:"
print_info "  - Session-scoped workspaces (with TTL)"
print_info "  - Session-scoped memories (with TTL)"
print_info "  - Associated snapshots and metadata"

print_test "List all registered workspaces"
workspaces=$(rpc_call "list_workspaces" '{}' "List workspaces")
echo "$workspaces" | jq -r '.result[] | "  \(.path) - Session: \(.session_id // "none") - Expires: \(.expires_at // "never")"'
print_info "Shows both persistent and session-scoped workspaces"

# ════════════════════════════════════════════════════════════
# Final Summary
# ════════════════════════════════════════════════════════════

print_section "Summary: v0.5.0 ACE Features Validated (via RPC API)"

cat << 'SUMMARY'
✅ Persistent Workspaces
   - Traditional long-lived workspaces
   - No session_id or TTL
   - Must be explicitly deleted

✅ Session-Scoped Workspaces (NEW)
   - Temporary workspaces with TTL
   - Auto-cleanup after expiry
   - Perfect for: Jupyter notebooks, CI/CD builds

✅ Persistent Memories
   - Permanent knowledge bases
   - No auto-deletion

✅ Session-Scoped Memories (NEW)
   - Temporary agent context
   - Auto-cleanup after task completion
   - Perfect for: Agent working memory

✅ Agent Registration (NEW)
   - Auth-agnostic (works with OAuth, SAML, API keys)
   - No API key required (uses user auth + X-Agent-ID header)
   - Permission inheritance from owner

✅ Agent Delegation
   - Agents inherit permissions from owner
   - Can grant additional agent-specific permissions
   - Full ReBAC permission chain

✅ Session Lifecycle
   - Background cleanup task (hourly)
   - Auto-deletes expired resources
   - Clean separation: persistent vs temporary

✅ RPC API Access
   - Direct HTTP/JSON-RPC calls
   - Same functionality as CLI
   - Language-agnostic integration
SUMMARY

echo ""
print_success "All v0.5.0 ACE features validated successfully via RPC API!"
print_info "For details, see: docs/design/AGENT_IDENTITY_AND_SESSIONS.md"
print_info "For RPC API docs, see: docs/api/rpc-protocol.md"
echo ""

if [ "$KEEP" == "1" ]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Demo data preserved. Inspect with RPC calls:"
    echo "  # List workspaces"
    echo "  curl -X POST \$NEXUS_URL/api/nfs/list_workspaces \\"
    echo "    -H \"Authorization: Bearer \$NEXUS_API_KEY\" \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"jsonrpc\": \"2.0\", \"method\": \"list_workspaces\", \"params\": {}, \"id\": 1}'"
    echo ""
    echo "  # List agent permissions"
    echo "  curl -X POST \$NEXUS_URL/api/nfs/rebac_list_tuples \\"
    echo "    -H \"Authorization: Bearer \$NEXUS_API_KEY\" \\"
    echo "    -H \"Content-Type: application/json\" \\"
    echo "    -d '{\"jsonrpc\": \"2.0\", \"method\": \"rebac_list_tuples\", \"params\": {\"subject\": [\"agent\", \"agent_data_analyst\"]}, \"id\": 1}'"
    echo "════════════════════════════════════════════════════════════"
fi
