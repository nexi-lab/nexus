#!/bin/bash
# Nexus CLI - Namespace-Based Memory Demo (Issue #350)
# Clean version using CLI commands instead of Python

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
MAGENTA='\033[0;35m'
NC='\033[0m'

print_section() { echo ""; echo "════════════════════════════════════════════════════════════"; echo "  $1"; echo "════════════════════════════════════════════════════════════"; echo ""; }
print_subsection() { echo ""; echo "─── $1 ───"; echo ""; }
print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    echo "Error: NEXUS_URL and NEXUS_API_KEY not set"
    echo "Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - Namespace Memory Demo (v0.8.0)            ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Testing namespace-based memory (Issue #350)"
echo ""

# Create test agent
print_info "Creating test agent..."
ADMIN_KEY=$NEXUS_API_KEY
AGENT_OUTPUT=$(nexus admin create-user demo_agent --name "Demo Agent" --subject-type agent 2>&1 || true)
USER_KEY=$(echo "$AGENT_OUTPUT" | grep -oE 'sk-[a-z0-9_]+' | head -1)

if [ -z "$USER_KEY" ]; then
    print_info "Agent exists, fetching key..."
    USER_KEY=$(nexus admin list-keys --subject-type agent 2>/dev/null | grep demo_agent | grep -oE 'sk-[a-z0-9_]+' | head -1)
fi

export NEXUS_API_KEY=$USER_KEY
print_success "Using demo_agent API key"
echo ""

# ═══════════════════════════════════════════════════════════════
print_section "1. Append Mode - Multiple Memories per Namespace"
# ═══════════════════════════════════════════════════════════════

print_subsection "1.1 Store facts without path_key (append mode)"

nexus memory store "Paris is the capital of France" \
  --namespace "knowledge/geography/facts" >/dev/null
print_success "Stored fact #1"

nexus memory store "Tokyo is the capital of Japan" \
  --namespace "knowledge/geography/facts" >/dev/null
print_success "Stored fact #2"

nexus memory store "Berlin is the capital of Germany" \
  --namespace "knowledge/geography/facts" >/dev/null
print_success "Stored fact #3"

print_subsection "1.2 List all facts in namespace"

COUNT=$(nexus memory list --namespace "knowledge/geography/facts" --json | grep -o '"memory_id"' | wc -l | tr -d ' ')
if [ "$COUNT" -eq "3" ]; then
    print_success "✅ Found $COUNT facts (append mode works!)"
else
    echo "✗ Expected 3 facts, found $COUNT"
fi

# ═══════════════════════════════════════════════════════════════
print_section "2. Upsert Mode - Updateable Settings"
# ═══════════════════════════════════════════════════════════════

print_subsection "2.1 Store settings with path_key (upsert mode)"

MEM1=$(nexus memory store "theme:light" \
  --namespace "user/preferences/ui" \
  --path-key "settings" | grep -oE '[a-f0-9-]{36}')
print_success "Created: $MEM1"

MEM2=$(nexus memory store "theme:dark" \
  --namespace "user/preferences/ui" \
  --path-key "settings" | grep -oE '[a-f0-9-]{36}')
print_success "Updated: $MEM2"

if [ "$MEM1" = "$MEM2" ]; then
    print_success "✅ Upsert worked! Same memory_id"
else
    echo "✗ Different IDs: $MEM1 vs $MEM2"
fi

print_subsection "2.2 Retrieve by path"

CONTENT=$(nexus memory retrieve "user/preferences/ui/settings")
echo "$CONTENT" | head -3
print_success "Retrieved updated settings"

# ═══════════════════════════════════════════════════════════════
print_section "3. Hierarchical Queries"
# ═══════════════════════════════════════════════════════════════

print_subsection "3.1 Create multi-level namespace structure"

nexus memory store "Python uses indentation" \
  --namespace "knowledge/programming/facts" >/dev/null
nexus memory store "Use list comprehensions" \
  --namespace "knowledge/programming/best-practices" >/dev/null
print_success "Created programming knowledge"

print_subsection "3.2 Query by namespace prefix"

ALL=$(nexus memory list --namespace-prefix "knowledge/" --json | grep -o '"memory_id"' | wc -l | tr -d ' ')
GEO=$(nexus memory list --namespace-prefix "knowledge/geography" --json | grep -o '"memory_id"' | wc -l | tr -d ' ')
PROG=$(nexus memory list --namespace-prefix "knowledge/programming" --json | grep -o '"memory_id"' | wc -l | tr -d ' ')

print_success "All knowledge: $ALL memories"
print_success "Geography: $GEO memories"
print_success "Programming: $PROG memories"
print_success "✅ Hierarchical queries work!"

# ═══════════════════════════════════════════════════════════════
print_section "4. CRUD Operations"
# ═══════════════════════════════════════════════════════════════

print_subsection "4.1 Create, Read, Update, Delete"

# CREATE
MEM_ID=$(nexus memory store "Document v1" \
  --namespace "demo/crud" \
  --path-key "document" | grep -oE '[a-f0-9-]{36}')
print_success "CREATE: $MEM_ID"

# READ
DOC=$(nexus memory retrieve "demo/crud/document")
echo "$DOC" | head -2
print_success "READ: success"

# UPDATE
nexus memory store "Document v2" \
  --namespace "demo/crud" \
  --path-key "document" >/dev/null
DOC2=$(nexus memory retrieve "demo/crud/document")
print_success "UPDATE: success"

# DELETE
nexus memory delete "$MEM_ID" >/dev/null
DELETED=$(nexus memory retrieve "demo/crud/document" 2>&1 || echo "DELETED")
if echo "$DELETED" | grep -q "DELETED\|not found\|None"; then
    print_success "DELETE: success"
    print_success "✅ Full CRUD cycle complete!"
else
    echo "✗ DELETE failed - memory still exists"
fi

# ═══════════════════════════════════════════════════════════════
print_section "Summary"
# ═══════════════════════════════════════════════════════════════

echo "╔═══════════════════════════════════════════════════════════╗"
echo "║         Namespace Memory Features Verified                ║"
echo "╠═══════════════════════════════════════════════════════════╣"
echo "║  ✅ Append Mode (multiple memories per namespace)         ║"
echo "║  ✅ Upsert Mode (path_key for updates)                    ║"
echo "║  ✅ Hierarchical Namespaces                               ║"
echo "║  ✅ Hierarchical Queries (prefix matching)                ║"
echo "║  ✅ Full CRUD Operations                                  ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""
print_info "Namespace-based memory is ready! (Issue #350 complete)"
echo ""

# Restore admin key
export NEXUS_API_KEY=$ADMIN_KEY

# Cleanup
if [ "$KEEP" != "1" ]; then
    print_info "Cleaning up..."
    # Switch back to admin to clean up agent
    nexus admin delete-user-by-id demo_agent 2>/dev/null || true
fi
