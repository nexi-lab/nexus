#!/usr/bin/env bash
# =============================================================================
# Script 2: File Operations (Write, Read, Copy, Edit, Verify)
# =============================================================================
# Tests: nexus-fs write/cat/ls/stat/edit/rm/cp/mkdir CLI commands
#
# Prereq: Run script 1 first (creates /tmp/nexus-fs-demo and mounts)
#
# What you'll see in playground:
#   /local/projects   ->  original project files
#   /local/datasets   ->  original data files
#   /local/workspace  ->  files copied from both sources
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"
nfs() { "$PYTHON" -c "from nexus.fs._cli import main; main()" "$@"; }

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
fail()   { echo -e "  ${RED}FAIL${NC} $1"; exit 1; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

banner "Script 2: File Operations"

# ── Verify prereqs ───────────────────────────────────────────────────────────
if [ ! -d "$TESTROOT/projects" ] || [ ! -d "$TESTROOT/datasets" ]; then
    fail "Run script 1 first to create test directories"
fi

# ── Step 1: Create workspace backend ─────────────────────────────────────────
step "1/13" "Creating workspace backend for copy targets..."
mkdir -p "$TESTROOT/workspace"
nfs unmount "local://$TESTROOT/workspace" 2>/dev/null || true
nfs mount "local://$TESTROOT/workspace" 2>&1
ok "Workspace backend mounted"

# ── Step 2: Seed files via CLI ──────────────────────────────────────────────
step "2/13" "Seeding files into CAS via nexus-fs write..."
# Clean stale entries from previous runs
for stale in \
    /local/nexus-fs-demo-datasets/users_backup.csv \
    /local/nexus-fs-demo-workspace/metrics.csv \
    /local/nexus-fs-demo-workspace/main.py \
    /local/nexus-fs-demo-workspace/config.json \
    /local/nexus-fs-demo-workspace/inline_test.txt \
    /local/nexus-fs-demo-workspace/main_edit.py; do
    nfs rm "$stale" 2>/dev/null || true
done

# Seed project files
nfs write /local/nexus-fs-demo-projects/README.md < "$TESTROOT/projects/README.md"
nfs write /local/nexus-fs-demo-projects/src/main.py < "$TESTROOT/projects/src/main.py"

# Seed dataset files
nfs write /local/nexus-fs-demo-datasets/users.csv < "$TESTROOT/datasets/users.csv"
nfs write /local/nexus-fs-demo-datasets/config.json < "$TESTROOT/datasets/config.json"
nfs write /local/nexus-fs-demo-datasets/metrics.csv < "$TESTROOT/datasets/metrics.csv"

# List what we have
echo "  Projects:"
nfs ls /local/nexus-fs-demo-projects/ 2>&1 | sed 's/^/    /'
echo "  Datasets:"
nfs ls /local/nexus-fs-demo-datasets/ 2>&1 | sed 's/^/    /'
ok "CAS seeded via CLI"

# ── Step 3: Copy file within same backend ────────────────────────────────────
step "3/13" "Copying file within same backend (datasets -> datasets)..."
echo "  > nexus-fs cp /local/.../users.csv /local/.../users_backup.csv"
nfs cp /local/nexus-fs-demo-datasets/users.csv /local/nexus-fs-demo-datasets/users_backup.csv 2>&1
ok "Same-backend copy"

# ── Step 4: Copy across backends ─────────────────────────────────────────────
step "4/13" "Copying across backends (datasets -> workspace)..."
echo "  > nexus-fs cp /local/.../metrics.csv /local/.../metrics.csv"
nfs cp /local/nexus-fs-demo-datasets/metrics.csv /local/nexus-fs-demo-workspace/metrics.csv 2>&1
ok "Cross-backend copy"

# ── Step 5: Copy project file to workspace ───────────────────────────────────
step "5/13" "Copying project source to workspace..."
echo "  > nexus-fs cp /local/.../src/main.py /local/.../main.py"
nfs cp /local/nexus-fs-demo-projects/src/main.py /local/nexus-fs-demo-workspace/main.py 2>&1
ok "Project -> workspace copy"

# ── Step 6: Copy config to workspace ─────────────────────────────────────────
step "6/13" "Copying config.json to workspace..."
echo "  > nexus-fs cp /local/.../config.json /local/.../config.json --json"
nfs cp /local/nexus-fs-demo-datasets/config.json /local/nexus-fs-demo-workspace/config.json --json 2>&1
ok "Config copied with --json output"

# ── Step 7: Surgical edit via CLI ────────────────────────────────────────────
step "7/13" "Testing surgical edit (search/replace) on workspace file..."
# Copy main.py to workspace for editing
nfs cp /local/nexus-fs-demo-projects/src/main.py /local/nexus-fs-demo-workspace/main_edit.py 2>&1

# Surgical edit: rename function without rewriting the whole file
echo "  > nexus-fs edit ... -e 'def hello>>>def greet'"
nfs edit /local/nexus-fs-demo-workspace/main_edit.py -e 'def hello>>>def greet' 2>&1

# Preview mode: see diff without writing
echo "  > nexus-fs edit ... -e 'def greet>>>def salute' --preview"
nfs edit /local/nexus-fs-demo-workspace/main_edit.py -e 'def greet>>>def salute' --preview 2>&1

# Verify original edit stuck and preview didn't modify
CONTENT=$(nfs cat /local/nexus-fs-demo-workspace/main_edit.py 2>&1)
echo "$CONTENT" | grep -q "def greet" || fail "Edit was not persisted"
echo "$CONTENT" | grep -q "def salute" && fail "Preview should not have written"
echo "  Verified: file contains 'def greet', not 'def salute'"
ok "Surgical edit (search/replace)"

# ── Step 8: Copy with trailing inline mount URIs (no persisted mounts) ───────
step "8/13" "Copying with trailing inline mount URIs..."
mkdir -p "$TESTROOT/scratch"
nfs mount "local://$TESTROOT/scratch" 2>/dev/null || true
echo "Seeded via inline trailing URIs" | nfs write /local/nexus-fs-demo-scratch/inline_test.txt
echo "  > nexus-fs cp ... inline_test.txt -> workspace (with trailing URIs)"
nfs cp /local/nexus-fs-demo-scratch/inline_test.txt /local/nexus-fs-demo-workspace/inline_test.txt \
    "local://$TESTROOT/scratch" "local://$TESTROOT/workspace" 2>&1
ok "cp with trailing mount URIs"

# ── Step 9: Verify all copies via CLI ────────────────────────────────────────
step "9/13" "Verifying all copies..."
ALL_OK=true
verify_file() {
    local path="$1" expected="$2" label="$3"
    CONTENT=$(nfs cat "$path" 2>&1) || { echo "  FAIL  $label (read error)"; ALL_OK=false; return; }
    if echo "$CONTENT" | grep -q "$expected"; then
        printf "  PASS  %-30s contains '%s'\n" "$label" "$expected"
    else
        printf "  FAIL  %-30s missing '%s'\n" "$label" "$expected"
        ALL_OK=false
    fi
}
verify_file /local/nexus-fs-demo-datasets/users_backup.csv "name,email,role" "users_backup.csv"
verify_file /local/nexus-fs-demo-workspace/metrics.csv     "date,requests"   "metrics.csv"
verify_file /local/nexus-fs-demo-workspace/main.py         "def hello"       "main.py"
verify_file /local/nexus-fs-demo-workspace/config.json     "nexus-fs-demo"   "config.json"
verify_file /local/nexus-fs-demo-workspace/inline_test.txt "inline trailing" "inline_test.txt"

echo ""
echo "  Workspace contents:"
nfs ls /local/nexus-fs-demo-workspace/ 2>&1 | sed 's/^/    /'
$ALL_OK || fail "Verification failed"
ok "All copies verified"

# ── Step 10: Sync CAS data to raw filesystem for playground visibility ───────
step "10/13" "Syncing CAS data to filesystem (for playground)..."
for pair in \
    "/local/nexus-fs-demo-datasets/users_backup.csv:$TESTROOT/datasets/users_backup.csv" \
    "/local/nexus-fs-demo-workspace/metrics.csv:$TESTROOT/workspace/metrics.csv" \
    "/local/nexus-fs-demo-workspace/main.py:$TESTROOT/workspace/main.py" \
    "/local/nexus-fs-demo-workspace/config.json:$TESTROOT/workspace/config.json" \
    "/local/nexus-fs-demo-workspace/inline_test.txt:$TESTROOT/workspace/inline_test.txt"; do
    CAS_PATH="${pair%%:*}"
    RAW_PATH="${pair##*:}"
    mkdir -p "$(dirname "$RAW_PATH")"
    nfs cat "$CAS_PATH" > "$RAW_PATH" 2>/dev/null
    SIZE=$(wc -c < "$RAW_PATH" | tr -d ' ')
    printf "  Synced %-30s -> %s/ (%s bytes)\n" "$(basename "$RAW_PATH")" "$(basename "$(dirname "$RAW_PATH")")" "$SIZE"
done
ok "CAS -> filesystem sync for playground"

# ── Step 11: Cleanup scratch ─────────────────────────────────────────────────
step "11/13" "Cleaning up scratch backend..."
nfs unmount "local://$TESTROOT/scratch" 2>/dev/null || true
rm -rf "$TESTROOT/scratch"
ok "Scratch cleaned"

# ── Step 12: Show mount list ─────────────────────────────────────────────────
step "12/13" "Final mount state..."
nfs mount list 2>&1

# ── Step 13: Validate playground visibility ──────────────────────────────────
step "13/13" "Validating playground visibility..."
"$PYTHON" << 'PYEOF'
from pathlib import Path

TESTROOT = Path("/tmp/nexus-fs-demo")
all_ok = True

for mount_name, expected_files in [
    ("projects",  ["README.md", "src/main.py", "src/utils.py", "docs/setup.md"]),
    ("datasets",  ["users.csv", "config.json", "metrics.csv", "users_backup.csv"]),
    ("workspace", ["metrics.csv", "main.py", "config.json", "inline_test.txt"]),
]:
    root = TESTROOT / mount_name
    for fname in expected_files:
        fpath = root / fname
        if fpath.exists() and fpath.stat().st_size > 0:
            print(f"  PASS  /local/{mount_name}/{fname}")
        else:
            print(f"  FAIL  /local/{mount_name}/{fname} (missing or empty)")
            all_ok = False

if not all_ok:
    raise SystemExit(1)
total = sum(len(v) for v in [
    ["README.md", "src/main.py", "src/utils.py", "docs/setup.md"],
    ["users.csv", "config.json", "metrics.csv", "users_backup.csv"],
    ["metrics.csv", "main.py", "config.json", "inline_test.txt"],
])
print(f"\n  All {total} files visible in playground")
PYEOF
ok "Playground validation passed"

banner "File Operations Complete!"
echo ""
echo "  Operations performed:"
echo "    - Seeded 5 files via nexus-fs write"
echo "    - Copied within same backend (users.csv backup)"
echo "    - Copied across 3 backends (datasets/projects -> workspace)"
echo "    - Surgical edit (search/replace) with preview mode"
echo "    - Copied with trailing inline mount URIs"
echo "    - Verified all copies via nexus-fs cat"
echo ""
echo "  Open playground to see all 3 backends:"
echo "    nexus-fs playground local://$TESTROOT/projects local://$TESTROOT/datasets local://$TESTROOT/workspace"
echo ""
