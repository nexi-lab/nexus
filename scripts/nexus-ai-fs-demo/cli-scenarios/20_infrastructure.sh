#!/bin/bash
# ============================================================================
# Scenario 20: Infrastructure — Cache, TLS, Context, MCP, Migrate, Network
# ============================================================================
# Commands: cache stats, cache warmup, cache hot, cache clear,
#           mcp export-tools,
#           context branches, context commit, context log, context diff,
#           tls init, tls show, tls trusted,
#           network status,
#           migrate status, migrate validate,
#           conflicts list,
#           llm --help (API key required for full run)
# TUI Tab: 4 (Zones → Bricks/Cache sub-tabs), 0 (Console)
#
# Story: Warm the cache, inspect hot files, export MCP tools, exercise
#        context branching, check TLS state, inspect migration status,
#        list conflicts, peek at network.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="20 — Infrastructure"
header "$SCENARIO_NAME"

WS="/workspace/scenario20"
nexus rm "$WS" --recursive --force 2>/dev/null || true
nexus mkdir "$WS" --parents 2>/dev/null || true
nexus write "$WS/file.md" "# Context test\nv1\n" 2>/dev/null || true

# ── 1. cache stats ──────────────────────────────────────────────────────
header "1. cache stats"
run_cli OUT nexus cache stats
assert_exit_code "cache stats" 0 "$OUT_RC"
info "Cache stats:"
echo "$OUT" | sed 's/^/    /'

# ── 2. cache warmup ─────────────────────────────────────────────────────
header "2. cache warmup"
run_cli OUT nexus cache warmup /workspace/demo/ --depth 2 --max-files 50
assert_or_infra_warn "cache warmup" "$OUT_RC" "$OUT"
info "Warmup: $OUT"

# ── 3. cache hot ─────────────────────────────────────────────────────────
header "3. cache hot"
run_cli OUT nexus cache hot --limit 10
assert_exit_code "cache hot" 0 "$OUT_RC"
info "Hot files:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 4. cache clear (metadata only) ──────────────────────────────────────
header "4. cache clear --metadata"
run_cli OUT nexus cache clear --metadata --yes
assert_exit_code "cache clear" 0 "$OUT_RC"

# ── 5. mcp export-tools ─────────────────────────────────────────────────
header "5. mcp export-tools"
run_cli OUT nexus mcp export-tools
assert_exit_code "mcp export" 0 "$OUT_RC"
assert_regex "json schema" "$OUT" '"name"|"tools"|"description"'
info "MCP tools (first 10 lines):"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 6. context branches ─────────────────────────────────────────────────
header "6. context branches"
run_cli OUT nexus context branches "$WS"
assert_or_infra_warn "context branches" "$OUT_RC" "$OUT"
info "Branches: $OUT"

# ── 7. context commit ───────────────────────────────────────────────────
header "7. context commit"
run_cli OUT nexus context commit "$WS" --message "initial snapshot"
assert_or_infra_warn "context commit" "$OUT_RC" "$OUT"
info "Commit: $OUT"

# ── 8. context branch + checkout ─────────────────────────────────────────
header "8. context branch"
run_cli OUT nexus context branch "$WS" --name "feature-20"
assert_or_infra_warn "context branch" "$OUT_RC" "$OUT"
info "Branch: $OUT"

run_cli OUT nexus context checkout "$WS" --target "feature-20"
assert_or_infra_warn "context checkout" "$OUT_RC" "$OUT"
info "Checkout: $OUT"

# ── 9. context log ──────────────────────────────────────────────────────
header "9. context log"
run_cli OUT nexus context log "$WS" --limit 10
assert_or_infra_warn "context log" "$OUT_RC" "$OUT"
info "Context log:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 10. tls show ─────────────────────────────────────────────────────────
header "10. tls show"
run_cli OUT nexus tls show 2>&1 || true
info "TLS:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 11. tls trusted ─────────────────────────────────────────────────────
header "11. tls trusted"
run_cli OUT nexus tls trusted 2>&1 || true
info "Trusted zones:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 12. network status ──────────────────────────────────────────────────
header "12. network status"
run_cli OUT nexus network status 2>&1 || true
info "Network:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 13. migrate status ──────────────────────────────────────────────────
header "13. migrate status"
run_cli OUT nexus migrate status
assert_exit_code "migrate status" 0 "$OUT_RC"
info "Migration status:"
echo "$OUT" | sed 's/^/    /'

# ── 14. migrate validate ────────────────────────────────────────────────
header "14. migrate validate"
run_cli OUT nexus migrate validate
assert_or_infra_warn "migrate validate" "$OUT_RC" "$OUT"
info "Validation:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 15. conflicts list ──────────────────────────────────────────────────
header "15. conflicts list"
run_cli OUT nexus conflicts list
assert_exit_code "conflicts list" 0 "$OUT_RC"
info "Conflicts:"
echo "$OUT" | head -5 | sed 's/^/    /'

# ── 16. llm --help ──────────────────────────────────────────────────────
header "16. llm --help"
run_cli OUT nexus llm --help
assert_or_infra_warn "llm help" "$OUT_RC" "$OUT"
if [ "$OUT_RC" -eq 0 ]; then
    # Help output may contain 'llm' or 'LLM' or model-related text
    assert_regex "llm help content" "$OUT" "[Ll][Ll][Mm]|model|completion|chat|Usage"
fi
info "LLM help:"
echo "$OUT" | head -5 | sed 's/^/    /'

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Console Panel (Tab 0)"
tui_switch_tab 0
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Console panel" "Console"
info "TUI snapshot:"
tui_capture | head -20 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
nexus rm "$WS" --recursive --force 2>/dev/null || true

print_summary
