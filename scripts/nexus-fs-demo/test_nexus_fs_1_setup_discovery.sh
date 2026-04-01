#!/usr/bin/env bash
# =============================================================================
# Script 1: Setup & Discovery
# =============================================================================
# Tests: --version, --help, mount, mount --at, mount list, mount test,
#        doctor, doctor --mount (multi), playground --help
#
# What you'll see in playground:
#   /local/projects  ->  sample project files (README, src/, docs/)
#   /local/datasets  ->  sample data files (CSV, JSON)
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"
nexus_fs() { "$PYTHON" -c "from nexus.fs._cli import main; main()" -- "$@"; }

# ── Colors ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

banner "Script 1: Setup & Discovery"

# ── Step 0a: --version ───────────────────────────────────────────────────────
step "0a/11" "Testing --version..."
echo "  > nexus-fs --version"
"$PYTHON" -c "
import importlib.metadata as _m
print(f'nexus-fs, version {_m.version(\"nexus-ai-fs\")}')
" 2>&1
ok "--version"

# ── Step 0b: --help ──────────────────────────────────────────────────────────
step "0b/11" "Testing --help..."
echo "  > nexus-fs --help"
"$PYTHON" -c "from nexus.fs._cli import main; main(['--help'])" 2>&1
ok "--help"

# ── Step 0c: playground --help (verify TUI does not launch) ──────────────────
step "0c/11" "Testing playground --help (non-interactive)..."
echo "  > nexus-fs playground --help"
"$PYTHON" -c "from nexus.fs._cli import main; main(['playground', '--help'])" 2>&1
ok "playground --help"

# ── Step 1: Clean slate ──────────────────────────────────────────────────────
step "1/11" "Cleaning previous test state..."
rm -rf "$TESTROOT"
# Remove any leftover mounts from previous runs
for uri in "local://$TESTROOT/projects" "local://$TESTROOT/datasets" "local://$TESTROOT/custom"; do
    "$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', '$uri'])" 2>/dev/null || true
done
ok "Clean slate"

# ── Step 2: Create sample project directory ──────────────────────────────────
step "2/11" "Creating sample project directory..."
mkdir -p "$TESTROOT/projects/src" "$TESTROOT/projects/docs"
cat > "$TESTROOT/projects/README.md" << 'EOF'
# Demo Project
This is a sample project for testing nexus-fs CLI.
EOF
cat > "$TESTROOT/projects/src/main.py" << 'EOF'
def hello():
    return "Hello from nexus-fs!"

if __name__ == "__main__":
    print(hello())
EOF
cat > "$TESTROOT/projects/src/utils.py" << 'EOF'
import json

def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
EOF
cat > "$TESTROOT/projects/docs/setup.md" << 'EOF'
# Setup Guide
1. Install dependencies
2. Run `nexus-fs mount local://./projects`
3. Open playground
EOF
ok "Created project tree at $TESTROOT/projects"
echo "  $(find "$TESTROOT/projects" -type f | wc -l | tr -d ' ') files created"

# ── Step 3: Create sample datasets directory ─────────────────────────────────
step "3/11" "Creating sample datasets directory..."
mkdir -p "$TESTROOT/datasets"
cat > "$TESTROOT/datasets/users.csv" << 'EOF'
name,email,role
Alice,alice@example.com,admin
Bob,bob@example.com,developer
Charlie,charlie@example.com,analyst
Diana,diana@example.com,developer
EOF
cat > "$TESTROOT/datasets/config.json" << 'EOF'
{
  "app_name": "nexus-fs-demo",
  "version": "1.0.0",
  "features": {
    "playground": true,
    "copy": true,
    "auth": true
  }
}
EOF
cat > "$TESTROOT/datasets/metrics.csv" << 'EOF'
date,requests,latency_ms,errors
2026-03-01,15200,42,3
2026-03-02,18400,38,1
2026-03-03,22100,45,7
2026-03-04,19800,41,2
2026-03-05,25600,39,0
EOF
ok "Created datasets at $TESTROOT/datasets"
echo "  $(find "$TESTROOT/datasets" -type f | wc -l | tr -d ' ') files created"

# ── Step 4: Mount backends ───────────────────────────────────────────────────
step "4/11" "Mounting local backends..."
echo "  > nexus-fs mount local://$TESTROOT/projects"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/projects'])" 2>&1
echo ""
echo "  > nexus-fs mount local://$TESTROOT/datasets"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/datasets'])" 2>&1
ok "Both backends mounted"

# ── Step 5: Mount with --at (custom mount point) ─────────────────────────────
step "5/11" "Mounting with custom mount point (--at)..."
mkdir -p "$TESTROOT/custom"
echo "custom mount content" > "$TESTROOT/custom/readme.txt"
echo "  > nexus-fs mount local://$TESTROOT/custom --at /my/custom/path"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/custom', '--at', '/my/custom/path'])" 2>&1
ok "Custom mount point registered"

# ── Step 6: List persisted mounts ────────────────────────────────────────────
step "6/11" "Listing persisted mounts..."
echo "  > nexus-fs mount list"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1
ok "Mount list retrieved (includes --at mount)"

# ── Step 7: Run doctor diagnostics ───────────────────────────────────────────
step "7/11" "Running doctor diagnostics..."
echo "  > nexus-fs doctor --mount local://$TESTROOT/projects"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--mount', 'local://$TESTROOT/projects'])" 2>&1
ok "Doctor completed"

# ── Step 8: Doctor with multiple --mount flags ───────────────────────────────
step "8/11" "Running doctor with multiple --mount flags..."
echo "  > nexus-fs doctor --mount local://$TESTROOT/projects --mount local://$TESTROOT/datasets"
"$PYTHON" -c "from nexus.fs._cli import main; main(['doctor', '--mount', 'local://$TESTROOT/projects', '--mount', 'local://$TESTROOT/datasets'])" 2>&1
ok "Doctor with multi-mount"

# ── Step 9: Test mount connectivity ──────────────────────────────────────────
step "9/11" "Testing mount connectivity..."
echo "  > nexus-fs mount test local://$TESTROOT/datasets"
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'test', 'local://$TESTROOT/datasets'])" 2>&1
ok "Mount test passed"

# ── Step 10: Unmount custom mount (cleanup for later scripts) ────────────────
step "10/11" "Unmounting custom mount point..."
echo "  > nexus-fs unmount local://$TESTROOT/custom"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/custom'])" 2>&1
ok "Custom mount removed"

# ── Step 11: Verify final mount list ─────────────────────────────────────────
step "11/11" "Final mount list..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1
ok "Only projects + datasets remain"

# ── Summary ──────────────────────────────────────────────────────────────────
banner "Setup Complete!"
echo ""
echo "  Mounts registered:"
echo "    local://$TESTROOT/projects  -> /local/projects"
echo "    local://$TESTROOT/datasets  -> /local/datasets"
echo ""
echo "  Open playground to browse:"
echo "    nexus-fs playground local://$TESTROOT/projects local://$TESTROOT/datasets"
echo ""
