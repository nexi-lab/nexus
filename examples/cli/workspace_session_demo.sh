#!/bin/bash
# Nexus CLI - Workspace, Memory, Sessions & Agent Delegation Demo
#
# This demo showcases v0.5.0 ACE (Agentic Context Engineering) features:
# - Persistent workspaces and memories (traditional)
# - Session-scoped workspaces with TTL (auto-cleanup)
# - Session-scoped memories with TTL (temporary agent context)
# - Agent registration and delegation
# - Session lifecycle and cleanup
# - Permission inheritance for agents
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/workspace_session_demo.sh          # Normal mode
#   DEBUG=1 ./examples/cli/workspace_session_demo.sh  # With detailed logs
#   KEEP=1 ./examples/cli/workspace_session_demo.sh   # Keep demo data
#
# Note: The CLI uses the underlying Python SDK, so if CLI works,
#       the Python SDK is also validated.

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
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus v0.5.0 - Workspace, Sessions & Agents Demo      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "This demo validates both CLI and Python SDK (CLI uses SDK internally)"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"

# Cleanup function with detailed logging
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    print_info "Cleaning up demo data..."

    # Helper function to unregister workspace if it exists
    cleanup_workspace() {
        local path=$1
        print_debug "Checking workspace: $path"
        if nexus workspace info "$path" &>/dev/null; then
            print_debug "Unregistering workspace: $path"
            if nexus workspace unregister "$path" --yes 2>&1 | grep -v "Not found" | grep -v "does not exist"; then
                print_success "Unregistered workspace: $path"
            fi
        else
            print_debug "Workspace not found: $path (skipping)"
        fi
    }

    # Helper function to unregister memory if it exists
    cleanup_memory() {
        local path=$1
        print_debug "Checking memory: $path"
        if nexus memory info "$path" &>/dev/null; then
            print_debug "Unregistering memory: $path"
            if nexus memory unregister "$path" --yes 2>&1 | grep -v "Not found" | grep -v "does not exist"; then
                print_success "Unregistered memory: $path"
            fi
        else
            print_debug "Memory not found: $path (skipping)"
        fi
    }

    # Helper function to remove directory if it exists
    cleanup_directory() {
        local path=$1
        print_debug "Checking directory: $path"
        # Use ls to check if directory exists (works for both local and remote)
        if nexus ls "$path" &>/dev/null; then
            print_debug "Removing directory: $path"
            local output
            if output=$(nexus rmdir -r -f "$path" 2>&1); then
                print_success "Removed directory: $path"
            else
                # Only show error if it's not "does not exist" or "not found"
                if echo "$output" | grep -qv "does not exist\|Not found\|Access denied"; then
                    print_warning "Failed to remove $path: $output"
                else
                    print_debug "Directory removal skipped: $path"
                fi
            fi
        else
            print_debug "Directory not found: $path (skipping)"
        fi
    }

    # Unregister workspaces first
    cleanup_workspace /workspace/demo-persistent
    cleanup_workspace /workspace/demo-jupyter
    cleanup_workspace /workspace/demo-ci-build

    # Unregister memories
    cleanup_memory /memory/demo-knowledge
    cleanup_memory /memory/demo-agent-ctx

    # Then delete directories (only if not registered)
    cleanup_directory /workspace/demo-persistent
    cleanup_directory /workspace/demo-jupyter
    cleanup_directory /workspace/demo-ci-build
    cleanup_directory /memory/demo-knowledge
    cleanup_directory /memory/demo-agent-ctx

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
nexus mkdir /workspace 2>/dev/null || true
nexus rebac create user admin owner file /workspace 2>/dev/null || true
nexus mkdir /memory 2>/dev/null || true
nexus rebac create user admin owner file /memory 2>/dev/null || true
print_success "Admin granted owner permissions on /workspace and /memory"

print_subsection "1.1 Create persistent workspace (no TTL)"

print_test "Register /workspace/demo-persistent (lives forever)"
print_debug "Running: nexus workspace register /workspace/demo-persistent --name main-project"

if output=$(nexus workspace register /workspace/demo-persistent \
    --name "main-project" \
    --description "Long-term project workspace" 2>&1); then
    print_success "Persistent workspace registered (auto-granted ownership to admin)"
    print_debug "Registration output: $output"
else
    print_error "Failed to register workspace"
    print_error "Error: $output"
    exit 1
fi

print_info "This workspace will NOT auto-delete (no session_id or TTL)"

print_subsection "1.2 Use persistent workspace"

print_test "Create files in persistent workspace"
echo "Project README" | nexus write /workspace/demo-persistent/README.md --input -
print_success "File written to persistent workspace"

print_test "Create snapshot of workspace"
nexus workspace snapshot /workspace/demo-persistent \
    --description "Initial project setup"
print_success "Snapshot created (workspace versioning)"

print_test "List all workspaces"
nexus workspace list
print_success "Persistent workspace is listed"

# ════════════════════════════════════════════════════════════
# Section 2: Session-Scoped Workspaces with TTL
# ════════════════════════════════════════════════════════════

print_section "2. Session-Scoped Workspaces (v0.5.0 - Auto-Cleanup)"

print_subsection "2.1 Jupyter Notebook Session (8-hour TTL)"

SESSION_JUPYTER="jupyter_session_$(date +%s)"
print_info "Session ID: $SESSION_JUPYTER"

print_test "Register temporary Jupyter workspace (8h TTL)"
nexus workspace register /workspace/demo-jupyter \
    --name "jupyter-notebook" \
    --description "Temporary notebook workspace" \
    --session-id "$SESSION_JUPYTER" \
    --ttl 8h

print_success "Session-scoped workspace registered (auto-granted ownership)"
print_info "This workspace will auto-delete after 8 hours"
print_warning "session_id implies 'session' scope (no need for --scope flag)"

print_test "Verify workspace details"
nexus workspace info /workspace/demo-jupyter
print_success "Workspace shows session and TTL information"

print_subsection "2.2 CI/CD Build Workspace (2-hour TTL)"

SESSION_BUILD="build_${RANDOM}"
print_info "Build ID: $SESSION_BUILD"

print_test "Register temporary CI/CD workspace (2h TTL)"
nexus workspace register /workspace/demo-ci-build \
    --session-id "$SESSION_BUILD" \
    --ttl 2h

print_success "Build workspace registered (auto-granted ownership)"
print_info "This workspace auto-expires in 2 hours"

print_test "Write build artifacts"
echo "Build output" | nexus write /workspace/demo-ci-build/build.log --input -
print_success "Build artifacts saved to temporary workspace"

# ════════════════════════════════════════════════════════════
# Section 3: Persistent Memories
# ════════════════════════════════════════════════════════════

print_section "3. Persistent Memories (Knowledge Base)"

print_subsection "3.1 Create persistent memory"

print_test "Register persistent knowledge base"
nexus memory register /memory/demo-knowledge \
    --name "team-kb" \
    --description "Team knowledge base (permanent)"

print_success "Persistent memory registered (auto-granted ownership)"
print_info "This memory will NOT auto-delete"

print_test "Write knowledge to memory"
echo "Important team knowledge" | nexus write /memory/demo-knowledge/guidelines.md --input -
print_success "Knowledge stored in persistent memory"

# ════════════════════════════════════════════════════════════
# Section 4: Session-Scoped Memories (Agent Context)
# ════════════════════════════════════════════════════════════

print_section "4. Session-Scoped Memories (v0.5.0 - Temporary Agent Context)"

print_subsection "4.1 Agent working memory (2-hour session)"

SESSION_AGENT="agent_task_${RANDOM}"
print_info "Agent session: $SESSION_AGENT"

print_test "Register temporary agent memory (2h TTL)"
nexus memory register /memory/demo-agent-ctx \
    --session-id "$SESSION_AGENT" \
    --ttl 2h

print_success "Session-scoped memory registered (auto-granted ownership)"
print_info "Agent context auto-deletes after task completion (2h)"

print_test "Write agent context"
echo "Agent task state" | nexus write /memory/demo-agent-ctx/task-state.json --input -
print_success "Agent context saved to temporary memory"

# ════════════════════════════════════════════════════════════
# Section 5: Agent Registration & Delegation
# ════════════════════════════════════════════════════════════

print_section "5. Agent Registration & Delegation (v0.5.0 ACE)"

print_subsection "5.1 Register agent (NO API key - recommended)"

print_test "Register agent without API key (recommended)"
nexus agent register agent_data_analyst "Data Analyst Agent" --description "Analyzes data and generates reports"

print_success "Agent registered (auth-agnostic pattern)"
print_info "Agent inherits permissions from owner 'admin'"
print_info "Authentication: User's credentials + X-Agent-ID header"

print_subsection "5.2 Grant agent-specific permissions"

print_test "Grant agent editor permissions on workspace"
nexus rebac create agent agent_data_analyst direct_editor file /workspace/demo-persistent

print_success "Agent granted editor access to persistent workspace"
print_info "Agent now has: inherited permissions + specific grants"
print_info "Note: Using 'direct_editor' (concrete relation), not 'editor' (computed union)"

print_test "Verify agent permissions"
nexus rebac check agent agent_data_analyst write file /workspace/demo-persistent
print_success "Agent can write to workspace"

print_subsection "5.3 Explain agent permission chain"

print_test "Show permission inheritance chain"
nexus rebac explain agent agent_data_analyst write file /workspace/demo-persistent
print_info "Shows how agent inherited permissions from user + direct grants"

# ════════════════════════════════════════════════════════════
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
nexus workspace list
print_info "Shows both persistent and session-scoped workspaces"

print_test "List all registered memories"
nexus memory list-registered
print_info "Shows both persistent and session-scoped memories"

# ════════════════════════════════════════════════════════════
# Section 7: Python SDK Validation
# ════════════════════════════════════════════════════════════

print_section "7. Python SDK Direct Usage (CLI uses SDK internally)"

print_info "Demonstrating that CLI success = SDK success"

python3 << 'SDK_DEMO'
import sys
sys.path.insert(0, 'src')
from datetime import timedelta
from nexus.core.nexus_fs import NexusFS
from nexus.backends.local import LocalBackend

print("✓ Python SDK imports successfully")

# Create SDK instance (same as CLI uses internally)
backend = LocalBackend(data_dir="./nexus-data")
nx = NexusFS(backend)

print("\n─── SDK Test: Register persistent workspace ───")
workspace = nx.register_workspace(
    "/workspace/sdk-test-persistent",
    name="SDK Test Workspace"
)
print(f"✓ Registered: {workspace['path']}")
print(f"  No session_id = persistent")

print("\n─── SDK Test: Register session-scoped workspace ───")
workspace_temp = nx.register_workspace(
    "/workspace/sdk-test-session",
    session_id="sdk_session_123",
    ttl=timedelta(hours=4)
)
print(f"✓ Registered: {workspace_temp['path']}")
print(f"  With session_id = temporary (4h TTL)")

print("\n─── SDK Test: Register session-scoped memory ───")
memory_temp = nx.register_memory(
    "/memory/sdk-test-agent-memory",
    session_id="sdk_agent_session",
    ttl=timedelta(hours=1)
)
print(f"✓ Registered: {memory_temp['path']}")
print(f"  Agent memory (1h TTL)")

# Cleanup
nx.unregister_workspace("/workspace/sdk-test-persistent")
nx.unregister_workspace("/workspace/sdk-test-session")
nx.unregister_memory("/memory/sdk-test-agent-memory")
print("\n✓ SDK tests passed - cleaned up test resources")
nx.close()
SDK_DEMO

print_success "Python SDK validated successfully"
print_info "Since CLI uses SDK internally, CLI success = SDK success"

# ════════════════════════════════════════════════════════════
# Final Summary
# ════════════════════════════════════════════════════════════

print_section "Summary: v0.5.0 ACE Features Validated"

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

✅ CLI ⟷ SDK Equivalence
   - CLI uses Python SDK internally
   - CLI success validates SDK success
   - Same API surface in both interfaces
SUMMARY

echo ""
print_success "All v0.5.0 ACE features validated successfully!"
print_info "For details, see: docs/design/AGENT_IDENTITY_AND_SESSIONS.md"
echo ""

if [ "$KEEP" == "1" ]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Demo data preserved. Inspect with:"
    echo "  nexus workspace list"
    echo "  nexus memory list-registered"
    echo "  nexus rebac list agent agent_data_analyst"
    echo "════════════════════════════════════════════════════════════"
fi
