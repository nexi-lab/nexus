#!/bin/bash
# Test Tiger Cache Write-Through (Issue #935)
#
# This script verifies that permission check results are written
# through to Tiger Cache after Rust computation.
#
# Usage:
#   ./scripts/test-tiger-write-through.sh

set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Tiger Cache Write-Through Tests (Issue #935)           ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ════════════════════════════════════════════════════════════
# Section 1: Unit Tests for new methods
# ════════════════════════════════════════════════════════════

echo -e "${CYAN}═══ 1. Running Unit Tests for Tiger Cache Methods ═══${NC}"
echo ""

python -m pytest tests/unit/core/test_tiger_cache.py::TestTigerCacheIncrementalUpdates -v --tb=short 2>&1 | grep -E "^tests/|PASSED|FAILED|ERROR|passed|failed"

if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo -e "${RED}✗ Unit tests failed!${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Unit tests passed${NC}"
echo ""

# ════════════════════════════════════════════════════════════
# Section 2: Integration Test - Write-Through Verification
# ════════════════════════════════════════════════════════════

echo -e "${CYAN}═══ 2. Running Write-Through Integration Test ═══${NC}"
echo ""

python3 -W ignore << 'PYTHON'
import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, 'src')

from nexus import LocalBackend, NexusFS
from nexus.core.permissions import OperationContext

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

def test(name, condition):
    if condition:
        print(f"  {PASS} {name}")
        return True
    else:
        print(f"  {FAIL} {name}")
        return False

all_passed = True

# Enable Tiger resource map sync
os.environ["NEXUS_SYNC_TIGER_RESOURCE_MAP"] = "true"

with tempfile.TemporaryDirectory() as tmpdir:
    tmpdir = Path(tmpdir)
    db_path = tmpdir / "metadata.db"

    print("Setting up NexusFS with Tiger Cache enabled...")

    nx = NexusFS(
        backend=LocalBackend(tmpdir / "data"),
        db_path=db_path,
        auto_parse=False,
        enforce_permissions=True,
        tenant_id="test_tenant",
    )

    # Check Tiger Cache is enabled
    has_tiger = nx._rebac_manager._tiger_cache is not None
    all_passed &= test("Tiger Cache is enabled", has_tiger)

    if not has_tiger:
        print("  Skipping remaining tests - Tiger Cache not available")
        sys.exit(1)

    tiger_cache = nx._rebac_manager._tiger_cache
    resource_map = tiger_cache._resource_map

    admin_ctx = OperationContext(
        user="admin",
        groups=[],
        is_admin=True,
        is_system=False,
        tenant_id="test_tenant",
    )

    # Grant admin ownership of root
    print("\nTest 1: Setup - granting permissions...")
    nx.rebac_create(
        subject=("user", "admin"),
        relation="direct_owner",
        object=("file", "/"),
        tenant_id="test_tenant",
        context=admin_ctx,
    )

    # Create test files
    nx.write("/doc1.txt", b"content1", context=admin_ctx)
    nx.write("/doc2.txt", b"content2", context=admin_ctx)
    nx.write("/doc3.txt", b"content3", context=admin_ctx)

    # Grant alice read access to doc1 and doc2
    nx.rebac_create(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", "/doc1.txt"),
        tenant_id="test_tenant",
        context=admin_ctx,
    )
    nx.rebac_create(
        subject=("user", "alice"),
        relation="direct_viewer",
        object=("file", "/doc2.txt"),
        tenant_id="test_tenant",
        context=admin_ctx,
    )
    all_passed &= test("Created files and granted permissions", True)

    # Check Tiger Cache is empty for alice initially
    print("\nTest 2: Verify Tiger Cache starts empty for alice...")
    accessible_before = tiger_cache.get_accessible_resources(
        "user", "alice", "read", "file", "test_tenant"
    )
    all_passed &= test(f"Tiger Cache initially empty for alice (has {len(accessible_before)} resources)", len(accessible_before) == 0)

    # Now trigger permission checks (this should populate Tiger Cache via write-through)
    print("\nTest 3: Triggering permission checks (write-through)...")
    alice_ctx = OperationContext(
        user="alice",
        groups=[],
        is_admin=False,
        is_system=False,
        tenant_id="test_tenant",
    )

    # Use rebac_check_bulk to trigger the write-through path
    checks = [
        (("user", "alice"), "read", ("file", "/doc1.txt")),
        (("user", "alice"), "read", ("file", "/doc2.txt")),
        (("user", "alice"), "read", ("file", "/doc3.txt")),  # Should be denied
    ]

    results = nx._rebac_manager.rebac_check_bulk(checks, tenant_id="test_tenant")

    # Verify permission check results
    doc1_allowed = results.get((("user", "alice"), "read", ("file", "/doc1.txt")), False)
    doc2_allowed = results.get((("user", "alice"), "read", ("file", "/doc2.txt")), False)
    doc3_allowed = results.get((("user", "alice"), "read", ("file", "/doc3.txt")), False)

    all_passed &= test(f"alice can read doc1: {doc1_allowed}", doc1_allowed is True)
    all_passed &= test(f"alice can read doc2: {doc2_allowed}", doc2_allowed is True)
    all_passed &= test(f"alice cannot read doc3: {doc3_allowed}", doc3_allowed is False)

    # Check Tiger Cache was populated (WRITE-THROUGH!)
    print("\nTest 4: Verify Tiger Cache populated via write-through...")
    accessible_after = tiger_cache.get_accessible_resources(
        "user", "alice", "read", "file", "test_tenant"
    )

    all_passed &= test(
        f"Tiger Cache now has {len(accessible_after)} resources for alice",
        len(accessible_after) >= 2  # doc1 and doc2
    )

    # Verify the specific resources are in Tiger Cache
    doc1_int_id = resource_map.get_or_create_int_id("file", "/doc1.txt", "test_tenant")
    doc2_int_id = resource_map.get_or_create_int_id("file", "/doc2.txt", "test_tenant")
    doc3_int_id = resource_map.get_or_create_int_id("file", "/doc3.txt", "test_tenant")

    all_passed &= test(f"doc1 (int_id={doc1_int_id}) in Tiger Cache", doc1_int_id in accessible_after)
    all_passed &= test(f"doc2 (int_id={doc2_int_id}) in Tiger Cache", doc2_int_id in accessible_after)
    all_passed &= test(f"doc3 (int_id={doc3_int_id}) NOT in Tiger Cache (denied)", doc3_int_id not in accessible_after)

    # Test that subsequent checks hit Tiger Cache
    print("\nTest 5: Verify subsequent checks use Tiger Cache...")
    result_from_tiger = tiger_cache.check_access(
        "user", "alice", "read", "file", "/doc1.txt", "test_tenant"
    )
    all_passed &= test(f"Tiger Cache check_access returns True", result_from_tiger is True)

    result_from_tiger_denied = tiger_cache.check_access(
        "user", "alice", "read", "file", "/doc3.txt", "test_tenant"
    )
    all_passed &= test(f"Tiger Cache check_access returns False for doc3", result_from_tiger_denied is False)

    nx.close()

# Summary
print("")
if all_passed:
    print("\033[92m════════════════════════════════════════════════════════════\033[0m")
    print("\033[92m  All Tiger Write-Through tests passed!\033[0m")
    print("\033[92m════════════════════════════════════════════════════════════\033[0m")
    sys.exit(0)
else:
    print("\033[91m════════════════════════════════════════════════════════════\033[0m")
    print("\033[91m  Some tests failed!\033[0m")
    print("\033[91m════════════════════════════════════════════════════════════\033[0m")
    sys.exit(1)
PYTHON

if [ $? -ne 0 ]; then
    echo -e "${RED}✗ Integration test failed!${NC}"
    exit 1
fi
echo ""

# ════════════════════════════════════════════════════════════
# Section 3: All Tiger Cache Tests
# ════════════════════════════════════════════════════════════

echo -e "${CYAN}═══ 3. Running All Tiger Cache Unit Tests ═══${NC}"
echo ""

python -m pytest tests/unit/core/test_tiger_cache.py -v --tb=short 2>&1 | tail -10

if [ ${PIPESTATUS[0]} -ne 0 ]; then
    echo -e "${RED}✗ Tiger Cache tests failed!${NC}"
    exit 1
fi
echo -e "${GREEN}✓ All Tiger Cache tests passed${NC}"
echo ""

# ════════════════════════════════════════════════════════════
# Summary
# ════════════════════════════════════════════════════════════

echo "╔═══════════════════════════════════════════════════════════════════╗"
echo "║           Issue #935 - All Tests Passed!                          ║"
echo "╠═══════════════════════════════════════════════════════════════════╣"
echo "║  ✅ add_to_bitmap() creates/updates bitmaps                       ║"
echo "║  ✅ remove_from_bitmap() removes from bitmaps                     ║"
echo "║  ✅ add_to_bitmap_bulk() efficiently adds multiple resources      ║"
echo "║  ✅ Write-through populates Tiger Cache after permission checks   ║"
echo "║  ✅ Only positive (allowed) results are cached                    ║"
echo "║  ✅ Subsequent checks can use Tiger Cache                         ║"
echo "╚═══════════════════════════════════════════════════════════════════╝"
echo ""
echo -e "${BLUE}Key improvement:${NC}"
echo "  OLD: Tiger Cache stayed empty after Rust computation"
echo "  NEW: Positive results written to Tiger Cache for O(1) subsequent lookups"
