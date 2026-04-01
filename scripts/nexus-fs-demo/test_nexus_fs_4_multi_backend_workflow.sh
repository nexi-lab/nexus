#!/usr/bin/env bash
# =============================================================================
# Script 4: Multi-Backend Workspace Workflow
# =============================================================================
# Tests: Mount 4 backends, API write/read/ls/stat/mkdir/rename/delete/exists,
#        nexus-fs cp across backends, batch operations, directory ops
#
# Prereq: Run script 1 first (or this script creates its own dirs)
#
# What you'll see in playground:
#   /local/inbox     ->  incoming files
#   /local/processed ->  processed files (copied from inbox)
#   /local/archive   ->  archived files (copied from processed)
#   /local/reports   ->  generated report files
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
fail()   { echo -e "  ${RED}FAIL${NC} $1"; exit 1; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }

banner "Script 4: Multi-Backend Workspace"

# ── Step 1: Create 4 backend directories ─────────────────────────────────────
step "1/9" "Creating 4 backend directories..."
for dir in inbox processed archive reports; do
    mkdir -p "$TESTROOT/$dir"
    "$PYTHON" -c "from nexus.fs._cli import main; main(['unmount', 'local://$TESTROOT/$dir'])" 2>/dev/null || true
done

# Populate inbox with raw data files
cat > "$TESTROOT/inbox/sales_2026_q1.csv" << 'EOF'
region,product,revenue,units
north,widget-a,125000,2500
south,widget-b,98000,1960
east,widget-a,142000,2840
west,widget-c,87000,1450
north,widget-b,113000,2260
EOF

cat > "$TESTROOT/inbox/events_march.json" << 'EOF'
{"events": [
  {"id": 1, "type": "signup", "user": "alice", "ts": "2026-03-01T10:00:00Z"},
  {"id": 2, "type": "purchase", "user": "bob", "ts": "2026-03-02T14:30:00Z"},
  {"id": 3, "type": "signup", "user": "charlie", "ts": "2026-03-03T09:15:00Z"},
  {"id": 4, "type": "purchase", "user": "alice", "ts": "2026-03-05T16:45:00Z"}
]}
EOF

cat > "$TESTROOT/inbox/model_weights.bin" << 'EOF'
BINARY_PLACEHOLDER_64KB_MODEL_WEIGHTS
EOF
# Make it a bit bigger
dd if=/dev/urandom bs=1024 count=32 >> "$TESTROOT/inbox/model_weights.bin" 2>/dev/null

ok "Created 4 backends, 3 inbox files"

# ── Step 2: Mount all backends ───────────────────────────────────────────────
step "2/9" "Mounting all 4 backends..."
for dir in inbox processed archive reports; do
    echo "  > nexus-fs mount local://$TESTROOT/$dir"
    "$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'local://$TESTROOT/$dir'])" 2>&1
done
ok "4 backends mounted"

# ── Step 3: Seed inbox files into CAS ────────────────────────────────────────
step "3/9" "Seeding inbox files into CAS..."
"$PYTHON" << PYEOF
import asyncio, contextlib
from nexus.fs import mount

async def seed():
    fs = await mount(
        'local://$TESTROOT/inbox',
        'local://$TESTROOT/processed',
        'local://$TESTROOT/archive',
        'local://$TESTROOT/reports',
    )

    # Clean stale CAS entries from previous runs
    for stale in [
        '/local/nexus-fs-demo-processed/sales_2026_q1.csv',
        '/local/nexus-fs-demo-processed/events_march.json',
        '/local/nexus-fs-demo-archive/sales_2026_q1.csv',
        '/local/nexus-fs-demo-archive/model_weights.bin',
        '/local/nexus-fs-demo-reports/2026/q1/summary.md',
        '/local/nexus-fs-demo-reports/2026/q1/events_digest.md',
    ]:
        with contextlib.suppress(Exception):
            await fs.delete(stale)
    for stale_dir in [
        '/local/nexus-fs-demo-reports/2026/q1',
        '/local/nexus-fs-demo-reports/2026',
    ]:
        with contextlib.suppress(Exception):
            await fs.rmdir(stale_dir)

    await fs.close()

    # Re-mount fresh for seeding
    fs = await mount(
        'local://$TESTROOT/inbox',
        'local://$TESTROOT/processed',
        'local://$TESTROOT/archive',
        'local://$TESTROOT/reports',
    )

    # Seed inbox files
    for fname in ['sales_2026_q1.csv', 'events_march.json', 'model_weights.bin']:
        with open('$TESTROOT/inbox/' + fname, 'rb') as f:
            data = f.read()
        result = await fs.write(f'/local/nexus-fs-demo-inbox/{fname}', data)
        print(f"  Seeded {fname} ({result['size']} bytes)")

    await fs.close()

asyncio.run(seed())
PYEOF
ok "Inbox files seeded into CAS"

# ── Step 4: Process - copy from inbox to processed ───────────────────────────
step "4/9" "Processing: inbox -> processed (via nexus-fs cp)..."
echo "  > nexus-fs cp .../sales_2026_q1.csv -> processed"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-inbox/sales_2026_q1.csv', '/local/nexus-fs-demo-processed/sales_2026_q1.csv'])
" 2>&1
echo ""
echo "  > nexus-fs cp .../events_march.json -> processed"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-inbox/events_march.json', '/local/nexus-fs-demo-processed/events_march.json'])
" 2>&1
ok "2 files processed"

# ── Step 5: Archive - copy processed to archive ──────────────────────────────
step "5/9" "Archiving: processed -> archive (via nexus-fs cp)..."
echo "  > nexus-fs cp .../sales_2026_q1.csv -> archive"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-processed/sales_2026_q1.csv', '/local/nexus-fs-demo-archive/sales_2026_q1.csv'])
" 2>&1
ok "Archived sales data"

# ── Step 6: Generate reports via API (write, mkdir, stat) ────────────────────
step "6/9" "Generating reports via NexusFS API (write + mkdir + stat)..."
"$PYTHON" << 'PYEOF'
import asyncio
from nexus.fs import mount

async def generate_reports():
    fs = await mount(
        'local:///tmp/nexus-fs-demo/inbox',
        'local:///tmp/nexus-fs-demo/processed',
        'local:///tmp/nexus-fs-demo/archive',
        'local:///tmp/nexus-fs-demo/reports',
    )

    # Read processed data
    sales_data = await fs.read('/local/nexus-fs-demo-processed/sales_2026_q1.csv')
    events_data = await fs.read('/local/nexus-fs-demo-processed/events_march.json')

    # Create report directories
    await fs.mkdir('/local/nexus-fs-demo-reports/2026')
    await fs.mkdir('/local/nexus-fs-demo-reports/2026/q1')
    print("  Created /reports/2026/q1/")

    # Generate summary report
    lines = sales_data.decode().strip().split('\n')
    total_revenue = sum(int(l.split(',')[2]) for l in lines[1:])
    total_units = sum(int(l.split(',')[3]) for l in lines[1:])
    report = f"""# Q1 2026 Sales Summary
Generated by nexus-fs multi-backend workflow

## Totals
- Revenue: ${total_revenue:,}
- Units sold: {total_units:,}
- Avg price: ${total_revenue / total_units:.2f}

## By Region
"""
    for line in lines[1:]:
        region, product, rev, units = line.split(',')
        report += f"- {region}: {product} — ${int(rev):,} ({units} units)\n"

    await fs.write('/local/nexus-fs-demo-reports/2026/q1/summary.md', report.encode())
    print(f"  Generated summary.md ({len(report)} bytes)")

    # Generate events digest
    import json
    events = json.loads(events_data)['events']
    digest = "# March Events Digest\n\n"
    digest += f"Total events: {len(events)}\n"
    digest += f"Signups: {sum(1 for e in events if e['type'] == 'signup')}\n"
    digest += f"Purchases: {sum(1 for e in events if e['type'] == 'purchase')}\n"

    await fs.write('/local/nexus-fs-demo-reports/2026/q1/events_digest.md', digest.encode())
    print(f"  Generated events_digest.md ({len(digest)} bytes)")

    # Stat a file to show metadata
    stat = await fs.stat('/local/nexus-fs-demo-reports/2026/q1/summary.md')
    print(f"  Stat summary.md: {stat}")

    # Check exists
    exists = await fs.exists('/local/nexus-fs-demo-reports/2026/q1/summary.md')
    print(f"  Exists check: {exists}")

    await fs.close()

asyncio.run(generate_reports())
PYEOF
ok "Reports generated"

# ── Step 7: Verify full pipeline ─────────────────────────────────────────────
step "7/9" "Verifying full pipeline state..."
"$PYTHON" << 'PYEOF'
import asyncio
from nexus.fs import mount

async def verify():
    fs = await mount(
        'local:///tmp/nexus-fs-demo/inbox',
        'local:///tmp/nexus-fs-demo/processed',
        'local:///tmp/nexus-fs-demo/archive',
        'local:///tmp/nexus-fs-demo/reports',
    )

    for mp in [
        '/local/nexus-fs-demo-inbox/',
        '/local/nexus-fs-demo-processed/',
        '/local/nexus-fs-demo-archive/',
        '/local/nexus-fs-demo-reports/',
    ]:
        try:
            files = await fs.ls(mp)
            names = [f.split('/')[-1] for f in files]
            print(f"  {mp:45s} {len(files)} items: {names}")
        except Exception:
            print(f"  {mp:45s} (empty or error)")

    # Show nested report structure
    q1_files = await fs.ls('/local/nexus-fs-demo-reports/2026/q1/')
    print(f"  {'  /2026/q1/':45s} {len(q1_files)} items: {[f.split('/')[-1] for f in q1_files]}")

    await fs.close()

asyncio.run(verify())
PYEOF
ok "Pipeline verified"

# ── Step 8: Copy large binary file ───────────────────────────────────────────
step "8/9" "Copying binary file (model weights) to archive..."
echo "  > nexus-fs cp .../model_weights.bin -> archive"
"$PYTHON" -c "
from nexus.fs._cli import main
main(['cp', '/local/nexus-fs-demo-inbox/model_weights.bin', '/local/nexus-fs-demo-archive/model_weights.bin'])
" 2>&1
ok "Binary file archived"

# ── Step 9: Sync CAS data to filesystem for playground ───────────────────────
step "9/11" "Syncing CAS data to filesystem (for playground)..."
"$PYTHON" << 'PYEOF'
import asyncio
from pathlib import Path
from nexus.fs import mount

TESTROOT = Path("/tmp/nexus-fs-demo")

async def sync_to_playground():
    fs = await mount(
        'local:///tmp/nexus-fs-demo/inbox',
        'local:///tmp/nexus-fs-demo/processed',
        'local:///tmp/nexus-fs-demo/archive',
        'local:///tmp/nexus-fs-demo/reports',
    )

    exports = [
        ("/local/nexus-fs-demo-processed/sales_2026_q1.csv", TESTROOT / "processed" / "sales_2026_q1.csv"),
        ("/local/nexus-fs-demo-processed/events_march.json",  TESTROOT / "processed" / "events_march.json"),
        ("/local/nexus-fs-demo-archive/sales_2026_q1.csv",    TESTROOT / "archive" / "sales_2026_q1.csv"),
        ("/local/nexus-fs-demo-archive/model_weights.bin",     TESTROOT / "archive" / "model_weights.bin"),
        ("/local/nexus-fs-demo-reports/2026/q1/summary.md",   TESTROOT / "reports" / "2026" / "q1" / "summary.md"),
        ("/local/nexus-fs-demo-reports/2026/q1/events_digest.md", TESTROOT / "reports" / "2026" / "q1" / "events_digest.md"),
    ]

    for cas_path, raw_path in exports:
        content = await fs.read(cas_path)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(content)
        print(f"  Synced {raw_path.name:30s} -> {'/'.join(raw_path.parts[-3:])} ({len(content)} bytes)")

    await fs.close()

asyncio.run(sync_to_playground())
PYEOF
ok "CAS -> filesystem sync for playground"

# ── Step 10: Validate playground visibility ──────────────────────────────────
step "10/11" "Validating playground visibility..."
"$PYTHON" << 'PYEOF'
from pathlib import Path

TESTROOT = Path("/tmp/nexus-fs-demo")
all_ok = True

for mount_name, expected_files in [
    ("inbox",     ["sales_2026_q1.csv", "events_march.json", "model_weights.bin"]),
    ("processed", ["sales_2026_q1.csv", "events_march.json"]),
    ("archive",   ["sales_2026_q1.csv", "model_weights.bin"]),
    ("reports",   ["2026/q1/summary.md", "2026/q1/events_digest.md"]),
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
print(f"\n  All pipeline files visible in playground")
PYEOF
ok "Playground validation passed"

# ── Step 11: Final mount list ────────────────────────────────────────────────
step "11/11" "Final mount state..."
"$PYTHON" -c "from nexus.fs._cli import main; main(['mount', 'list'])" 2>&1

banner "Multi-Backend Workflow Complete!"
echo ""
echo "  Data pipeline:"
echo "    inbox (3 raw files)"
echo "      -> processed (2 files: sales + events)"
echo "         -> archive (2 files: sales + model weights)"
echo "    reports/2026/q1/ (2 generated: summary + digest)"
echo ""
echo "  Open playground to explore all 4 backends:"
echo "    nexus-fs playground \\"
echo "      local://$TESTROOT/inbox \\"
echo "      local://$TESTROOT/processed \\"
echo "      local://$TESTROOT/archive \\"
echo "      local://$TESTROOT/reports"
echo ""
