#!/bin/bash
# Demo: Phase 2 Integration - Memory Paths with CLI (v0.4.0)
#
# This demo shows how memory virtual paths work with CLI commands.
# Users can use standard nexus commands (cat/write/ls/rm) with memory paths!

set -e

# Setup test environment
TEST_DIR=$(mktemp -d)
export NEXUS_DATA_DIR=$TEST_DIR
export NEXUS_TENANT_ID=acme
export NEXUS_USER_ID=alice
export NEXUS_AGENT_ID=agent1

echo "======================================================================"
echo "Phase 2 Integration: Memory Paths with CLI (v0.4.0)"
echo "======================================================================"
echo ""
echo "Test directory: $TEST_DIR"

# Initialize
nexus init > /dev/null 2>&1

# ==========================================================================
# DEMO 1: Order-Neutral Paths with CLI
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 1: Order-Neutral Paths with CLI"
echo "──────────────────────────────────────────────────────────────────────"
echo ""
echo "Concept: Multiple path orders access the SAME memory!"
echo ""

# Store via one path
echo "1. Store via File API:"
nexus write "/workspace/alice/agent1/memory/facts" "Python is great!" 2>/dev/null
echo "   ✓ nexus write '/workspace/alice/agent1/memory/facts' 'Python is great!'"

# Read via different paths
echo ""
echo "2. Read via different path orders (all return same content):"

PATHS=(
    "/workspace/alice/agent1/memory/facts"
    "/workspace/agent1/alice/memory/facts"
    "/memory/by-user/alice/facts"
    "/memory/by-agent/agent1/facts"
)

for path in "${PATHS[@]}"; do
    CONTENT=$(nexus cat "$path" 2>/dev/null || echo "Error")
    printf "   %-50s → %s\n" "$path" "$CONTENT"
done

# ==========================================================================
# DEMO 2: File API vs Memory API - Two Ways, Same Result
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 2: File API vs Memory API - Two Ways, Same Result"
echo "──────────────────────────────────────────────────────────────────────"

# Method 1: Memory API
echo ""
echo "Method 1: Memory API (traditional)"
MEM_ID=$(nexus memory store "Machine learning is awesome!" --scope user 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
echo "   ✓ nexus memory store → memory_id: $MEM_ID"
CONTENT=$(nexus memory get "$MEM_ID" 2>/dev/null | grep "content:" | cut -d: -f2- | xargs)
echo "   ✓ nexus memory get   → content: $CONTENT"

# Method 2: File API
echo ""
echo "Method 2: File API (Phase 2 Integration)"
nexus write "/workspace/alice/agent1/memory/preferences" "I love Python!" 2>/dev/null
echo "   ✓ nexus write '/workspace/alice/agent1/memory/preferences' 'I love Python!'"
CONTENT=$(nexus cat "/workspace/alice/agent1/memory/preferences" 2>/dev/null)
echo "   ✓ nexus cat   → content: $CONTENT"

echo ""
echo "💡 Both methods store memories in the same system!"

# ==========================================================================
# DEMO 3: Directory Listing for Memories
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 3: Directory Listing for Memories"
echo "──────────────────────────────────────────────────────────────────────"

echo ""
echo "✓ nexus ls '/workspace/alice/agent1/memory' output:"
nexus ls "/workspace/alice/agent1/memory" 2>/dev/null | head -5 | while read -r line; do
    echo "  • $line"
done

# ==========================================================================
# DEMO 4: CRUD Operations with CLI
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 4: CRUD Operations with CLI"
echo "──────────────────────────────────────────────────────────────────────"

# Create
echo ""
echo "1. Create:"
nexus write "/workspace/alice/agent1/memory/todo" "Buy groceries" 2>/dev/null
echo "   ✓ nexus write '/workspace/alice/agent1/memory/todo' 'Buy groceries'"

# Read
echo ""
echo "2. Read:"
CONTENT=$(nexus cat "/workspace/alice/agent1/memory/todo" 2>/dev/null)
echo "   ✓ nexus cat '/workspace/alice/agent1/memory/todo'"
echo "     Content: $CONTENT"

# Update
echo ""
echo "3. Update:"
nexus write "/workspace/alice/agent1/memory/todo" "Buy groceries and cook dinner" 2>/dev/null
CONTENT=$(nexus cat "/workspace/alice/agent1/memory/todo" 2>/dev/null)
echo "   ✓ nexus write (updated) → Content: $CONTENT"

# Delete (via Memory API to get ID)
echo ""
echo "4. Delete:"
MEM_ID=$(nexus memory query --user-id alice --scope user 2>/dev/null | grep "memory_id:" | head -1 | cut -d: -f2 | xargs)
if [ -n "$MEM_ID" ]; then
    nexus rm "/objs/memory/$MEM_ID" 2>/dev/null
    echo "   ✓ nexus rm '/objs/memory/$MEM_ID'"
fi

# ==========================================================================
# DEMO 5: Canonical Paths (Direct Access)
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 5: Canonical Paths (Direct Access)"
echo "──────────────────────────────────────────────────────────────────────"

echo ""
echo "Storing memory via Memory API to get canonical path:"
MEM_ID=$(nexus memory store "Deep learning breakthrough!" --scope user 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
echo "   ✓ Memory ID: $MEM_ID"

echo ""
echo "Reading via canonical path:"
CANONICAL="/objs/memory/$MEM_ID"
CONTENT=$(nexus cat "$CANONICAL" 2>/dev/null)
echo "   ✓ nexus cat '$CANONICAL'"
echo "     Content: $CONTENT"

# ==========================================================================
# DEMO 6: Mixing CLI Commands
# ==========================================================================
echo ""
echo "──────────────────────────────────────────────────────────────────────"
echo "DEMO 6: Mixing CLI Commands"
echo "──────────────────────────────────────────────────────────────────────"

echo ""
echo "1. Store via File API (nexus write):"
nexus write "/workspace/alice/agent1/memory/research" "Transformers paper" 2>/dev/null
echo "   ✓ nexus write '/workspace/alice/agent1/memory/research' 'Transformers paper'"

echo ""
echo "2. Query via Memory API (nexus memory query):"
COUNT=$(nexus memory query --user-id alice --scope user 2>/dev/null | grep -c "memory_id:")
echo "   ✓ nexus memory query --user-id alice → $COUNT memories"

echo ""
echo "3. Read via File API (nexus cat):"
CONTENT=$(nexus cat "/workspace/alice/agent1/memory/research" 2>/dev/null)
echo "   ✓ nexus cat '/workspace/alice/agent1/memory/research'"
echo "     → $CONTENT"

echo ""
echo "💡 Mix and match CLI commands - they all work together!"

# ==========================================================================
# Summary
# ==========================================================================
echo ""
echo "======================================================================"
echo "Summary: Phase 2 Integration Benefits"
echo "======================================================================"
cat <<EOF

✓ Order-Neutral Paths: Any ID order works
  nexus cat /workspace/alice/agent1/memory
  nexus cat /workspace/agent1/alice/memory  # Same result!

✓ Two APIs, One System: Choose your interface
  - Memory API: nexus memory store/get/query
  - File API: nexus cat/write/ls/rm

✓ Virtual Paths: Multiple views of same memory
  - /objs/memory/{id} (canonical)
  - /workspace/{user}/{agent}/memory (workspace view)
  - /memory/by-user/{user} (user-centric)
  - /memory/by-agent/{agent} (agent-centric)

✓ Standard Commands: Use familiar file operations
  - nexus cat <memory-path>
  - nexus write <memory-path> <content>
  - nexus ls <memory-path>
  - nexus rm <memory-path>

✓ Forward Compatible: Ready for issue #121 workspace structure

EOF

# Cleanup
rm -rf "$TEST_DIR"
echo "✓ Cleaned up test directory: $TEST_DIR"
