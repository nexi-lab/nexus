# Tenant Strategy: Personal vs Company

## Overview

The system automatically determines tenant strategy based on email domain:
- **Personal email** (gmail.com, outlook.com, etc.) → Personal workspace
- **Company email** (xxx@acme.com) → Company tenant

## Strategy Logic

### Personal Email Domains

Configured in `PERSONAL_EMAIL_DOMAINS`:
- Google: gmail.com, googlemail.com
- Microsoft: hotmail.com, outlook.com, live.com, msn.com
- Yahoo: yahoo.com, ymail.com
- Apple: icloud.com, me.com, mac.com
- Others: aol.com, protonmail.com, zoho.com, fastmail.com, qq.com, 163.com, 126.com

### Detection Function

```python
from nexus.server.auth.tenant_helpers import get_tenant_strategy_from_email

base_slug, tenant_name_base, domain, is_personal = get_tenant_strategy_from_email(email)
```

## Examples

### Example 1: Personal Email (Gmail)

**Input**: `alice@gmail.com`
- Display name: "Alice Smith"

**Result**:
```python
tenant_id = "alice"                    # Email username
name = "Alice's Workspace"             # First name + "'s Workspace"
domain = "gmail.com"                   # Email domain
role = "admin"                         # Admin of personal workspace
is_personal = True
```

**ReBAC**:
```
(user:alice-uuid, admin-of, group:tenant-alice)
```

### Example 2: Personal Email (Outlook)

**Input**: `bob.jones@outlook.com`
- Display name: "Bob Jones"

**Result**:
```python
tenant_id = "bob-jones"                # Normalized email username
name = "Bob's Workspace"               # First name + "'s Workspace"
domain = "outlook.com"
role = "admin"
is_personal = True
```

### Example 3: Company Email (First User)

**Input**: `alice@acme.com`
- Display name: "Alice Smith"

**Result**:
```python
tenant_id = "acme-com"                 # Domain as slug
name = "Acme"                          # Company name from domain
domain = "acme.com"                    # Email domain
role = "member"                        # Regular member
is_personal = False
```

**ReBAC**:
```
(user:alice-uuid, member-of, group:tenant-acme-com)
```

### Example 4: Company Email (Second User)

**Input**: `bob@acme.com`
- Display name: "Bob Jones"

**Result**:
```python
tenant_id = "acme-com"                 # SAME tenant (shared)
name = "Acme"                          # Existing tenant name
domain = "acme.com"
role = "member"
is_personal = False
```

**Behavior**: Joins existing tenant "acme-com"

**ReBAC**:
```
(user:alice-uuid, member-of, group:tenant-acme-com)
(user:bob-uuid, member-of, group:tenant-acme-com)
```

### Example 5: Subdomain Company Email

**Input**: `charlie@eng.techcorp.io`

**Result**:
```python
tenant_id = "eng-techcorp-io"          # Full domain as slug
name = "Eng"                           # Subdomain as company name
domain = "eng.techcorp.io"
role = "member"
is_personal = False
```

### Example 6: No Display Name

**Input**: `david@gmail.com`
- Display name: None

**Result**:
```python
tenant_id = "david"
name = "david's Workspace"             # Fallback to email username
domain = "gmail.com"
role = "admin"
is_personal = True
```

## Comparison Table

| User Email | Display Name | tenant_id | Tenant Name | Role | Type |
|------------|-------------|-----------|-------------|------|------|
| alice@gmail.com | Alice Smith | `alice` | Alice's Workspace | admin | Personal |
| bob@outlook.com | Bob Jones | `bob` | Bob's Workspace | admin | Personal |
| charlie@acme.com | Charlie Lee | `acme-com` | Acme | member | Company |
| david@acme.com | David Kim | `acme-com` | Acme (existing) | member | Company |
| eve@startup.io | Eve Chen | `startup-io` | Startup | member | Company |
| frank.smith@gmail.com | Frank Smith | `frank-smith` | Frank's Workspace | admin | Personal |

## Benefits

### For Personal Users
- ✅ Own workspace immediately
- ✅ Full admin control
- ✅ Friendly name: "Alice's Workspace"
- ✅ No tenant conflicts

### For Company Users
- ✅ Automatic team tenant
- ✅ Shared workspace for same company
- ✅ Company branding: "Acme"
- ✅ Domain verification via DNS (future)

## Future Enhancements

### 1. First Company User Auto-Admin
Promote first user from company to admin:
```python
if not existing_tenant and not is_personal:
    user_role = "admin"  # First user is admin
```

### 2. Domain Verification
Require DNS verification for company domains:
```
TXT record: _nexus-verify=abc123
```

### 3. Domain Invitation Only
Lock company tenants to invited users only:
```python
if not is_personal and not invited:
    raise HTTPException("Company domain requires invitation")
```

### 4. Custom Personal Tenant Names
Allow users to customize their workspace name:
```
"Alice's Workspace" → "Alice's Projects"
```

## Implementation Files

- **Strategy Logic**: [tenant_helpers.py](../src/nexus/server/auth/tenant_helpers.py)
- **OAuth Integration**: [auth_routes.py](../src/nexus/server/auth/auth_routes.py)
- **Models**: [models.py](../src/nexus/storage/models.py)

## Testing

```python
# Test personal email detection
from nexus.server.auth.tenant_helpers import get_tenant_strategy_from_email

# Gmail (personal)
result = get_tenant_strategy_from_email("alice@gmail.com")
assert result == ("alice", "alice", "gmail.com", True)

# Company email
result = get_tenant_strategy_from_email("bob@acme.com")
assert result == ("acme-com", "Acme", "acme.com", False)
```
