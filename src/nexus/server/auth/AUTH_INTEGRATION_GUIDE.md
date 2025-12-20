# Authentication Integration Guide

This guide shows how to integrate the new user authentication system into your Nexus server.

## Overview

The authentication system supports:
- **Username/Password authentication** (database-backed with bcrypt)
- **Google OAuth authentication** (user login)
- **User profile management**
- **Multi-tenant support** (via ReBAC groups)

## Quick Start

### 1. Run Database Migration

```bash
# Apply the user model migration
cd nexus
alembic upgrade head
```

This creates three new tables:
- `users` - Core user accounts
- `user_oauth_accounts` - OAuth account linking
- `external_user_services` - External auth service config

### 2. Initialize Authentication Providers

```python
from sqlalchemy.orm import sessionmaker
from nexus.server.auth.database_local import DatabaseLocalAuth
from nexus.server.auth.oauth_user_auth import OAuthUserAuth
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.server.auth.auth_routes import router, set_auth_provider, set_oauth_provider

# Create session factory
session_factory = sessionmaker(bind=engine)

# Initialize password authentication
auth_provider = DatabaseLocalAuth(
    session_factory=session_factory,
    jwt_secret=os.getenv("NEXUS_JWT_SECRET", "your-secret-key-here"),
    token_expiry=3600  # 1 hour
)

# Initialize OAuth authentication (if using Google OAuth)
oauth_crypto = OAuthCrypto()  # Handles token encryption
oauth_provider = OAuthUserAuth(
    session_factory=session_factory,
    google_client_id=os.getenv("GOOGLE_CLIENT_ID"),
    google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    google_redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/auth/oauth/callback"),
    jwt_secret=os.getenv("NEXUS_JWT_SECRET", "your-secret-key-here"),
    oauth_crypto=oauth_crypto
)

# Set up dependency injection
set_auth_provider(auth_provider)
set_oauth_provider(oauth_provider)

# Include router in FastAPI app
app.include_router(router)
```

### 3. Environment Variables

```bash
# Required for production
export NEXUS_JWT_SECRET="your-long-random-secret-key"

# Required for Google OAuth
export GOOGLE_CLIENT_ID="123456789.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="GOCSPX-xxxxxxxxxxxxx"
export GOOGLE_REDIRECT_URI="http://localhost:8080/auth/oauth/callback"

# Optional: OAuth encryption key (auto-generated if not set)
export NEXUS_OAUTH_ENCRYPTION_KEY="your-encryption-key"
```

## API Endpoints

### Password Authentication

#### Register New User
```http
POST /auth/register
Content-Type: application/json

{
  "email": "alice@example.com",
  "password": "securepassword123",
  "username": "alice",
  "display_name": "Alice Smith"
}

Response:
{
  "user_id": "uuid-here",
  "email": "alice@example.com",
  "username": "alice",
  "display_name": "Alice Smith",
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "message": "User registered successfully"
}
```

#### Login
```http
POST /auth/login
Content-Type: application/json

{
  "identifier": "alice@example.com",  // or username
  "password": "securepassword123"
}

Response:
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "user": {
    "user_id": "uuid-here",
    "email": "alice@example.com",
    "username": "alice",
    ...
  },
  "message": "Login successful"
}
```

#### Get Current User
```http
GET /auth/me
Authorization: Bearer <token>

Response:
{
  "user_id": "uuid-here",
  "email": "alice@example.com",
  "username": "alice",
  "display_name": "Alice Smith",
  "primary_auth_method": "password",
  "is_global_admin": false,
  "email_verified": false,
  "created_at": "2025-12-19T00:00:00Z",
  "last_login_at": "2025-12-19T01:00:00Z"
}
```

#### Change Password
```http
POST /auth/change-password
Authorization: Bearer <token>
Content-Type: application/json

{
  "old_password": "securepassword123",
  "new_password": "newsecurepassword456"
}

Response:
{
  "message": "Password changed successfully",
  "success": true
}
```

#### Update Profile
```http
PATCH /auth/me
Authorization: Bearer <token>
Content-Type: application/json

{
  "display_name": "Alice Johnson",
  "avatar_url": "https://example.com/avatar.jpg"
}

Response:
{
  "user_id": "uuid-here",
  "email": "alice@example.com",
  "display_name": "Alice Johnson",
  "avatar_url": "https://example.com/avatar.jpg",
  ...
}
```

### Google OAuth Authentication

#### Get OAuth Authorization URL
```http
GET /auth/oauth/google/authorize

Response:
{
  "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "state": "random-state-token",
  "message": "Redirect user to auth_url to begin OAuth flow"
}
```

**Client Flow:**
1. Store `state` in session for CSRF protection
2. Redirect user to `auth_url`
3. User authorizes in Google
4. Google redirects back with `code` and `state`
5. Verify `state` matches stored value
6. Call OAuth callback endpoint

#### Handle OAuth Callback
```http
POST /auth/oauth/callback
Content-Type: application/json

{
  "provider": "google",
  "code": "4/0AY0e-...",
  "state": "random-state-token"
}

Response:
{
  "token": "eyJhbGciOiJIUzI1NiIs...",
  "user": {
    "user_id": "uuid-here",
    "email": "alice@example.com",
    "display_name": "Alice Smith",
    "primary_auth_method": "oauth",
    ...
  },
  "is_new_user": true,
  "message": "OAuth authentication successful"
}
```

#### List Linked OAuth Accounts
```http
GET /auth/oauth/accounts
Authorization: Bearer <token>

Response:
[
  {
    "oauth_account_id": "uuid-here",
    "provider": "google",
    "provider_email": "alice@example.com",
    "created_at": "2025-12-19T00:00:00Z",
    "last_used_at": "2025-12-19T01:00:00Z"
  }
]
```

#### Unlink OAuth Account
```http
DELETE /auth/oauth/accounts/{oauth_account_id}
Authorization: Bearer <token>

Response:
{
  "message": "OAuth account unlinked successfully",
  "success": true
}
```

## Testing Authentication

### Python Client Example

```python
import httpx

BASE_URL = "http://localhost:8080"

# Test password authentication
async def test_password_auth():
    async with httpx.AsyncClient() as client:
        # Register
        response = await client.post(
            f"{BASE_URL}/auth/register",
            json={
                "email": "test@example.com",
                "password": "securepassword123",
                "username": "testuser"
            }
        )
        data = response.json()
        token = data["token"]
        print(f"Registered user: {data['email']}")

        # Get user info
        response = await client.get(
            f"{BASE_URL}/auth/me",
            headers={"Authorization": f"Bearer {token}"}
        )
        user = response.json()
        print(f"User info: {user}")

        # Login
        response = await client.post(
            f"{BASE_URL}/auth/login",
            json={
                "identifier": "test@example.com",
                "password": "securepassword123"
            }
        )
        data = response.json()
        print(f"Logged in: {data['user']['email']}")

# Test OAuth authentication
async def test_oauth_auth():
    async with httpx.AsyncClient() as client:
        # Get OAuth URL
        response = await client.get(f"{BASE_URL}/auth/oauth/google/authorize")
        data = response.json()
        print(f"OAuth URL: {data['auth_url']}")
        print(f"State: {data['state']}")

        # After user authorizes in browser, handle callback
        # (Replace 'code' with actual authorization code from Google)
        response = await client.post(
            f"{BASE_URL}/auth/oauth/callback",
            json={
                "provider": "google",
                "code": "4/0AY0e-...",
                "state": data['state']
            }
        )
        data = response.json()
        token = data["token"]
        print(f"OAuth login successful: {data['user']['email']}")
        print(f"Is new user: {data['is_new_user']}")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_password_auth())
    # asyncio.run(test_oauth_auth())
```

### cURL Examples

```bash
# Register user
curl -X POST http://localhost:8080/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test@example.com",
    "password": "securepassword123",
    "username": "testuser"
  }'

# Login
curl -X POST http://localhost:8080/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "identifier": "test@example.com",
    "password": "securepassword123"
  }'

# Get user info (replace TOKEN with actual token)
curl -X GET http://localhost:8080/auth/me \
  -H "Authorization: Bearer TOKEN"

# Get Google OAuth URL
curl -X GET http://localhost:8080/auth/oauth/google/authorize
```

## Security Considerations

### Password Security
- **Minimum length**: 12 characters (enforced at API level)
- **Hashing**: bcrypt with 12 rounds (industry standard)
- **Storage**: Only password hash stored, never plaintext

### OAuth Security
- **Email verification**: Auto-linking only if both emails verified
- **Race condition protection**: Unique constraint on (provider, provider_user_id)
- **Token storage**: ID tokens encrypted at rest using Fernet
- **CSRF protection**: State parameter for OAuth flow

### JWT Tokens
- **Algorithm**: HS256 (HMAC-SHA256)
- **Expiry**: Configurable (default: 1 hour)
- **Secret**: Must be set via environment variable in production

### Multi-Tenant Isolation
- **Tenant membership**: Managed via ReBAC groups
- **No primary_tenant_id**: All relationships via ReBAC
- **Group pattern**: `group:tenant-{tenant_id}`

## TODO: Future Enhancements

The following features have TODO markers and are not yet implemented:

1. **Email Verification**
   - Send verification email on registration
   - Verify email with token
   - Resend verification email

2. **Password Reset**
   - Request password reset via email
   - Reset password with time-limited token
   - Token expiry and security

3. **Token Blacklisting**
   - Server-side logout (invalidate tokens)
   - Token revocation list

4. **External User Services**
   - Support for Auth0, Okta, custom services
   - JWT validation from external services

5. **Frontend Integration**
   - Update multifi frontend to use real OAuth
   - Replace TEMP_API_KEY with proper tokens

## Database Schema

### users table
```sql
CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    username VARCHAR(255),
    email VARCHAR(255),
    display_name VARCHAR(255),
    avatar_url TEXT,
    password_hash VARCHAR(512),  -- bcrypt hash
    primary_auth_method VARCHAR(50) NOT NULL DEFAULT 'password',
    external_user_id VARCHAR(255),
    external_user_service VARCHAR(100),
    is_global_admin INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    deleted_at TIMESTAMP,  -- Soft delete
    email_verified INTEGER DEFAULT 0,
    user_metadata TEXT,  -- JSON
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    last_login_at TIMESTAMP,
    -- Partial unique indexes created via migration
    -- UNIQUE(email) WHERE is_active=1 AND deleted_at IS NULL
    -- UNIQUE(username) WHERE is_active=1 AND deleted_at IS NULL
);
```

### user_oauth_accounts table
```sql
CREATE TABLE user_oauth_accounts (
    oauth_account_id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    provider VARCHAR(50) NOT NULL,
    provider_user_id VARCHAR(255) NOT NULL,
    provider_email VARCHAR(255),
    encrypted_id_token TEXT,  -- Encrypted ID token
    token_expires_at TIMESTAMP,
    provider_profile TEXT,  -- JSON
    created_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    UNIQUE (provider, provider_user_id)
);
```

### external_user_services table
```sql
CREATE TABLE external_user_services (
    service_id VARCHAR(36) PRIMARY KEY,
    service_name VARCHAR(100) UNIQUE NOT NULL,
    auth_endpoint TEXT NOT NULL,
    user_lookup_endpoint TEXT,
    auth_method VARCHAR(50) NOT NULL,
    encrypted_config TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
```

## Support

For questions or issues:
- Check the design document: `nexus/docs/design/user-model-design.md`
- Review implementation notes: `nexus/docs/design/user-model-implementation-notes.md`
- File issues on GitHub
