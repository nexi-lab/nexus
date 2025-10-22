#!/bin/bash
# FUSE CLI Demo in E2B (macOS-friendly alternative)
#
# This script runs the full fuse_cli_demo.sh inside an E2B Linux sandbox,
# avoiding the need for macFUSE installation on macOS.
#
# Prerequisites:
#   1. E2B CLI: npm install -g @e2b/cli
#   2. E2B template built: cd examples/e2b && e2b template build
#   3. E2B API key: export E2B_API_KEY=your-key
#
# Usage:
#   export E2B_API_KEY=your-key
#   bash examples/e2b/fuse_cli_demo_e2b.sh

set -e

# Check prerequisites
if ! command -v e2b &> /dev/null; then
    echo "Error: E2B CLI not installed"
    echo "Install with: npm install -g @e2b/cli"
    exit 1
fi

if [ -z "$E2B_API_KEY" ]; then
    echo "Error: E2B_API_KEY not set"
    echo "Get your key from: https://e2b.dev/docs"
    exit 1
fi

E2B_TEMPLATE="${E2B_TEMPLATE:-nexus-sandbox-v1}"

echo "Creating E2B sandbox with FUSE support..."
SANDBOX_ID=$(e2b sandbox create --template "$E2B_TEMPLATE" --json | jq -r '.sandboxId')

if [ -z "$SANDBOX_ID" ] || [ "$SANDBOX_ID" = "null" ]; then
    echo "✗ Failed to create sandbox"
    exit 1
fi

echo "✓ Sandbox created: $SANDBOX_ID"

# Cleanup function
cleanup() {
    echo ""
    echo "Cleaning up sandbox..."
    e2b sandbox delete "$SANDBOX_ID" 2>/dev/null || true
    echo "✓ Done"
}
trap cleanup EXIT

# Copy the demo script to sandbox
echo ""
echo "Uploading demo script..."
e2b sandbox upload "$SANDBOX_ID" examples/fuse_cli_demo.sh /tmp/fuse_cli_demo.sh
e2b sandbox upload "$SANDBOX_ID" examples/sample-local-pdf.pdf /tmp/sample-local-pdf.pdf 2>/dev/null || echo "(PDF not found - will use text fallback)"

# Run the demo in the sandbox
echo ""
echo "==================================================="
echo "Running FUSE CLI Demo in E2B Linux Sandbox"
echo "==================================================="
echo ""

e2b sandbox exec "$SANDBOX_ID" "bash /tmp/fuse_cli_demo.sh"

echo ""
echo "==================================================="
echo "Demo completed successfully in E2B!"
echo "==================================================="
echo ""
echo "💡 This ran in a Linux sandbox - no macFUSE needed!"
