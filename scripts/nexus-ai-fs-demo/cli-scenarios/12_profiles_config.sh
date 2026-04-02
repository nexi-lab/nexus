#!/bin/bash
# ============================================================================
# Scenario 12: Profiles & Configuration
# ============================================================================
# Commands: config show, config set, config get, config reset,
#           profile list, profile add, profile show, profile rename,
#           profile use, profile delete, connect
# TUI Tab: N/A (CLI-only management)
#
# Story: Inspect current config, add a test profile, rename it, switch
#        between profiles, clean up. Also exercise config set/get/reset.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="12 — Profiles & Config"
header "$SCENARIO_NAME"

# ── 1. config show ──────────────────────────────────────────────────────
header "1. config show"
run_cli OUT nexus config show
assert_exit_code "config show" 0 "$OUT_RC"
info "Config:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 2. config set ───────────────────────────────────────────────────────
header "2. config set"
run_cli OUT nexus config set default_zone "test_zone_12"
assert_exit_code "config set" 0 "$OUT_RC"

# ── 3. config get ────────────────────────────────────────────────────────
header "3. config get"
run_cli OUT nexus config get default_zone
assert_exit_code "config get" 0 "$OUT_RC"
if [ "$OUT_RC" -eq 0 ]; then
    assert_contains "value" "$OUT" "test_zone_12"
fi

# ── 4. config reset ─────────────────────────────────────────────────────
header "4. config reset"
run_cli OUT nexus config reset default_zone
assert_exit_code "config reset" 0 "$OUT_RC"

# ── 5. profile list ─────────────────────────────────────────────────────
header "5. profile list"
run_cli OUT nexus profile list
assert_exit_code "profile list" 0 "$OUT_RC"
info "Profiles:"
echo "$OUT" | sed 's/^/    /'

# ── 6. profile add ──────────────────────────────────────────────────────
header "6. profile add"
run_cli OUT nexus profile add test_profile_12 \
    --url "http://localhost:9999" --api-key "sk-fake12" --no-use
assert_exit_code "profile add" 0 "$OUT_RC"

# ── 7. profile show ─────────────────────────────────────────────────────
header "7. profile show"
run_cli OUT nexus profile show
assert_exit_code "profile show" 0 "$OUT_RC"
info "Current profile:"
echo "$OUT" | sed 's/^/    /'

# ── 8. profile rename ───────────────────────────────────────────────────
header "8. profile rename"
run_cli OUT nexus profile rename test_profile_12 renamed_profile_12
assert_exit_code "rename" 0 "$OUT_RC"

run_cli OUT nexus profile list
assert_contains "renamed" "$OUT" "renamed_profile_12"
assert_not_contains "old name gone" "$OUT" "test_profile_12"

# ── 9. profile use ──────────────────────────────────────────────────────
header "9. profile use (switch then switch back)"
# Remember current profile name
CURRENT=$(nexus profile show 2>/dev/null | grep -oP '(?<=Profile: )\S+' || echo "default")

run_cli OUT nexus profile use renamed_profile_12
assert_exit_code "use renamed" 0 "$OUT_RC"

# Switch back to the original profile
nexus profile use "$CURRENT" 2>/dev/null || nexus profile use default 2>/dev/null || true
ok "Switched back to original profile"

# ── 10. profile delete ──────────────────────────────────────────────────
header "10. profile delete"
run_cli OUT nexus profile delete renamed_profile_12 --force
assert_exit_code "delete" 0 "$OUT_RC"

run_cli OUT nexus profile list
assert_not_contains "deleted" "$OUT" "renamed_profile_12"

# ── 11. connect (non-interactive check) ──────────────────────────────────
header "11. connect"
# connect is typically interactive; just verify it starts and prints help
run_cli OUT nexus connect --help 2>&1 || true
assert_contains "connect help" "$OUT" "connect"

# ── Summary ──────────────────────────────────────────────────────────────
print_summary
