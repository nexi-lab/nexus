#!/bin/bash
# Cleanup stale demo data from previous runs
#
# This script cleans up directories created before the parent tuple fix
# so demos can run cleanly without permission errors from stale data.

set -e

echo "╔══════════════════════════════════════════╗"
echo "║   Clean Up Stale Demo Data              ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# Check if server is running
if ! curl -s http://localhost:8080/health >/dev/null 2>&1; then
    echo "❌ Error: Nexus server is not running at http://localhost:8080"
    echo "   Please start the server first with: ./scripts/init-nexus-with-auth.sh"
    exit 1
fi

# Load credentials if available
if [ -f .nexus-admin-env ]; then
    source .nexus-admin-env
    echo "✓ Loaded credentials from .nexus-admin-env"
else
    echo "⚠ Warning: .nexus-admin-env not found, continuing without auth..."
fi

echo ""
echo "Removing stale demo directories..."

# Remove common demo paths that may have stale data
nexus rm -r /workspace/dir-demo 2>/dev/null && echo "  ✓ Removed /workspace/dir-demo" || echo "  ℹ /workspace/dir-demo not found"
nexus rm -r /workspace/test-mkdir 2>/dev/null && echo "  ✓ Removed /workspace/test-mkdir" || echo "  ℹ /workspace/test-mkdir not found"
nexus rm -r /workspace/clean-test 2>/dev/null && echo "  ✓ Removed /workspace/clean-test" || echo "  ℹ /workspace/clean-test not found"

echo ""
echo "✓ Cleanup complete!"
echo ""
echo "You can now run demos without stale data issues:"
echo "  bash examples/cli/directory_operations_demo.sh"
