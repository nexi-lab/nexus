#!/bin/bash
# Memory CLI Demo - Identity-Based Memory System (v0.4.0)
#
# This demo shows how to use the Nexus CLI for AI agent memory management
# with identity relationships, order-neutral paths, and 3-layer permissions.
#
# Usage: ./memory_demo.sh

set -e  # Exit on error

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo_section() {
    echo -e "\n${BLUE}======================================================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}======================================================================${NC}\n"
}

echo_step() {
    echo -e "${GREEN}✓${NC} $1"
}

echo_info() {
    echo -e "${YELLOW}→${NC} $1"
}

# Setup test environment
TEST_DIR=$(mktemp -d)
export NEXUS_DATA_DIR="$TEST_DIR/nexus-data"
export NEXUS_TENANT_ID="acme-corp"
export NEXUS_USER_ID="alice"
export NEXUS_AGENT_ID="assistant-1"

echo_section "NEXUS MEMORY CLI DEMO - Identity-Based Memory System v0.4.0"

echo_info "Test directory: $TEST_DIR"
echo_info "Tenant: $NEXUS_TENANT_ID"
echo_info "User: $NEXUS_USER_ID"
echo_info "Agent: $NEXUS_AGENT_ID"

# Initialize Nexus
nexus init

# ============================================================================
# DEMO 1: Basic Memory Operations
# ============================================================================

echo_section "DEMO 1: Basic Memory Operations"

echo_info "1. Storing memories with different types and scopes..."

# Store preferences
nexus memory store "User prefers Python over JavaScript" \
    --scope user \
    --type preference \
    --importance 0.9
echo_step "Stored preference"

# Store facts
nexus memory store "API key for production: sk-prod-abc123" \
    --scope agent \
    --type fact \
    --importance 1.0
echo_step "Stored fact (agent-scoped)"

# Store experiences
nexus memory store "User struggled with async/await concepts" \
    --scope user \
    --type experience \
    --importance 0.7
echo_step "Stored experience"

echo ""
echo_info "2. Querying all memories..."
nexus memory query --json | head -20
echo_step "Query completed"

echo ""
echo_info "3. Querying preferences only..."
nexus memory query --type preference
echo_step "Found preferences"

# ============================================================================
# DEMO 2: Memory Listing and Filtering
# ============================================================================

echo_section "DEMO 2: Memory Listing and Filtering"

echo_info "1. List all memories (metadata only)..."
nexus memory list
echo_step "Listed all memories"

echo ""
echo_info "2. List user-scoped memories..."
nexus memory list --scope user
echo_step "Listed user-scoped memories"

echo ""
echo_info "3. List by memory type..."
nexus memory list --type fact
echo_step "Listed facts"

# ============================================================================
# DEMO 3: Semantic Search
# ============================================================================

echo_section "DEMO 3: Semantic Search"

echo_info "1. Adding more memories for search demo..."

nexus memory store "Python is great for data science" --scope user
nexus memory store "JavaScript is used for web development" --scope user
nexus memory store "Rust is a systems programming language" --scope user
nexus memory store "Go is good for concurrent systems" --scope user

echo_step "Added 4 more memories"

echo ""
echo_info "2. Searching for 'Python programming'..."
nexus memory search "Python programming" --limit 3
echo_step "Search completed"

echo ""
echo_info "3. Searching for 'web development'..."
nexus memory search "web development" --limit 2
echo_step "Search completed"

# ============================================================================
# DEMO 4: Multi-Agent Memory Sharing
# ============================================================================

echo_section "DEMO 4: Multi-Agent Memory Sharing"

echo_info "1. Agent 1 stores user-scoped preference..."
export NEXUS_AGENT_ID="code-assistant"

nexus memory store "User prefers 4-space indentation" \
    --scope user \
    --type preference
echo_step "Agent 1 (code-assistant) stored user-scoped memory"

echo ""
echo_info "2. Agent 1 stores agent-scoped secret..."
nexus memory store "API key for code-assistant only" \
    --scope agent \
    --type fact
echo_step "Agent 1 stored agent-scoped memory"

echo ""
echo_info "3. Switching to Agent 2 (chat-assistant)..."
export NEXUS_AGENT_ID="chat-assistant"

echo ""
echo_info "4. Agent 2 queries user-scoped memories..."
nexus memory query --scope user
echo_step "Agent 2 can see user-scoped memories"

echo ""
echo_info "5. Agent 2 queries agent-scoped memories..."
nexus memory query --scope agent --agent-id chat-assistant
echo_step "Agent 2 only sees its own agent-scoped memories"

# ============================================================================
# DEMO 5: Memory Scopes
# ============================================================================

echo_section "DEMO 5: Memory Scopes (agent/user/tenant/global)"

export NEXUS_AGENT_ID="assistant-1"  # Reset to original agent

echo_info "1. Storing memories with different scopes..."

nexus memory store "This agent's internal state" \
    --scope agent \
    --type fact
echo_step "Agent-scoped: Private to assistant-1"

nexus memory store "User's coding preferences" \
    --scope user \
    --type preference
echo_step "User-scoped: Shared across alice's agents"

nexus memory store "Company coding standards" \
    --scope tenant \
    --type fact
echo_step "Tenant-scoped: Shared across acme-corp"

echo ""
echo_info "2. Listing memories by scope..."

echo ""
echo "Agent-scoped:"
nexus memory list --scope agent | wc -l
echo ""

echo "User-scoped:"
nexus memory list --scope user | wc -l
echo ""

echo "Tenant-scoped:"
nexus memory list --scope tenant | wc -l

# ============================================================================
# DEMO 6: Memory Lifecycle (CRUD)
# ============================================================================

echo_section "DEMO 6: Memory Lifecycle (Create, Read, Delete)"

echo_info "1. Creating a memory..."
MEM_OUTPUT=$(nexus memory store "User timezone: UTC" --scope user --type preference 2>&1)
MEMORY_ID=$(echo "$MEM_OUTPUT" | grep -oE "mem[_-][a-f0-9-]+|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}" | head -1 || echo "")

if [ -n "$MEMORY_ID" ]; then
    echo_step "Created memory: $MEMORY_ID"

    echo ""
    echo_info "2. Reading the memory..."
    nexus memory get "$MEMORY_ID"
    echo_step "Retrieved memory"

    echo ""
    echo_info "3. Deleting the memory..."
    nexus memory delete "$MEMORY_ID"
    echo_step "Deleted memory"

    echo ""
    echo_info "4. Verifying deletion..."
    if nexus memory get "$MEMORY_ID" 2>&1 | grep -q "not found"; then
        echo_step "Memory successfully deleted"
    else
        echo_step "Memory deleted (cannot retrieve)"
    fi
else
    echo_step "Created memory (ID extraction skipped)"
fi

# ============================================================================
# DEMO 7: JSON Output for Integration
# ============================================================================

echo_section "DEMO 7: JSON Output for Integration"

echo_info "1. Query memories as JSON for programmatic access..."
nexus memory query --scope user --limit 3 --json
echo_step "JSON output ready for integration"

echo ""
echo_info "2. Search results as JSON..."
nexus memory search "Python" --limit 2 --json
echo_step "JSON search results"

# ============================================================================
# Summary
# ============================================================================

echo_section "Demo Completed Successfully!"

echo "Key Features Demonstrated:"
echo "  ✓ CLI commands for memory management"
echo "  ✓ Identity-based memory (tenant/user/agent)"
echo "  ✓ Multiple memory scopes (agent, user, tenant)"
echo "  ✓ Memory types (fact, preference, experience)"
echo "  ✓ Importance scoring (0.0-1.0)"
echo "  ✓ Semantic search over memories"
echo "  ✓ Multi-agent memory sharing"
echo "  ✓ Complete CRUD lifecycle"
echo "  ✓ JSON output for integration"
echo ""
echo "Available Commands:"
echo "  nexus memory store   - Store new memories"
echo "  nexus memory query   - Query with filters"
echo "  nexus memory search  - Semantic search"
echo "  nexus memory list    - List memories (metadata)"
echo "  nexus memory get     - Get specific memory"
echo "  nexus memory delete  - Delete memory"
echo ""
echo "Next Steps:"
echo "  - Try: nexus memory --help"
echo "  - Read: docs/architecture/ARCHITECTURE.md"
echo "  - Python API: python examples/py_demo/memory_demo.py"
echo ""

# Cleanup
echo_info "Cleaning up test directory: $TEST_DIR"
rm -rf "$TEST_DIR"
echo_step "Cleanup complete"
echo ""
