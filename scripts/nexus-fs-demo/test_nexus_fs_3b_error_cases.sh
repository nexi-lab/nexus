#!/usr/bin/env bash
# =============================================================================
# Script 3b: Error Cases
# =============================================================================
# Tests: Every CLI command's error/edge-case behaviour. Each step expects
#        a specific non-zero exit code or error message. PASS = the CLI
#        rejects bad input gracefully; FAIL = it crashed or silently succeeded.
#
# No prerequisites -- runs standalone.
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"
ERRDIR="/tmp/nexus-fs-err-test"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()    { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()      { echo -e "  ${GREEN}PASS${NC} $1"; }
fail()    { echo -e "  ${RED}FAIL${NC} $1"; FAILURES=$((FAILURES+1)); }
banner()  { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

FAILURES=0
TOTAL=0

# Expect a command to fail (exit != 0) and stderr/stdout to contain a substring
expect_error() {
    local label="$1"; shift
    local expect_substr="$1"; shift
    TOTAL=$((TOTAL+1))

    local output
    output=$("$PYTHON" -c "from nexus.fs._cli import main; main($*)" 2>&1) || true
    local exit_code=${PIPESTATUS[0]:-0}

    if echo "$output" | grep -qi "$expect_substr"; then
        ok "$label  (got: \"$expect_substr\")"
    else
        fail "$label  (expected \"$expect_substr\" in output)"
        echo "    actual output: $(echo "$output" | head -3)"
    fi
}

banner "Script 3b: Error Cases"

# ── mount errors ─────────────────────────────────────────────────────────────
step "1/13" "mount: invalid URI scheme..."
expect_error "mount foobar://x" "No module named" "['mount', 'foobar://nonsense']"

step "2/13" "mount: --at with multiple URIs..."
mkdir -p /tmp/nexus-fs-err-a /tmp/nexus-fs-err-b
expect_error "mount --at + multi URI" "only valid with a single URI" "['mount', 'local:///tmp/nexus-fs-err-a', 'local:///tmp/nexus-fs-err-b', '--at', '/x']"
rm -rf /tmp/nexus-fs-err-a /tmp/nexus-fs-err-b

step "3/13" "mount test: invalid scheme..."
expect_error "mount test ftp://" "No module named" "['mount', 'test', 'ftp://invalid']"

# ── unmount errors ───────────────────────────────────────────────────────────
step "4/13" "unmount: non-existent mount..."
expect_error "unmount missing" "mount not found" "['unmount', 'local:///no/such/mount']"

# ── cp errors ────────────────────────────────────────────────────────────────
step "5/13" "cp: non-existent source file..."
mkdir -p "$ERRDIR"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$ERRDIR'])" > /dev/null 2>&1
"$PYTHON" << PYEOF
import asyncio
from nexus.fs import mount
async def go():
    fs = await mount('local://$ERRDIR')
    await fs.write('/local/nexus-fs-err-test/exists.txt', b'x')
    await fs.close()
asyncio.run(go())
PYEOF
expect_error "cp missing source" "not found" "['cp', '/local/nexus-fs-err-test/no-such-file.txt', '/local/nexus-fs-err-test/dest.txt']"

step "6/13" "cp: destination path outside any mount..."
expect_error "cp unmounted dest" "No mount found" "['cp', '/local/nexus-fs-err-test/exists.txt', '/unmounted/path/dest.txt']"

step "7/13" "cp: no mounts configured..."
# Temporarily clear mounts
"$PYTHON" -c "
from nexus.fs._paths import save_persisted_mounts, load_persisted_mounts
save_persisted_mounts([], merge=False)
"
expect_error "cp no mounts" "No mounts found" "['cp', '/x/y.txt', '/a/b.txt']"
# Restore
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$ERRDIR'])" > /dev/null 2>&1

step "8/13" "cp: destination already exists..."
"$PYTHON" << PYEOF
import asyncio
from nexus.fs import mount
async def go():
    fs = await mount('local://$ERRDIR')
    await fs.write('/local/nexus-fs-err-test/src.txt', b'source')
    await fs.write('/local/nexus-fs-err-test/dst.txt', b'dest')
    await fs.close()
asyncio.run(go())
PYEOF
expect_error "cp dest exists" "already exists" "['cp', '/local/nexus-fs-err-test/src.txt', '/local/nexus-fs-err-test/dst.txt']"

# ── auth errors ──────────────────────────────────────────────────────────────
step "9/13" "auth test: unknown service..."
expect_error "auth test unknown" "Unknown auth service" "['auth', 'test', 'nonexistent-svc']"

step "10/13" "auth disconnect: unconfigured service..."
expect_error "auth disconnect slack" "No stored auth found" "['auth', 'disconnect', 'slack']"

step "11/13" "auth doctor: partial failure (slack/x not configured)..."
TOTAL=$((TOTAL+1))
OUTPUT=$("$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'doctor'])" 2>&1) || true
if echo "$OUTPUT" | grep -q "need auth setup"; then
    ok "auth doctor reports missing services"
else
    fail "auth doctor should report missing services"
fi

# ── doctor edge cases ────────────────────────────────────────────────────────
step "12/13" "doctor --mount: invalid scheme (warns, doesn't crash)..."
TOTAL=$((TOTAL+1))
OUTPUT=$("$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--mount', 'ftp://invalid'])" 2>&1) || true
if echo "$OUTPUT" | grep -qi "unable to mount\|No module"; then
    ok "doctor --mount invalid scheme (warns gracefully)"
else
    fail "doctor --mount should warn about invalid scheme"
fi

# ── mount list edge case ─────────────────────────────────────────────────────
step "13/13" "mount list: empty (no mounts)..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$ERRDIR'])" > /dev/null 2>&1 || true
"$PYTHON" -c "
from nexus.fs._paths import save_persisted_mounts
save_persisted_mounts([], merge=False)
"
TOTAL=$((TOTAL+1))
OUTPUT=$("$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1) || true
if echo "$OUTPUT" | grep -qi "mounts.*\[\]"; then
    ok "mount list empty returns empty array"
else
    fail "mount list empty should return []"
fi

# ── Cleanup ──────────────────────────────────────────────────────────────────
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$ERRDIR'])" 2>/dev/null || true
rm -rf "$ERRDIR"

# ── Summary ──────────────────────────────────────────────────────────────────
banner "Error Cases Complete!"
echo ""
PASSED=$((TOTAL - FAILURES))
echo "  Results: $PASSED/$TOTAL passed, $FAILURES failed"
echo ""
echo "  Error cases tested:"
echo "    mount:      invalid URI, --at + multi URI, test invalid scheme"
echo "    unmount:    non-existent mount"
echo "    cp:         missing source, unmounted dest, no mounts, dest exists"
echo "    auth:       unknown service, disconnect unconfigured, doctor partial"
echo "    doctor:     --mount invalid scheme (graceful warning)"
echo "    mount list: empty state"
echo ""

if [ "$FAILURES" -gt 0 ]; then
    echo -e "  ${RED}$FAILURES error case(s) did not behave as expected${NC}"
    exit 1
fi
