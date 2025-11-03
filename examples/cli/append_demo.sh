#!/bin/bash
# Nexus CLI - Append Operation Demo
#
# This demo showcases the append() operation for building files incrementally:
# - Creating and appending to log files
# - Building JSONL (JSON Lines) files incrementally
# - Version tracking with append operations
# - Optimistic concurrency control
# - Performance comparison vs read-modify-write
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/append_demo.sh

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
print_demo() { echo -e "${CYAN}→${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set. Run: source .nexus-admin-env"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Nexus CLI - Append Operation Demo                ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "Demonstrating efficient file append operations"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"
export DEMO_BASE="/workspace/append-demo"

# Cleanup function (only runs if KEEP != 1)
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true
    rm -f /tmp/append-demo-*.txt /tmp/append-demo-*.json
}

# Gate cleanup behind KEEP flag for post-mortem inspection
if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# Clean up any stale data
print_info "Cleaning up stale test data..."
nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true

# Create demo directory
nexus mkdir $DEMO_BASE --parents
print_success "Created demo workspace: $DEMO_BASE"

# Create a regular user (not admin)
print_info "Creating regular user 'alice' for the demo..."
ALICE_KEY=$(python3 scripts/create-api-key.py alice "Alice Developer" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')
print_success "Created user: alice"

# Grant alice editor access to demo workspace
nexus rebac create user alice direct_editor file $DEMO_BASE
print_success "Granted alice editor access to $DEMO_BASE"

# Switch to alice's context
export NEXUS_API_KEY="$ALICE_KEY"
print_info "Switched to alice's context"
echo ""

# ════════════════════════════════════════════════════════════
# Section 1: Basic Append - Building a Log File
# ════════════════════════════════════════════════════════════

print_section "1. Basic Append - Building a Log File"

print_subsection "1.1 Create initial log entry"
print_demo "Appending first log entry..."
nexus append $DEMO_BASE/application.log "2025-01-01 10:00:00 [INFO] Application started"
print_success "Created log file with first entry"

print_demo "Reading log file..."
nexus cat $DEMO_BASE/application.log
echo ""

print_subsection "1.2 Append additional log entries"
print_info "Appending more log entries (simulating application activity)..."

nexus append $DEMO_BASE/application.log "
2025-01-01 10:00:15 [INFO] Database connection established"

nexus append $DEMO_BASE/application.log "
2025-01-01 10:00:30 [INFO] User 'alice' logged in"

nexus append $DEMO_BASE/application.log "
2025-01-01 10:01:00 [INFO] Processing request: GET /api/users"

nexus append $DEMO_BASE/application.log "
2025-01-01 10:01:05 [INFO] Request completed: 200 OK"

print_success "Appended 4 additional log entries"

print_demo "Final log file:"
nexus cat $DEMO_BASE/application.log
echo ""

print_subsection "1.3 Check version history"
print_info "Each append creates a new version..."
python3 << 'PYTHON_VERSIONS'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

versions = nx.list_versions(f"{base}/application.log")
print(f"  Total versions: {len(versions)}")
print(f"  Latest version: {versions[0]['version']}")
print(f"  File size: {versions[0]['size']} bytes")

nx.close()
PYTHON_VERSIONS

# ════════════════════════════════════════════════════════════
# Section 2: JSONL (JSON Lines) - Event Streaming
# ════════════════════════════════════════════════════════════

print_section "2. JSONL (JSON Lines) - Event Streaming"

print_subsection "2.1 Build event log incrementally"
print_info "Creating JSONL file by appending JSON events one by one..."

# Event 1: Login
echo '{"timestamp": "2025-01-01T10:00:00Z", "event": "user_login", "user": "alice", "ip": "192.168.1.100"}' | \
    nexus append $DEMO_BASE/events.jsonl --input -
print_demo "Event 1: user_login"

# Event 2: File upload
echo '{"timestamp": "2025-01-01T10:01:00Z", "event": "file_upload", "user": "alice", "file": "report.pdf", "size": 2048576}' | \
    nexus append $DEMO_BASE/events.jsonl --input -
print_demo "Event 2: file_upload"

# Event 3: API call
echo '{"timestamp": "2025-01-01T10:02:00Z", "event": "api_call", "user": "alice", "endpoint": "/api/data", "method": "GET"}' | \
    nexus append $DEMO_BASE/events.jsonl --input -
print_demo "Event 3: api_call"

# Event 4: File download
echo '{"timestamp": "2025-01-01T10:03:00Z", "event": "file_download", "user": "alice", "file": "report.pdf", "size": 2048576}' | \
    nexus append $DEMO_BASE/events.jsonl --input -
print_demo "Event 4: file_download"

# Event 5: Logout
echo '{"timestamp": "2025-01-01T10:05:00Z", "event": "user_logout", "user": "alice", "session_duration": 300}' | \
    nexus append $DEMO_BASE/events.jsonl --input -
print_demo "Event 5: user_logout"

print_success "Built JSONL file with 5 events"

print_subsection "2.2 Parse and display JSONL events"
print_info "Reading back JSONL file and parsing events..."
python3 << 'PYTHON_JSONL'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

content = nx.read(f"{base}/events.jsonl").decode('utf-8')
events = [json.loads(line) for line in content.strip().split('\n')]

print(f"\n  Parsed {len(events)} events:\n")
for i, event in enumerate(events, 1):
    print(f"  {i}. {event['timestamp']} - {event['event']}")
    for key, value in event.items():
        if key not in ['timestamp', 'event']:
            print(f"     {key}: {value}")

nx.close()
PYTHON_JSONL

# ════════════════════════════════════════════════════════════
# Section 3: Append with Optimistic Concurrency Control
# ════════════════════════════════════════════════════════════

print_section "3. Optimistic Concurrency Control"

print_subsection "3.1 Get current file state with metadata"
print_info "Creating a shared file that might be modified concurrently..."
nexus write $DEMO_BASE/counter.txt "Count: 0" --show-metadata

print_subsection "3.2 Safe append with ETag verification"
print_info "Getting current ETag before append..."

ETAG=$(python3 << 'PYTHON_ETAG'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

result = nx.read(f"{base}/counter.txt", return_metadata=True)
print(result['etag'])

nx.close()
PYTHON_ETAG
)

print_demo "Current ETag: ${ETAG:0:16}..."

print_info "Attempting append with correct ETag..."
if nexus append $DEMO_BASE/counter.txt "
Count: 1" --if-match "$ETAG" --show-metadata 2>/dev/null; then
    print_success "Append succeeded with correct ETag"
else
    print_error "Append failed (unexpected)"
fi

print_subsection "3.3 Demonstrate conflict detection"
print_info "Attempting append with WRONG ETag (simulating concurrent modification)..."
if nexus append $DEMO_BASE/counter.txt "
Count: 2" --if-match "wrong_etag_12345" 2>/dev/null; then
    print_error "Append succeeded with wrong ETag (BUG!)"
else
    print_success "Append correctly rejected - conflict detected!"
fi

print_subsection "3.4 Conflict Resolution Strategy - Retry Pattern"
print_info "Demonstrating how to resolve conflicts with automatic retry..."

python3 << 'PYTHON_RETRY'
import sys, os
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS
from nexus.core.exceptions import ConflictError

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

# Retry pattern for safe concurrent appends
max_retries = 3
content_to_append = b"\nCount: 2"

for attempt in range(max_retries):
    try:
        print(f"  Attempt {attempt + 1}: Reading current state and appending...")

        # Read current file with metadata
        result = nx.read(f"{base}/counter.txt", return_metadata=True)
        current_etag = result['etag']

        # Attempt append with ETag check
        nx.append(
            f"{base}/counter.txt",
            content_to_append,
            if_match=current_etag
        )

        print(f"  ✓ Append succeeded on attempt {attempt + 1}")
        break

    except ConflictError as e:
        print(f"  ⚠ Conflict on attempt {attempt + 1}: File was modified")
        if attempt < max_retries - 1:
            print(f"  → Retrying...")
        else:
            print(f"  ✗ Max retries reached, giving up")
            raise

nx.close()
PYTHON_RETRY

print_success "✅ Conflict resolved with retry pattern"

print_subsection "3.5 When to Use --if-match vs Simple Append"
echo ""
print_info "Use Cases:"
echo ""
echo "  1. Simple Append (NO --if-match):"
echo "     • Log files where order doesn't matter"
echo "     • JSONL event streams (append-only)"
echo "     • Metrics collection"
echo "     → Just append, no conflict checking needed"
echo ""
echo "  2. Safe Append (WITH --if-match + retry):"
echo "     • Counter files where you read-modify-write"
echo "     • Appending based on current content"
echo "     • Critical data where conflicts matter"
echo "     → Use ETag + retry pattern shown above"
echo ""
print_success "For most log/JSONL use cases, simple append is sufficient!"

# ════════════════════════════════════════════════════════════
# Section 4: Performance Pattern - Append vs Read-Modify-Write
# ════════════════════════════════════════════════════════════

print_section "4. Performance Pattern Comparison"

print_subsection "4.1 Pattern A: Using append() - One operation"
print_info "Building a data collection file with append..."

cat > /tmp/append-demo-data.txt << 'EOF'
Data point 1
Data point 2
Data point 3
EOF

START_TIME=$(date +%s%N)
for i in {1..5}; do
    echo "Sample data line $i" | nexus append $DEMO_BASE/data-append.txt --input - 2>/dev/null
done
END_TIME=$(date +%s%N)
APPEND_TIME=$(( (END_TIME - START_TIME) / 1000000 ))

print_success "Pattern A (append): Completed in ${APPEND_TIME}ms"

print_subsection "4.2 Pattern B: Manual read-modify-write - Three operations"
print_info "Building a data collection file with read + modify + write..."

START_TIME=$(date +%s%N)
for i in {1..5}; do
    # Read existing content (or empty if doesn't exist)
    CONTENT=$(nexus cat $DEMO_BASE/data-manual.txt 2>/dev/null || echo "")
    # Append in memory
    NEW_CONTENT="${CONTENT}Sample data line $i
"
    # Write back
    echo -n "$NEW_CONTENT" | nexus write $DEMO_BASE/data-manual.txt --input - 2>/dev/null
done
END_TIME=$(date +%s%N)
MANUAL_TIME=$(( (END_TIME - START_TIME) / 1000000 ))

print_success "Pattern B (manual): Completed in ${MANUAL_TIME}ms"

print_subsection "4.3 Results"
print_info "Comparison:"
echo "  Pattern A (append):        ${APPEND_TIME}ms - ✓ Simple, one-line"
echo "  Pattern B (read+modify+write): ${MANUAL_TIME}ms - ✗ Complex, error-prone"
echo ""
print_success "append() provides cleaner, more maintainable code"

# ════════════════════════════════════════════════════════════
# Section 5: Real-World Use Case - Application Metrics
# ════════════════════════════════════════════════════════════

print_section "5. Real-World Use Case - Application Metrics"

print_subsection "5.1 Collect metrics over time"
print_info "Simulating an application collecting performance metrics..."

python3 << 'PYTHON_METRICS'
import sys, os, json, time, random
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

# Simulate 10 metric collections
for i in range(10):
    metric = {
        "timestamp": f"2025-01-01T10:{i:02d}:00Z",
        "cpu_percent": round(random.uniform(20, 80), 2),
        "memory_mb": random.randint(512, 2048),
        "requests_per_sec": random.randint(10, 100),
        "response_time_ms": round(random.uniform(50, 300), 2)
    }

    line = json.dumps(metric) + "\n"
    nx.append(f"{base}/metrics.jsonl", line.encode('utf-8'))

    if (i + 1) % 5 == 0:
        print(f"  Collected {i + 1} metrics...")

print(f"  ✓ Collected 10 metrics successfully")

nx.close()
PYTHON_METRICS

print_subsection "5.2 Analyze collected metrics"
print_info "Computing statistics from metrics..."

python3 << 'PYTHON_ANALYZE'
import sys, os, json
sys.path.insert(0, 'src')
from nexus.remote.client import RemoteNexusFS

nx = RemoteNexusFS(os.getenv('NEXUS_URL'), api_key=os.getenv('NEXUS_API_KEY'))
base = os.getenv('DEMO_BASE')

content = nx.read(f"{base}/metrics.jsonl").decode('utf-8')
metrics = [json.loads(line) for line in content.strip().split('\n')]

avg_cpu = sum(m['cpu_percent'] for m in metrics) / len(metrics)
avg_memory = sum(m['memory_mb'] for m in metrics) / len(metrics)
avg_rps = sum(m['requests_per_sec'] for m in metrics) / len(metrics)
avg_response = sum(m['response_time_ms'] for m in metrics) / len(metrics)

print(f"\n  Metrics Summary (10 samples):")
print(f"  ────────────────────────────────")
print(f"  Avg CPU:          {avg_cpu:.2f}%")
print(f"  Avg Memory:       {avg_memory:.0f} MB")
print(f"  Avg Requests/sec: {avg_rps:.0f}")
print(f"  Avg Response:     {avg_response:.2f} ms")

nx.close()
PYTHON_ANALYZE

# ════════════════════════════════════════════════════════════
# Section 6: Permission Enforcement - Different Access Levels
# ════════════════════════════════════════════════════════════

print_section "6. Permission Enforcement on Append Operations"

# Switch back to admin to set up test users
export NEXUS_API_KEY="$ADMIN_KEY"

print_subsection "6.1 Create test users with different permission levels"
print_info "Creating test file and users..."

# Create a shared file that we'll test permissions on
echo "Shared log content" | nexus write $DEMO_BASE/shared.log - 2>/dev/null
print_success "Created shared.log"

# Create bob (viewer) and charlie (no permissions)
BOB_KEY=$(python3 scripts/create-api-key.py bob "Bob Viewer" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')
CHARLIE_KEY=$(python3 scripts/create-api-key.py charlie "Charlie NoAccess" --days 1 2>/dev/null | grep "API Key:" | awk '{print $3}')

# Grant bob viewer (read-only) access to shared.log
nexus rebac create user bob direct_viewer file $DEMO_BASE/shared.log
print_success "Bob: granted direct_viewer (read-only) on shared.log"

# Alice already has direct_editor on $DEMO_BASE (can read+write)
print_info "Alice: has direct_editor on $DEMO_BASE (can read+write)"

# Charlie has NO permissions
print_info "Charlie: has NO permissions on shared.log"

print_subsection "6.2 Test: Viewer (read-only) cannot append"
export NEXUS_API_KEY="$BOB_KEY"
print_demo "Bob (viewer) attempting to append..."
if echo "Bob's line" | nexus append $DEMO_BASE/shared.log --input - 2>/dev/null; then
    print_error "❌ BUG: Viewer was able to append (should be denied)!"
else
    print_success "✅ Viewer correctly denied append (read-only access)"
fi

print_subsection "6.3 Test: Editor can append"
export NEXUS_API_KEY="$ALICE_KEY"
print_demo "Alice (editor) attempting to append..."
if echo "Alice's line" | nexus append $DEMO_BASE/shared.log --input - 2>/dev/null; then
    print_success "✅ Editor can append (as expected)"
else
    print_error "❌ BUG: Editor was denied append!"
fi

print_subsection "6.4 Test: User with no permissions cannot append"
export NEXUS_API_KEY="$CHARLIE_KEY"
print_demo "Charlie (no permissions) attempting to append..."
if echo "Charlie's line" | nexus append $DEMO_BASE/shared.log --input - 2>/dev/null; then
    print_error "❌ BUG: User with no permissions was able to append!"
else
    print_success "✅ User with no permissions correctly denied"
fi

print_subsection "6.5 Test: Editor can create new files in permitted directory"
export NEXUS_API_KEY="$ALICE_KEY"
print_demo "Alice (editor on $DEMO_BASE) creating new file..."
if nexus append $DEMO_BASE/alice-new.log "Alice created this" 2>/dev/null; then
    print_success "✅ Editor can create new files in permitted directory"
else
    print_error "❌ BUG: Editor couldn't create new file!"
fi

print_subsection "6.6 Verify final content (only alice's line should be present)"
export NEXUS_API_KEY="$ADMIN_KEY"
CONTENT=$(nexus cat $DEMO_BASE/shared.log 2>/dev/null)
if echo "$CONTENT" | grep -q "Alice's line" && ! echo "$CONTENT" | grep -q "Bob's line" && ! echo "$CONTENT" | grep -q "Charlie's line"; then
    print_success "✅ Only authorized appends were written"
else
    print_warning "Unexpected content in shared.log: $CONTENT"
fi

# ════════════════════════════════════════════════════════════
# Section 7: Summary and Best Practices
# ════════════════════════════════════════════════════════════

print_section "✅ Append Operation Demo Complete!"

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║                    Key Capabilities Demonstrated                  ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ Incremental log file building                                 ║"
echo "║  ✅ JSONL (JSON Lines) event streaming                            ║"
echo "║  ✅ Version tracking on each append                               ║"
echo "║  ✅ Optimistic concurrency control (ETag)                         ║"
echo "║  ✅ Performance comparison vs manual pattern                      ║"
echo "║  ✅ Real-world metrics collection use case                        ║"
echo "║  ✅ Permission enforcement (viewer/editor/no-access)              ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""

print_info "Best Practices for append():"
echo ""
echo "  1. ✓ Use for log files - Natural fit for append-only logs"
echo "  2. ✓ Use for JSONL - Perfect for event streaming"
echo "  3. ✓ Use --if-match - Add ETag for safe concurrent appends"
echo "  4. ✓ Monitor versions - Track file growth over time"
echo "  5. ⚠ Consider rotation - For very large files, rotate logs"
echo ""

print_success "Demo files created in: $DEMO_BASE"
if [ "$KEEP" = "1" ]; then
    echo ""
    print_info "Files preserved for inspection:"
    echo "  - $DEMO_BASE/application.log (incremental log)"
    echo "  - $DEMO_BASE/events.jsonl (JSON Lines events)"
    echo "  - $DEMO_BASE/metrics.jsonl (application metrics)"
    echo "  - $DEMO_BASE/counter.txt (OCC example)"
fi
echo ""
