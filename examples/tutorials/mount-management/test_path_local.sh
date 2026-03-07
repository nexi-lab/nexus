#!/usr/bin/env bash
# Test PathLocalBackend — files stored at actual paths, no CAS overhead.
set -euo pipefail

DIR=/tmp/test-path-local-$$
trap 'rm -rf "$DIR"' EXIT

echo "=== 1. Init workspace ==="
uv run nexus init "$DIR"
export NEXUS_DATA_DIR="$DIR/nexus-data"

echo ""
echo "=== 2. Write files ==="
uv run nexus write /hello.txt "hello world"
uv run nexus write /docs/readme.md "# README"

echo ""
echo "=== 3. Verify files at actual paths (not CAS-sharded) ==="
test -f "$NEXUS_DATA_DIR/files/hello.txt" && echo "OK: hello.txt at actual path"
test -f "$NEXUS_DATA_DIR/files/docs/readme.md" && echo "OK: docs/readme.md at actual path"
! test -d "$NEXUS_DATA_DIR/cas" && echo "OK: no cas/ directory"

echo ""
echo "=== 4. Read back ==="
OUT=$(uv run nexus cat /hello.txt)
[ "$OUT" = "hello world" ] && echo "OK: read matches"

echo ""
echo "=== 5. Delete ==="
echo "y" | uv run nexus rm /hello.txt
! test -f "$NEXUS_DATA_DIR/files/hello.txt" && echo "OK: file removed from disk"

echo ""
echo "=== 6. Rename ==="
echo "y" | uv run nexus move /docs/readme.md /docs/info.md
test -f "$NEXUS_DATA_DIR/files/docs/info.md" && echo "OK: file moved on disk"
! test -f "$NEXUS_DATA_DIR/files/docs/readme.md" && echo "OK: old path gone"

echo ""
echo "=== All checks passed ==="
