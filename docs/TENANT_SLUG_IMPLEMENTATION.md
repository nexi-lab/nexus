# Tenant Slug Implementation

## Overview

Implemented slug-based `zone_id` system following industry best practices (GitHub, Slack, Notion, etc.). Tenants now use human-readable, URL-friendly identifiers instead of UUIDs or email addresses.

## Changes Summary

### 1. TenantModel (Database Schema)

**File**: `src/nexus/storage/models.py`

Added new `TenantModel` table to store tenant metadata:

```python
class TenantModel(Base):
    zone_id: str          # Primary key: user-provided slug (e.g., "acme", "techcorp")
    name: str               # Display name: "Acme Corporation"
    domain: str | None      # Unique domain: "acme.com"
    description: str | None # Optional description
    settings: str | None    # JSON settings (extensible)
    is_active: int          # Soft delete flag
    deleted_at: datetime    # Soft delete timestamp
    created_at: datetime
    updated_at: datetime
```

**Key Features**:
- `zone_id` is a URL-friendly slug (3-63 chars, lowercase alphanumeric + hyphens)
- `domain` is unique (for company identification)
- Soft delete support
- Timestamps for audit trail

**Migration**: `alembic/versions/add_tenant_model_table.py`

### 2. Tenant Helpers (Validation & Creation)

**File**: `src/nexus/server/auth/tenant_helpers.py`

**Functions**:

#### `validate_zone_id(zone_id: str) -> tuple[bool, str | None]`
Validates zone_id format:
- Length: 3-63 characters
- Format: `^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$`
- Cannot be reserved name (`admin`, `api`, `system`, etc.)

```python
validate_zone_id("acme")        # (True, None)
validate_zone_id("admin")       # (False, "Zone ID 'admin' is reserved")
validate_zone_id("a")           # (False, "Must be 3-63 characters")
```

#### `normalize_to_slug(name: str) -> str`
Converts display name to slug:

```python
normalize_to_slug("Acme Corporation")    # "acme-corporation"
normalize_to_slug("Tech@Startup!!! Inc") # "tech-startup-inc"
```

#### `suggest_zone_id(base_name: str, session: Session) -> str`
Suggests available zone_id:

```python
suggest_zone_id("acme", session)  # "acme" (if available)
suggest_zone_id("acme", session)  # "acme-2" (if "acme" is taken)
```

#### `create_tenant(...)`
Creates tenant with validation.

#### Reserved zone_id values:
```python
RESERVED_TENANT_IDS = {
    "admin", "system", "default", "tenant", "user", "agent", "group", "root",
    "nexus", "api", "auth", "oauth", "login", "signup", "register", "logout",
    "callback", "health", "status", "docs", "swagger", "settings", "billing",
    "support", "help", "pricing", "features"
}
```

### 3. OAuth Callback Integration

**File**: `src/nexus/server/auth/auth_routes.py`

**Changes**: OAuth callback now creates proper tenants with slug IDs

**Before**:
```python
zone_id = user.email  # ❌ "alice@example.com"
```

**After**:
```python
# Generate slug from user info
email_username = user.email.split("@")[0]  # "alice"
suggested_slug = normalize_to_slug(user.display_name or email_username)
zone_id = suggest_zone_id(suggested_slug, session)  # "alice"

# Create tenant metadata
create_tenant(
    session=session,
    zone_id=zone_id,
    name=user.display_name or user.email,
    domain=user.email.split("@")[1],  # "example.com"
    description=f"Personal workspace for {user.display_name}"
)

# Add user to tenant via ReBAC
add_user_to_tenant(
    rebac_manager=rebac_manager,
    user_id=user.user_id,
    zone_id=zone_id,
    role="admin"  # User is admin of their personal tenant
)
```

### 4. Tenant Management API

**File**: `src/nexus/server/auth/tenant_routes.py`

**Endpoints**:

#### `POST /api/tenants` - Create tenant
```json
{
  "zone_id": "acme",  // Optional, auto-generated if not provided
  "name": "Acme Corporation",
  "domain": "acme.com",
  "description": "Enterprise software company"
}
```

Response:
```json
{
  "zone_id": "acme",
  "name": "Acme Corporation",
  "domain": "acme.com",
  "description": "Enterprise software company",
  "is_active": true,
  "created_at": "2025-12-19T12:00:00Z",
  "updated_at": "2025-12-19T12:00:00Z"
}
```

#### `GET /api/tenants/{zone_id}` - Get tenant
#### `GET /api/tenants` - List tenants

### 5. FastAPI Integration

**File**: `src/nexus/server/fastapi_server.py`

Registered tenant routes:
```python
app.include_router(tenant_routes.router)
logger.info("Tenant management routes registered at /api/tenants")
```

## Architecture Decisions

### zone_id = Slug (User-provided)

**Why slug for zone_id?**
- Human-readable: `nexus.com/acme/dashboard` ✅ vs `nexus.com/550e8400.../dashboard` ❌
- Professional: Brand identity in URLs
- Memorable: Easy to communicate ("zone ID is 'acme'")
- Industry standard: GitHub, Slack, Notion all use slugs

### user_id = UUID (Auto-generated) ✅

**Why UUID for user_id?**
- Security: Prevents user enumeration attacks
- Privacy: User identity not exposed in logs/URLs
- Stability: Never changes, even when username changes
- Separation: `user_id` (internal) vs `username` (public identity)

**Current design is correct:**
```python
user_id = "550e8400-e29b-41d4-a716-446655440000"  # Internal, secure
username = "alice"                                  # Public, changeable
```

### ReBAC Integration

Tenant membership still managed via ReBAC:
```
(user:alice, member-of, group:tenant-acme)
(user:alice, admin-of, group:tenant-acme)
```

TenantModel stores **metadata only** (name, domain, settings), not relationships.

## Usage Examples

### 1. Create tenant via API

```bash
curl -X POST http://localhost:8000/api/tenants \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Acme Corporation",
    "domain": "acme.com"
  }'
```

Response:
```json
{
  "zone_id": "acme-corporation",
  "name": "Acme Corporation",
  "domain": "acme.com",
  ...
}
```

### 2. OAuth user registration

When user signs in with Google OAuth:
1. Extract email: `alice@example.com`
2. Generate slug: `alice`
3. Create tenant: `zone_id="alice"`, `domain="example.com"`
4. Add user to tenant: `(user:alice, admin-of, group:tenant-alice)`
5. Create API key with `zone_id="alice"`

### 3. Get user's tenants

```python
from nexus.server.auth.user_helpers import get_user_tenants
from nexus.storage.models import TenantModel

# Get zone IDs from ReBAC
zone_ids = get_user_tenants(rebac_manager, user_id)  # ["acme", "techcorp"]

# Get tenant metadata
tenants = session.query(TenantModel).filter(
    TenantModel.zone_id.in_(zone_ids)
).all()

for tenant in tenants:
    print(f"{tenant.name} ({tenant.domain})")
# Output:
#   Acme Corporation (acme.com)
#   TechCorp Inc (techcorp.com)
```

## Migration Path

### Running the Migration

```bash
# Run migration to create tenants table
alembic upgrade head
```

### Migrating Existing Data

If you have existing users with email-based `zone_id`, migrate them:

```python
from nexus.server.auth.tenant_helpers import normalize_to_slug, create_tenant
from nexus.storage.models import UserModel, TenantModel

# For each user with old email-based zone_id
for user in session.query(UserModel).filter(UserModel.zone_id.like("%@%")).all():
    old_zone_id = user.zone_id  # "alice@example.com"

    # Generate new slug
    new_zone_id = normalize_to_slug(user.username or old_zone_id.split("@")[0])

    # Create tenant metadata
    create_tenant(
        session=session,
        zone_id=new_zone_id,
        name=user.display_name or user.email,
        domain=old_zone_id.split("@")[1] if "@" in old_zone_id else None
    )

    # Update user's zone_id
    user.zone_id = new_zone_id

    # Update API keys
    for api_key in user.api_keys:
        api_key.zone_id = new_zone_id

    session.commit()
```

## Testing

Test tenant slug validation:
```bash
pytest tests/ -k tenant -v
```

## Future Enhancements

1. **Tenant Settings UI**: Frontend for managing tenant metadata
2. **Domain Verification**: Verify domain ownership before allowing registration
3. **Tenant Quotas**: Limit resources per tenant (storage, users, etc.)
4. **Tenant Branding**: Custom logos, colors, themes per tenant
5. **Tenant Billing**: Integration with billing systems
6. **Subdomain Routing**: `acme.nexus.com` instead of `nexus.com/acme`

## References

- [TenantModel schema](../src/nexus/storage/models.py)
- [Tenant helpers](../src/nexus/server/auth/tenant_helpers.py)
- [Tenant routes](../src/nexus/server/auth/tenant_routes.py)
- [OAuth integration](../src/nexus/server/auth/auth_routes.py)
- [User helpers (ReBAC)](../src/nexus/server/auth/user_helpers.py)
