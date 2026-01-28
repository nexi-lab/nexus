# Testing provision_user RPC Method

This guide shows how to test the newly implemented `provision_user` RPC method with a running Nexus server.

## Prerequisites

1. **Running Nexus Server**: Your server should be running on port 2026 (default)
2. **Admin API Key**: You need an admin API key to test the endpoint
3. **Database Configured**: The server should have a database connection

## Test 1: Provision a New User

Create a new user with all default resources:

```bash
# Set your admin API key
export NEXUS_API_KEY="your-admin-key-here"

# Test provision_user
curl -X POST http://localhost:2026/api/nfs/provision_user \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "email": "alice@example.com",
    "display_name": "Alice Smith"
  }' | jq .
```

**Expected Response:**
```json
{
  "jsonrpc": "2.0",
  "result": {
    "user_id": "alice",
    "tenant_id": "alice",
    "api_key": "sk-...",
    "workspace_path": "/tenant:alice/user:alice/workspace/ws_personal_...",
    "agent_paths": [
      "/tenant:alice/user:alice/agent/ImpersonatedUser/config.yaml",
      "/tenant:alice/user:alice/agent/UntrustedAgent/config.yaml"
    ],
    "skill_paths": [
      "/tenant:alice/user:alice/skill/skill-creator/",
      "/tenant:alice/user:alice/skill/pdf/",
      ...
    ],
    "created_resources": {
      "user": true,
      "tenant": true,
      "directories": [...],
      "workspace": "...",
      "agents": ["impersonated", "untrusted"],
      "skills": [...]
    }
  },
  "id": 1
}
```

## Test 2: Test Idempotency

Call provision_user again with the same user to verify idempotency:

```bash
# Second call with same user
curl -X POST http://localhost:2026/api/nfs/provision_user \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "alice",
    "email": "alice@example.com",
    "display_name": "Alice Smith"
  }' | jq .
```

**Expected Behavior:**
- Should return success (not an error)
- Same `user_id` and `tenant_id`
- Same `workspace_path`
- No duplicate resources created
- Log messages should indicate resources already exist

## Test 3: Provision Without API Key

Test creating a user without generating an API key:

```bash
curl -X POST http://localhost:2026/api/nfs/provision_user \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "bob",
    "email": "bob@company.com",
    "display_name": "Bob Jones",
    "create_api_key": false
  }' | jq .
```

**Expected:**
- `api_key` should be `null`
- All other resources should be created

## Test 4: Provision Without Skills

Test creating a user without importing skills:

```bash
curl -X POST http://localhost:2026/api/nfs/provision_user \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "charlie",
    "email": "charlie@example.com",
    "import_skills": false
  }' | jq .
```

**Expected:**
- `skill_paths` should be empty `[]`
- All other resources should be created

## Test 5: Custom Tenant ID

Test creating a user with a custom tenant_id:

```bash
curl -X POST http://localhost:2026/api/nfs/provision_user \
  -H "Authorization: Bearer $NEXUS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "dave",
    "email": "dave@company.com",
    "tenant_id": "mycompany",
    "display_name": "Dave Wilson"
  }' | jq .
```

**Expected:**
- `tenant_id` should be "mycompany" (not "dave")
- User created under `/tenant:mycompany/user:dave/`

## Verification Steps

### 1. Check Database Records

```bash
# Connect to your database
psql $NEXUS_DATABASE_URL

# Check tenant
SELECT tenant_id, name, is_active FROM tenants WHERE tenant_id = 'alice';

# Check user
SELECT user_id, email, display_name, tenant_id, is_active
FROM users WHERE user_id = 'alice';

# Check API key
SELECT key_id, user_id, subject_type, subject_id, name, created_at
FROM api_keys WHERE user_id = 'alice';
```

### 2. Check File System

```bash
# List user directories (adjust path based on your backend)
ls -la /path/to/nexus/data/dirs/tenant:alice/user:alice/

# Should see:
# - workspace/
# - memory/
# - skill/
# - agent/
# - connector/
# - resource/
```

### 3. Test API Key Works

Use the returned API key to make a test request:

```bash
# Extract API key from provision response
API_KEY="sk-..."  # From provision_user response

# Test the key works
curl -X POST http://localhost:2026/api/nfs/ls \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "/tenant:alice/user:alice/"
  }' | jq .
```

**Expected:**
- Should list directories: workspace/, memory/, skill/, etc.
- Should NOT return authentication error

### 4. Check Agents Were Created

```bash
curl -X POST http://localhost:2026/api/nfs/list_agents \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" | jq .
```

**Expected:**
- Should show 2 agents: ImpersonatedUser and UntrustedAgent
- Each agent should have proper metadata

## OAuth Integration Test

To test OAuth integration, perform an OAuth login:

1. Navigate to: `http://localhost:2026/api/auth/oauth/google/login`
2. Complete Google OAuth flow
3. After successful login, verify:
   - User directories were created
   - Workspace exists
   - Agents were created
   - Skills were imported

Check server logs for:
```
INFO: Provisioned OAuth user resources: {'user_id': '...', 'tenant_id': '...', ...}
```

## Troubleshooting

### Error: "user_id is required"
- Make sure you're passing `user_id` in the request body

### Error: "Valid email required"
- Ensure email contains "@" symbol

### Error: "Failed to create agents"
- Check that `/scripts/_core/agent_manager.py` is accessible
- Verify the scripts directory structure is intact

### Error: "Failed to import skills"
- Verify `/scripts/data/skills/` directory exists
- Check that `.skill` files are present

### No API Key Returned
- Check if `create_api_key: false` was passed
- Verify database API key tables are properly configured

## Success Criteria

✅ User provisioned successfully on first call
✅ Second call (idempotency) succeeds without errors
✅ User record exists in database
✅ Tenant record exists in database
✅ API key works for authentication
✅ All 6 user directories created
✅ Default workspace created and registered
✅ 2 agents created (ImpersonatedUser, UntrustedAgent)
✅ Skills imported (if create_skills=true)
✅ ReBAC permissions granted
✅ OAuth users get provisioned automatically

## Next Steps

Once testing is complete:

1. **Push the branch**:
   ```bash
   git push -u origin feature/provision-user-api
   ```

2. **Create Pull Request**:
   ```bash
   gh pr create --title "Feature: User Provisioning API (Issue #820)" \
     --body "Implements comprehensive user provisioning with OAuth integration"
   ```

3. **Link to Issue**:
   - Reference #820 in the PR description
   - Add implementation details and testing results
