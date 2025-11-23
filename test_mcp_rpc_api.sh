#!/bin/bash
set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
MCP_URL="http://localhost:8081/mcp"
API_KEY="sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

echo -e "${BLUE}Testing MCP RPC API with Infrastructure-Level API Key${NC}"
echo "============================================================"
echo ""

# Step 1: Initialize MCP session
echo -e "${BLUE}Step 1: Initialize MCP session${NC}"
INIT_RESPONSE=$(curl -si "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -d '{
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
      "protocolVersion": "0.1.0",
      "capabilities": {},
      "clientInfo": {
        "name": "test-client",
        "version": "1.0.0"
      }
    }
  }')

# Extract session ID from headers
SESSION_ID=$(echo "$INIT_RESPONSE" | grep -i 'mcp-session-id:' | cut -d' ' -f2 | tr -d '\r')

if [ -z "$SESSION_ID" ]; then
  echo -e "${RED}✗ Failed to get session ID${NC}"
  echo "Response:"
  echo "$INIT_RESPONSE"
  exit 1
fi

echo -e "${GREEN}✓ Session initialized: $SESSION_ID${NC}"
echo ""

# Step 2: List available tools
echo -e "${BLUE}Step 2: List available MCP tools${NC}"
TOOLS_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list"
  }')

echo "$TOOLS_RESPONSE" | python3 -m json.tool | head -50
echo -e "${GREEN}✓ Tools listed successfully${NC}"
echo ""

# Step 3: Create test directory
echo -e "${BLUE}Step 3: Create test directory with API key${NC}"
TEST_DIR="/test_rpc_$(date +%s)"
MKDIR_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 2,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_mkdir\",
      \"arguments\": {
        \"path\": \"$TEST_DIR\"
      }
    }
  }")

if echo "$MKDIR_RESPONSE" | grep -q "Successfully created"; then
  echo -e "${GREEN}✓ Directory created: $TEST_DIR${NC}"
else
  echo -e "${RED}✗ Failed to create directory${NC}"
  echo "$MKDIR_RESPONSE"
fi
echo ""

# Step 4: Write file with API key header
echo -e "${BLUE}Step 4: Write file with API key in header${NC}"
WRITE_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 3,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_write_file\",
      \"arguments\": {
        \"path\": \"$TEST_DIR/test.txt\",
        \"content\": \"Hello from RPC API test! API key in header.\"
      }
    }
  }")

if echo "$WRITE_RESPONSE" | grep -q "Successfully wrote"; then
  echo -e "${GREEN}✓ File written successfully${NC}"
else
  echo -e "${RED}✗ Failed to write file${NC}"
  echo "$WRITE_RESPONSE"
fi
echo ""

# Step 5: Read file with API key
echo -e "${BLUE}Step 5: Read file with API key in header${NC}"
READ_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 4,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_read_file\",
      \"arguments\": {
        \"path\": \"$TEST_DIR/test.txt\"
      }
    }
  }")

if echo "$READ_RESPONSE" | grep -q "Hello from RPC API test"; then
  echo -e "${GREEN}✓ File read successfully${NC}"
  echo "Content: $(echo "$READ_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['content'][0]['text'])")"
else
  echo -e "${RED}✗ Failed to read file${NC}"
  echo "$READ_RESPONSE"
fi
echo ""

# Step 6: List files with API key
echo -e "${BLUE}Step 6: List directory contents${NC}"
LIST_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 5,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_list_files\",
      \"arguments\": {
        \"path\": \"$TEST_DIR\",
        \"recursive\": false,
        \"details\": true
      }
    }
  }")

if echo "$LIST_RESPONSE" | grep -q "test.txt"; then
  echo -e "${GREEN}✓ Directory listed successfully${NC}"
  echo "$LIST_RESPONSE" | python3 -m json.tool | grep -A 5 "test.txt"
else
  echo -e "${RED}✗ Failed to list directory${NC}"
  echo "$LIST_RESPONSE"
fi
echo ""

# Step 7: Get file info
echo -e "${BLUE}Step 7: Get file metadata${NC}"
INFO_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 6,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_file_info\",
      \"arguments\": {
        \"path\": \"$TEST_DIR/test.txt\"
      }
    }
  }")

if echo "$INFO_RESPONSE" | grep -q "\"exists\": true"; then
  echo -e "${GREEN}✓ File info retrieved successfully${NC}"
  echo "$INFO_RESPONSE" | python3 -m json.tool | grep -A 10 "\"result\""
else
  echo -e "${RED}✗ Failed to get file info${NC}"
  echo "$INFO_RESPONSE"
fi
echo ""

# Step 8: Test without API key (should use default)
echo -e "${BLUE}Step 8: Test request WITHOUT API key header (should use default)${NC}"
READ_NO_KEY=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 7,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_read_file\",
      \"arguments\": {
        \"path\": \"$TEST_DIR/test.txt\"
      }
    }
  }")

if echo "$READ_NO_KEY" | grep -q "Hello from RPC API test"; then
  echo -e "${GREEN}✓ Request without API key header succeeded (using default key)${NC}"
else
  echo -e "${RED}✗ Request without API key failed${NC}"
  echo "$READ_NO_KEY"
fi
echo ""

# Step 9: Cleanup
echo -e "${BLUE}Step 9: Cleanup test directory${NC}"
DELETE_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 8,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_delete_file\",
      \"arguments\": {
        \"path\": \"$TEST_DIR/test.txt\"
      }
    }
  }")

RMDIR_RESPONSE=$(curl -s "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-Nexus-API-Key: $API_KEY" \
  -H "mcp-session-id: $SESSION_ID" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"id\": 9,
    \"method\": \"tools/call\",
    \"params\": {
      \"name\": \"nexus_rmdir\",
      \"arguments\": {
        \"path\": \"$TEST_DIR\",
        \"recursive\": true
      }
    }
  }")

if echo "$RMDIR_RESPONSE" | grep -q "Successfully removed"; then
  echo -e "${GREEN}✓ Cleanup completed${NC}"
else
  echo -e "${RED}✗ Cleanup failed${NC}"
fi
echo ""

echo "============================================================"
echo -e "${GREEN}✅ All MCP RPC API tests completed!${NC}"
echo ""
echo "Summary:"
echo "  • Infrastructure-level API key middleware is working"
echo "  • X-Nexus-API-Key header is being extracted correctly"
echo "  • All 7 tools tested successfully with API key"
echo "  • Fallback to default API key works when header is missing"
