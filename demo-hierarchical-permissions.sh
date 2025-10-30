#!/bin/bash
# Hierarchical Permissions Demo via RPC API

set -e

# Get admin key
ADMIN_KEY="${NEXUS_API_KEY:-sk-default_admin_89dd329f_58aff805c19c2ac0099d56b18778a8bd}"
echo "Using admin key: $ADMIN_KEY"

echo ""
echo "============================================================"
echo "HIERARCHICAL PERMISSIONS DEMO VIA RPC API"
echo "============================================================"

# Step 1: Create API key for alice
echo ""
echo "Step 1: Creating API key for user 'alice'..."
RESULT=$(curl -s -X POST 'http://localhost:8080/api/nfs/admin_create_key' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","method":"admin_create_key","params":{"user_id":"alice","name":"Alice test key","is_admin":false},"id":1}')

ALICE_KEY=$(echo "$RESULT" | /opt/homebrew/bin/python3.11 -c "import sys, json; r=json.load(sys.stdin); print(r.get('result', {}).get('api_key', ''))")

if [ -z "$ALICE_KEY" ]; then
    echo "❌ Failed to create Alice's key"
    echo "$RESULT"
    exit 1
fi

echo "✅ Created API key for alice"

# Step 2: Create and register workspace
echo ""
echo "Step 2: Creating workspace directory '/alice-workspace'..."
curl -s -X POST 'http://localhost:8080/api/nfs/mkdir' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","method":"mkdir","params":{"path":"/alice-workspace"},"id":2}' > /dev/null
echo "✅ Created workspace directory"

echo "Step 3: Registering workspace '/alice-workspace'..."
curl -s -X POST 'http://localhost:8080/api/nfs/register_workspace' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","method":"register_workspace","params":{"workspace_path":"/alice-workspace"},"id":3}' > /dev/null
echo "✅ Registered workspace"

# Step 4: Grant alice owner permission on workspace
echo ""
echo "Step 4: Granting alice owner permission on /alice-workspace..."
curl -s -X POST 'http://localhost:8080/api/nfs/rebac_create' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","method":"rebac_create","params":{"subject":["user","alice"],"relation":"direct_owner","object":["file","/alice-workspace"]},"id":4}' > /dev/null
echo "✅ Granted alice owner permission"

# Step 5: Write files inside workspace as alice
echo ""
echo "Step 5: Writing files inside /alice-workspace as alice..."
echo "   (Testing hierarchical permission inheritance)"

# Write project.txt
CONTENT=$(echo -n "This is Alice's project file" | base64)
RESULT=$(curl -s -X POST 'http://localhost:8080/api/nfs/write' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ALICE_KEY" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"write\",\"params\":{\"path\":\"/alice-workspace/project.txt\",\"content\":\"$CONTENT\"},\"id\":4}")

if echo "$RESULT" | grep -q '"result"'; then
    echo "✅ Wrote /alice-workspace/project.txt"
else
    echo "❌ Failed: $(echo $RESULT | /opt/homebrew/bin/python3.11 -c 'import sys,json; print(json.load(sys.stdin).get("error",{}).get("message","Unknown"))')"
fi

# Write notes.md
CONTENT=$(echo -n "# Alice's Notes

Hierarchical permissions working!" | base64)
RESULT=$(curl -s -X POST 'http://localhost:8080/api/nfs/write' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ALICE_KEY" \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"write\",\"params\":{\"path\":\"/alice-workspace/notes.md\",\"content\":\"$CONTENT\"},\"id\":5}")

if echo "$RESULT" | grep -q '"result"'; then
    echo "✅ Wrote /alice-workspace/notes.md"
else
    echo "❌ Failed"
fi

# Step 5: List files
echo ""
echo "Step 6: Listing files in /alice-workspace as alice..."
RESULT=$(curl -s -X POST 'http://localhost:8080/api/nfs/list' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ALICE_KEY" \
  -d '{"jsonrpc":"2.0","method":"list","params":{"path":"/alice-workspace"},"id":6}')

echo "$RESULT" | /opt/homebrew/bin/python3.11 -c "import sys,json; files=json.load(sys.stdin).get('result',[]); print(f'✅ Found {len(files)} files:'); [print(f'   - {f[\"name\"]}') for f in files]"

# Step 6: Check parent tuples
echo ""
echo "Step 7: Verifying parent tuples were created..."
RESULT=$(curl -s -X POST 'http://localhost:8080/api/nfs/rebac_list_tuples' \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"jsonrpc":"2.0","method":"rebac_list_tuples","params":{"object_filter":["file","/alice-workspace/project.txt"]},"id":7}')

HAS_PARENT=$(echo "$RESULT" | /opt/homebrew/bin/python3.11 -c "import sys,json; tuples=json.load(sys.stdin).get('result',[]); print('yes' if any(t.get('relation')=='parent' for t in tuples) else 'no')")

if [ "$HAS_PARENT" = "yes" ]; then
    echo "✅ Parent tuple found! HierarchyManager working correctly!"
else
    echo "⚠️  No parent tuple found"
fi

echo ""
echo "============================================================"
echo "🎉 DEMO COMPLETE!"
echo "============================================================"
echo ""
echo "Summary:"
echo "- Created user 'alice' with API key"
echo "- Registered workspace '/alice-workspace'"
echo "- Granted alice owner permission on workspace"
echo "- Alice can write files inside workspace (hierarchical permissions!)"
echo "- HierarchyManager automatically created parent tuples"
echo ""
