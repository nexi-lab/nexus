#!/bin/bash
# ============================================================================
# Scenario 14: Workflows
# ============================================================================
# Commands: workflows discover, workflows load, workflows list,
#           workflows test, workflows runs, workflows enable,
#           workflows disable, workflows unload
# TUI Tab: 8 (Workflows)
#
# Story: Discover available workflows, load one, test-run it, inspect
#        execution history, toggle enable/disable, unload.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="14 — Workflows"
header "$SCENARIO_NAME"

# Create a minimal workflow definition
WF_DIR="/tmp/nexus-scenario14-wf"
rm -rf "$WF_DIR"; mkdir -p "$WF_DIR"
cat > "$WF_DIR/hello_workflow.yaml" <<'EOF'
name: hello_workflow
description: A simple test workflow
triggers:
  - type: manual
steps:
  - name: greet
    action: log
    params:
      message: "Hello from workflow!"
EOF

# ── 1. workflows discover ───────────────────────────────────────────────
header "1. workflows discover"
run_cli OUT nexus workflows discover "$WF_DIR"
assert_or_infra_warn "discover" "$OUT_RC" "$OUT"
info "Discovered:"
echo "$OUT" | sed 's/^/    /'

# ── 2. workflows load ───────────────────────────────────────────────────
header "2. workflows load"
run_cli OUT nexus workflows load "$WF_DIR/hello_workflow.yaml"
assert_or_infra_warn "load" "$OUT_RC" "$OUT"
info "Load: $OUT"

# ── 3. workflows list ───────────────────────────────────────────────────
header "3. workflows list"
run_cli OUT nexus workflows list
assert_or_infra_warn "list" "$OUT_RC" "$OUT"
if [ "$OUT_RC" -eq 0 ]; then
    assert_contains "hello_workflow" "$OUT" "hello_workflow"
fi
info "Workflows:"
echo "$OUT" | sed 's/^/    /'

# ── 4. workflows test ───────────────────────────────────────────────────
header "4. workflows test"
run_cli OUT nexus workflows test hello_workflow
assert_or_infra_warn "test" "$OUT_RC" "$OUT"
info "Test run:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 5. workflows runs ───────────────────────────────────────────────────
header "5. workflows runs"
run_cli OUT nexus workflows runs hello_workflow --limit 5
assert_or_infra_warn "runs" "$OUT_RC" "$OUT"
info "Runs:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 6. workflows disable ────────────────────────────────────────────────
header "6. workflows disable"
run_cli OUT nexus workflows disable hello_workflow
assert_or_infra_warn "disable" "$OUT_RC" "$OUT"

# ── 7. workflows enable ─────────────────────────────────────────────────
header "7. workflows enable"
run_cli OUT nexus workflows enable hello_workflow
assert_or_infra_warn "enable" "$OUT_RC" "$OUT"

# ── 8. workflows unload ─────────────────────────────────────────────────
header "8. workflows unload"
run_cli OUT nexus workflows unload hello_workflow
assert_or_infra_warn "unload" "$OUT_RC" "$OUT"

run_cli OUT nexus workflows list
if [ "$OUT_RC" -eq 0 ]; then
    assert_not_contains "unloaded" "$OUT" "hello_workflow"
else
    assert_or_infra_warn "list after unload" "$OUT_RC" "$OUT"
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Workflows Panel (Tab 8)"
tui_switch_tab 8
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Workflows panel" "Workflows"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
rm -rf "$WF_DIR"

print_summary
