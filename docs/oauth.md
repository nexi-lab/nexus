# OAuth Token Management System

**Version:** 0.7.0
**Issue:** #137

## Overview

Nexus now includes a comprehensive OAuth 2.0 token management system for secure integration with cloud services like Google Drive, Microsoft OneDrive, Dropbox, and more. The system provides:

- **Secure token storage** with encryption at rest
- **Automatic token refresh** when tokens expire
- **Multi-provider support** (Google, Microsoft, etc.)
- **Multi-tenant isolation** for organization security
- **CLI commands** for easy token management
- **Audit logging** for compliance

## Architecture

The OAuth system consists of 4 main components:

```
┌─────────────────────────────────────────────────────────────┐
│                      TokenManager                           │
│  (Centralized credential storage + auto-refresh)            │
└──────────────┬──────────────────────────────┬───────────────┘
               │                              │
       ┌───────▼────────┐            ┌────────▼─────────┐
       │  OAuthProvider │            │   OAuthCrypto    │
       │   (Abstract)   │            │  (Encryption)    │
       └───────┬────────┘            └──────────────────┘
               │
       ┌───────┴────────────────────┐
       │                            │
┌──────▼────────┐      ┌────────────▼──────┐
│ GoogleOAuth   │      │ MicrosoftOAuth    │
│   Provider    │      │   Provider        │
└───────────────┘      └───────────────────┘
```

### 1. **TokenManager** (`token_manager.py`)
- Centralized storage and retrieval of OAuth credentials
- Automatic token refresh using MindsDB's refresh pattern
- Database-backed persistence with encryption
- Tenant isolation for multi-organization deployments

### 2. **OAuthProvider** (`oauth_provider.py`)
- Abstract base class for provider-specific implementations
- Handles OAuth flow (authorization URL, code exchange, token refresh)
- Provider-specific: `GoogleOAuthProvider`, `MicrosoftOAuthProvider`

### 3. **OAuthCrypto** (`oauth_crypto.py`)
- Fernet symmetric encryption (AES-128 + HMAC-SHA256)
- Secure key management via environment variable
- Key rotation support

### 4. **Database Model** (`models.py`)
- `OAuthCredentialModel` stores encrypted tokens
- Indexes for fast lookup by provider, user, tenant
- Audit fields for compliance

## Quick Start

### 1. Setup Encryption Key

```bash
# Generate encryption key
python -c "from nexus.server.auth import OAuthCrypto; print(OAuthCrypto.generate_key())"

# Set environment variable
export NEXUS_OAUTH_ENCRYPTION_KEY="your_generated_key_here"
```

### 2. Initialize OAuth Flow (Google Drive Example)

```python
from nexus.server.auth import TokenManager, GoogleOAuthProvider

# Initialize manager
manager = TokenManager(db_path="~/.nexus/nexus.db")

# Create Google OAuth provider
provider = GoogleOAuthProvider(
    client_id="123456.apps.googleusercontent.com",
    client_secret="GOCSPX-...",
    redirect_uri="http://localhost:8080/oauth/callback",
    scopes=["https://www.googleapis.com/auth/drive"]
)

# Register provider
manager.register_provider("google", provider)

# Get authorization URL
auth_url = provider.get_authorization_url()
print(f"Visit: {auth_url}")

# After user grants permission, exchange code for tokens
credential = await provider.exchange_code(authorization_code)

# Store credential
await manager.store_credential(
    provider="google",
    user_email="alice@example.com",
    credential=credential,
    tenant_id="org_acme"
)
```

### 3. Use Tokens (Automatic Refresh)

```python
# Get valid access token (auto-refreshes if expired)
access_token = await manager.get_valid_token(
    provider="google",
    user_email="alice@example.com",
    tenant_id="org_acme"
)

# Use token for API calls
headers = {"Authorization": f"Bearer {access_token}"}
response = requests.get("https://www.googleapis.com/drive/v3/files", headers=headers)
```

## CLI Commands

### List Credentials

```bash
# List all stored credentials
nexus oauth list

# Filter by tenant
nexus oauth list --tenant-id org_acme
```

**Output:**
```
┌──────────┬─────────────────────┬───────────┬──────────┬─────────────────────┬────────────┐
│ Provider │ User Email          │ Tenant ID │ Status   │ Expires At          │ Last Used  │
├──────────┼─────────────────────┼───────────┼──────────┼─────────────────────┼────────────┤
│ google   │ alice@example.com   │ org_acme  │ 🟢 Valid │ 2025-01-21T10:00:00 │ 2 min ago  │
│ microsoft│ bob@company.com     │ org_other │ 🔴 Expired│ 2025-01-20T09:00:00│ 1 hour ago │
└──────────┴─────────────────────┴───────────┴──────────┴─────────────────────┴────────────┘
```

### Revoke Credential

```bash
# Revoke a credential (marks as revoked + calls provider API)
nexus oauth revoke google alice@example.com

# With tenant isolation
nexus oauth revoke google alice@example.com --tenant-id org_acme
```

### Test Credential

```bash
# Test if credential is valid (attempts auto-refresh)
nexus oauth test google alice@example.com
```

### Initialize OAuth Flow

```bash
# Google Drive
nexus oauth init google \
    --client-id "123456.apps.googleusercontent.com" \
    --client-secret "GOCSPX-..." \
    --scopes "https://www.googleapis.com/auth/drive"

# Microsoft OneDrive
nexus oauth init microsoft \
    --client-id "12345678-1234-1234-1234-123456789012" \
    --client-secret "secret~..." \
    --scopes "Files.ReadWrite.All" \
    --scopes "offline_access"
```

## Supported Providers

| Provider | OAuth Scopes | Notes |
|----------|--------------|-------|
| **Google** | `https://www.googleapis.com/auth/drive` | Google Drive access |
| | `https://www.googleapis.com/auth/gmail.readonly` | Gmail readonly |
| | `https://www.googleapis.com/auth/calendar` | Calendar access |
| **Microsoft** | `Files.ReadWrite.All` | OneDrive access |
| | `Mail.Read`, `Mail.Send` | Outlook access |
| | `Sites.Read.All` | SharePoint access |
| | `offline_access` | **Required** for refresh tokens |

## Security Features

### 1. **Encryption at Rest**
- Tokens encrypted using Fernet (AES-128 CBC + HMAC-SHA256)
- Encryption key stored in environment variable
- Key rotation supported

### 2. **Automatic Token Refresh**
- Follows MindsDB's conditional refresh pattern
- Refreshes tokens automatically when expired
- Updates database with new tokens
- Falls back gracefully if refresh fails

### 3. **Tenant Isolation**
- Each credential tied to a tenant ID
- Database queries filter by tenant
- Prevents cross-tenant token access

### 4. **Audit Logging**
- All token operations logged
- Tracks: creation, updates, refreshes, revocations
- Format: `AUDIT: {operation} | provider={} | user={} | tenant={}`

## Implementation Details

### MindsDB-Inspired Refresh Pattern

```python
# MindsDB pattern (simple and effective)
if credential.is_expired() and credential.refresh_token:
    # Refresh token
    new_credential = await provider.refresh_token(credential)

    # Update database
    model.encrypted_access_token = crypto.encrypt_token(new_credential.access_token)
    model.expires_at = new_credential.expires_at
    model.last_refreshed_at = datetime.now(UTC)
    session.commit()
```

### Database Schema

```sql
CREATE TABLE oauth_credentials (
    credential_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    user_email TEXT NOT NULL,
    tenant_id TEXT,
    encrypted_access_token TEXT NOT NULL,
    encrypted_refresh_token TEXT,
    token_type TEXT DEFAULT 'Bearer',
    expires_at TIMESTAMP,
    scopes TEXT,  -- JSON array
    client_id TEXT,
    token_uri TEXT,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    last_refreshed_at TIMESTAMP,
    revoked INTEGER DEFAULT 0,
    revoked_at TIMESTAMP,
    created_by TEXT,
    last_used_at TIMESTAMP,

    UNIQUE(provider, user_email, tenant_id)
);

CREATE INDEX idx_oauth_provider ON oauth_credentials(provider);
CREATE INDEX idx_oauth_user_email ON oauth_credentials(user_email);
CREATE INDEX idx_oauth_tenant ON oauth_credentials(tenant_id);
CREATE INDEX idx_oauth_expires ON oauth_credentials(expires_at);
CREATE INDEX idx_oauth_revoked ON oauth_credentials(revoked);
```

## Testing

The OAuth system includes comprehensive tests:

```bash
# Run all OAuth tests
python -m pytest tests/unit/server/test_oauth_crypto.py tests/unit/server/test_token_manager.py -v

# Test coverage
# - test_oauth_crypto.py: 14 tests for encryption utilities
# - test_token_manager.py: 16 tests for token management
# - Total: 30 tests, 95%+ coverage for token_manager.py
```

## Future Enhancements

- [ ] Add Dropbox OAuth provider
- [ ] Add Box OAuth provider
- [ ] Implement proper audit trail database table
- [ ] Add health checks for proactive token monitoring
- [ ] Support for OAuth 2.0 PKCE flow (for mobile/SPA)
- [ ] Web UI for OAuth flow management
- [ ] Token usage analytics

## References

- Issue #137: OAuth Token Management System
- MindsDB OAuth implementation: PR #10516, #11428
- RFC 6749: OAuth 2.0 Authorization Framework
- Google OAuth: https://developers.google.com/identity/protocols/oauth2
- Microsoft OAuth: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow

## API Reference

### TokenManager

```python
class TokenManager:
    def __init__(self, db_path: str | None = None, db_url: str | None = None, encryption_key: str | None = None)

    def register_provider(self, provider_name: str, provider: OAuthProvider) -> None

    async def store_credential(self, provider: str, user_email: str, credential: OAuthCredential,
                               tenant_id: str | None = None, created_by: str | None = None) -> str

    async def get_valid_token(self, provider: str, user_email: str, tenant_id: str | None = None) -> str

    async def get_credential(self, provider: str, user_email: str, tenant_id: str | None = None) -> OAuthCredential | None

    async def revoke_credential(self, provider: str, user_email: str, tenant_id: str | None = None) -> bool

    async def list_credentials(self, tenant_id: str | None = None) -> list[dict[str, Any]]

    def close(self) -> None
```

### OAuthProvider

```python
class OAuthProvider(ABC):
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str | None = None, scopes: list[str] | None = None)

    @abstractmethod
    def get_authorization_url(self, state: str | None = None) -> str

    @abstractmethod
    async def exchange_code(self, code: str) -> OAuthCredential

    @abstractmethod
    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential

    @abstractmethod
    async def revoke_token(self, credential: OAuthCredential) -> bool

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool
```

### OAuthCrypto

```python
class OAuthCrypto:
    def __init__(self, encryption_key: str | None = None)

    @staticmethod
    def generate_key() -> str

    def encrypt_token(self, token: str) -> str

    def decrypt_token(self, encrypted_token: str) -> str

    def encrypt_dict(self, data: dict[str, Any]) -> str

    def decrypt_dict(self, encrypted_data: str) -> dict[str, Any]

    def rotate_key(self, old_key: str, new_key: str, encrypted_token: str) -> str
```
