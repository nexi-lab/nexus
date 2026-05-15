#!/bin/bash
# ============================================================================
# Scenario 18: Memory & Knowledge (RLM)
# ============================================================================
# Commands: memory (via nexus memory subcommands), rlm infer
# TUI Tab: 7 (Search → Memories sub-tab)
#
# Story: Store agent memories, list and retrieve them, run an RLM
#        inference query against the knowledge base.
#
# NOTE: memory and rlm are enterprise bricks. If not enabled, commands
#       will return gracefully and we just verify they don't crash.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="18 — Memory & Knowledge"
header "$SCENARIO_NAME"

# ── 1. memory — list (may be empty or command may not exist) ────────────
header "1. memory list"
# Try 'nexus memory list' first; if memory is not a command, try --help
run_cli OUT nexus memory list
assert_or_infra_warn "memory list" "$OUT_RC" "$OUT"
info "Memory output:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 2. memory subcommands ───────────────────────────────────────────────
header "2. memory --help"
run_cli OUT nexus memory --help
assert_or_infra_warn "memory help" "$OUT_RC" "$OUT"
info "Memory help:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 3. rlm infer ────────────────────────────────────────────────────────
header "3. rlm infer"
run_cli OUT nexus rlm infer /workspace/demo/ \
    --prompt "What is the architecture of this system?"
assert_or_infra_warn "rlm infer" "$OUT_RC" "$OUT"
info "RLM output:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Search Memories (Tab 7)"
tui_switch_tab 7
sleep 2
# Navigate to Memories sub-tab
tui_send "Tab"; sleep 1
tui_send "Tab"; sleep 1
tui_send "r"
sleep 2
tui_assert_contains "Search panel" "Search"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

print_summary
