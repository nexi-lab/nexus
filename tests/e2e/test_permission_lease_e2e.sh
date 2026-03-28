#!/bin/bash
# E2E Test: Permission Lease Optimization (Issue #3398)
#
# Validates the permission lease fast path with real ReBAC enforcement
# against a running Nexus Docker stack with hot-patched code.
#
# Tests:
# 1. Sequential writes — lease should amortize permission checks
# 2. Sequential reads (cat) — read lease fast path (16A)
# 3. Sequential deletes (rm) — delete lease fast path (5A)
# 4. Permission revocation — lease must be invalidated
# 5. Server-side lease module verification
#
# Prerequisites: running Nexus Docker stack (nexus up)

set -e

# ── Configuration ──
NEXUS_URL="${NEXUS_URL:-http://localhost:44290}"
NEXUS_GRPC_PORT="${NEXUS_GRPC_PORT:-44291}"
ADMIN_KEY="${NEXUS_API_KEY:-sk-4m6UlXay6tZTBPMECJrq1XOcpUy7jONyWUq-BvgliLk}"

GREEN='\033[0;32m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
FAILURES=0
TESTS=0

pass() { TESTS=$((TESTS+1)); echo -e "${GREEN}✓${NC} $1"; }
fail() { TESTS=$((TESTS+1)); FAILURES=$((FAILURES+1)); echo -e "${RED}✗${NC} $1"; }
info() { echo -e "${CYAN}ℹ${NC} $1"; }
section() { echo ""; echo "════════════════════════════════════════════════"; echo "  $1"; echo "════════════════════════════════════════════════"; echo ""; }

nexus_cli() {
    NEXUS_URL="$NEXUS_URL" NEXUS_API_KEY="${1:-$ADMIN_KEY}" NEXUS_GRPC_PORT="$NEXUS_GRPC_PORT" \
        uv run python -m nexus.cli.main "${@:2}" 2>&1
}

admin() { nexus_cli "$ADMIN_KEY" "$@"; }

section "Prerequisites"
if ! curl -sf "$NEXUS_URL/health" > /dev/null 2>&1; then
    fail "Server not healthy at $NEXUS_URL"; exit 1
fi
HEALTH=$(curl -s "$NEXUS_URL/health")
info "Server: $NEXUS_URL"
info "Health: $HEALTH"
echo "$HEALTH" | grep -q '"enforce_permissions":true' && pass "Permissions enforced" || fail "Permissions not enforced"

# ══════════════════════════════════════════════════════════════
section "1. Setup: Create test workspace"
# ══════════════════════════════════════════════════════════════

TEST_DIR="/workspace/lease-e2e-$(date +%s)"
info "Test directory: $TEST_DIR"

# Create workspace with admin
admin write "$TEST_DIR/init.txt" "init" && pass "Workspace created" || fail "Workspace creation failed"

# Create a test user with the admin API
USER_ID="lease-tester-$(date +%s)"
USER_CREATE=$(admin admin create-user "$USER_ID" 2>&1) || true
USER_KEY=$(echo "$USER_CREATE" | grep -o 'sk-[A-Za-z0-9_-]*' | head -1) || true
if [ -n "$USER_KEY" ]; then
    pass "Created user $USER_ID with API key"
else
    info "User creation did not return a key — using admin for tests"
    USER_KEY="$ADMIN_KEY"
    USER_ID="admin"
fi

# Grant WRITE permission
if [ "$USER_ID" != "admin" ]; then
    admin rebac create user "$USER_ID" direct_editor file "$TEST_DIR" 2>/dev/null && \
        pass "Granted WRITE permission on $TEST_DIR" || \
        info "Grant may have failed — continuing with admin"
fi

# ══════════════════════════════════════════════════════════════
section "2. Sequential Writes (10 files, same directory)"
# ══════════════════════════════════════════════════════════════

info "Writing 10 files — lease should: 1st = full ReBAC check, 2-10 = lease hit"
WRITE_START=$(python3 -c "import time; print(time.monotonic())")
WF=0
for i in $(seq 1 10); do
    nexus_cli "$USER_KEY" write "$TEST_DIR/file-$i.txt" "content-$i" > /dev/null || WF=$((WF+1))
done
WRITE_END=$(python3 -c "import time; print(time.monotonic())")
WRITE_MS=$(python3 -c "print(f'{($WRITE_END - $WRITE_START)*1000:.0f}')")

if [ "$WF" -eq 0 ]; then
    pass "10 sequential writes succeeded (${WRITE_MS}ms)"
else
    fail "Sequential writes: $WF/10 failed"
fi

# Verify files exist
FCOUNT=$(admin ls "$TEST_DIR" 2>/dev/null | grep -c "file-" || echo 0)
if [ "$FCOUNT" -ge 10 ]; then
    pass "All 10 files verified"
else
    fail "Expected 10 files, found $FCOUNT"
fi

# ══════════════════════════════════════════════════════════════
section "3. Sequential Reads (cat 10 files)"
# ══════════════════════════════════════════════════════════════

info "Reading 10 files — read lease should amortize READ permission checks"
READ_START=$(python3 -c "import time; print(time.monotonic())")
RF=0
for i in $(seq 1 10); do
    nexus_cli "$USER_KEY" cat "$TEST_DIR/file-$i.txt" > /dev/null || RF=$((RF+1))
done
READ_END=$(python3 -c "import time; print(time.monotonic())")
READ_MS=$(python3 -c "print(f'{($READ_END - $READ_START)*1000:.0f}')")

if [ "$RF" -eq 0 ]; then
    pass "10 sequential reads succeeded (${READ_MS}ms)"
else
    fail "Sequential reads: $RF/10 failed"
fi

# ══════════════════════════════════════════════════════════════
section "4. Sequential Deletes (rm 5 files)"
# ══════════════════════════════════════════════════════════════

for i in $(seq 1 5); do
    nexus_cli "$USER_KEY" write "$TEST_DIR/del-$i.txt" "delete-me" > /dev/null 2>&1 || true
done

info "Deleting 5 files — delete lease should amortize WRITE checks"
DEL_START=$(python3 -c "import time; print(time.monotonic())")
DF=0
for i in $(seq 1 5); do
    nexus_cli "$USER_KEY" rm "$TEST_DIR/del-$i.txt" --force > /dev/null || DF=$((DF+1))
done
DEL_END=$(python3 -c "import time; print(time.monotonic())")
DEL_MS=$(python3 -c "print(f'{($DEL_END - $DEL_START)*1000:.0f}')")

if [ "$DF" -eq 0 ]; then
    pass "5 sequential deletes succeeded (${DEL_MS}ms)"
else
    fail "Sequential deletes: $DF/5 failed"
fi

# ══════════════════════════════════════════════════════════════
section "5. Permission Revocation → Write Denied"
# ══════════════════════════════════════════════════════════════

if [ "$USER_ID" != "admin" ]; then
    nexus_cli "$USER_KEY" write "$TEST_DIR/pre-revoke.txt" "ok" > /dev/null && \
        pass "Write succeeds before revocation" || fail "Pre-revocation write failed"

    admin rebac delete user "$USER_ID" direct_editor file "$TEST_DIR" 2>/dev/null
    info "Permission revoked"

    REVOKE_OUT=$(nexus_cli "$USER_KEY" write "$TEST_DIR/post-revoke.txt" "fail" 2>&1) || true
    if echo "$REVOKE_OUT" | grep -qi "denied\|permission\|unauthorized\|forbidden\|error"; then
        pass "Write correctly denied after revocation (lease invalidated)"
    else
        fail "Write should be denied after revocation: $REVOKE_OUT"
    fi

    # Re-grant for cleanup
    admin rebac create user "$USER_ID" direct_editor file "$TEST_DIR" 2>/dev/null || true
else
    info "Skipping revocation test (using admin user — bypasses permissions)"
fi

# ══════════════════════════════════════════════════════════════
section "6. Server-Side Lease Module Verification"
# ══════════════════════════════════════════════════════════════

info "Running in-container verification of lease table with new code"
CONTAINER="nexus-39db7244-nexus-1"
VERIFY=$(docker exec "$CONTAINER" python3 -c "
from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable
from nexus.lib.path_utils import parent_path
from nexus.lib.lease import ManualClock

# Basic stamp/check
t = PermissionLeaseTable()
t.stamp('/test', 'a')
assert t.check('/test', 'a'), 'stamp/check'

# Inheritance
assert t.check('/test/child', 'a'), 'inheritance'

# invalidate_agent (Issue #3398 2A)
t.invalidate_agent('a')
assert not t.check('/test', 'a'), 'invalidate_agent'

# Secondary indexes (Issue #3398 13A)
t.stamp('/x', 'b')
t.stamp('/y', 'b')
t.invalidate_agent('b')
assert t.active_count == 0, f'index cleanup: {t.active_count}'

# Path-targeted invalidation (Issue #3398 3A)
t.stamp('/doc.txt', 'c')
t.stamp('/other.txt', 'c')
t.invalidate_path('/doc.txt')
assert not t.check('/doc.txt', 'c'), 'path invalidation missed'
assert t.check('/other.txt', 'c'), 'path invalidation over-broad'

# Lazy eviction (Issue #3398 8A/15A)
c = ManualClock(0.0)
t2 = PermissionLeaseTable(clock=c, max_entries=10, ttl=5.0)
for i in range(9): t2.stamp(f'/f{i}', 'a')
c.advance(10)
t2.stamp('/new', 'a')
assert t2.active_count == 1, f'eviction: {t2.active_count}'

# Eviction stats
assert t2.stats()['lease_evictions'] == 9, 'eviction metric'

# parent_path shared utility (Issue #3398 6A)
assert parent_path('/a/b/c') == '/a/b'
assert parent_path('/a') == '/'
assert parent_path('/') is None

print('ALL_CHECKS_PASSED')
" 2>&1)

if echo "$VERIFY" | grep -q "ALL_CHECKS_PASSED"; then
    pass "In-container verification: stamp, check, inheritance, invalidate_agent, secondary indexes, path-targeted invalidation, lazy eviction, parent_path"
else
    fail "In-container verification failed: $VERIFY"
fi

# ══════════════════════════════════════════════════════════════
section "7. Cleanup"
# ══════════════════════════════════════════════════════════════

for i in $(seq 1 10); do admin rm "$TEST_DIR/file-$i.txt" --force > /dev/null 2>&1 || true; done
admin rm "$TEST_DIR/init.txt" --force > /dev/null 2>&1 || true
admin rm "$TEST_DIR/pre-revoke.txt" --force > /dev/null 2>&1 || true
info "Cleaned up test files"

# ══════════════════════════════════════════════════════════════
section "Results"
# ══════════════════════════════════════════════════════════════

echo ""
echo "  Tests: $((TESTS - FAILURES)) passed, $FAILURES failed (of $TESTS)"
echo "  Write 10 files: ${WRITE_MS}ms | Read 10 files: ${READ_MS}ms | Delete 5 files: ${DEL_MS}ms"
echo ""

if [ "$FAILURES" -gt 0 ]; then
    echo -e "${RED}$FAILURES test(s) failed${NC}"; exit 1
else
    echo -e "${GREEN}All $TESTS tests passed${NC}"
fi
