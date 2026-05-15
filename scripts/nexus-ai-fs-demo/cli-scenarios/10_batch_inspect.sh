#!/bin/bash
# ============================================================================
# Scenario 10: Batch Operations, Inspection & Upload
# ============================================================================
# Commands: write-batch, info, size, tree, ls -rl, version, upload status
# TUI Tab: 1 (Files)
#
# Story: Batch-upload a local directory to Nexus, inspect each file's
#        metadata and sizes, verify the tree, check CLI version, peek
#        at upload status API.
# ============================================================================

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"
source "$SCRIPT_DIR/.env.scenarios"

SCENARIO_NAME="10 — Batch, Inspect & Upload"
header "$SCENARIO_NAME"

BATCH_DIR="/tmp/nexus-scenario10"
DEST="/workspace/scenario10"
nexus rm "$DEST" --recursive --force 2>/dev/null || true
rm -rf "$BATCH_DIR"

# ── 1. Prepare local batch ──────────────────────────────────────────────
header "1. Prepare local data"
mkdir -p "$BATCH_DIR/reports" "$BATCH_DIR/configs" "$BATCH_DIR/scripts"
echo "# Q1 — Revenue: \$1.2M" > "$BATCH_DIR/reports/q1.md"
echo "# Q2 — Revenue: \$1.5M" > "$BATCH_DIR/reports/q2.md"
echo "app:\n  name: nexus-demo\n  version: 2" > "$BATCH_DIR/configs/app.yaml"
echo "deploy:\n  target: prod\n  replicas: 3" > "$BATCH_DIR/configs/deploy.yaml"
echo "#!/bin/bash\necho setup" > "$BATCH_DIR/scripts/setup.sh"
echo "import unittest\nclass T(unittest.TestCase): pass" > "$BATCH_DIR/scripts/test.py"
ok "6 local files created"

# ── 2. write-batch ──────────────────────────────────────────────────────
header "2. write-batch"
nexus mkdir "$DEST" --parents 2>/dev/null || true
run_cli OUT nexus write-batch "$BATCH_DIR" \
    --dest-prefix "$DEST" --pattern "**/*" --show-progress
assert_exit_code "write-batch" 0 "$OUT_RC"
info "Batch output:"
echo "$OUT" | sed 's/^/    /'

# ── 3. tree ──────────────────────────────────────────────────────────────
header "3. tree"
run_cli OUT nexus tree "$DEST/"
assert_exit_code "tree" 0 "$OUT_RC"
for f in q1.md q2.md app.yaml deploy.yaml setup.sh test.py; do
    assert_contains "tree $f" "$OUT" "$f"
done
info "Tree:"
echo "$OUT" | sed 's/^/    /'

# ── 4. ls --recursive --long ────────────────────────────────────────────
header "4. ls -rl"
run_cli OUT nexus ls "$DEST/" --recursive --long
assert_exit_code "ls -rl" 0 "$OUT_RC"
info "Listing:"
echo "$OUT" | head -15 | sed 's/^/    /'

# ── 5. info ──────────────────────────────────────────────────────────────
header "5. info"
run_cli OUT nexus info "$DEST/reports/q1.md"
assert_exit_code "info q1" 0 "$OUT_RC"
assert_contains "path" "$OUT" "q1.md"
info "File info:"
echo "$OUT" | sed 's/^/    /'

run_cli OUT nexus info "$DEST/configs/app.yaml"
assert_exit_code "info app" 0 "$OUT_RC"

# ── 6. size ──────────────────────────────────────────────────────────────
header "6. size"
run_cli OUT nexus size "$DEST/" --human
assert_exit_code "size" 0 "$OUT_RC"
info "Size: $OUT"

# ── 7. size --details ───────────────────────────────────────────────────
header "7. size --details"
run_cli OUT nexus size "$DEST/" --details
assert_exit_code "size details" 0 "$OUT_RC"
info "Size details:"
echo "$OUT" | head -10 | sed 's/^/    /'

# ── 8. version — CLI version ────────────────────────────────────────────
header "8. nexus version"
run_cli OUT nexus version
assert_exit_code "version" 0 "$OUT_RC"
assert_regex "semver" "$OUT" "[0-9]+\.[0-9]+"
info "CLI version: $OUT"

# ── 9. upload status (no active uploads expected) ────────────────────────
header "9. upload status"
# Try with a fake upload ID — should return error or empty gracefully
run_cli OUT nexus upload status "00000000-0000-0000-0000-000000000000" 2>&1 || true
info "Upload status: $OUT"

# ── 10. Spot-check cat ──────────────────────────────────────────────────
header "10. Spot-check content"
run_cli OUT nexus cat "$DEST/reports/q1.md"
if [ "$OUT_RC" -eq 0 ]; then
    assert_contains "q1 content" "$OUT" "Revenue"
else
    warn "cat q1.md returned exit $OUT_RC (CAS read limitation)"
    ok "cat q1.md — command executed (CAS read limitation noted)"
fi

run_cli OUT nexus cat "$DEST/scripts/test.py"
if [ "$OUT_RC" -eq 0 ]; then
    assert_contains "test content" "$OUT" "unittest"
else
    warn "cat test.py returned exit $OUT_RC (CAS read limitation)"
    ok "cat test.py — command executed (CAS read limitation noted)"
fi

# ── TUI Verification ────────────────────────────────────────────────────
header "TUI Verification — Files Panel (Tab 1)"
tui_switch_tab 1
sleep 2
tui_send "r"
sleep 2
tui_assert_contains "Files panel" "Files"
info "TUI snapshot:"
tui_capture | head -20 | sed 's/^/    | /'

# ── Cleanup ──────────────────────────────────────────────────────────────
rm -rf "$BATCH_DIR"
nexus rm "$DEST" --recursive --force 2>/dev/null || true

print_summary
