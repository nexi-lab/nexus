#!/bin/bash
# Test script to reproduce SQLite segfault when accessing skills registry

set -e

echo "=== Testing SQLite Skills Registry Segfault ==="
echo ""

# Get admin API key from environment
ADMIN_KEY="${ADMIN_KEY:-sk-default_admin_d38a7427_244c5f756dcc064eea6e68a64aa2111e}"

echo "Step 1: Verify server is running..."
if ! curl -s http://localhost:2026/health > /dev/null 2>&1; then
    echo "❌ Server is not running. Start it with: ./scripts/local-demo.sh --start --sqlite"
    exit 1
fi
echo "✅ Server is running"
echo ""

echo "Step 2: Trigger skills registry by listing skills..."
echo "This should cause the segfault..."
echo ""

# Make API call that triggers skills registry
# This will scan /zone:default/user:admin/skill/ and read many SKILL.md files
curl -v -X POST http://localhost:2026/api/nfs/list \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    -d '{
        "jsonrpc": "2.0",
        "method": "list",
        "params": {
            "path": "/skills/",
            "recursive": true
        },
        "id": 1
    }'

echo ""
echo ""
echo "Step 3: Check if server is still alive..."
sleep 2
if curl -s http://localhost:2026/health > /dev/null 2>&1; then
    echo "✅ Server survived! No segfault."
else
    echo "❌ Server crashed (segfault)"
    exit 1
fi
