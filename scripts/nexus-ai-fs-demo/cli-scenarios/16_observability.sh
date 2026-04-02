#!/bin/bash
# ============================================================================
# Scenario 16: Observability & Audit
# ============================================================================
# Commands: events replay, audit list, audit export,
#           secrets-audit list, secrets-audit export, secrets-audit verify,
#           locks list, locks info,
#           governance status, governance alerts, governance rings
# TUI Tab: 9 (Events)
#
# Story: Replay recent events, inspect the audit trail, review secret
#        access logs, check locks, review governance alerts.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="16 — Observability & Audit"
header "$SCENARIO_NAME"

EXPORT_DIR="/tmp/nexus-scenario16"
rm -rf "$EXPORT_DIR"; mkdir -p "$EXPORT_DIR"

# ── 1. events replay ────────────────────────────────────────────────────
header "1. events replay"
run_cli OUT nexus events replay --limit 20
assert_or_infra_warn "events replay" "$OUT_RC" "$OUT"
info "Events:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 2. audit list ───────────────────────────────────────────────────────
header "2. audit list"
run_cli OUT nexus audit list --limit 20
assert_or_infra_warn "audit list" "$OUT_RC" "$OUT"
info "Audit entries:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 3. audit export — JSON ──────────────────────────────────────────────
header "3. audit export (json)"
run_cli OUT nexus audit export --format json --output "$EXPORT_DIR/audit.json"
assert_or_infra_warn "audit export json" "$OUT_RC" "$OUT"
[ -f "$EXPORT_DIR/audit.json" ] && ok "Audit JSON exported" || warn "No audit file"

# ── 4. audit export — CSV ───────────────────────────────────────────────
header "4. audit export (csv)"
run_cli OUT nexus audit export --format csv --output "$EXPORT_DIR/audit.csv"
assert_or_infra_warn "audit export csv" "$OUT_RC" "$OUT"

# ── 5. secrets-audit list ───────────────────────────────────────────────
header "5. secrets-audit list"
run_cli OUT nexus secrets-audit list --limit 20
assert_or_infra_warn "secrets-audit list" "$OUT_RC" "$OUT"
info "Secret access log:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 6. secrets-audit export ─────────────────────────────────────────────
header "6. secrets-audit export"
run_cli OUT nexus secrets-audit export --format json \
    --output "$EXPORT_DIR/secrets.json"
assert_or_infra_warn "secrets-audit export" "$OUT_RC" "$OUT"

# ── 7. secrets-audit verify ─────────────────────────────────────────────
header "7. secrets-audit verify"
# Use a placeholder ID — just confirm the command runs
run_cli OUT nexus secrets-audit verify "00000000-0000-0000-0000-000000000000" 2>&1 || true
info "Verify: $OUT"

# ── 8. locks list ────────────────────────────────────────────────────────
header "8. locks list"
run_cli OUT nexus lock list
assert_or_infra_warn "locks list" "$OUT_RC" "$OUT"
info "Active locks:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 9. locks info ────────────────────────────────────────────────────────
header "9. locks info"
run_cli OUT nexus lock info /workspace/demo/README.md
assert_or_infra_warn "lock info" "$OUT_RC" "$OUT"
info "Lock info: $OUT"

# ── 10. governance status ────────────────────────────────────────────────
header "10. governance status"
run_cli OUT nexus governance status
assert_or_infra_warn "gov status" "$OUT_RC" "$OUT"
info "Governance:"
echo "$OUT" | sed 's/^/    /'

# ── 11. governance alerts ────────────────────────────────────────────────
header "11. governance alerts"
run_cli OUT nexus governance alerts --limit 10
assert_or_infra_warn "gov alerts" "$OUT_RC" "$OUT"
info "Alerts:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 12. governance rings ────────────────────────────────────────────────
header "12. governance rings"
run_cli OUT nexus governance rings
assert_or_infra_warn "gov rings" "$OUT_RC" "$OUT"
info "Fraud rings:"
echo "$OUT" | head -5 | sed 's/^/    /'

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Events Panel (Tab 9)"
tui_switch_tab 9
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Events panel" "Event"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
rm -rf "$EXPORT_DIR"

print_summary
