#!/bin/bash
# ============================================================================
# Scenario 08: Delegation & Scheduling
# ============================================================================
# Commands: delegation create (COPY + SHARED), delegation list,
#           delegation show, delegation revoke,
#           scheduler status, scheduler queue
# TUI Tab: 3 (Agents → Delegations sub-tab)
#
# Story: A coordinator delegates tasks to two workers with different
#        isolation modes, inspects the delegation chain, checks the
#        scheduler queue, then revokes one delegation.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="08 — Delegation & Scheduling"
header "$SCENARIO_NAME"

C="coord_08"
W1="worker_08a"
W2="worker_08b"
nexus agent delete "$C"  --yes 2>/dev/null || true
nexus agent delete "$W1" --yes 2>/dev/null || true
nexus agent delete "$W2" --yes 2>/dev/null || true
nexus agent register "$C"  "Coordinator 08" --if-not-exists 2>/dev/null || true
nexus agent register "$W1" "Worker A"       --if-not-exists 2>/dev/null || true
nexus agent register "$W2" "Worker B"       --if-not-exists 2>/dev/null || true

# ── 1. delegation create — COPY mode ────────────────────────────────────
header "1. delegation create (COPY)"
run_cli OUT nexus delegation create "$C" "$W1" \
    --mode COPY --scope "/workspace/demo" --ttl 3600
assert_exit_code "create COPY" 0 "$OUT_RC"
D1=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)

# ── 2. delegation create — SHARED mode ──────────────────────────────────
header "2. delegation create (SHARED)"
run_cli OUT nexus delegation create "$C" "$W2" \
    --mode SHARED --scope "/workspace/demo/code" --ttl 7200
assert_exit_code "create SHARED" 0 "$OUT_RC"
D2=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)

# ── 3. delegation list ──────────────────────────────────────────────────
header "3. delegation list"
run_cli OUT nexus delegation list
assert_exit_code "list all" 0 "$OUT_RC"
if [ -n "${D1:-}" ]; then
    assert_contains "worker A" "$OUT" "$W1"
fi
if [ -n "${D2:-}" ]; then
    assert_contains "worker B" "$OUT" "$W2"
fi
info "All delegations:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 4. delegation list — filtered ───────────────────────────────────────
header "4. delegation list --coordinator"
run_cli OUT nexus delegation list --coordinator "$C"
assert_exit_code "list filtered" 0 "$OUT_RC"
if [ -n "${D1:-}" ] || [ -n "${D2:-}" ]; then
    assert_contains "coord filtered" "$OUT" "$C"
fi

# ── 5. delegation show ──────────────────────────────────────────────────
header "5. delegation show"
if [ -n "${D1:-}" ]; then
    run_cli OUT nexus delegation show "$D1"
    assert_exit_code "show" 0 "$OUT_RC"
    if [ "$OUT_RC" -eq 0 ]; then
        assert_contains "coordinator" "$OUT" "$C"
        assert_contains "worker" "$OUT" "$W1"
    fi
    info "Chain:"
    echo "$OUT" | sed 's/^/    /'
else
    warn "No delegation ID — skipping show"
fi

# ── 6. Verify demo delegations (from seeding) ───────────────────────────
header "6. Verify seeded delegations"
run_cli OUT nexus delegation list --coordinator coordinator
assert_exit_code "demo deleg" 0 "$OUT_RC"
if [ "$OUT_RC" -eq 0 ]; then
    assert_contains "researcher" "$OUT" "researcher"
    assert_contains "coder" "$OUT" "coder"
fi

# ── 7. scheduler status ─────────────────────────────────────────────────
header "7. scheduler status"
run_cli OUT nexus scheduler status
assert_or_infra_warn "scheduler status" "$OUT_RC" "$OUT"
info "Scheduler status:"
echo "$OUT" | sed 's/^/    /'

# ── 8. scheduler queue ──────────────────────────────────────────────────
header "8. scheduler queue"
run_cli OUT nexus scheduler queue
assert_or_infra_warn "scheduler queue" "$OUT_RC" "$OUT"
info "Queue:"
echo "$OUT" | sed 's/^/    /'

# ── 9. delegation revoke ────────────────────────────────────────────────
header "9. delegation revoke"
if [ -n "${D2:-}" ]; then
    run_cli OUT nexus delegation revoke "$D2"
    assert_exit_code "revoke" 0 "$OUT_RC"

    if [ "$OUT_RC" -eq 0 ]; then
        run_cli OUT nexus delegation list --coordinator "$C"
        assert_not_contains "W2 revoked" "$OUT" "$W2"
    fi
else
    warn "No delegation ID — skipping revoke"
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Agents Delegations (Tab 3)"
tui_switch_tab 3
sleep 2
tui_send "Tab"  # switch to Delegations sub-tab
sleep 1
tui_send "r"
sleep 2
tui_assert_contains "Agents panel" "Agent"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
[ -n "${D1:-}" ] && nexus delegation revoke "$D1" 2>/dev/null || true
nexus agent delete "$C"  --yes 2>/dev/null || true
nexus agent delete "$W1" --yes 2>/dev/null || true
nexus agent delete "$W2" --yes 2>/dev/null || true

print_summary
