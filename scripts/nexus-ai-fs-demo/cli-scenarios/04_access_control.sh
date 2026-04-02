#!/bin/bash
# ============================================================================
# Scenario 04: ReBAC & Access Manifests
# ============================================================================
# Commands: rebac create, rebac list, rebac check, rebac expand, rebac delete,
#           manifest create, manifest list, manifest show, manifest evaluate,
#           manifest revoke
# TUI Tab: 5 (Access — Manifests sub-tab)
#
# Story: Grant layered permissions (viewer → editor → owner), verify access,
#        create an access manifest for an agent, evaluate tool access, revoke.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="04 — ReBAC & Access Manifests"
header "$SCENARIO_NAME"

BASE="/workspace/scenario04"
nexus rm "$BASE" --recursive --force 2>/dev/null || true
nexus mkdir "$BASE" --parents 2>/dev/null || true
nexus write "$BASE/secret.md" "Top secret content" 2>/dev/null || true

# ── 1. rebac create — viewer ────────────────────────────────────────────
header "1. rebac create viewer"
run_cli OUT nexus rebac create user alice direct_viewer file "$BASE"
assert_exit_code "create viewer" 0 "$OUT_RC"
TUPLE_1=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)

# ── 2. rebac create — editor ────────────────────────────────────────────
header "2. rebac create editor"
run_cli OUT nexus rebac create agent scan_bot direct_editor file "$BASE"
assert_exit_code "create editor" 0 "$OUT_RC"
TUPLE_2=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)

# ── 3. rebac create — owner ─────────────────────────────────────────────
header "3. rebac create owner"
run_cli OUT nexus rebac create user bob direct_owner file "$BASE/secret.md"
assert_exit_code "create owner" 0 "$OUT_RC"
TUPLE_3=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)

# ── 4. rebac list ────────────────────────────────────────────────────────
header "4. rebac list"
run_cli OUT nexus rebac list --format compact
assert_exit_code "list all" 0 "$OUT_RC"
assert_contains "alice" "$OUT" "alice"
assert_contains "scan_bot" "$OUT" "scan_bot"
assert_contains "bob" "$OUT" "bob"

# ── 5. rebac list — filtered ────────────────────────────────────────────
header "5. rebac list filtered"
run_cli OUT nexus rebac list --subject-type user --subject-id alice --format compact
assert_exit_code "list filtered" 0 "$OUT_RC"
assert_contains "alice only" "$OUT" "alice"

# ── 6. rebac check ──────────────────────────────────────────────────────
header "6. rebac check"
run_cli OUT nexus rebac check user alice read file "$BASE"
assert_exit_code "check alice read" 0 "$OUT_RC"
assert_regex "alice granted" "$OUT" "GRANTED|true|allowed|True"

run_cli OUT nexus rebac check agent scan_bot write file "$BASE"
assert_exit_code "check scan_bot write" 0 "$OUT_RC"
assert_regex "scan_bot write" "$OUT" "GRANTED|true|allowed|True"

# ── 7. rebac expand ─────────────────────────────────────────────────────
header "7. rebac expand"
run_cli OUT nexus rebac expand direct_viewer file "$BASE"
assert_exit_code "expand" 0 "$OUT_RC"
assert_contains "alice in expansion" "$OUT" "alice"

# ── 8. manifest create — agent access manifest ──────────────────────────
header "8. manifest create"
nexus agent register "manifest_bot_04" "Manifest Bot" --if-not-exists 2>/dev/null || true
run_cli OUT nexus manifest create manifest_bot_04 \
    --name "bot-04-manifest" \
    --entry "read:$BASE/*" \
    --entry "write:$BASE/*" \
    --valid-hours 24
assert_or_infra_warn "manifest create" "$OUT_RC" "$OUT"
MANIFEST_ID=$(echo "$OUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)
info "Manifest ID: ${MANIFEST_ID:-<none>}"

# ── 9. manifest list ────────────────────────────────────────────────────
header "9. manifest list"
run_cli OUT nexus manifest list
assert_or_infra_warn "manifest list" "$OUT_RC" "$OUT"
info "Manifests:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 10. manifest show ───────────────────────────────────────────────────
header "10. manifest show"
if [ -n "${MANIFEST_ID:-}" ]; then
    run_cli OUT nexus manifest show "$MANIFEST_ID"
    assert_or_infra_warn "manifest show" "$OUT_RC" "$OUT"
    if [ "$OUT_RC" -eq 0 ]; then
        assert_contains "manifest details" "$OUT" "manifest_bot_04"
    fi
else
    warn "No manifest ID — skipping show"
fi

# ── 11. manifest evaluate — test tool access ─────────────────────────────
header "11. manifest evaluate"
if [ -n "${MANIFEST_ID:-}" ]; then
    run_cli OUT nexus manifest evaluate "$MANIFEST_ID" --tool-name "read"
    assert_or_infra_warn "manifest evaluate" "$OUT_RC" "$OUT"
    info "Evaluate: $OUT"
else
    warn "No manifest ID — skipping evaluate"
fi

# ── 12. manifest revoke ─────────────────────────────────────────────────
header "12. manifest revoke"
if [ -n "${MANIFEST_ID:-}" ]; then
    run_cli OUT nexus manifest revoke "$MANIFEST_ID"
    assert_or_infra_warn "manifest revoke" "$OUT_RC" "$OUT"
else
    warn "No manifest ID — skipping revoke"
fi

# ── 13. rebac delete ────────────────────────────────────────────────────
header "13. rebac delete"
for tid in "$TUPLE_1" "$TUPLE_2" "$TUPLE_3"; do
    [ -n "${tid:-}" ] && nexus rebac delete "$tid" 2>/dev/null || true
done
ok "Tuples cleaned up"

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
nexus agent delete "manifest_bot_04" --yes 2>/dev/null || true
nexus rm "$BASE" --recursive --force 2>/dev/null || true

print_summary
