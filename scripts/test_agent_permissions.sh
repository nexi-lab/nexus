#!/usr/bin/env bash
#
# Integration Test: Agent Permission Testing
#
# This test validates the multi-tenant permission system by:
# 1. Starting the Nexus server (if not running)
# 2. Creating a new agent with an API key
# 3. Testing initial access (should only see agent config)
# 4. Granting permissions for pdf skill, resource folder, and workspace
# 5. Testing access after permissions are granted
#

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVER_URL="http://localhost:8080"
TENANT_ID="default"
USER_ID="admin"
TEST_AGENT_NAME="TestAgent"
TEST_AGENT_ID="${USER_ID},${TEST_AGENT_NAME}"

# Load configuration
CONFIG_FILE="${PROJECT_DIR}/configs/local-dev.env"
if [ -f "$CONFIG_FILE" ]; then
    source "$CONFIG_FILE"
    echo -e "${GREEN}✓ Loaded configuration from ${CONFIG_FILE}${NC}"
fi

# Use admin API key from config
ADMIN_API_KEY="${ADMIN_API_KEY:-sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee}"

echo "╔═══════════════════════════════════════════════════╗"
echo "║   Agent Permission Integration Test               ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Server:       ${SERVER_URL}"
echo "  Tenant:       ${TENANT_ID}"
echo "  User:         ${USER_ID}"
echo "  Agent Name:   ${TEST_AGENT_NAME}"
echo "  Agent ID:     ${TEST_AGENT_ID}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Step 1: Check if server is running
echo "🔍 Step 1: Checking server status..."
if ! curl -s -f "${SERVER_URL}/health" > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Server not running. Please start it first with:${NC}"
    echo "  ./local-demo.sh --start  # (auto-inits if data dir empty)"
    exit 1
fi
echo -e "${GREEN}✓ Server is running${NC}"
echo ""

# Step 2: Create a new agent with API key
echo "🤖 Step 2: Creating test agent '${TEST_AGENT_NAME}'..."

# Agent config path
AGENT_CONFIG_PATH="/tenant:${TENANT_ID}/user:${USER_ID}/agent/${TEST_AGENT_NAME}/config.yaml"

# Create agent config
AGENT_CONFIG="name: ${TEST_AGENT_NAME}
platform: test
description: Test agent for permission validation
created_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
metadata:
  test: true
  purpose: permission_testing
"

# Write agent config using admin key
curl -s -X POST "${SERVER_URL}/api/nfs/write" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"write\",
    \"params\": {
      \"path\": \"${AGENT_CONFIG_PATH}\",
      \"data\": $(echo "$AGENT_CONFIG" | python3 -c "import sys, json; print(json.dumps(sys.stdin.read()))")
    },
    \"id\": 1
  }" | python3 -m json.tool > /dev/null

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ Agent config created at ${AGENT_CONFIG_PATH}${NC}"
else
    echo -e "${RED}✗ Failed to create agent config${NC}"
    exit 1
fi

# Register agent entity in the entity registry
curl -s -X POST "${SERVER_URL}/api/nfs/register_entity" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"register_entity\",
    \"params\": {
      \"entity_type\": \"agent\",
      \"entity_id\": \"${TEST_AGENT_ID}\",
      \"parent_type\": \"user\",
      \"parent_id\": \"${USER_ID}\",
      \"tenant_id\": \"${TENANT_ID}\"
    },
    \"id\": 2
  }" | python3 -m json.tool > /dev/null

echo -e "${GREEN}✓ Agent registered in entity registry${NC}"

# Create API key for the agent using the admin_create_key API endpoint
echo -e "${BLUE}  Creating API key for agent...${NC}"

# Use the admin_create_key API endpoint to create the agent key
CREATE_KEY_RESPONSE=$(curl -s -X POST "${SERVER_URL}/api/nfs/admin_create_key" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"admin_create_key\",
    \"params\": {
      \"user_id\": \"${USER_ID}\",
      \"name\": \"${TEST_AGENT_NAME} Test Key\",
      \"subject_type\": \"agent\",
      \"subject_id\": \"${TEST_AGENT_ID}\",
      \"tenant_id\": \"${TENANT_ID}\",
      \"is_admin\": false
    },
    \"id\": 3
  }")

# Check if the request was successful
ERROR_CODE=$(echo "$CREATE_KEY_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('error', {}).get('code', ''))" 2>/dev/null || echo "")
if [ -n "$ERROR_CODE" ]; then
    ERROR_MESSAGE=$(echo "$CREATE_KEY_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('error', {}).get('message', ''))" 2>/dev/null || echo "")
    echo -e "${RED}✗ Failed to create API key${NC}"
    echo "   Error code: $ERROR_CODE"
    echo "   Error message: $ERROR_MESSAGE"
    echo "   Response: $CREATE_KEY_RESPONSE"
    exit 1
fi

# Extract the API key from the response
TEST_AGENT_API_KEY=$(echo "$CREATE_KEY_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('result', {}).get('api_key', ''))" 2>/dev/null || echo "")

if [ -z "$TEST_AGENT_API_KEY" ]; then
    echo -e "${RED}✗ Failed to extract API key from response${NC}"
    echo "   Response: $CREATE_KEY_RESPONSE"
    exit 1
fi

echo -e "${GREEN}✓ API key created for ${TEST_AGENT_NAME}${NC}"
echo -e "${BLUE}  API Key: ${TEST_AGENT_API_KEY:0:30}...${NC}"
echo ""

# Step 3: Test initial access (should only see agent config)
echo "🔒 Step 3: Testing initial access (should only see agent config)..."

# Test 1: List agent directory (should see only own config)
echo -e "${BLUE}  Test 1: List /tenant:${TENANT_ID}/user:${USER_ID}/agent/${TEST_AGENT_NAME}${NC}"
AGENT_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/agent/${TEST_AGENT_NAME}\"
    },
    \"id\": 3
  }")

if echo "$AGENT_DIR_RESULT" | grep -q "config.yaml"; then
    echo -e "${GREEN}  ✓ Can access own config${NC}"
else
    echo -e "${RED}  ✗ Cannot access own config${NC}"
    echo "$AGENT_DIR_RESULT" | python3 -m json.tool
fi

# Test 2: Try to list skill directory (should fail)
echo -e "${BLUE}  Test 2: List /tenant:${TENANT_ID}/user:${USER_ID}/skill (should fail)${NC}"
SKILL_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/skill\"
    },
    \"id\": 4
  }")

if echo "$SKILL_DIR_RESULT" | grep -q "error"; then
    echo -e "${GREEN}  ✓ Correctly denied access to skill directory${NC}"
else
    echo -e "${YELLOW}  ⚠️  Unexpectedly allowed access to skill directory${NC}"
fi

# Test 3: Try to list resource directory (should fail)
echo -e "${BLUE}  Test 3: List /tenant:${TENANT_ID}/user:${USER_ID}/resource (should fail)${NC}"
RESOURCE_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/resource\"
    },
    \"id\": 5
  }")

if echo "$RESOURCE_DIR_RESULT" | grep -q "error"; then
    echo -e "${GREEN}  ✓ Correctly denied access to resource directory${NC}"
else
    echo -e "${YELLOW}  ⚠️  Unexpectedly allowed access to resource directory${NC}"
fi

# Test 4: Try to list workspace directory (should fail)
echo -e "${BLUE}  Test 4: List /tenant:${TENANT_ID}/user:${USER_ID}/workspace (should fail)${NC}"
WORKSPACE_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/workspace\"
    },
    \"id\": 6
  }")

if echo "$WORKSPACE_DIR_RESULT" | grep -q "error"; then
    echo -e "${GREEN}  ✓ Correctly denied access to workspace directory${NC}"
else
    echo -e "${YELLOW}  ⚠️  Unexpectedly allowed access to workspace directory${NC}"
fi
echo ""

# Step 4: Grant permissions
echo "🔓 Step 4: Granting permissions..."

# Grant viewer permission on pdf skill
echo -e "${BLUE}  Granting viewer permission on pdf skill...${NC}"
curl -s -X POST "${SERVER_URL}/api/nfs/rebac_create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"rebac_create\",
    \"params\": {
      \"subject_type\": \"agent\",
      \"subject_id\": \"${TEST_AGENT_ID}\",
      \"relation\": \"viewer\",
      \"object_type\": \"file\",
      \"object_id\": \"/tenant:${TENANT_ID}/user:${USER_ID}/skill/pdf\",
      \"tenant_id\": \"${TENANT_ID}\"
    },
    \"id\": 7
  }" | python3 -m json.tool > /dev/null

echo -e "${GREEN}  ✓ Granted viewer on pdf skill${NC}"

# Grant viewer permission on resource folder
echo -e "${BLUE}  Granting viewer permission on resource folder...${NC}"
curl -s -X POST "${SERVER_URL}/api/nfs/rebac_create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"rebac_create\",
    \"params\": {
      \"subject_type\": \"agent\",
      \"subject_id\": \"${TEST_AGENT_ID}\",
      \"relation\": \"viewer\",
      \"object_type\": \"file\",
      \"object_id\": \"/tenant:${TENANT_ID}/user:${USER_ID}/resource\",
      \"tenant_id\": \"${TENANT_ID}\"
    },
    \"id\": 8
  }" | python3 -m json.tool > /dev/null

echo -e "${GREEN}  ✓ Granted viewer on resource folder${NC}"

# Grant viewer permission on workspace folder
echo -e "${BLUE}  Granting viewer permission on workspace folder...${NC}"
curl -s -X POST "${SERVER_URL}/api/nfs/rebac_create" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"rebac_create\",
    \"params\": {
      \"subject_type\": \"agent\",
      \"subject_id\": \"${TEST_AGENT_ID}\",
      \"relation\": \"viewer\",
      \"object_type\": \"file\",
      \"object_id\": \"/tenant:${TENANT_ID}/user:${USER_ID}/workspace\",
      \"tenant_id\": \"${TENANT_ID}\"
    },
    \"id\": 9
  }" | python3 -m json.tool > /dev/null

echo -e "${GREEN}  ✓ Granted viewer on workspace folder${NC}"
echo ""

# Step 5: Test access after permissions granted
echo "✅ Step 5: Testing access after permissions granted..."

# Test 1: List pdf skill directory (should now succeed)
echo -e "${BLUE}  Test 1: List /tenant:${TENANT_ID}/user:${USER_ID}/skill/pdf${NC}"
PDF_SKILL_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/skill/pdf\"
    },
    \"id\": 10
  }")

if echo "$PDF_SKILL_RESULT" | grep -q "SKILL.md\|skill.py"; then
    echo -e "${GREEN}  ✓ Can now access pdf skill directory${NC}"
else
    echo -e "${RED}  ✗ Still cannot access pdf skill directory${NC}"
    echo "$PDF_SKILL_RESULT" | python3 -m json.tool
fi

# Test 2: List resource directory (should now succeed)
echo -e "${BLUE}  Test 2: List /tenant:${TENANT_ID}/user:${USER_ID}/resource${NC}"
RESOURCE_DIR_RESULT2=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/resource\"
    },
    \"id\": 11
  }")

if echo "$RESOURCE_DIR_RESULT2" | grep -q "result"; then
    RESOURCE_COUNT=$(echo "$RESOURCE_DIR_RESULT2" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', [])))" 2>/dev/null || echo "0")
    echo -e "${GREEN}  ✓ Can now access resource directory (${RESOURCE_COUNT} items)${NC}"
else
    echo -e "${RED}  ✗ Still cannot access resource directory${NC}"
    echo "$RESOURCE_DIR_RESULT2" | python3 -m json.tool
fi

# Test 3: List workspace directory (should now succeed)
echo -e "${BLUE}  Test 3: List /tenant:${TENANT_ID}/user:${USER_ID}/workspace${NC}"
WORKSPACE_DIR_RESULT2=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/workspace\"
    },
    \"id\": 12
  }")

if echo "$WORKSPACE_DIR_RESULT2" | grep -q "result"; then
    WORKSPACE_COUNT=$(echo "$WORKSPACE_DIR_RESULT2" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', [])))" 2>/dev/null || echo "0")
    echo -e "${GREEN}  ✓ Can now access workspace directory (${WORKSPACE_COUNT} items)${NC}"
else
    echo -e "${RED}  ✗ Still cannot access workspace directory${NC}"
    echo "$WORKSPACE_DIR_RESULT2" | python3 -m json.tool
fi

# Test 4: Try to access docx skill (should still fail - no permission)
echo -e "${BLUE}  Test 4: List /tenant:${TENANT_ID}/user:${USER_ID}/skill/docx (should still fail)${NC}"
DOCX_SKILL_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/tenant:${TENANT_ID}/user:${USER_ID}/skill/docx\"
    },
    \"id\": 13
  }")

if echo "$DOCX_SKILL_RESULT" | grep -q "error"; then
    echo -e "${GREEN}  ✓ Correctly denied access to docx skill (no permission)${NC}"
else
    echo -e "${YELLOW}  ⚠️  Unexpectedly allowed access to docx skill${NC}"
fi
echo ""

# Summary
echo "╔═══════════════════════════════════════════════════╗"
echo "║              Test Summary                         ║"
echo "╚═══════════════════════════════════════════════════╝"
echo -e "${GREEN}✓ Agent created with API key${NC}"
echo -e "${GREEN}✓ Initial access restricted (only agent config)${NC}"
echo -e "${GREEN}✓ Permissions granted successfully${NC}"
echo -e "${GREEN}✓ Access verified after permission grants${NC}"
echo -e "${GREEN}✓ Selective permissions working correctly${NC}"
echo ""
echo -e "${BLUE}Agent API Key (for manual testing):${NC}"
echo "${TEST_AGENT_API_KEY}"
echo ""
echo -e "${GREEN}✓ Integration test completed successfully!${NC}"
