# Nexus Authentication System - Quick Start Guide

## Summary

The user authentication system has been successfully integrated into the Nexus FastAPI server. All **19 unit tests passing** ✅

## Features Implemented

### Phase 1: Core User Model ✅
- **UserModel**: Core user accounts with multi-auth support
- **UserOAuthAccountModel**: OAuth provider linking
- **ExternalUserServiceModel**: External auth service configuration
- Database migration with partial unique indexes for soft delete support
- User helper functions for lookups and ReBAC integration

### Phase 2: Username/Password Authentication ✅
- **DatabaseLocalAuth Provider**: Database-backed password authentication
- User registration with bcrypt password hashing (12 rounds)
- Login with email or username
- Password change functionality
- Profile management (display name, avatar URL)
- JWT token generation (HS256, 1-hour expiry)

### Phase 3: Google OAuth Authentication ✅
- **OAuthUserAuth Provider**: Google OAuth 2.0 integration
- OAuth authorization URL generation
- OAuth callback handling with token exchange
- Automatic user creation from OAuth
- Email-based account linking with verification checks
- Race condition protection via unique constraints
- OAuth account management (list, unlink)

### FastAPI Integration ✅
- Auth routes automatically registered at `/auth` prefix
- Dependency injection for auth providers
- Environment variable configuration
- Graceful handling when OAuth not configured

## API Endpoints

### Registration & Login
- `POST /auth/register` - Register new user
- `POST /auth/login` - Login with email/username and password
- `POST /auth/logout` - Logout (client-side token discard)

### User Profile
- `GET /auth/me` - Get current user info
- `PATCH /auth/me` - Update current user profile

### Password Management
- `POST /auth/change-password` - Change password

### OAuth
- `GET /auth/oauth/providers` - List available OAuth providers
- `GET /auth/oauth/google/authorize` - Get Google OAuth URL
- `POST /auth/oauth/callback` - Handle OAuth callback
- `GET /auth/oauth/accounts` - List linked OAuth accounts
- `DELETE /auth/oauth/accounts/{id}` - Unlink OAuth account

### Email Verification & Password Reset (TODO)
- `POST /auth/verify-email` - Verify email (not implemented)
- `POST /auth/resend-verification` - Resend verification (not implemented)
- `POST /auth/reset-password` - Request password reset (not implemented)
- `POST /auth/reset-password/confirm` - Confirm password reset (not implemented)

## Environment Variables

### Required
- `NEXUS_JWT_SECRET` - JWT signing secret (auto-generated if not set, but tokens invalid after restart)

### Optional (for OAuth)
- `GOOGLE_CLIENT_ID` - Google OAuth client ID
- `GOOGLE_CLIENT_SECRET` - Google OAuth client secret
- `GOOGLE_REDIRECT_URI` - OAuth redirect URI (default: `http://localhost:2026/auth/oauth/callback`)

## Database Migration

Run Alembic migration to create user tables:

```bash
cd nexus
alembic upgrade head
```

This creates:
- `users` table with partial unique indexes
- `user_oauth_accounts` table
- `external_user_services` table

## Testing

### Unit Tests (19 tests, all passing ✅)
```bash
python -m pytest tests/test_user_auth.py -v
```

Tests cover:
- User registration and duplicate detection
- Login with email and username
- Password changes and validation
- Profile updates
- OAuth URL generation and account management
- User helper functions
- Full authentication flow integration

## Example Usage

### 1. Register a New User

```bash
curl -X POST http://localhost:2026/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "alice@example.com",
    "password": "securepassword123",
    "username": "alice",
    "display_name": "Alice Smith"
  }'
```

Response:
```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "email": "alice@example.com",
  "username": "alice",
  "display_name": "Alice Smith",
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "message": "User registered successfully"
}
```

### 2. Login

```bash
curl -X POST http://localhost:2026/auth/login \
  -H "Content-Type: application/json" \
  -d '{
    "identifier": "alice@example.com",
    "password": "securepassword123"
  }'
```

Response:
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user": {
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "alice@example.com",
    "username": "alice",
    "display_name": "Alice Smith",
    "primary_auth_method": "password",
    "is_global_admin": false,
    "email_verified": false
  },
  "message": "Login successful"
}
```

### 3. Access Protected Endpoint

```bash
curl -X GET http://localhost:2026/auth/me \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
```

### 4. OAuth Flow

#### Step 1: Get Authorization URL
```bash
curl http://localhost:2026/auth/oauth/google/authorize
```

Response:
```json
{
  "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...",
  "state": "random-state-token",
  "message": "Redirect user to auth_url to begin OAuth flow"
}
```

#### Step 2: Handle Callback
After user authorizes, Google redirects to your callback with a `code` parameter.

```bash
curl -X POST http://localhost:2026/auth/oauth/callback \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "google",
    "code": "authorization-code-from-google",
    "state": "random-state-token"
  }'
```

Response:
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user": {
    "user_id": "...",
    "email": "alice@gmail.com",
    "primary_auth_method": "oauth"
  },
  "is_new_user": true,
  "message": "OAuth authentication successful"
}
```

## Code Structure

### Auth Providers
- `nexus/src/nexus/server/auth/database_local.py` - Password authentication
- `nexus/src/nexus/server/auth/oauth_user_auth.py` - OAuth authentication
- `nexus/src/nexus/server/auth/local.py` - JWT token handling
- `nexus/src/nexus/server/auth/oauth_crypto.py` - OAuth token encryption

### API Routes
- `nexus/src/nexus/server/auth/auth_routes.py` - FastAPI endpoints

### Database Models
- `nexus/src/nexus/storage/models.py` - UserModel, UserOAuthAccountModel, ExternalUserServiceModel

### Helpers
- `nexus/src/nexus/server/auth/user_helpers.py` - User lookup and ReBAC functions

### Tests
- `nexus/tests/test_user_auth.py` - 19 comprehensive unit tests

## Integration with FastAPI Server

The authentication routes are automatically registered when:
1. A `database_url` is provided to `create_app()`
2. The database contains the user auth tables

The `fastapi_server.py` now:
- Creates `DatabaseLocalAuth` provider automatically
- Creates `OAuthUserAuth` provider if Google credentials are set
- Sets up dependency injection
- Includes `/auth` router

## Security Features

- ✅ Bcrypt password hashing (12 rounds)
- ✅ JWT token signing with HS256
- ✅ Token expiration (1 hour, configurable)
- ✅ Password minimum length (12 characters)
- ✅ Email/username uniqueness for active users
- ✅ Soft delete support (allows email/username reuse)
- ✅ OAuth token encryption (Fernet)
- ✅ Race condition protection
- ✅ Email verification checks before account linking
- ⚠️ TODO: Email verification
- ⚠️ TODO: Password reset via email
- ⚠️ TODO: Server-side token blacklisting for logout

## Next Steps

1. **Email Verification** - Implement email sending service and verification flow
2. **Password Reset** - Implement password reset token generation and email sending
3. **Token Blacklisting** - Add Redis/database-backed token revocation for logout
4. **Rate Limiting** - Add rate limiting to login/register endpoints
5. **2FA/MFA** - Add two-factor authentication support
6. **Session Management** - Add session tracking and management
7. **Audit Logging** - Log authentication events for security monitoring

## Documentation

For detailed integration guide, see:
- `nexus/src/nexus/server/auth/AUTH_INTEGRATION_GUIDE.md`

## Support

For issues or questions, check:
- Unit tests: `tests/test_user_auth.py`
- Implementation: `src/nexus/server/auth/`
- Migration: `alembic/versions/add_user_model_tables.py`
