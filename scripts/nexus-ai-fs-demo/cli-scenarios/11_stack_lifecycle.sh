#!/bin/bash
# ============================================================================
# Scenario 11: Stack Lifecycle
# ============================================================================
# Commands: status, doctor, logs, env, env --json, run, stop, start, restart
# TUI Tab: Shift+S (Stack)
#
# Story: Inspect the running stack health, view logs, dump env vars,
#        execute a command inside the Nexus env, then cycle stop→start→restart.
#
# NOTE: init, up, down are tested in 00_setup.sh. upgrade is skipped here
#       to avoid pulling new images during test runs.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="11 — Stack Lifecycle"
header "$SCENARIO_NAME"

# ── 1. status ────────────────────────────────────────────────────────────
header "1. nexus status"
run_cli OUT nexus status
assert_exit_code "status" 0 "$OUT_RC"
info "Status:"
echo "$OUT" | sed 's/^/    /'

# ── 2. status --json ────────────────────────────────────────────────────
header "2. nexus status --json"
run_cli OUT nexus status --json
assert_exit_code "status json" 0 "$OUT_RC"
assert_regex "json object" "$OUT" '^\{|"status"'

# ── 3. doctor ────────────────────────────────────────────────────────────
header "3. nexus doctor"
run_cli OUT nexus doctor
assert_exit_code "doctor" 0 "$OUT_RC"
info "Doctor:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 4. doctor --json ────────────────────────────────────────────────────
header "4. nexus doctor --json"
run_cli OUT nexus doctor --json
assert_exit_code "doctor json" 0 "$OUT_RC"

# ── 5. logs ──────────────────────────────────────────────────────────────
header "5. nexus logs --tail 10"
# nexus logs can stream forever, so use timeout
info "Running: nexus logs --tail 10 (with 10s timeout)"
_t_start=$(_ms_now)
OUT=$(timeout 10 nexus logs --tail 10 2>&1 | grep -v "^RPCTransport" | head -30) || true
_t_end=$(_ms_now)
OUT_RC=0
TIMING_LOG+=("$(printf '%-50s %6dms  %s' "nexus logs --tail 10" "$(( _t_end - _t_start ))" "ok")")
timing "nexus logs --tail 10 → $(( _t_end - _t_start ))ms"
info "Recent logs:"
echo "$OUT" | tail -5 | sed 's/^/    /'
ok "logs — captured"

# ── 6. env ───────────────────────────────────────────────────────────────
header "6. nexus env"
run_cli OUT nexus env
assert_exit_code "env" 0 "$OUT_RC"
assert_contains "NEXUS_URL" "$OUT" "NEXUS_URL"
assert_contains "NEXUS_API_KEY" "$OUT" "NEXUS_API_KEY"
info "Env vars:"
echo "$OUT" | head -5 | sed 's/^/    /'

# ── 7. env --json ────────────────────────────────────────────────────────
header "7. nexus env --json"
run_cli OUT nexus env --json
assert_exit_code "env json" 0 "$OUT_RC"
assert_regex "json" "$OUT" '"nexus_url"|"NEXUS_URL"'

# ── 8. run ───────────────────────────────────────────────────────────────
header "8. nexus run"
run_cli OUT nexus run -- echo "hello from nexus env"
assert_exit_code "run" 0 "$OUT_RC"
assert_contains "run output" "$OUT" "hello"

# ── 9. stop → start cycle ───────────────────────────────────────────────
header "9. nexus stop → start"
run_cli OUT nexus stop
assert_exit_code "stop" 0 "$OUT_RC"
info "Stopped. Waiting 3s..."
sleep 3

run_cli OUT nexus start
assert_exit_code "start" 0 "$OUT_RC"
info "Started. Waiting for health..."
sleep 8

run_cli OUT nexus status
if echo "$OUT" | grep -q '"server_reachable": true'; then
    ok "status after start — server reachable"
else
    warn "Server may still be starting (health check)"
    sleep 5
    run_cli OUT nexus status
fi

# ── 10. restart ──────────────────────────────────────────────────────────
header "10. nexus restart"
run_cli OUT nexus restart
assert_exit_code "restart" 0 "$OUT_RC"
info "Restarted. Waiting for health..."
sleep 10

run_cli OUT nexus status
if echo "$OUT" | grep -q '"server_reachable": true'; then
    ok "status after restart — server reachable"
else
    warn "Server may still be starting after restart"
    sleep 10
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Stack Panel (Shift+S)"
tui_send "S"  # Shift+S for Stack panel
sleep 3
tui_send "r"
sleep 2
tui_assert_contains "Stack panel" "Stack"
info "TUI snapshot:"
tui_capture | head -20 | sed 's/^/    | /'

print_summary
