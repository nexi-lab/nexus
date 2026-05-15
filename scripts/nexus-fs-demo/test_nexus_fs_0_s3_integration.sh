#!/usr/bin/env bash
# =============================================================================
# Script 0: S3 Integration (Optional - requires AWS credentials)
# =============================================================================
# Tests: mount s3://, doctor --mount s3://, cp local->s3, cp s3->local,
#        mount test, auth test s3, playground with S3
#
# Prereq: AWS credentials configured (aws configure or env vars)
#         Set S3_BUCKET env var or edit the default below
# =============================================================================
set -euo pipefail

PYTHON="${NEXUS_FS_PYTHON:-/Users/tafeng/nexus/.venv/bin/python}"
TESTROOT="/tmp/nexus-fs-demo"
S3_BUCKET="${S3_BUCKET:-nexus-fs-demo-$(whoami)}"
S3_URI="s3://$S3_BUCKET"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()   { echo -e "\n${CYAN}[$1]${NC} $2"; }
ok()     { echo -e "  ${GREEN}OK${NC} $1"; }
fail()   { echo -e "  ${RED}FAIL${NC} $1"; exit 1; }
banner() { echo -e "\n${YELLOW}════════════════════════════════════════════════${NC}"; echo -e "${YELLOW}  $1${NC}"; echo -e "${YELLOW}════════════════════════════════════════════════${NC}"; }
nfs() { "$PYTHON" -c "from nexus.fs._cli import main; main()" "$@"; }

banner "Script 0: S3 Integration"
echo "  S3 bucket: $S3_BUCKET"

# ── Step 1: Verify S3 auth ───────────────────────────────────────────────────
step "1/8" "Verifying S3 credentials..."
echo "  > nexus-fs auth test s3"
nfs auth test s3 2>&1 || fail "S3 auth failed"
ok "S3 credentials valid"

# ── Step 2: Create local test data ───────────────────────────────────────────
step "2/8" "Creating local test data..."
mkdir -p "$TESTROOT/s3test"
cat > "$TESTROOT/s3test/upload.txt" << 'EOF'
This file was uploaded to S3 via nexus-fs cp.
Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
cat > "$TESTROOT/s3test/data.json" << 'EOF'
{
  "test": "nexus-fs-s3-integration",
  "items": [1, 2, 3, 4, 5],
  "ok": true
}
EOF
ok "Local test files created"

# ── Step 3: Mount local + S3 ─────────────────────────────────────────────────
step "3/8" "Mounting local and S3 backends..."
nfs unmount "local://$TESTROOT/s3test" 2>/dev/null || true
nfs unmount "$S3_URI" 2>/dev/null || true

echo "  > nexus-fs mount local://$TESTROOT/s3test"
nfs mount "local://$TESTROOT/s3test" 2>&1
echo ""
echo "  > nexus-fs mount $S3_URI"
nfs mount "$S3_URI" 2>&1
ok "Both backends mounted"

# ── Step 4: Test S3 mount connectivity ───────────────────────────────────────
step "4/8" "Testing S3 mount connectivity..."
echo "  > nexus-fs mount test $S3_URI"
nfs mount test "$S3_URI" 2>&1
ok "S3 connectivity confirmed"

# ── Step 5: Seed local files into CAS and upload to S3 ──────────────────────
step "5/8" "Seeding files and copying local -> S3..."
"$PYTHON" << PYEOF
import asyncio
from nexus.fs import mount

async def seed_and_copy():
    fs = await mount('local://$TESTROOT/s3test', '$S3_URI')

    # Seed local files
    for fname in ['upload.txt', 'data.json']:
        with open('$TESTROOT/s3test/' + fname, 'rb') as f:
            data = f.read()
        await fs.write(f'/local/nexus-fs-demo-s3test/{fname}', data)
        print(f"  Seeded {fname} ({len(data)} bytes)")

    await fs.close()

asyncio.run(seed_and_copy())
PYEOF

echo ""
echo "  > nexus-fs cp local -> S3 (upload.txt)"
nfs cp "/local/nexus-fs-demo-s3test/upload.txt" "/s3/$S3_BUCKET/nexus-fs-test/upload.txt" 2>&1

echo ""
echo "  > nexus-fs cp local -> S3 (data.json)"
nfs cp "/local/nexus-fs-demo-s3test/data.json" "/s3/$S3_BUCKET/nexus-fs-test/data.json" 2>&1
ok "Files uploaded to S3"

# ── Step 6: Copy back from S3 ───────────────────────────────────────────────
step "6/8" "Copying S3 -> local (round-trip)..."
echo "  > nexus-fs cp S3 -> local (upload.txt)"
nfs cp "/s3/$S3_BUCKET/nexus-fs-test/upload.txt" "/local/nexus-fs-demo-s3test/downloaded.txt" 2>&1
ok "Round-trip complete"

# ── Step 7: Verify round-trip ────────────────────────────────────────────────
step "7/8" "Verifying round-trip data integrity..."
"$PYTHON" << PYEOF
import asyncio
from nexus.fs import mount

async def verify():
    fs = await mount('local://$TESTROOT/s3test', '$S3_URI')

    original = await fs.read('/local/nexus-fs-demo-s3test/upload.txt')
    downloaded = await fs.read('/local/nexus-fs-demo-s3test/downloaded.txt')

    if original == downloaded:
        print("  PASS  Round-trip: original == downloaded")
    else:
        print(f"  FAIL  Mismatch: {len(original)} vs {len(downloaded)} bytes")
        raise SystemExit(1)

    # List S3 contents
    s3_files = await fs.ls('/s3/$S3_BUCKET/nexus-fs-test/')
    print(f"  S3 nexus-fs-test/: {[f.split('/')[-1] for f in s3_files]}")

    await fs.close()

asyncio.run(verify())
PYEOF
ok "Data integrity verified"

# ── Step 8: Doctor with both mounts ──────────────────────────────────────────
step "8/8" "Doctor with local + S3 mounts..."
echo "  > nexus-fs doctor --mount local://$TESTROOT/s3test --mount $S3_URI"
nfs doctor --mount "local://$TESTROOT/s3test" --mount "$S3_URI" 2>&1
ok "Doctor passed for both backends"

# ── Cleanup ──────────────────────────────────────────────────────────────────
banner "S3 Integration Complete!"
echo ""
echo "  Operations tested:"
echo "    - auth test s3"
echo "    - mount local + S3"
echo "    - mount test S3"
echo "    - cp local -> S3 (upload)"
echo "    - cp S3 -> local (download)"
echo "    - round-trip verification"
echo "    - doctor with S3 connectivity"
echo ""
echo "  Open playground to browse local + S3:"
echo "    nexus-fs playground local://$TESTROOT/s3test $S3_URI"
echo ""
echo "  To clean up S3 test data:"
echo "    aws s3 rm s3://$S3_BUCKET/nexus-fs-test/ --recursive"
echo "    nexus-fs unmount $S3_URI"
echo ""
