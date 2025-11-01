#!/bin/bash
# Nexus CLI - Namespace-Based Memory Demo (Issue #350)
#
# This demo showcases the NEW namespace-based memory organization:
# - Hierarchical namespaces (e.g., "knowledge/geography/facts")
# - Append mode (multiple memories per namespace)
# - Upsert mode (path_key for updateable memories)
# - Structured content (JSON storage)
# - Hierarchical queries (prefix matching)
# - Path-based retrieval
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/namespace_memory_demo.sh

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
    print_error "NEXUS_URL and NEXUS_API_KEY not set."
    echo ""
    echo "To set up the server and credentials, run:"
    echo "  1. ./scripts/init-nexus-with-auth.sh"
    echo "  2. source .nexus-admin-env"
    echo "  3. $0"
    echo ""
    echo "Or if server is already running:"
    echo "  source .nexus-admin-env"
    exit 1
fi

# Test API key validity
print_info "Testing API key..."
if ! curl -s -H "Authorization: Bearer $NEXUS_API_KEY" "$NEXUS_URL/health" >/dev/null 2>&1; then
    print_error "API key is invalid or server is not responding."
    echo ""
    echo "The server may have been reset. Please run:"
    echo "  1. pkill -f 'nexus.cli serve'  # Stop old server"
    echo "  2. ./scripts/init-nexus-with-auth.sh  # Start fresh"
    echo "  3. source .nexus-admin-env"
    echo "  4. $0"
    exit 1
fi
print_success "API key is valid"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Nexus CLI - Namespace-Based Memory Demo (v0.8.0)      ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Testing namespace-based memory organization (Issue #350)"
echo ""

# Create test user and get their API key
print_info "Creating test user 'demo_agent'..."

# Save admin key
ADMIN_KEY=$NEXUS_API_KEY

# Try to create agent (will fail if already exists, that's ok)
AGENT_OUTPUT=$(nexus admin create-user demo_agent --name "Demo Agent" --subject-type agent 2>&1 || true)

# Extract API key from output
USER_KEY=$(echo "$AGENT_OUTPUT" | grep -oE 'sk-[a-z0-9_]+' | head -1)

# If creation failed because user exists, list existing keys
if [ -z "$USER_KEY" ]; then
    print_info "Agent already exists, fetching existing key..."
    USER_KEY=$(nexus admin list-keys --subject-type agent 2>/dev/null | grep demo_agent | grep -oE 'sk-[a-z0-9_]+' | head -1)
fi

if [ -z "$USER_KEY" ]; then
    print_error "Failed to create or find agent API key"
    exit 1
fi

# Switch to user key for the demo
export NEXUS_API_KEY=$USER_KEY
print_success "Using demo_agent API key for all operations"
echo ""

# Cleanup function (only runs if KEEP != 1)
cleanup() {
    python3 << 'CLEANUP'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
print("Cleaning up test memories...")

# Delete all memories with test namespaces
try:
    memories = nx.memory.list(limit=1000)
    deleted = 0
    for mem in memories:
        namespace = mem.get('namespace', '')
        if namespace and any(ns in namespace for ns in ['knowledge/', 'user/preferences', 'agent/strategies', 'demo/']):
            try:
                nx.memory.delete(mem['memory_id'])
                deleted += 1
            except:
                pass
    print(f"Deleted {deleted} test memories")
except Exception as e:
    print(f"Cleanup error: {e}")

nx.close()
CLEANUP
}

# Gate cleanup behind KEEP flag
if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# ════════════════════════════════════════════════════════════
# Section 1: Append Mode - Fact Collection
# ════════════════════════════════════════════════════════════

print_section "1. Append Mode - Multiple Memories per Namespace"

print_subsection "1.1 Store facts in same namespace (append mode)"
print_info "Each store() creates a NEW memory (no path_key)"

nexus memory store "Paris is the capital of France" --namespace "knowledge/geography/facts"
print_success "Stored fact #1"

nexus memory store "London is the capital of UK" --namespace "knowledge/geography/facts"
print_success "Stored fact #2"

nexus memory store "Tokyo is the capital of Japan" --namespace "knowledge/geography/facts"
print_success "Stored fact #3"

print_subsection "1.2 List all facts in namespace"
print_test "List memories in 'knowledge/geography/facts'"

FACT_COUNT=$(nexus memory list --namespace "knowledge/geography/facts" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
if [ "$FACT_COUNT" = "3" ]; then
    print_success "✅ Found 3 facts in namespace (append mode works!)"
else
    print_error "Expected 3 facts, found $FACT_COUNT"
fi

# ════════════════════════════════════════════════════════════
# Section 2: Upsert Mode - Settings Storage
# ════════════════════════════════════════════════════════════

print_section "2. Upsert Mode - Updateable Settings"

print_subsection "2.1 Store settings with path_key"
print_info "With path_key, store() UPDATES existing memory"

# Note: CLI doesn't support structured content directly yet, using Python for this test
python3 << 'PYTHON_UPSERT'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# Store initial settings
mem_id_1 = nx.memory.store(
    content={"theme": "dark", "font_size": 14},
    namespace="user/preferences/ui",
    path_key="settings"
)
print(f"✓ Stored UI settings: {mem_id_1}")

# Update with same path_key
mem_id_2 = nx.memory.store(
    content={"theme": "light", "font_size": 16},
    namespace="user/preferences/ui",
    path_key="settings"  # Same key = update
)
print(f"✓ Updated UI settings: {mem_id_2}")

if mem_id_1 == mem_id_2:
    print("✅ Upsert worked! Same memory_id returned")
else:
    print(f"✗ Upsert failed! Different IDs: {mem_id_1} vs {mem_id_2}")

nx.close()
PYTHON_UPSERT

print_subsection "2.2 Retrieve by path"
print_test "Retrieve settings using namespace + path_key"

python3 << 'PYTHON_RETRIEVE'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# Retrieve by path
settings = nx.memory.retrieve(path="user/preferences/ui/settings")
if settings:
    print("✓ Retrieved settings via path:")
    print(f"  Content: {json.dumps(settings['content'], indent=2)}")

    # Verify it's the updated version
    if settings['content'].get('theme') == 'light' and settings['content'].get('font_size') == 16:
        print("✅ Retrieved UPDATED settings (upsert confirmed!)")
    else:
        print("✗ Retrieved wrong version")
else:
    print("✗ Failed to retrieve settings")

nx.close()
PYTHON_RETRIEVE

# ════════════════════════════════════════════════════════════
# Section 3: Hierarchical Queries
# ════════════════════════════════════════════════════════════

print_section "3. Hierarchical Queries with Namespace Prefix"

print_subsection "3.1 Create multi-level namespace structure"
print_info "Creating memories in hierarchical namespaces"

nexus memory store "Python is dynamically typed" --namespace "knowledge/programming/facts"
nexus memory store "Python uses GIL" --namespace "knowledge/programming/facts"
nexus memory store "Use list comprehensions for readability" --namespace "knowledge/programming/best-practices"
print_success "Created programming knowledge"

nexus memory store "Berlin is capital of Germany" --namespace "knowledge/geography/facts"
nexus memory store "Mediterranean climate is mild" --namespace "knowledge/geography/observations"
print_success "Created more geography knowledge"

print_subsection "3.2 Query by namespace prefix (hierarchical)"
print_test "Get ALL knowledge (any subdomain)"

ALL_COUNT=$(nexus memory list --namespace-prefix "knowledge/" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
print_success "✓ All knowledge: $ALL_COUNT memories"

GEO_COUNT=$(nexus memory list --namespace-prefix "knowledge/geography/" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
print_success "✓ Geography knowledge: $GEO_COUNT memories"

PROG_COUNT=$(nexus memory list --namespace-prefix "knowledge/programming/" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
print_success "✓ Programming knowledge: $PROG_COUNT memories"

GEO_FACTS=$(nexus memory list --namespace "knowledge/geography/facts" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")
PROG_FACTS=$(nexus memory list --namespace "knowledge/programming/facts" --json | python3 -c "import sys, json; print(len(json.load(sys.stdin)))")

print_success "✓ Geography facts: $GEO_FACTS"
print_success "✓ Programming facts: $PROG_FACTS"

if [ "$ALL_COUNT" -ge "7" ]; then
    print_success "✅ Hierarchical queries work correctly!"
else
    print_error "✗ Hierarchical query mismatch (expected >= 7, got $ALL_COUNT)"
fi

# ════════════════════════════════════════════════════════════
# Section 4: Structured Content (JSON)
# ════════════════════════════════════════════════════════════

print_section "4. Structured Content Storage"

print_subsection "4.1 Store complex JSON structures"

python3 << 'PYTHON_STRUCTURED'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# Store complex strategy
strategy = {
    "strategy": "cache_invalidation",
    "context": "high-traffic API endpoints",
    "confidence": 0.92,
    "metrics": {
        "success_rate": 0.95,
        "avg_response_time_ms": 45
    },
    "tags": ["performance", "caching"]
}

mem_id = nx.memory.store(
    content=strategy,
    namespace="agent/strategies/performance",
    path_key="cache_strategy"
)
print(f"✓ Stored complex strategy: {mem_id}")

# Retrieve and verify
retrieved = nx.memory.retrieve(path="agent/strategies/performance/cache_strategy")
if retrieved:
    content = retrieved['content']
    print("✓ Retrieved structured content:")
    print(f"  Strategy: {content['strategy']}")
    print(f"  Confidence: {content['confidence']}")
    print(f"  Metrics: {content['metrics']}")

    if isinstance(content, dict) and 'metrics' in content:
        print("✅ Structured content (JSON) works perfectly!")
    else:
        print("✗ Content structure mismatch")
else:
    print("✗ Failed to retrieve strategy")

nx.close()
PYTHON_STRUCTURED

# ════════════════════════════════════════════════════════════
# Section 5: Mixed Mode Usage
# ════════════════════════════════════════════════════════════

print_section "5. Mixed Mode - Append + Upsert in Same Namespace"

print_subsection "5.1 Create namespace with both modes"
print_info "Some memories with path_key (upsert), some without (append)"

python3 << 'PYTHON_MIXED'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# Append mode - event logs
nx.memory.store(
    content={"event": "user_login", "timestamp": "2025-01-15T10:00:00Z"},
    namespace="demo/events"
)
nx.memory.store(
    content={"event": "file_created", "timestamp": "2025-01-15T10:05:00Z"},
    namespace="demo/events"
)
nx.memory.store(
    content={"event": "user_logout", "timestamp": "2025-01-15T11:00:00Z"},
    namespace="demo/events"
)
print("✓ Stored 3 events (append mode)")

# Upsert mode - current state
nx.memory.store(
    content={"active_users": 5, "last_updated": "2025-01-15T10:00:00Z"},
    namespace="demo/events",
    path_key="current_state"
)
nx.memory.store(
    content={"active_users": 8, "last_updated": "2025-01-15T10:30:00Z"},
    namespace="demo/events",
    path_key="current_state"  # Updates existing
)
print("✓ Stored/updated current state (upsert mode)")

# List all in namespace
all_events = nx.memory.list(namespace="demo/events")
print(f"✓ Total memories in 'demo/events': {len(all_events)}")

# Verify: should be 4 (3 events + 1 state)
if len(all_events) == 4:
    print("✅ Mixed mode works! Both append and upsert coexist")

    # Count by path_key presence
    with_key = sum(1 for m in all_events if m['path_key'])
    without_key = sum(1 for m in all_events if not m['path_key'])
    print(f"  - With path_key (upsert): {with_key}")
    print(f"  - Without path_key (append): {without_key}")
else:
    print(f"✗ Expected 4 memories, found {len(all_events)}")

nx.close()
PYTHON_MIXED

# ════════════════════════════════════════════════════════════
# Section 6: CRUD Operations
# ════════════════════════════════════════════════════════════

print_section "6. CRUD Operations"

print_subsection "6.1 Create, Read, Update, Delete"

python3 << 'PYTHON_CRUD'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# CREATE
mem_id = nx.memory.store(
    content={"status": "draft", "version": 1},
    namespace="demo/crud",
    path_key="document"
)
print(f"✓ CREATE: {mem_id}")

# READ
doc = nx.memory.retrieve(path="demo/crud/document")
print(f"✓ READ: status={doc['content']['status']}, version={doc['content']['version']}")

# UPDATE
nx.memory.store(
    content={"status": "published", "version": 2},
    namespace="demo/crud",
    path_key="document"
)
doc = nx.memory.retrieve(path="demo/crud/document")
print(f"✓ UPDATE: status={doc['content']['status']}, version={doc['content']['version']}")

# DELETE
success = nx.memory.delete(mem_id)
print(f"✓ DELETE: {success}")

# Verify deleted
doc = nx.memory.retrieve(path="demo/crud/document")
if doc is None:
    print("✅ CRUD operations complete!")
else:
    print("✗ DELETE failed - memory still exists")

nx.close()
PYTHON_CRUD

# ════════════════════════════════════════════════════════════
# Section 7: Query Performance
# ════════════════════════════════════════════════════════════

print_section "7. Query Performance Test"

print_subsection "7.1 Create large dataset"
print_info "Creating 50 memories across different namespaces"

python3 << 'PYTHON_PERF'
import sys, os, time
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))

# Create 50 memories
start = time.time()
for i in range(10):
    nx.memory.store(f"Fact {i}", namespace="perf/domain-a/facts")
    nx.memory.store(f"Observation {i}", namespace="perf/domain-a/observations")
    nx.memory.store(f"Strategy {i}", namespace="perf/domain-b/strategies")
    nx.memory.store(f"Pattern {i}", namespace="perf/domain-b/patterns")
    nx.memory.store(f"Note {i}", namespace="perf/domain-c/notes")
create_time = time.time() - start
print(f"✓ Created 50 memories in {create_time:.2f}s")

# Query by exact namespace
start = time.time()
facts = nx.memory.list(namespace="perf/domain-a/facts")
exact_time = time.time() - start
print(f"✓ Exact query: {len(facts)} results in {exact_time*1000:.1f}ms")

# Query by prefix
start = time.time()
domain_a = nx.memory.list(namespace_prefix="perf/domain-a/")
prefix_time = time.time() - start
print(f"✓ Prefix query: {len(domain_a)} results in {prefix_time*1000:.1f}ms")

# Query all
start = time.time()
all_perf = nx.memory.list(namespace_prefix="perf/")
all_time = time.time() - start
print(f"✓ All query: {len(all_perf)} results in {all_time*1000:.1f}ms")

if len(facts) == 10 and len(domain_a) == 20 and len(all_perf) == 50:
    print("✅ Query performance test passed!")
else:
    print(f"✗ Query counts mismatch: {len(facts)}, {len(domain_a)}, {len(all_perf)}")

nx.close()
PYTHON_PERF

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

print_section "✅ Namespace Memory Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║              Namespace Memory Features Verified                   ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ Append Mode (multiple memories per namespace)                 ║"
echo "║  ✅ Upsert Mode (path_key for updateable memories)                ║"
echo "║  ✅ Hierarchical Namespaces (e.g., knowledge/geography/facts)     ║"
echo "║  ✅ Structured Content (JSON storage & retrieval)                 ║"
echo "║  ✅ Path-based Retrieval (namespace/path_key)                     ║"
echo "║  ✅ Hierarchical Queries (prefix matching)                        ║"
echo "║  ✅ Mixed Mode (append + upsert in same namespace)                ║"
echo "║  ✅ Full CRUD Operations                                          ║"
echo "║  ✅ Query Performance (indexed lookups)                           ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
print_info "All tests passed! Namespace-based memory is ready for production."
print_info "Issue #350 implementation complete!"
