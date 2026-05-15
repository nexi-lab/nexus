#!/bin/bash
# ============================================================================
# Scenario 19: Sandbox, ACP & Plugins
# ============================================================================
# Commands: sandbox list, sandbox create, sandbox status, sandbox run,
#           sandbox stop,
#           acp agents, acp ps, acp history,
#           plugins list, plugins info
# TUI Tab: 3 (Agents)
#
# Story: List sandboxes, create one, run code in it, list ACP agents
#        and processes, inspect plugins, stop sandbox.
#
# NOTE: sandbox create requires E2B or Docker provider. ACP requires
#       agent coding protocol. Commands are tested for non-crash; deep
#       integration depends on provider availability.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="19 — Sandbox, ACP & Plugins"
header "$SCENARIO_NAME"

# ── 1. sandbox list ─────────────────────────────────────────────────────
header "1. sandbox list"
run_cli OUT nexus sandbox list
assert_or_infra_warn "sandbox list" "$OUT_RC" "$OUT"
info "Sandboxes:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 2. sandbox create ───────────────────────────────────────────────────
header "2. sandbox create"
run_cli OUT nexus sandbox create "test-sb-19" --ttl 300
assert_or_infra_warn "sandbox create" "$OUT_RC" "$OUT"
SB_ID=$(echo "$OUT" | grep -oE '[a-zA-Z0-9_-]+' | head -1 || true)
info "Create: $OUT"

# ── 3. sandbox status ───────────────────────────────────────────────────
header "3. sandbox status"
if [ -n "${SB_ID:-}" ]; then
    run_cli OUT nexus sandbox status "$SB_ID"
    assert_or_infra_warn "sandbox status" "$OUT_RC" "$OUT"
    info "Status: $OUT"
else
    run_cli OUT nexus sandbox status "test-sb-19"
    assert_or_infra_warn "sandbox status" "$OUT_RC" "$OUT"
    info "Status: $OUT"
fi

# ── 4. sandbox run ──────────────────────────────────────────────────────
header "4. sandbox run"
if [ -n "${SB_ID:-}" ]; then
    run_cli OUT nexus sandbox run "$SB_ID" \
        --language python --code "print('hello from sandbox')"
    assert_or_infra_warn "sandbox run" "$OUT_RC" "$OUT"
    info "Run: $OUT"
else
    warn "No sandbox — skipping run"
fi

# ── 5. sandbox get-or-create (idempotent) ────────────────────────────────
header "5. sandbox get-or-create"
run_cli OUT nexus sandbox get-or-create "idempotent-sb-19" --ttl 300
assert_or_infra_warn "sandbox get-or-create" "$OUT_RC" "$OUT"
info "Get-or-create: $OUT"

# ── 6. sandbox stop ─────────────────────────────────────────────────────
header "6. sandbox stop"
run_cli OUT nexus sandbox stop "test-sb-19"
assert_or_infra_warn "sandbox stop" "$OUT_RC" "$OUT"
info "Stop: $OUT"
run_cli OUT nexus sandbox stop "idempotent-sb-19"
assert_or_infra_warn "sandbox stop 2" "$OUT_RC" "$OUT"

# ── 7. acp agents ───────────────────────────────────────────────────────
header "7. acp agents"
run_cli OUT nexus acp agents
assert_exit_code "acp agents" 0 "$OUT_RC"
info "ACP agents:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 8. acp ps ────────────────────────────────────────────────────────────
header "8. acp ps"
run_cli OUT nexus acp ps
assert_exit_code "acp ps" 0 "$OUT_RC"
info "ACP processes:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 9. acp history ──────────────────────────────────────────────────────
header "9. acp history"
run_cli OUT nexus acp history --limit 10
assert_exit_code "acp history" 0 "$OUT_RC"
info "ACP history:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 10. plugins list ────────────────────────────────────────────────────
header "10. plugins list"
run_cli OUT nexus plugins list
assert_exit_code "plugins list" 0 "$OUT_RC"
info "Plugins:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 11. plugins info ────────────────────────────────────────────────────
header "11. plugins info"
# Get first plugin name
FIRST_PLUGIN=$(echo "$OUT" | grep -oE '\b[a-z][a-z0-9_-]+\b' | head -1 || true)
if [ -n "${FIRST_PLUGIN:-}" ]; then
    run_cli OUT nexus plugins info "$FIRST_PLUGIN"
    assert_exit_code "plugins info" 0 "$OUT_RC"
    info "Plugin detail:"
    echo "$OUT" | head -10 | sed 's/^/    /'
else
    warn "No plugins found — skipping info"
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Agents Panel (Tab 3)"
tui_switch_tab 3
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Agents panel" "Agent"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

print_summary
