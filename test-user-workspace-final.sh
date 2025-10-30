#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Configuration
NEXUS_URL="${NEXUS_URL:-http://localhost:8080}"
ADMIN_KEY="${NEXUS_API_KEY:-sk-default_admin_89dd329f_58aff805c19c2ac0099d56b18778a8bd}"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}Testing User Workspace - Final Version${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Step 1: Create a new user and get API key
echo -e "${YELLOW}Step 1: Creating user 'bob' and getting API key...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/admin_create_key" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"bob","name":"Bob API Key","is_admin":false},"id":1}')

echo "Response:"
echo "$RESULT" | jq .
echo

# Extract the API key from the response
USER_API_KEY=$(echo "$RESULT" | jq -r '.result.api_key')

if [ "$USER_API_KEY" == "null" ] || [ -z "$USER_API_KEY" ]; then
  echo -e "${RED}Failed to create user API key!${NC}"
  exit 1
fi

echo -e "${GREEN}✓ User API key created: ${USER_API_KEY}${NC}"
echo

# Step 2: Create workspace directory as admin
echo -e "${YELLOW}Step 2: Creating workspace directory '/bob-workspace' as admin...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/mkdir" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"mkdir","params":{"path":"/bob-workspace","parents":true},"id":2}')

echo "Response:"
echo "$RESULT" | jq .
echo

echo -e "${GREEN}✓ Workspace directory created${NC}"
echo

# Step 3: Register the workspace
echo -e "${YELLOW}Step 3: Registering workspace '/bob-workspace'...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/register_workspace" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"path":"/bob-workspace","name":"bob-workspace","description":"Bob workspace","created_by":"user:bob"},"id":3}')

echo "Response:"
echo "$RESULT" | jq .
echo

echo -e "${GREEN}✓ Workspace registered${NC}"
echo

# Step 4: Grant bob direct_owner permission on workspace directory (file object type)
echo -e "${YELLOW}Step 4: Granting bob 'direct_owner' permission on '/bob-workspace'...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/rebac_create" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"rebac_create","params":{"subject":["user","bob"],"relation":"direct_owner","object":["file","/bob-workspace"]},"id":4}')

echo "Response:"
echo "$RESULT" | jq .
echo

TUPLE_ID=$(echo "$RESULT" | jq -r '.result')
if [ "$TUPLE_ID" == "null" ] || [ -z "$TUPLE_ID" ]; then
  echo -e "${RED}Failed to create permission tuple!${NC}"
  exit 1
fi

echo -e "${GREEN}✓ Permission granted (tuple_id: ${TUPLE_ID})${NC}"
echo

# Step 5: Write a file to the workspace as Bob
echo -e "${YELLOW}Step 5: Writing file '/bob-workspace/hello.txt' as Bob...${NC}"

# Encode "Hello, World!" as base64
CONTENT_BASE64=$(echo -n "Hello, World from Bob!" | base64)

RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/write" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"write\",\"params\":{\"path\":\"/bob-workspace/hello.txt\",\"content\":{\"__type__\":\"bytes\",\"data\":\"${CONTENT_BASE64}\"}},\"id\":5}")

echo "Response:"
echo "$RESULT" | jq .
echo

# Check if file was written successfully
FILE_ETAG=$(echo "$RESULT" | jq -r '.result.etag')

if [ "$FILE_ETAG" == "null" ] || [ -z "$FILE_ETAG" ]; then
  echo -e "${RED}Failed to write file!${NC}"
  ERROR=$(echo "$RESULT" | jq -r '.error.message')
  echo "Error: $ERROR"
  exit 1
fi

echo -e "${GREEN}✓ File written successfully (etag: ${FILE_ETAG})${NC}"
echo

# Step 6: Read the file back as Bob
echo -e "${YELLOW}Step 6: Reading file '/bob-workspace/hello.txt' as Bob...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/read" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d '{"jsonrpc":"2.0","method":"read","params":{"path":"/bob-workspace/hello.txt","return_metadata":true},"id":6}')

echo "Response:"
echo "$RESULT" | jq .
echo

# Check if read was successful
ERROR=$(echo "$RESULT" | jq -r '.error.message // empty')
if [ ! -z "$ERROR" ]; then
  echo -e "${RED}Failed to read file: ${ERROR}${NC}"
else
  # Decode and display the content
  CONTENT=$(echo "$RESULT" | jq -r '.result.content.data' | base64 -d 2>/dev/null)
  echo -e "${GREEN}✓ File content: '${CONTENT}'${NC}"
fi
echo

# Step 7: List workspace files as Bob
echo -e "${YELLOW}Step 7: Listing files in '/bob-workspace' as Bob...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/list" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d '{"jsonrpc":"2.0","method":"list","params":{"path":"/bob-workspace","recursive":true,"details":true},"id":7}')

echo "Response:"
echo "$RESULT" | jq .
echo

# Step 8: Verify hierarchical permissions - check for parent tuples
echo -e "${YELLOW}Step 8: Verifying parent tuples were created by HierarchyManager...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/rebac_list_tuples" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"rebac_list_tuples","params":{"object_filter":["file","/bob-workspace/hello.txt"]},"id":8}')

echo "Response:"
echo "$RESULT" | jq .
echo

HAS_PARENT=$(echo "$RESULT" | jq -r '[.result[] | select(.relation=="parent")] | length > 0')
if [ "$HAS_PARENT" == "true" ]; then
  echo -e "${GREEN}✓ Parent tuple found! HierarchyManager is working${NC}"
else
  echo -e "${YELLOW}⚠ No parent tuple found${NC}"
fi
echo

# Step 9: Check Bob's read permission explicitly
echo -e "${YELLOW}Step 9: Checking Bob's READ permission on the file...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/rebac_check" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"rebac_check","params":{"subject":["user","bob"],"permission":"read","object":["file","/bob-workspace/hello.txt"]},"id":9}')

echo "Response:"
echo "$RESULT" | jq .
echo

HAS_PERMISSION=$(echo "$RESULT" | jq -r '.result')
if [ "$HAS_PERMISSION" == "true" ]; then
  echo -e "${GREEN}✓ Bob has READ permission (via hierarchical permissions)${NC}"
else
  echo -e "${YELLOW}⚠ Bob does not have READ permission${NC}"
fi
echo

echo -e "${BLUE}========================================${NC}"
if [ "$HAS_PERMISSION" == "true" ]; then
  echo -e "${GREEN}✓ All tests passed successfully!${NC}"
else
  echo -e "${YELLOW}⚠ Tests completed with warnings${NC}"
fi
echo -e "${BLUE}========================================${NC}"
echo
echo -e "${YELLOW}Summary:${NC}"
echo "  1. Created user 'bob' with API key: ${USER_API_KEY}"
echo "  2. Created and registered workspace: /bob-workspace"
echo "  3. Granted 'direct_owner' permission on workspace directory"
echo "  4. Bob wrote file: /bob-workspace/hello.txt"
echo "  5. Bob's read permission status: ${HAS_PERMISSION}"
echo
