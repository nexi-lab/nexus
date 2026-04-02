#!/bin/bash
# ============================================================================
# Scenario 13: Payments
# ============================================================================
# Commands: pay balance, pay transfer, pay history
# TUI Tab: 6 (Payments)
#
# Story: Check an agent's credit balance, transfer credits between
#        agents, view transaction history.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="13 — Payments"
header "$SCENARIO_NAME"

# Ensure agents exist for transfers
nexus agent register "payer_13" "Payer Agent" --if-not-exists 2>/dev/null || true
nexus agent register "payee_13" "Payee Agent" --if-not-exists 2>/dev/null || true

# ── 1. pay balance ──────────────────────────────────────────────────────
header "1. pay balance"
run_cli OUT nexus pay balance
assert_or_infra_warn "balance (default)" "$OUT_RC" "$OUT"
info "Balance:"
echo "$OUT" | sed 's/^/    /'

# ── 2. pay balance — specific agent ─────────────────────────────────────
header "2. pay balance (agent)"
run_cli OUT nexus pay balance payer_13
assert_or_infra_warn "balance payer" "$OUT_RC" "$OUT"
info "Payer balance: $OUT"

# ── 3. pay transfer ─────────────────────────────────────────────────────
header "3. pay transfer"
run_cli OUT nexus pay transfer payee_13 10 --memo "Scenario 13 test transfer"
assert_or_infra_warn "transfer" "$OUT_RC" "$OUT"
info "Transfer: $OUT"

# ── 4. pay history ──────────────────────────────────────────────────────
header "4. pay history"
run_cli OUT nexus pay history --limit 10
assert_exit_code "history" 0 "$OUT_RC"
info "Transaction history:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Payments Panel (Tab 6)"
tui_switch_tab 6
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Payments panel" "Pay"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
nexus agent delete "payer_13" --yes 2>/dev/null || true
nexus agent delete "payee_13" --yes 2>/dev/null || true

print_summary
