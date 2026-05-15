#!/bin/bash
# ============================================================================
# Scenario 15: Mounts & Connectors
# ============================================================================
# Commands: mounts list, mounts add, mounts info, mounts remove,
#           connectors list, connectors info, connectors capabilities
# TUI Tab: Shift+C (Connectors)
#
# Story: List existing mounts, add a local-fs mount, inspect it, list
#        available connectors, check capabilities, remove the mount.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="15 — Mounts & Connectors"
header "$SCENARIO_NAME"

MOUNT_POINT="/mnt/scenario15"

# ── 1. mounts list ──────────────────────────────────────────────────────
header "1. mounts list"
run_cli OUT nexus mounts list
assert_exit_code "mounts list" 0 "$OUT_RC"
info "Current mounts:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 2. mounts add — local backend ───────────────────────────────────────
header "2. mounts add (local)"
run_cli OUT nexus mounts add "$MOUNT_POINT" local '{"root": "/tmp/nexus-mnt-15"}'
assert_exit_code "mounts add" 0 "$OUT_RC"
info "Add mount: $OUT"
MOUNT_ADDED=$OUT_RC

# ── 3. mounts list — verify ─────────────────────────────────────────────
header "3. mounts list (verify)"
run_cli OUT nexus mounts list
assert_exit_code "mounts list" 0 "$OUT_RC"
if [ "${MOUNT_ADDED:-1}" -eq 0 ]; then
    assert_contains "mount listed" "$OUT" "scenario15"
fi
info "Mounts:"
echo "$OUT" | sed 's/^/    /'

# ── 4. mounts info ──────────────────────────────────────────────────────
header "4. mounts info"
run_cli OUT nexus mounts info "$MOUNT_POINT"
assert_exit_code "mounts info" 0 "$OUT_RC"
info "Mount info:"
echo "$OUT" | sed 's/^/    /'

# ── 5. connectors list ──────────────────────────────────────────────────
header "5. connectors list"
run_cli OUT nexus connectors list
assert_exit_code "connectors list" 0 "$OUT_RC"
info "Connectors:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 6. connectors info ──────────────────────────────────────────────────
header "6. connectors info"
# Try to extract a connector name from JSON output
FIRST_CONN=$(echo "$OUT" | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    items=d.get('data',{}).get('connectors',d.get('data',[]))
    if isinstance(items,list) and items:
        print(items[0].get('name',items[0].get('type','')))
except: pass
" 2>/dev/null || true)
if [ -n "${FIRST_CONN:-}" ]; then
    run_cli OUT nexus connectors info "$FIRST_CONN"
    assert_exit_code "connectors info" 0 "$OUT_RC"
    info "Connector detail:"
    echo "$OUT" | head -10 | sed 's/^/    /'
else
    warn "No connectors found or could not parse name — skipping info"
    ok "connectors info — skipped (no connectors)"
fi

# ── 7. connectors capabilities ──────────────────────────────────────────
header "7. connectors capabilities"
run_cli OUT nexus connectors capabilities
assert_exit_code "capabilities" 0 "$OUT_RC"
info "Capabilities:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 8. mounts remove ────────────────────────────────────────────────────
header "8. mounts remove"
run_cli OUT nexus mounts remove "$MOUNT_POINT"
assert_exit_code "mounts remove" 0 "$OUT_RC"

run_cli OUT nexus mounts list
if [ "$OUT_RC" -eq 0 ]; then
    assert_not_contains "mount removed" "$OUT" "scenario15"
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Connectors Panel (Shift+C)"
tui_send "C"  # Shift+C
sleep 3
tui_send "r"
sleep 2
tui_assert_contains "Connectors panel" "Conn"
info "TUI snapshot:"
tui_capture | head -25 | sed 's/^/    | /'

print_summary
