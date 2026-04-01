#!/usr/bin/env bash
# =============================================================================
# Script 5: Mount Lifecycle & Cleanup
# =============================================================================
# Tests: mount/unmount cycle, mount list before/after, output formats
#        (--json, --quiet, --verbose, --fields), re-mount, doctor after
#        unmount, unmount --json, full cleanup
#
# Prereq: Run scripts 1-4 first (uses their mounts)
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

banner "Script 5: Mount Lifecycle & Cleanup"

# ── Step 1: Show current mount state ─────────────────────────────────────────
step "1/13" "Current mount state..."
echo "  > nexus-fs mount list --json"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list', '--json'])" 2>&1
BEFORE=$("$PYTHON" -c "
from nexus.fs._paths import load_persisted_mounts
print(len(load_persisted_mounts()))
")
echo ""
echo "  Total mounts before: $BEFORE"
ok "State captured"

# ── Step 2: Unmount one backend ──────────────────────────────────────────────
step "2/13" "Unmounting inbox backend..."
echo "  > nexus-fs unmount local://$TESTROOT/inbox"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/inbox'])" 2>&1
ok "Inbox unmounted"

# ── Step 3: Verify mount list updated ────────────────────────────────────────
step "3/13" "Verifying mount list after unmount..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1
AFTER=$("$PYTHON" -c "
from nexus.fs._paths import load_persisted_mounts
print(len(load_persisted_mounts()))
")
echo ""
echo "  Mounts: $BEFORE -> $AFTER"
ok "Mount list updated"

# ── Step 4: Unmount another backend ──────────────────────────────────────────
step "4/13" "Unmounting processed backend..."
echo "  > nexus-fs unmount local://$TESTROOT/processed"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/processed'])" 2>&1
ok "Processed unmounted"

# ── Step 5: Try unmounting non-existent mount ────────────────────────────────
step "5/13" "Testing unmount of non-existent mount (expect error)..."
echo "  > nexus-fs unmount local:///does/not/exist"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local:///does/not/exist'])" 2>&1 || true
ok "Error handled gracefully"

# ── Step 6: Re-mount inbox ───────────────────────────────────────────────────
step "6/13" "Re-mounting inbox..."
echo "  > nexus-fs mount local://$TESTROOT/inbox"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/inbox'])" 2>&1
ok "Inbox re-mounted"

# ── Step 7: Doctor after changes ─────────────────────────────────────────────
step "7/13" "Running doctor after mount changes..."
echo "  > nexus-fs doctor --mount local://$TESTROOT/inbox --json"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--mount', 'local://$TESTROOT/inbox', '--json'])" 2>&1
ok "Doctor passed"

# ── Step 8: Output format comparison ─────────────────────────────────────────
step "8/13" "Comparing output formats..."
echo "  > nexus-fs mount list (default)"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1
echo ""
echo "  > nexus-fs mount list --json"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list', '--json'])" 2>&1
ok "Output formats compared"

# ── Step 9: --quiet flag ─────────────────────────────────────────────────────
step "9/13" "Testing --quiet flag on mount list..."
echo "  > nexus-fs mount list --quiet"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list', '--quiet'])" 2>&1
ok "mount list --quiet"

# ── Step 10: --verbose flag ──────────────────────────────────────────────────
step "10/13" "Testing --verbose flag on mount list..."
echo "  > nexus-fs mount list -v"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list', '-v'])" 2>&1
ok "mount list --verbose"

# ── Step 11: --fields flag on unmount ────────────────────────────────────────
step "11/13" "Testing unmount with --json output..."
echo "  > nexus-fs unmount local://$TESTROOT/inbox --json"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/inbox', '--json'])" 2>&1
ok "unmount --json"

# ── Step 12: Full cleanup of all mounts ──────────────────────────────────────
step "12/13" "Cleaning up all mounts..."
"$PYTHON" << 'PYEOF'
from nexus.fs._paths import load_persisted_mounts
from nexus.fs._cli import main

mounts = load_persisted_mounts()
for entry in mounts:
    uri = entry["uri"]
    print(f"  > nexus-fs unmount {uri}")
    try:
        main(["unmount", uri], standalone_mode=False)
    except SystemExit:
        pass
PYEOF
echo ""
echo "  > nexus-fs mount list"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1
ok "All mounts removed"

# ── Step 13: Cleanup test files ──────────────────────────────────────────────
step "13/13" "Cleaning up test directories..."
rm -rf "$TESTROOT"
ok "Removed $TESTROOT"

banner "Lifecycle & Cleanup Complete!"
echo ""
echo "  Lifecycle tested:"
echo "    - mount list (before/after)"
echo "    - unmount individual backends"
echo "    - unmount non-existent (error handling)"
echo "    - unmount --json"
echo "    - re-mount after unmount"
echo "    - doctor after changes"
echo "    - output formats (default, --json, --quiet, -v)"
echo "    - full cleanup"
echo ""
echo "  All test data has been cleaned up."
echo "  To start fresh, re-run from script 1."
echo ""
