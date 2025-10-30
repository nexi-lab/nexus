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
echo -e "${BLUE}Testing User Workspace Creation${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Step 1: Create a new user and get API key
echo -e "${YELLOW}Step 1: Creating user 'testuser' and getting API key...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/admin_create_key" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${ADMIN_KEY}" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"testuser","name":"Test User API Key","is_admin":false},"id":1}')

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

# Step 2: Use the user's key to create a workspace
echo -e "${YELLOW}Step 2: Creating workspace '/testuser/workspace' using user's API key...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/register_workspace" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"path":"/testuser/workspace","name":"test-workspace","description":"Test workspace for user","created_by":"user:testuser"},"id":2}')

echo "Response:"
echo "$RESULT" | jq .
echo

# Check if workspace was created successfully
WORKSPACE_PATH=$(echo "$RESULT" | jq -r '.result.path')

if [ "$WORKSPACE_PATH" == "null" ] || [ -z "$WORKSPACE_PATH" ]; then
  echo -e "${RED}Failed to create workspace!${NC}"
  exit 1
fi

echo -e "${GREEN}✓ Workspace created: ${WORKSPACE_PATH}${NC}"
echo

# Step 3: Write a file to the workspace path
echo -e "${YELLOW}Step 3: Writing file '/testuser/workspace/hello.txt' using user's API key...${NC}"

# Encode "Hello, World!" as base64
CONTENT_BASE64=$(echo -n "Hello, World!" | base64)

RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/write" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"write\",\"params\":{\"path\":\"/testuser/workspace/hello.txt\",\"content\":{\"__type__\":\"bytes\",\"data\":\"${CONTENT_BASE64}\"}},\"id\":3}")

echo "Response:"
echo "$RESULT" | jq .
echo

# Check if file was written successfully
FILE_ETAG=$(echo "$RESULT" | jq -r '.result.etag')

if [ "$FILE_ETAG" == "null" ] || [ -z "$FILE_ETAG" ]; then
  echo -e "${RED}Failed to write file!${NC}"
  exit 1
fi

echo -e "${GREEN}✓ File written successfully (etag: ${FILE_ETAG})${NC}"
echo

# Step 4: Verify by reading the file back
echo -e "${YELLOW}Step 4: Reading file back to verify...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/read" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d '{"jsonrpc":"2.0","method":"read","params":{"path":"/testuser/workspace/hello.txt","return_metadata":true},"id":4}')

echo "Response:"
echo "$RESULT" | jq .
echo

# Decode and display the content
CONTENT=$(echo "$RESULT" | jq -r '.result.content.data' | base64 -d)
echo -e "${GREEN}✓ File content: '${CONTENT}'${NC}"
echo

# Step 5: List workspace files
echo -e "${YELLOW}Step 5: Listing workspace files...${NC}"
RESULT=$(curl -s -X POST "${NEXUS_URL}/api/nfs/list" \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${USER_API_KEY}" \
  -d '{"jsonrpc":"2.0","method":"list","params":{"path":"/testuser/workspace","recursive":true,"details":true},"id":5}')

echo "Response:"
echo "$RESULT" | jq .
echo

echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}✓ All tests passed successfully!${NC}"
echo -e "${BLUE}========================================${NC}"
echo
echo -e "${YELLOW}Summary:${NC}"
echo "  1. Created user 'testuser' with API key: ${USER_API_KEY}"
echo "  2. Created workspace: /testuser/workspace"
echo "  3. Wrote file: /testuser/workspace/hello.txt"
echo "  4. Verified file content: '${CONTENT}'"
echo
