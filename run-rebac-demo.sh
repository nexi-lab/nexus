#!/bin/bash
# Quick-start script to run the comprehensive ReBAC demo
#
# Usage:
#   ./run-rebac-demo.sh           # Normal run with auto-cleanup
#   KEEP=1 ./run-rebac-demo.sh    # Keep demo data for inspection

set -e

echo "╔══════════════════════════════════════════════════════════╗"
echo "║      Nexus ReBAC Comprehensive Demo - Quick Start       ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Check if we're in the right directory
if [ ! -f "scripts/init-nexus-with-auth.sh" ]; then
    echo "❌ Error: Must run from nexus root directory"
    echo "   cd /Users/tafeng/nexus && ./run-rebac-demo.sh"
    exit 1
fi

# Step 1: Check if server is running
echo "📡 Step 1: Checking server status..."
if curl -s http://localhost:8080/health 2>/dev/null | grep -q "healthy"; then
    echo "✓ Server already running"
else
    echo "🚀 Starting Nexus server..."
    ./scripts/init-nexus-with-auth.sh > /tmp/nexus-demo-server.log 2>&1 &
    SERVER_PID=$!

    # Wait for server to be ready
    echo "   Waiting for server to start..."
    for i in {1..30}; do
        if curl -s http://localhost:8080/health 2>/dev/null | grep -q "healthy"; then
            echo "✓ Server started (PID: $SERVER_PID)"
            break
        fi
        sleep 1
    done

    if ! curl -s http://localhost:8080/health 2>/dev/null | grep -q "healthy"; then
        echo "❌ Server failed to start within 30 seconds"
        echo "   Check logs: tail -f /tmp/nexus-demo-server.log"
        exit 1
    fi
fi

# Step 2: Load credentials
echo ""
echo "🔑 Step 2: Loading admin credentials..."
source .nexus-admin-env

if [ -z "$NEXUS_API_KEY" ]; then
    echo "❌ Failed to load credentials"
    echo "   Check if .nexus-admin-env exists"
    exit 1
fi

echo "✓ Credentials loaded"
echo "   URL: $NEXUS_URL"
echo "   Key: ${NEXUS_API_KEY:0:20}..."

# Step 3: Run the demo
echo ""
echo "🎯 Step 3: Running comprehensive ReBAC demo..."
echo ""

if [ "$KEEP" = "1" ]; then
    echo "   Mode: KEEP=1 (data will be preserved for inspection)"
    echo ""
fi

# Run the demo
if ./examples/cli/permissions_demo_enhanced.sh; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║  ✅ Demo Completed Successfully!                         ║"
    echo "╚══════════════════════════════════════════════════════════╝"

    if [ "$KEEP" = "1" ]; then
        echo ""
        echo "📂 Demo data preserved at: /workspace/rebac-comprehensive-demo"
        echo ""
        echo "Try these inspection commands:"
        echo "  nexus ls /workspace/rebac-comprehensive-demo"
        echo "  nexus cat /workspace/rebac-comprehensive-demo/team-file.txt"
        echo "  nexus rebac list --subject user:bob"
        echo ""
        echo "To cleanup:"
        echo "  source .nexus-admin-env"
        echo "  nexus rm -r /workspace/rebac-comprehensive-demo"
    fi
else
    echo ""
    echo "❌ Demo encountered errors"
    exit 1
fi
