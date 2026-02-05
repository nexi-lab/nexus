#!/usr/bin/env bash
#
# Integration Test: Agent Permission Testing
#
# This test validates the multi-zone permission system by:
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
SERVER_URL="http://localhost:2026"
ZONE_ID="default"
USER_ID="admin"
TEST_AGENT_NAME="TestAgent"
TEST_AGENT_ID="${USER_ID},${TEST_AGENT_NAME}"

# Load configuration from .env
ENV_FILE="${PROJECT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
    echo -e "${GREEN}✓ Loaded configuration from ${ENV_FILE}${NC}"
elif [ -f "${PROJECT_DIR}/.env.example" ]; then
    echo -e "${YELLOW}⚠️  No .env file found, using .env.example${NC}"
    set -a
    source "${PROJECT_DIR}/.env.example"
    set +a
fi

# Use admin API key from env (may be set as NEXUS_API_KEY)
ADMIN_API_KEY="${NEXUS_API_KEY:-sk-default_admin_dddddddd_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee}"

echo "╔═══════════════════════════════════════════════════╗"
echo "║   Agent Permission Integration Test               ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""
echo -e "${BLUE}Configuration:${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Server:       ${SERVER_URL}"
echo "  Zone:         ${ZONE_ID}"
echo "  User:         ${USER_ID}"
echo "  Agent Name:   ${TEST_AGENT_NAME}"
echo "  Agent ID:     ${TEST_AGENT_ID}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Step 1: Check if server is running
echo "🔍 Step 1: Checking server status..."
if ! curl -s -f "${SERVER_URL}/health" > /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Server not running. Please start it first with:${NC}"
    echo "  ./scripts/local-demo.sh --start  # (auto-inits if data dir empty)"
    exit 1
fi
echo -e "${GREEN}✓ Server is running${NC}"
echo ""

# Step 2: Create a new agent with API key
echo "🤖 Step 2: Registering test agent '${TEST_AGENT_NAME}'..."

# Use register_agent API
REGISTER_RESPONSE=$(curl -s -X POST "${SERVER_URL}/api/nfs/register_agent" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${ADMIN_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"register_agent\",
    \"params\": {
      \"agent_id\": \"${TEST_AGENT_ID}\",
      \"name\": \"${TEST_AGENT_NAME}\",
      \"description\": \"Test agent for permission validation\",
      \"generate_api_key\": true,
      \"metadata\": {
        \"platform\": \"test\",
        \"test\": true,
        \"purpose\": \"permission_testing\"
      }
    },
    \"id\": 1
  }")

# Check if the request was successful
ERROR_CODE=$(echo "$REGISTER_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('error', {}).get('code', ''))" 2>/dev/null || echo "")
if [ -n "$ERROR_CODE" ]; then
    ERROR_MESSAGE=$(echo "$REGISTER_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('error', {}).get('message', ''))" 2>/dev/null || echo "")
    echo -e "${RED}✗ Failed to register agent${NC}"
    echo "   Error code: $ERROR_CODE"
    echo "   Error message: $ERROR_MESSAGE"
    echo "   Response: $REGISTER_RESPONSE"
    exit 1
fi

# Extract the API key from the response
TEST_AGENT_API_KEY=$(echo "$REGISTER_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('result', {}).get('api_key', ''))" 2>/dev/null || echo "")

if [ -z "$TEST_AGENT_API_KEY" ]; then
    echo -e "${RED}✗ Failed to extract API key from response${NC}"
    echo "   Response: $REGISTER_RESPONSE"
    exit 1
fi

AGENT_CONFIG_PATH=$(echo "$REGISTER_RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('result', {}).get('config_path', ''))" 2>/dev/null || echo "")

echo -e "${GREEN}✓ Agent registered: ${TEST_AGENT_NAME}${NC}"
echo -e "${GREEN}✓ Config created at: ${AGENT_CONFIG_PATH}${NC}"
echo -e "${GREEN}✓ API key generated${NC}"
echo -e "${BLUE}  API Key: ${TEST_AGENT_API_KEY:0:30}...${NC}"
echo ""

# Track test failures
FAILED_TESTS=0

# Step 3: Test initial access (should only see agent config)
echo "🔒 Step 3: Testing initial access (zero permissions except own config)..."

# Test 1: List agent directory (should see own config - auto-granted)
echo -e "${BLUE}  Test 1: List /zone/${ZONE_ID}/user:${USER_ID}/agent/${TEST_AGENT_NAME}${NC}"
AGENT_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/agent/${TEST_AGENT_NAME}\"
    },
    \"id\": 3
  }")

if echo "$AGENT_DIR_RESULT" | grep -q "config.yaml"; then
    echo -e "${GREEN}  ✓ Can access own config (expected)${NC}"
else
    echo -e "${RED}  ✗ Cannot access own config (FAILED)${NC}"
    echo "$AGENT_DIR_RESULT" | python3 -m json.tool
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 2: Try to list skill directory (should return empty - no permission)
echo -e "${BLUE}  Test 2: List /zone/${ZONE_ID}/user:${USER_ID}/skill (should be empty)${NC}"
SKILL_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/skill\"
    },
    \"id\": 4
  }")

# Check if result array is empty (permission filtering returns empty array, not error)
SKILL_COUNT=$(echo "$SKILL_DIR_RESULT" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', {}).get('files', [])))" 2>/dev/null || echo "-1")
if [ "$SKILL_COUNT" = "0" ]; then
    echo -e "${GREEN}  ✓ Correctly returns empty (no permission)${NC}"
elif [ "$SKILL_COUNT" = "-1" ]; then
    echo -e "${GREEN}  ✓ Error response (also acceptable)${NC}"
else
    echo -e "${RED}  ✗ Unexpectedly has access to skill directory (${SKILL_COUNT} items) (FAILED)${NC}"
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 3: Try to list resource directory (should return empty - no permission)
echo -e "${BLUE}  Test 3: List /zone/${ZONE_ID}/user:${USER_ID}/resource (should be empty)${NC}"
RESOURCE_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/resource\"
    },
    \"id\": 5
  }")

RESOURCE_COUNT=$(echo "$RESOURCE_DIR_RESULT" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', {}).get('files', [])))" 2>/dev/null || echo "-1")
if [ "$RESOURCE_COUNT" = "0" ]; then
    echo -e "${GREEN}  ✓ Correctly returns empty (no permission)${NC}"
elif [ "$RESOURCE_COUNT" = "-1" ]; then
    echo -e "${GREEN}  ✓ Error response (also acceptable)${NC}"
else
    echo -e "${RED}  ✗ Unexpectedly has access to resource directory (${RESOURCE_COUNT} items) (FAILED)${NC}"
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 4: Try to list workspace directory (should return empty - no permission)
echo -e "${BLUE}  Test 4: List /zone/${ZONE_ID}/user:${USER_ID}/workspace (should be empty)${NC}"
WORKSPACE_DIR_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/workspace\"
    },
    \"id\": 6
  }")

WORKSPACE_COUNT=$(echo "$WORKSPACE_DIR_RESULT" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', {}).get('files', [])))" 2>/dev/null || echo "-1")
if [ "$WORKSPACE_COUNT" = "0" ]; then
    echo -e "${GREEN}  ✓ Correctly returns empty (no permission)${NC}"
elif [ "$WORKSPACE_COUNT" = "-1" ]; then
    echo -e "${GREEN}  ✓ Error response (also acceptable)${NC}"
else
    echo -e "${RED}  ✗ Unexpectedly has access to workspace directory (${WORKSPACE_COUNT} items) (FAILED)${NC}"
    FAILED_TESTS=$((FAILED_TESTS + 1))
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
      \"subject\": [\"agent\", \"${TEST_AGENT_ID}\"],
      \"relation\": \"viewer\",
      \"object\": [\"file\", \"/zone/${ZONE_ID}/user:${USER_ID}/skill/pdf\"],
      \"zone_id\": \"${ZONE_ID}\"
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
      \"subject\": [\"agent\", \"${TEST_AGENT_ID}\"],
      \"relation\": \"viewer\",
      \"object\": [\"file\", \"/zone/${ZONE_ID}/user:${USER_ID}/resource\"],
      \"zone_id\": \"${ZONE_ID}\"
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
      \"subject\": [\"agent\", \"${TEST_AGENT_ID}\"],
      \"relation\": \"viewer\",
      \"object\": [\"file\", \"/zone/${ZONE_ID}/user:${USER_ID}/workspace\"],
      \"zone_id\": \"${ZONE_ID}\"
    },
    \"id\": 9
  }" | python3 -m json.tool > /dev/null

echo -e "${GREEN}  ✓ Granted viewer on workspace folder${NC}"
echo ""

# Step 5: Test access after permissions granted
echo "✅ Step 5: Testing access after permissions granted..."

# Test 1: List pdf skill directory (should now succeed)
echo -e "${BLUE}  Test 1: List /zone/${ZONE_ID}/user:${USER_ID}/skill/pdf${NC}"
PDF_SKILL_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/skill/pdf\"
    },
    \"id\": 10
  }")

# Note: pdf skill directory might be empty, so we just verify no error (permission works)
if echo "$PDF_SKILL_RESULT" | grep -q '"result"'; then
    PDF_SKILL_COUNT=$(echo "$PDF_SKILL_RESULT" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', {}).get('files', [])))" 2>/dev/null || echo "-1")
    echo -e "${GREEN}  ✓ Can access pdf skill directory (${PDF_SKILL_COUNT} items)${NC}"
elif echo "$PDF_SKILL_RESULT" | grep -q "error"; then
    echo -e "${RED}  ✗ Permission grant didn't work - still denied (FAILED)${NC}"
    echo "$PDF_SKILL_RESULT" | python3 -m json.tool
    FAILED_TESTS=$((FAILED_TESTS + 1))
else
    echo -e "${RED}  ✗ Unexpected response (FAILED)${NC}"
    echo "$PDF_SKILL_RESULT" | python3 -m json.tool
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 2: List resource directory (should now succeed)
echo -e "${BLUE}  Test 2: List /zone/${ZONE_ID}/user:${USER_ID}/resource${NC}"
RESOURCE_DIR_RESULT2=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/resource\"
    },
    \"id\": 11
  }")

if echo "$RESOURCE_DIR_RESULT2" | grep -q "result"; then
    RESOURCE_COUNT=$(echo "$RESOURCE_DIR_RESULT2" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', [])))" 2>/dev/null || echo "0")
    echo -e "${GREEN}  ✓ Can now access resource directory (${RESOURCE_COUNT} items)${NC}"
else
    echo -e "${RED}  ✗ Still cannot access resource directory (FAILED)${NC}"
    echo "$RESOURCE_DIR_RESULT2" | python3 -m json.tool
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 3: List workspace directory (should now succeed)
echo -e "${BLUE}  Test 3: List /zone/${ZONE_ID}/user:${USER_ID}/workspace${NC}"
WORKSPACE_DIR_RESULT2=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/workspace\"
    },
    \"id\": 12
  }")

if echo "$WORKSPACE_DIR_RESULT2" | grep -q "result"; then
    WORKSPACE_COUNT=$(echo "$WORKSPACE_DIR_RESULT2" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', [])))" 2>/dev/null || echo "0")
    echo -e "${GREEN}  ✓ Can now access workspace directory (${WORKSPACE_COUNT} items)${NC}"
else
    echo -e "${RED}  ✗ Still cannot access workspace directory (FAILED)${NC}"
    echo "$WORKSPACE_DIR_RESULT2" | python3 -m json.tool
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi

# Test 4: Try to access docx skill (should return empty - no permission)
echo -e "${BLUE}  Test 4: List /zone/${ZONE_ID}/user:${USER_ID}/skill/docx (should be empty)${NC}"
DOCX_SKILL_RESULT=$(curl -s -X POST "${SERVER_URL}/api/nfs/list" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${TEST_AGENT_API_KEY}" \
  -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"list\",
    \"params\": {
      \"path\": \"/zone/${ZONE_ID}/user:${USER_ID}/skill/docx\"
    },
    \"id\": 13
  }")

DOCX_COUNT=$(echo "$DOCX_SKILL_RESULT" | python3 -c "import sys, json; data = json.load(sys.stdin); print(len(data.get('result', {}).get('files', [])))" 2>/dev/null || echo "-1")
if [ "$DOCX_COUNT" = "0" ]; then
    echo -e "${GREEN}  ✓ Correctly returns empty (no permission)${NC}"
elif [ "$DOCX_COUNT" = "-1" ]; then
    echo -e "${GREEN}  ✓ Error response (also acceptable)${NC}"
else
    echo -e "${RED}  ✗ Unexpectedly has access to docx skill (${DOCX_COUNT} items) (FAILED)${NC}"
    FAILED_TESTS=$((FAILED_TESTS + 1))
fi
echo ""

# Summary
echo "╔═══════════════════════════════════════════════════╗"
echo "║              Test Summary                         ║"
echo "╚═══════════════════════════════════════════════════╝"

if [ $FAILED_TESTS -eq 0 ]; then
    echo -e "${GREEN}✓ Agent created with API key${NC}"
    echo -e "${GREEN}✓ Initial access restricted (only agent config)${NC}"
    echo -e "${GREEN}✓ Permissions granted successfully${NC}"
    echo -e "${GREEN}✓ Access verified after permission grants${NC}"
    echo -e "${GREEN}✓ Selective permissions working correctly${NC}"
    echo ""
    echo -e "${BLUE}Agent API Key (for manual testing):${NC}"
    echo "${TEST_AGENT_API_KEY}"
    echo ""
    echo -e "${GREEN}✓ All tests passed! Integration test completed successfully!${NC}"
    exit 0
else
    echo -e "${RED}✗ ${FAILED_TESTS} test(s) failed${NC}"
    echo ""
    echo -e "${BLUE}Agent API Key (for manual testing):${NC}"
    echo "${TEST_AGENT_API_KEY}"
    echo ""
    echo -e "${RED}✗ Integration test FAILED${NC}"
    exit 1
fi
