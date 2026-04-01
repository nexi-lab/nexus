#!/usr/bin/env bash
# =============================================================================
# Script 3: Auth & Diagnostics
# =============================================================================
# Tests: auth list, auth list --json --fields, auth test, auth test --target,
#        auth test --user-email, auth connect native, auth connect secret --set,
#        auth disconnect, auth doctor, doctor, doctor --mount,
#        --quiet, --verbose, --fields
#
# No prerequisites -- runs standalone.
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; DIM='\033[2m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

banner "Script 3: Auth & Diagnostics"

# ── Step 1: Auth list ────────────────────────────────────────────────────────
step "1/15" "Listing all configured auth services..."
echo "  > nexus-fs auth list"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'list'])" 2>&1
ok "Auth list retrieved"

# ── Step 2: Auth list (JSON) ─────────────────────────────────────────────────
step "2/15" "Auth list with JSON output..."
echo "  > nexus-fs auth list --json"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'list', '--json'])" 2>&1
ok "JSON auth list"

# ── Step 3: Auth list with --fields filter ───────────────────────────────────
step "3/15" "Auth list with --fields filter..."
echo "  > nexus-fs auth list --json --fields service,status"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'list', '--json', '--fields', 'service,status'])" 2>&1
ok "Filtered auth list (service + status only)"

# ── Step 4: Test S3 auth ─────────────────────────────────────────────────────
step "4/15" "Testing S3 credentials..."
echo "  > nexus-fs auth test s3"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'test', 's3'])" 2>&1
ok "S3 auth tested"

# ── Step 5: Test GCS auth ────────────────────────────────────────────────────
step "5/15" "Testing GCS credentials..."
echo "  > nexus-fs auth test gcs"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'test', 'gcs'])" 2>&1 || echo -e "  ${DIM}(GCS auth not configured -- expected in some environments)${NC}"
ok "GCS auth tested"

# ── Step 6: Auth test with --user-email ──────────────────────────────────────
step "6/15" "Testing auth with --user-email flag..."
echo "  > nexus-fs auth test gcs --user-email test@example.com"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'test', 'gcs', '--user-email', 'test@example.com'])" 2>&1 || echo -e "  ${DIM}(GCS may not need user-email -- flag accepted)${NC}"
ok "auth test --user-email"

# ── Step 7: Auth test with --target (GWS target readiness) ──────────────────
step "7/15" "Testing GWS target readiness (--target drive)..."
echo "  > nexus-fs auth test gws --target drive"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'test', 'gws', '--target', 'drive'])" 2>&1 || echo -e "  ${DIM}(GWS target check completed with status above)${NC}"
ok "auth test --target"

# ── Step 8: Auth connect native (S3) ────────────────────────────────────────
step "8/15" "Connecting S3 with native auth..."
echo "  > nexus-fs auth connect s3 native"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'connect', 's3', 'native'])" 2>&1
ok "auth connect native"

# ── Step 9: Auth connect secret with --set (S3) ─────────────────────────────
step "9/15" "Connecting S3 with secret auth (--set key=value)..."
echo "  > nexus-fs auth connect s3 secret --set access_key_id=... --set secret_access_key=..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'connect', 's3', 'secret', '--set', 'access_key_id=AKIAIOSFODNN7EXAMPLE', '--set', 'secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'])" 2>&1
ok "auth connect secret --set"

# ── Step 10: Verify secret was stored ────────────────────────────────────────
step "10/15" "Verifying stored secret auth..."
echo "  > nexus-fs auth test s3"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'test', 's3'])" 2>&1
ok "Secret auth verified"

# ── Step 11: Auth disconnect ─────────────────────────────────────────────────
step "11/15" "Disconnecting S3 stored auth..."
echo "  > nexus-fs auth disconnect s3"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'disconnect', 's3'])" 2>&1
ok "auth disconnect"

# ── Step 11b: Restore S3 native auth ────────────────────────────────────────
echo "  > Restoring S3 native auth..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'connect', 's3', 'native'])" 2>&1
ok "S3 native auth restored"

# ── Step 12: Auth doctor ─────────────────────────────────────────────────────
step "12/15" "Running auth doctor..."
echo "  > nexus-fs auth doctor"
"$PYTHON" -c "from nexus.fs._cli import main; main(['auth', 'doctor'])" 2>&1 || true
ok "Auth doctor completed"

# ── Step 13: Full doctor + --quiet flag ──────────────────────────────────────
step "13/15" "Running doctor with --quiet flag..."
echo "  > nexus-fs doctor --quiet"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--quiet'])" 2>&1 || true
ok "doctor --quiet"

# ── Step 14: Doctor with --verbose flag ──────────────────────────────────────
step "14/15" "Running doctor with --verbose flag..."
echo "  > nexus-fs doctor -v"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '-v'])" 2>&1
ok "doctor --verbose"

# ── Step 15: Doctor with mount connectivity ──────────────────────────────────
step "15/15" "Running doctor with mount connectivity check..."
mkdir -p "$TESTROOT/healthcheck"
echo "healthcheck" > "$TESTROOT/healthcheck/probe.txt"
echo "  > nexus-fs doctor --mount local://$TESTROOT/healthcheck"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--mount', 'local://$TESTROOT/healthcheck'])" 2>&1
rm -rf "$TESTROOT/healthcheck"
ok "Doctor with mount check completed"

# ── Summary ──────────────────────────────────────────────────────────────────
banner "Auth & Diagnostics Complete!"
echo ""
echo "  Checks performed:"
echo "    - auth list           (human + JSON + --fields filter)"
echo "    - auth test s3/gcs    (native credential chain)"
echo "    - auth test --user-email  (email override flag)"
echo "    - auth test --target  (GWS target readiness)"
echo "    - auth connect native (S3 provider chain)"
echo "    - auth connect secret (--set key=value pairs)"
echo "    - auth disconnect     (remove stored auth)"
echo "    - auth doctor         (auth-only summary)"
echo "    - doctor              (env + backends + mounts)"
echo "    - doctor --quiet      (suppress non-error output)"
echo "    - doctor -v           (verbose output)"
echo "    - doctor --mount      (connectivity + latency)"
echo ""
