# E2E OAuth Testing Guide

## Prerequisites

1. **PostgreSQL running**:
```bash
docker compose -f docker-compose.demo.yml up postgres -d
```

2. **Run migrations**:
```bash
cd nexus
alembic upgrade head
```

3. **Backend running**:
```bash
cd nexus
python -m nexus.server.fastapi_server
```

4. **Frontend running**:
```bash
cd nexus-frontend
npm run dev
```

## Step 1: Clean Database

Run the cleanup script to start fresh:

```bash
cd nexus
python scripts/cleanup_users.py
```

This will delete:
- All users
- OAuth accounts
- API keys
- Tenants
- ReBAC tuples
- Entity registry entries

## Step 2: Test Personal Email (Gmail)

### Test Case 1: Gmail User

**Email**: Use your personal Gmail account (e.g., `alice@gmail.com`)

**Expected Result**:
```
zone_id = "alice"
name      = "Alice's Workspace"  (or "alice's Workspace" if no display name)
domain    = "gmail.com"
role      = "admin"
```

**Steps**:
1. Go to: http://localhost:5173/login
2. Click "Sign in with Google"
3. Login with Gmail account
4. After redirect, you should be logged in

**Verify**:
```bash
# Connect to PostgreSQL
docker exec -it nexus-postgres psql -U postgres -d nexus

# Check user
SELECT user_id, email, username, display_name, zone_id FROM users;

# Check tenant
SELECT zone_id, name, domain, description FROM tenants;

# Check ReBAC membership
SELECT subject_id, relation, object_id FROM rebac_tuples WHERE object_type = 'group';

# Exit
\q
```

**Expected Output**:
```sql
-- users table
user_id     | email           | username | display_name | zone_id
------------|-----------------|----------|--------------|----------
<uuid>      | alice@gmail.com | alice    | Alice Smith  | alice

-- tenants table
zone_id | name              | domain     | description
----------|-------------------|------------|-----------------------------
alice     | Alice's Workspace | gmail.com  | Personal workspace for Alice Smith

-- rebac_tuples table
subject_id | relation  | object_id
-----------|-----------|------------------
<uuid>     | admin-of  | tenant-alice
```

## Step 3: Test Company Email

### Test Case 2: Company Email (First User)

**Email**: Use a company email (e.g., `bob@acme.com`)
- Note: You'll need to use a real domain you control for Google OAuth, or use a test OAuth provider

**Expected Result**:
```
zone_id = "acme-com"
name      = "Acme"
domain    = "acme.com"
role      = "member"
```

**Verify**:
```sql
-- users table
user_id | email         | display_name | zone_id
--------|---------------|--------------|----------
<uuid>  | bob@acme.com  | Bob Jones    | acme-com

-- tenants table
zone_id | name  | domain    | description
----------|-------|-----------|--------------------------------
acme-com  | Acme  | acme.com  | Organization workspace for acme.com

-- rebac_tuples table
subject_id | relation   | object_id
-----------|------------|------------------
<uuid>     | member-of  | tenant-acme-com
```

### Test Case 3: Company Email (Second User)

**Email**: Use another email from SAME domain (e.g., `charlie@acme.com`)

**Expected Result**:
```
zone_id = "acme-com"  (SAME tenant!)
name      = "Acme"      (existing tenant)
domain    = "acme.com"
role      = "member"
```

**Verify**:
```sql
-- users table (2 users now!)
user_id  | email            | display_name  | zone_id
---------|------------------|---------------|----------
<uuid-1> | bob@acme.com     | Bob Jones     | acme-com
<uuid-2> | charlie@acme.com | Charlie Lee   | acme-com

-- tenants table (still just 1 tenant!)
zone_id | name  | domain
----------|-------|----------
acme-com  | Acme  | acme.com

-- rebac_tuples table (both users in same tenant!)
subject_id | relation  | object_id
-----------|-----------|------------------
<uuid-1>   | member-of | tenant-acme-com
<uuid-2>   | member-of | tenant-acme-com
```

## Step 4: Test Different Email Providers

Test with various personal email providers:

| Email | zone_id | Tenant Name | Expected |
|-------|-----------|-------------|----------|
| alice@gmail.com | `alice` | Alice's Workspace | ✅ Personal |
| bob@outlook.com | `bob` | Bob's Workspace | ✅ Personal |
| charlie@yahoo.com | `charlie` | Charlie's Workspace | ✅ Personal |
| david@icloud.com | `david` | David's Workspace | ✅ Personal |
| eve@protonmail.com | `eve` | Eve's Workspace | ✅ Personal |
| frank@acme.com | `acme-com` | Acme | ✅ Company |
| grace@techcorp.io | `techcorp-io` | Techcorp | ✅ Company |

## Step 5: Check API Key Creation

Verify that API keys are created automatically:

```sql
-- Check API keys
SELECT key_id, user_id, name, zone_id, subject_type, subject_id
FROM api_keys
ORDER BY created_at DESC;
```

**Expected**: Each user should have 1 API key with their zone_id.

## Step 6: Test API with Bearer Token

After logging in, check the browser console or network tab for the JWT token.

```bash
# Get user info
curl -H "Authorization: Bearer <your-jwt-token>" \
  http://localhost:8000/api/whoami

# Expected response:
{
  "authenticated": true,
  "subject_type": "user",
  "subject_id": "<user-uuid>",
  "zone_id": "alice",  # or "acme-com"
  "is_admin": false,
  "user": "<user-uuid>"
}
```

## Step 7: Verify Tenant Membership via API

```bash
# List tenants (future endpoint)
curl -H "Authorization: Bearer <your-jwt-token>" \
  http://localhost:8000/api/tenants

# Get specific tenant
curl -H "Authorization: Bearer <your-jwt-token>" \
  http://localhost:8000/api/tenants/alice
```

## Common Issues

### Issue 1: OAuth Callback Fails

**Symptom**: Redirect to `/oauth/callback` but error shown

**Check**:
1. Google OAuth credentials configured correctly
2. Callback URL in Google Console: `http://localhost:8000/auth/oauth/google/callback`
3. Check backend logs: `docker logs nexus-backend`

### Issue 2: Tenant Not Created

**Symptom**: User created but tenant table is empty

**Debug**:
```bash
# Check backend logs
docker logs nexus-backend | grep -i tenant

# Look for:
# "Created personal tenant 'alice'"
# "Created company tenant 'acme-com'"
```

### Issue 3: Domain Not Recognized

**Symptom**: Company email treated as personal

**Fix**: Add domain to `PERSONAL_EMAIL_DOMAINS` if it's actually personal, or remove if it's company.

File: `nexus/src/nexus/server/auth/tenant_helpers.py`

## Cleanup Between Tests

To reset and test again:

```bash
# Clean database
python scripts/cleanup_users.py

# Restart services
docker compose -f docker-compose.demo.yml restart backend
```

## Success Criteria

✅ Personal email creates personal workspace
✅ Company email creates company tenant
✅ Second user from same company joins existing tenant
✅ Tenant names are user-friendly
✅ API keys created automatically
✅ ReBAC relationships established
✅ JWT token works for API calls

## Next Steps

After E2E testing works:
1. Add domain verification for company emails
2. Implement tenant switching UI
3. Add tenant invitation system
4. Implement tenant admin promotion logic
