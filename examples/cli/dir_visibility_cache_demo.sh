#!/bin/bash
# Issue #919: Directory Visibility Cache Performance Demo
#
# This demo verifies the DirectoryVisibilityCache implementation by:
# 1. Creating a test directory with many files
# 2. Granting a user access to some files
# 3. Running list operations and measuring cold vs warm cache performance
# 4. Displaying cache metrics and hit rates
#
# Prerequisites:
# 1. Server running: ./scripts/init-nexus-with-auth.sh
# 2. Load admin credentials: source .nexus-admin-env
#
# Usage:
#   ./examples/cli/dir_visibility_cache_demo.sh
#
# Expected result: 1000x+ speedup on second list operation (cache HIT)

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

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_info() { echo -e "${BLUE}ℹ${NC} $1"; }
print_warning() { echo -e "${YELLOW}⚠${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_cache() { echo -e "${CYAN}CACHE:${NC} $1"; }
print_perf() { echo -e "${MAGENTA}PERF:${NC} $1"; }

# Check prerequisites
if [ -z "$NEXUS_URL" ] || [ -z "$NEXUS_API_KEY" ]; then
    print_error "NEXUS_URL and NEXUS_API_KEY not set."
    print_info "Run: source .nexus-admin-env"
    print_info "Or start server: ./scripts/init-nexus-with-auth.sh"
    exit 1
fi

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Issue #919: Directory Visibility Cache Performance     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
print_info "Server: $NEXUS_URL"
print_info "This test verifies O(1) directory visibility lookups"
echo ""

ADMIN_KEY="$NEXUS_API_KEY"
export DEMO_BASE="/workspace/dir-visibility-demo"
export NUM_FILES=50

# Cleanup function
cleanup() {
    export NEXUS_API_KEY="$ADMIN_KEY"
    nexus rmdir -r -f $DEMO_BASE 2>/dev/null || true
}

if [ "$KEEP" != "1" ]; then
    trap cleanup EXIT
    print_info "Cleanup enabled. To keep demo data, run: KEEP=1 $0"
else
    print_info "KEEP=1 set - demo data will NOT be cleaned up"
fi

# ════════════════════════════════════════════════════════════
# Run the complete test in Python for reliability
# ════════════════════════════════════════════════════════════

python3 << 'PYTHON_TEST'
import os
import sys
import time

sys.path.insert(0, 'src')

from nexus.remote.client import RemoteNexusFS

# Configuration
NEXUS_URL = os.getenv('NEXUS_URL', 'http://localhost:2026')
NEXUS_API_KEY = os.getenv('NEXUS_API_KEY')
DEMO_BASE = os.getenv('DEMO_BASE', '/workspace/dir-visibility-demo')
NUM_FILES = int(os.getenv('NUM_FILES', '50'))

# Colors
GREEN = '\033[0;32m'
BLUE = '\033[0;34m'
YELLOW = '\033[1;33m'
RED = '\033[0;31m'
CYAN = '\033[0;36m'
MAGENTA = '\033[0;35m'
NC = '\033[0m'

def print_section(title):
    print(f"\n{'═' * 60}")
    print(f"  {title}")
    print(f"{'═' * 60}\n")

def print_success(msg): print(f"{GREEN}✓{NC} {msg}")
def print_info(msg): print(f"{BLUE}ℹ{NC} {msg}")
def print_warning(msg): print(f"{YELLOW}⚠{NC} {msg}")
def print_error(msg): print(f"{RED}✗{NC} {msg}")
def print_cache(msg): print(f"{CYAN}CACHE:{NC} {msg}")
def print_perf(msg): print(f"{MAGENTA}PERF:{NC} {msg}")

# ═══════════════════════════════════════════════════════════
# Section 1: Setup
# ═══════════════════════════════════════════════════════════

print_section("1. Setup Test Environment")

nx = RemoteNexusFS(NEXUS_URL, api_key=NEXUS_API_KEY)

# Cleanup old data
print_info("Cleaning up old test data...")
try:
    nx.rmdir(DEMO_BASE, recursive=True, force=True)
except:
    pass

# Delete old tuples for testuser
old_tuples = nx.rebac_list_tuples(subject=("user", "testuser"))
for t in old_tuples:
    try:
        nx.rebac_delete(t['tuple_id'])
    except:
        pass
print_success(f"Cleaned up {len(old_tuples)} old tuples")

# ═══════════════════════════════════════════════════════════
# Section 2: Create Files
# ═══════════════════════════════════════════════════════════

print_section(f"2. Creating {NUM_FILES} Files")

# Create directory structure
nx.mkdir(DEMO_BASE, parents=True)
nx.mkdir(f"{DEMO_BASE}/documents", parents=True)
nx.mkdir(f"{DEMO_BASE}/private", parents=True)

# Create files in /documents (will be partially shared)
for i in range(NUM_FILES):
    path = f"{DEMO_BASE}/documents/file_{i:03d}.txt"
    nx.write(path, f"Content of file {i}".encode())
    if (i + 1) % 10 == 0:
        print_info(f"Created {i + 1}/{NUM_FILES} files...")

print_success(f"Created {NUM_FILES} files in {DEMO_BASE}/documents/")

# Create some private files (not shared)
for i in range(10):
    path = f"{DEMO_BASE}/private/secret_{i}.txt"
    nx.write(path, f"Secret content {i}".encode())

print_success("Created 10 private files in {DEMO_BASE}/private/")

# ═══════════════════════════════════════════════════════════
# Section 3: Grant Permissions
# ═══════════════════════════════════════════════════════════

print_section("3. Granting Permissions to testuser")

# Grant testuser access to every other file in /documents
shared_count = 0
for i in range(0, NUM_FILES, 2):  # Every other file
    path = f"{DEMO_BASE}/documents/file_{i:03d}.txt"
    nx.rebac_create(
        subject=("user", "testuser"),
        relation="direct_viewer",
        object=("file", path),
        tenant_id="default"
    )
    shared_count += 1

print_success(f"Granted testuser access to {shared_count} files")
print_info("testuser has NO access to /private/ directory")

# ═══════════════════════════════════════════════════════════
# Section 4: Create testuser API key and switch context
# ═══════════════════════════════════════════════════════════

print_section("4. Creating testuser API Key")

# Create API key for testuser to actually test permission filtering
# Use the admin_create_key RPC method
try:
    # Create API key via RPC method
    result = nx._call_rpc("admin_create_key", {
        "user_id": "testuser",
        "name": "demo-key",
        "tenant_id": "default",
        "expires_days": 1,
    })

    if result and 'api_key' in result:
        testuser_key = result['api_key']
        print_success(f"Created API key for testuser")

        # Create new client with testuser credentials
        nx_testuser = RemoteNexusFS(NEXUS_URL, api_key=testuser_key)
        use_testuser = True
    else:
        print_warning(f"Could not create testuser API key: {result}")
        nx_testuser = nx
        use_testuser = False
except Exception as e:
    print_warning(f"Could not create testuser API key: {e}")
    nx_testuser = nx
    use_testuser = False

if not use_testuser:
    print_info("Falling back to admin user (permission checks may be bypassed)")

# ═══════════════════════════════════════════════════════════
# Section 5: Cold Cache Test (First List)
# ═══════════════════════════════════════════════════════════

print_section("5. Cold Cache Performance Test")

print_info("Listing as testuser (first time - cache MISS expected)")
print_info("This call must check descendant access for visibility...")
print()

start = time.time()
result1 = nx_testuser.list(DEMO_BASE, recursive=True)
cold_time = (time.time() - start) * 1000

# list() returns strings when details=False
visible_files = [f for f in result1 if 'file_' in str(f)]
print_cache(f"Cold cache list: {cold_time:.2f}ms")
print_info(f"Files visible: {len(visible_files)}")

# ═══════════════════════════════════════════════════════════
# Section 6: Warm Cache Test (Second List)
# ═══════════════════════════════════════════════════════════

print_section("6. Warm Cache Performance Test")

print_info("Listing as testuser (second time - cache HIT expected)")
print_info("DirectoryVisibilityCache should return O(1) result...")
print()

start = time.time()
result2 = nx_testuser.list(DEMO_BASE, recursive=True)
warm_time = (time.time() - start) * 1000

print_cache(f"Warm cache list: {warm_time:.2f}ms")

# ═══════════════════════════════════════════════════════════
# Section 7: Third List (Verify Consistency)
# ═══════════════════════════════════════════════════════════

print_section("7. Cache Consistency Verification")

start = time.time()
result3 = nx_testuser.list(DEMO_BASE, recursive=True)
third_time = (time.time() - start) * 1000

print_cache(f"Third list: {third_time:.2f}ms")
print_info(f"Results consistent: {len(result1) == len(result2) == len(result3)}")

# ═══════════════════════════════════════════════════════════
# Section 8: Test Cache Invalidation
# ═══════════════════════════════════════════════════════════

print_section("8. Cache Invalidation Test")

print_info("Granting access to a new file (should invalidate cache)...")

# Grant access to one more file (using admin client)
new_file = f"{DEMO_BASE}/documents/file_001.txt"  # Was previously not accessible
nx.rebac_create(
    subject=("user", "testuser"),
    relation="direct_viewer",
    object=("file", new_file),
    tenant_id="default"
)
print_success(f"Granted access to {new_file}")

# List again as testuser - should see the new file
start = time.time()
result4 = nx_testuser.list(DEMO_BASE, recursive=True)
invalidated_time = (time.time() - start) * 1000

new_visible = [f for f in result4 if 'file_' in str(f)]
print_cache(f"Post-invalidation list: {invalidated_time:.2f}ms")
print_info(f"Files visible after grant: {len(new_visible)}")

# ═══════════════════════════════════════════════════════════
# Section 9: Performance Summary
# ═══════════════════════════════════════════════════════════

print_section("9. Performance Summary")

print(f"""
┌─────────────────────────────────────────────────────────┐
│  Directory Visibility Cache Performance Results         │
├─────────────────────────────────────────────────────────┤
│  Test Setup:                                            │
│    Files created:        {NUM_FILES:>5}                          │
│    Files shared:         {shared_count:>5}                          │
│    Private files:        {10:>5}                          │
├─────────────────────────────────────────────────────────┤
│  List Operation Timing:                                 │
│    Cold cache (1st):     {cold_time:>8.2f}ms                     │
│    Warm cache (2nd):     {warm_time:>8.2f}ms                     │
│    Third call:           {third_time:>8.2f}ms                     │
│    After invalidation:   {invalidated_time:>8.2f}ms                     │
├─────────────────────────────────────────────────────────┤
│  Speedup Analysis:                                      │
│    Cold -> Warm:         {cold_time/max(warm_time, 0.01):>8.1f}x                     │
│    Cold -> Third:        {cold_time/max(third_time, 0.01):>8.1f}x                     │
└─────────────────────────────────────────────────────────┘
""")

# Determine if the test passed
if warm_time < cold_time:
    speedup = cold_time / max(warm_time, 0.01)
    if speedup > 1.5:
        print_success(f"DirectoryVisibilityCache is working! ({speedup:.1f}x speedup)")
    else:
        print_warning(f"Cache working but speedup is modest ({speedup:.1f}x)")
else:
    print_warning("Cache may not be active (server-side caching)")

# Note about server-side caching
print()
print_info("Note: Full cache benefits are visible in server logs.")
print_info("Server-side _has_descendant_access() uses DirectoryVisibilityCache")
print_info("for O(1) lookups after first computation.")

# ═══════════════════════════════════════════════════════════
# Section 10: Verify Access Control
# ═══════════════════════════════════════════════════════════

print_section("10. Access Control Verification")

# Verify testuser cannot access private files
print_info("Verifying testuser CANNOT access private files...")

private_check = nx.rebac_check(
    subject=("user", "testuser"),
    permission="read",
    object=("file", f"{DEMO_BASE}/private/secret_0.txt"),
    tenant_id="default"
)

if not private_check:
    print_success("testuser correctly DENIED access to private files")
else:
    print_error("testuser incorrectly has access to private files!")

# Verify testuser can access shared files
print_info("Verifying testuser CAN access shared files...")

shared_check = nx.rebac_check(
    subject=("user", "testuser"),
    permission="read",
    object=("file", f"{DEMO_BASE}/documents/file_000.txt"),
    tenant_id="default"
)

if shared_check:
    print_success("testuser correctly has access to shared files")
else:
    print_error("testuser incorrectly denied access to shared files!")

# Cleanup clients
if use_testuser and nx_testuser != nx:
    nx_testuser.close()
nx.close()

print()
print("═" * 60)
print("  Demo Complete!")
print("═" * 60)
print()
PYTHON_TEST

echo ""
print_info "Demo finished. Check results above."
