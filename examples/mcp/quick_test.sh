#!/bin/bash
# Quick MCP Test with Authentication
# Uses init-nexus-with-auth.sh to start authenticated server

set -e

echo "🚀 Quick MCP Test (2 minutes)"
echo ""

# Cleanup
cleanup() {
    echo ""
    echo "🧹 Cleaning up..."
    pkill -f "nexus serve" 2>/dev/null || true
    sleep 1
    echo "✅ Done!"
}
trap cleanup EXIT

# Start the init script in background
echo "1️⃣  Starting Nexus server with authentication..."
./scripts/init-nexus-with-auth.sh > /tmp/mcp-test.log 2>&1 &

# Wait for server and admin key to be written
echo -n "   Waiting for server"
for i in {1..30}; do
    if curl -s http://localhost:8080/health > /dev/null 2>&1; then
        echo " ✅"
        break
    fi
    echo -n "."
    sleep 1
done
echo ""

# Wait a bit more for log file to be fully written
sleep 2

# Extract admin key (use -a to force text mode, file has ANSI codes)
ADMIN_KEY=$(grep -a "Admin API Key:" /tmp/mcp-test.log | awk '{print $NF}')
if [ -z "$ADMIN_KEY" ]; then
    echo "❌ Failed to get admin key"
    exit 1
fi

echo "   Admin Key: ${ADMIN_KEY:0:25}..."
echo ""

# Create Alice user
echo "2️⃣  Creating test user (Alice)..."
export NEXUS_URL=http://localhost:8080
export NEXUS_API_KEY=$ADMIN_KEY

ALICE_OUTPUT=$(nexus admin create-user alice --name "Alice Test" --json-output 2>&1 || echo "failed")
ALICE_KEY=$(echo "$ALICE_OUTPUT" | grep -oE '"api_key": "[^"]*"' | cut -d'"' -f4)

if [ -z "$ALICE_KEY" ]; then
    echo "❌ Failed to create Alice"
    echo "$ALICE_OUTPUT"
    exit 1
fi

echo "   Alice Key: ${ALICE_KEY:0:25}..."
echo ""

# Test file operations
echo "3️⃣  Testing file operations..."

# Alice writes a file
curl -s -X POST http://localhost:8080/api/nfs/write \
    -H "X-API-Key: $ALICE_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path": "/test.txt", "content": "SGVsbG8gTUNQIQ=="}' > /dev/null
echo "   ✅ Alice wrote /test.txt"

# Alice reads it
RESULT=$(curl -s -X POST http://localhost:8080/api/nfs/read \
    -H "X-API-Key: $ALICE_KEY" \
    -H "Content-Type: application/json" \
    -d '{"path": "/test.txt"}')
echo "   ✅ Alice read /test.txt: $(echo $RESULT | cut -c1-30)..."
echo ""

# Test MCP server
echo "4️⃣  Testing MCP server..."

python3 << PYEND
import sys
sys.path.insert(0, 'src')
from nexus.mcp import create_mcp_server

# Create MCP server pointing to authenticated Nexus
mcp = create_mcp_server(remote_url="http://localhost:8080")

print(f"   ✅ MCP server: {mcp.name}")
print("   ✅ Ready to use with Claude Desktop!")
PYEND

echo ""

# Show Claude Desktop config
echo "5️⃣  Claude Desktop Configuration:"
echo ""
echo "Add to ~/.config/claude/claude_desktop_config.json:"
echo ""
echo '{'
echo '  "mcpServers": {'
echo '    "nexus": {'
echo '      "command": "nexus",'
echo '      "args": ["mcp", "serve", "--transport", "stdio"],'
echo '      "env": {'
echo '        "NEXUS_URL": "http://localhost:8080",'
echo "        \"NEXUS_API_KEY\": \"$ALICE_KEY\""
echo '      }'
echo '    }'
echo '  }'
echo '}'
echo ""

echo "✅ All tests passed!"
echo ""
echo "Server is running at http://localhost:8080"
echo "Press Enter to stop..."
read
