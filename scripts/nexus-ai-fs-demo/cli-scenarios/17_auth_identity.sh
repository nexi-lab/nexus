#!/bin/bash
# ============================================================================
# Scenario 17: Auth, OAuth & Identity
# ============================================================================
# Commands: auth list, auth test, auth doctor,
#           identity show, identity credentials, identity passport,
#           identity verify, oauth list
# TUI Tab: 5 (Access → Credentials)
#
# Story: Inspect authentication config, test connectivity, examine
#        an agent's digital identity & credentials, check OAuth state.
#
# NOTE: auth connect/disconnect and oauth setup-* require interactive
#       input or external providers, so we only test read-only commands.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="17 — Auth, OAuth & Identity"
header "$SCENARIO_NAME"

# Ensure a demo agent exists for identity inspection
nexus agent register "id_bot_17" "Identity Bot" --if-not-exists 2>/dev/null || true

# ── 1. auth list ─────────────────────────────────────────────────────────
header "1. auth list"
run_cli OUT nexus auth list
assert_exit_code "auth list" 0 "$OUT_RC"
info "Auth config:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 2. auth test ─────────────────────────────────────────────────────────
header "2. auth test"
# Test the nexus backend auth
run_cli OUT nexus auth test nexus
assert_or_infra_warn "auth test" "$OUT_RC" "$OUT"
info "Auth test: $OUT"

# ── 3. auth doctor ──────────────────────────────────────────────────────
header "3. auth doctor"
run_cli OUT nexus auth doctor
assert_or_infra_warn "auth doctor" "$OUT_RC" "$OUT"
info "Auth doctor:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 4. identity show ────────────────────────────────────────────────────
header "4. identity show"
run_cli OUT nexus identity show demo_agent
assert_or_infra_warn "identity show" "$OUT_RC" "$OUT"
info "Identity:"
echo "$OUT" | sed 's/^/    /'

# ── 5. identity credentials ─────────────────────────────────────────────
header "5. identity credentials"
run_cli OUT nexus identity credentials demo_agent
assert_or_infra_warn "credentials" "$OUT_RC" "$OUT"
info "Credentials:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 6. identity passport ────────────────────────────────────────────────
header "6. identity passport"
run_cli OUT nexus identity passport demo_agent
assert_or_infra_warn "passport" "$OUT_RC" "$OUT"
info "Digital Agent Passport:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 7. identity verify ──────────────────────────────────────────────────
header "7. identity verify"
# This needs a message + signature — test with a dummy to confirm API works
run_cli OUT nexus identity verify demo_agent \
    --message "test" --signature "dummy"
assert_or_infra_warn "identity verify" "$OUT_RC" "$OUT"
info "Verify: $OUT"

# ── 8. oauth list ────────────────────────────────────────────────────────
header "8. oauth list"
run_cli OUT nexus oauth list
assert_exit_code "oauth list" 0 "$OUT_RC"
info "OAuth credentials:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Access Panel (Tab 5)"
tui_switch_tab 5
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Access panel" "Access"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
nexus agent delete "id_bot_17" --yes 2>/dev/null || true

print_summary
