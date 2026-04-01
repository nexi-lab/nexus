#!/usr/bin/env bash
# =============================================================================
# Script 2: File Operations (Write, Read, Copy, Verify)
# =============================================================================
# Tests: Python API write/read, nexus-fs cp, cross-directory copy, --json,
#        cp with trailing mount URIs (inline mounts)
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
nexus_fs() { "$PYTHON" -c "from nexus.fs._cli import main; main()" -- "$@"; }

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
step "1/10" "Creating workspace backend for copy targets..."
mkdir -p "$TESTROOT/workspace"
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/workspace'])" 2>/dev/null || true
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/workspace'])" 2>&1
ok "Workspace backend mounted"

# ── Step 2: Seed files via NexusFS API ───────────────────────────────────────
step "2/10" "Seeding files into CAS via NexusFS API..."
"$PYTHON" << PYEOF
import asyncio, contextlib
from nexus.fs import mount

async def seed():
    fs = await mount(
        'local://$TESTROOT/projects',
        'local://$TESTROOT/datasets',
        'local://$TESTROOT/workspace',
    )

    # Clean stale CAS entries from previous runs
    for stale in [
        '/local/nexus-fs-demo-datasets/users_backup.csv',
        '/local/nexus-fs-demo-workspace/metrics.csv',
        '/local/nexus-fs-demo-workspace/main.py',
        '/local/nexus-fs-demo-workspace/config.json',
        '/local/nexus-fs-demo-workspace/inline_test.txt',
    ]:
        with contextlib.suppress(Exception):
            await fs.delete(stale)

    # Seed project files into CAS
    await fs.write('/local/nexus-fs-demo-projects/README.md', open('$TESTROOT/projects/README.md', 'rb').read())
    await fs.write('/local/nexus-fs-demo-projects/src/main.py', open('$TESTROOT/projects/src/main.py', 'rb').read())

    # Seed dataset files into CAS
    await fs.write('/local/nexus-fs-demo-datasets/users.csv', open('$TESTROOT/datasets/users.csv', 'rb').read())
    await fs.write('/local/nexus-fs-demo-datasets/config.json', open('$TESTROOT/datasets/config.json', 'rb').read())
    await fs.write('/local/nexus-fs-demo-datasets/metrics.csv', open('$TESTROOT/datasets/metrics.csv', 'rb').read())

    # List what we have
    proj_files = await fs.ls('/local/nexus-fs-demo-projects/')
    data_files = await fs.ls('/local/nexus-fs-demo-datasets/')
    print(f"  Seeded {len(proj_files)} project files: {[f.split('/')[-1] for f in proj_files]}")
    print(f"  Seeded {len(data_files)} dataset files: {[f.split('/')[-1] for f in data_files]}")

    await fs.close()

asyncio.run(seed())
PYEOF
ok "CAS seeded"

# ── Step 3: Copy file within same backend ────────────────────────────────────
step "3/10" "Copying file within same backend (datasets -> datasets)..."
echo "  > nexus-fs cp /local/.../users.csv /local/.../users_backup.csv"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-datasets/users.csv', '/local/nexus-fs-demo-datasets/users_backup.csv'])
" 2>&1
ok "Same-backend copy"

# ── Step 4: Copy across backends ─────────────────────────────────────────────
step "4/10" "Copying across backends (datasets -> workspace)..."
echo "  > nexus-fs cp /local/.../metrics.csv /local/.../metrics.csv"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-datasets/metrics.csv', '/local/nexus-fs-demo-workspace/metrics.csv'])
" 2>&1
ok "Cross-backend copy"

# ── Step 5: Copy project file to workspace ───────────────────────────────────
step "5/10" "Copying project source to workspace..."
echo "  > nexus-fs cp /local/.../src/main.py /local/.../main.py"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-projects/src/main.py', '/local/nexus-fs-demo-workspace/main.py'])
" 2>&1
ok "Project -> workspace copy"

# ── Step 6: Copy config to workspace ─────────────────────────────────────────
step "6/10" "Copying config.json to workspace..."
echo "  > nexus-fs cp /local/.../config.json /local/.../config.json --json"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-datasets/config.json', '/local/nexus-fs-demo-workspace/config.json', '--json'])
" 2>&1
ok "Config copied with --json output"

# ── Step 7: Copy with trailing inline mount URIs (no persisted mounts) ───────
step "7/10" "Copying with trailing inline mount URIs..."
mkdir -p "$TESTROOT/scratch"
"$PYTHON" << PYEOF
import asyncio
from nexus.fs import mount
async def go():
    fs = await mount('local://$TESTROOT/workspace', 'local://$TESTROOT/scratch')
    # Seed a file in scratch via kernel for the next cp
    await fs.write('/local/nexus-fs-demo-scratch/inline_test.txt', b'Copied via inline trailing URIs')
    await fs.close()
asyncio.run(go())
PYEOF
echo "  > nexus-fs cp ... inline_test.txt -> workspace (with trailing URIs)"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-scratch/inline_test.txt', '/local/nexus-fs-demo-workspace/inline_test.txt',
      'local://$TESTROOT/scratch', 'local://$TESTROOT/workspace'])
" 2>&1
ok "cp with trailing mount URIs"

# ── Step 8: Verify all copies via API ────────────────────────────────────────
step "8/10" "Verifying all copies..."
"$PYTHON" << 'PYEOF'
import asyncio
from nexus.fs import mount

async def verify():
    fs = await mount(
        'local:///tmp/nexus-fs-demo/projects',
        'local:///tmp/nexus-fs-demo/datasets',
        'local:///tmp/nexus-fs-demo/workspace',
        'local:///tmp/nexus-fs-demo/scratch',
    )

    checks = [
        ("/local/nexus-fs-demo-datasets/users_backup.csv", "name,email,role"),
        ("/local/nexus-fs-demo-workspace/metrics.csv", "date,requests"),
        ("/local/nexus-fs-demo-workspace/main.py", "def hello"),
        ("/local/nexus-fs-demo-workspace/config.json", "nexus-fs-demo"),
        ("/local/nexus-fs-demo-workspace/inline_test.txt", "inline trailing URIs"),
    ]

    all_ok = True
    for path, expected_substr in checks:
        try:
            content = await fs.read(path)
            text = content.decode()
            if expected_substr in text:
                print(f"  PASS  {path.split('/')[-1]:30s} contains '{expected_substr}'")
            else:
                print(f"  FAIL  {path.split('/')[-1]:30s} missing '{expected_substr}'")
                all_ok = False
        except Exception as e:
            print(f"  FAIL  {path.split('/')[-1]:30s} error: {e}")
            all_ok = False

    # Show workspace contents
    ws_files = await fs.ls('/local/nexus-fs-demo-workspace/')
    print(f"\n  Workspace files: {[f.split('/')[-1] for f in ws_files]}")

    await fs.close()
    return all_ok

ok = asyncio.run(verify())
if not ok:
    raise SystemExit(1)
PYEOF
ok "All copies verified"

# ── Step 9: Sync CAS data to raw filesystem for playground visibility ────────
step "9/12" "Syncing CAS data to filesystem (for playground)..."
"$PYTHON" << 'PYEOF'
import asyncio
from pathlib import Path
from nexus.fs import mount

TESTROOT = Path("/tmp/nexus-fs-demo")

async def sync_to_playground():
    fs = await mount(
        'local:///tmp/nexus-fs-demo/projects',
        'local:///tmp/nexus-fs-demo/datasets',
        'local:///tmp/nexus-fs-demo/workspace',
    )

    # Export CAS files to raw directories so playground can see them
    exports = [
        ("/local/nexus-fs-demo-datasets/users_backup.csv", TESTROOT / "datasets" / "users_backup.csv"),
        ("/local/nexus-fs-demo-workspace/metrics.csv",     TESTROOT / "workspace" / "metrics.csv"),
        ("/local/nexus-fs-demo-workspace/main.py",         TESTROOT / "workspace" / "main.py"),
        ("/local/nexus-fs-demo-workspace/config.json",     TESTROOT / "workspace" / "config.json"),
        ("/local/nexus-fs-demo-workspace/inline_test.txt", TESTROOT / "workspace" / "inline_test.txt"),
    ]

    for cas_path, raw_path in exports:
        content = await fs.read(cas_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(content)
        print(f"  Synced {raw_path.name:30s} -> {raw_path.parent.name}/ ({len(content)} bytes)")

    await fs.close()

asyncio.run(sync_to_playground())
PYEOF
ok "CAS -> filesystem sync for playground"

# ── Step 10: Cleanup scratch ─────────────────────────────────────────────────
step "10/12" "Cleaning up scratch backend..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/scratch'])" 2>/dev/null || true
rm -rf "$TESTROOT/scratch"
ok "Scratch cleaned"

# ── Step 11: Show mount list ─────────────────────────────────────────────────
step "11/12" "Final mount state..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1

# ── Step 12: Validate playground visibility ──────────────────────────────────
step "12/12" "Validating playground visibility..."
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
print(f"\n  All {sum(len(v) for v in [['README.md','src/main.py','src/utils.py','docs/setup.md'],['users.csv','config.json','metrics.csv','users_backup.csv'],['metrics.csv','main.py','config.json','inline_test.txt']])} files visible in playground")
PYEOF
ok "Playground validation passed"

banner "File Operations Complete!"
echo ""
echo "  Operations performed:"
echo "    - Seeded 5 files via API"
echo "    - Copied within same backend (users.csv backup)"
echo "    - Copied across 3 backends (datasets/projects -> workspace)"
echo "    - Copied with trailing inline mount URIs"
echo "    - Verified all copies match"
echo ""
echo "  Open playground to see all 3 backends:"
echo "    nexus-fs playground local://$TESTROOT/projects local://$TESTROOT/datasets local://$TESTROOT/workspace"
echo ""
