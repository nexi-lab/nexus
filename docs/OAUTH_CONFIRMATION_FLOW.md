# OAuth Confirmation Flow Implementation

## Overview

Implemented a user confirmation flow for OAuth login that allows new users to review and customize their account information before creation.

## Changes Made

### Backend Changes

#### New API Endpoints

1. **POST `/auth/oauth/check`** - Check if OAuth user exists and needs confirmation
   - **Input**: `{ provider, code, state }`
   - **Output** (Existing User): Regular OAuth callback response with JWT token
   - **Output** (New User): Confirmation response with pending token

2. **POST `/auth/oauth/confirm`** - Complete user registration after confirmation
   - **Input**: `{ pending_token, tenant_name? }`
   - **Output**: OAuth callback response with JWT token

#### New Response Models

```typescript
// Confirmation response for new users
interface OAuthConfirmationResponse {
  needs_confirmation: true;
  pending_token: string;  // Token to complete registration (valid for 10 minutes)
  user_info: {
    email: string;
    display_name: string | null;
    avatar_url: string | null;
    oauth_provider: string;
    oauth_code: string;
    oauth_state: string | null;
  };
  tenant_info: {
    tenant_id: string;
    name: string;
    domain: string | null;
    description: string | null;
    is_personal: boolean;
    can_edit_name: boolean;  // true for personal workspaces
  };
  message: string;
}

// Regular OAuth response (for existing users or after confirmation)
interface OAuthCallbackResponse {
  token: string;
  user: object;
  is_new_user: boolean;
  api_key: string | null;
  tenant_id: string | null;
  message: string;
  needs_confirmation: false;
}
```

### Flow Diagram

**Existing User Flow:**
```
1. User clicks "Sign in with Google"
2. Redirected to Google OAuth
3. Google redirects to frontend callback
4. Frontend calls POST /auth/oauth/check with code
5. Backend checks - user EXISTS
6. Returns JWT token immediately
7. Frontend stores token, redirects to dashboard
```

**New User Flow:**
```
1. User clicks "Sign in with Google"
2. Redirected to Google OAuth
3. Google redirects to frontend callback
4. Frontend calls POST /auth/oauth/check with code
5. Backend checks - user DOES NOT EXIST
6. Returns confirmation data with pending_token
7. Frontend shows confirmation page with:
   - User info (email, name, avatar)
   - Tenant info (name, domain, type)
   - Option to edit tenant name (if personal workspace)
8. User confirms or customizes tenant name
9. Frontend calls POST /auth/oauth/confirm with:
   - pending_token
   - tenant_name (if customized)
10. Backend creates user, tenant, and API key
11. Returns JWT token
12. Frontend stores token, redirects to dashboard
```

## Frontend Implementation Guide

### 1. Modify OAuth Callback Handler

Update your OAuth callback to use `/auth/oauth/check` instead of `/auth/oauth/callback`:

```typescript
// In your OAuth callback component (e.g., OAuthCallback.tsx)
async function handleOAuthCallback(code: string, state: string) {
  try {
    const response = await fetch('http://localhost:2026/auth/oauth/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: 'google',
        code,
        state,
      }),
    });

    const data = await response.json();

    // Check if confirmation is needed
    if (data.needs_confirmation) {
      // New user - show confirmation page
      showConfirmationPage(data);
    } else {
      // Existing user - login complete
      localStorage.setItem('auth_token', data.token);
      navigate('/dashboard');
    }
  } catch (error) {
    console.error('OAuth check failed:', error);
  }
}
```

### 2. Create Confirmation Page Component

Create a new component to show confirmation page:

```typescript
// ConfirmationPage.tsx
import React, { useState } from 'react';

interface ConfirmationPageProps {
  confirmationData: {
    pending_token: string;
    user_info: {
      email: string;
      display_name: string | null;
      avatar_url: string | null;
    };
    tenant_info: {
      tenant_id: string;
      name: string;
      domain: string | null;
      is_personal: boolean;
      can_edit_name: boolean;
    };
  };
}

export function ConfirmationPage({ confirmationData }: ConfirmationPageProps) {
  const { user_info, tenant_info, pending_token } = confirmationData;
  const [tenantName, setTenantName] = useState(tenant_info.name);
  const [isLoading, setIsLoading] = useState(false);

  const handleConfirm = async () => {
    setIsLoading(true);

    try {
      const response = await fetch('http://localhost:2026/auth/oauth/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pending_token,
          tenant_name: tenant_info.can_edit_name ? tenantName : null,
        }),
      });

      const data = await response.json();

      // Store auth token and redirect
      localStorage.setItem('auth_token', data.token);
      localStorage.setItem('api_key', data.api_key);
      window.location.href = '/dashboard';
    } catch (error) {
      console.error('Confirmation failed:', error);
      alert('Registration failed. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="confirmation-container">
      <h1>Confirm Your Information</h1>

      {/* Section 1: User Information */}
      <section className="user-info-section">
        <h2>User Information</h2>
        <div className="info-card">
          {user_info.avatar_url && (
            <img src={user_info.avatar_url} alt="Profile" className="avatar" />
          )}
          <div>
            <p><strong>Name:</strong> {user_info.display_name || 'Not provided'}</p>
            <p><strong>Email:</strong> {user_info.email}</p>
          </div>
        </div>
      </section>

      {/* Section 2: Tenant Information */}
      <section className="tenant-info-section">
        <h2>Workspace Information</h2>
        <div className="info-card">
          <p>
            <strong>Type:</strong>{' '}
            {tenant_info.is_personal ? 'Personal Workspace' : 'Company Workspace'}
          </p>
          <p><strong>Domain:</strong> {tenant_info.domain}</p>

          {/* Allow editing tenant name for personal workspaces */}
          {tenant_info.can_edit_name ? (
            <div className="edit-tenant-name">
              <label htmlFor="tenant-name">
                <strong>Workspace Name:</strong>
              </label>
              <input
                id="tenant-name"
                type="text"
                value={tenantName}
                onChange={(e) => setTenantName(e.target.value)}
                placeholder="Enter workspace name"
              />
              <small>You can customize your personal workspace name</small>
            </div>
          ) : (
            <p><strong>Workspace Name:</strong> {tenant_info.name}</p>
          )}
        </div>
      </section>

      {/* Confirmation Button */}
      <button
        onClick={handleConfirm}
        disabled={isLoading}
        className="confirm-button"
      >
        {isLoading ? 'Creating Account...' : 'Confirm and Continue'}
      </button>
    </div>
  );
}
```

### 3. Update OAuth Callback Route

Add routing logic to show confirmation page:

```typescript
// In your router or OAuth callback component
function OAuthCallbackHandler() {
  const [confirmationData, setConfirmationData] = useState(null);
  const searchParams = new URLSearchParams(window.location.search);
  const code = searchParams.get('code');
  const state = searchParams.get('state');

  useEffect(() => {
    if (code) {
      handleOAuthCallback(code, state);
    }
  }, [code, state]);

  async function handleOAuthCallback(code, state) {
    const response = await fetch('http://localhost:2026/auth/oauth/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: 'google', code, state }),
    });

    const data = await response.json();

    if (data.needs_confirmation) {
      setConfirmationData(data);
    } else {
      // Existing user - complete login
      localStorage.setItem('auth_token', data.token);
      window.location.href = '/dashboard';
    }
  }

  if (confirmationData) {
    return <ConfirmationPage confirmationData={confirmationData} />;
  }

  return <div>Processing OAuth callback...</div>;
}
```

### 4. Styling Example

```css
.confirmation-container {
  max-width: 600px;
  margin: 50px auto;
  padding: 30px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
}

.info-card {
  background: #f5f5f5;
  padding: 20px;
  border-radius: 6px;
  margin: 15px 0;
}

.avatar {
  width: 64px;
  height: 64px;
  border-radius: 50%;
  margin-right: 15px;
}

.edit-tenant-name {
  margin-top: 15px;
}

.edit-tenant-name input {
  width: 100%;
  padding: 10px;
  margin: 8px 0;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 16px;
}

.confirm-button {
  width: 100%;
  padding: 12px;
  background: #4285f4;
  color: white;
  border: none;
  border-radius: 4px;
  font-size: 16px;
  cursor: pointer;
  margin-top: 20px;
}

.confirm-button:hover {
  background: #357ae8;
}

.confirm-button:disabled {
  background: #ccc;
  cursor: not-allowed;
}
```

## Testing the Flow

### Test Case 1: New Personal Email User (Gmail)

1. Clean database: `python scripts/cleanup_users.py`
2. Go to http://localhost:5173/login
3. Click "Sign in with Google"
4. Login with `alice@gmail.com`
5. **Expected**: Confirmation page appears with:
   - User info: Alice's name and email
   - Tenant type: "Personal Workspace"
   - Tenant name: "Alice's Workspace" (editable)
6. Optionally edit workspace name to "Alice's Projects"
7. Click "Confirm and Continue"
8. **Expected**: Redirected to dashboard with JWT token

**Verify in Database:**
```sql
SELECT tenant_id, name, domain FROM tenants;
-- Expected: tenant_id='alice', name='Alice's Projects' (or 'Alice's Workspace')

SELECT email, tenant_id FROM users;
-- Expected: email='alice@gmail.com', tenant_id='alice'
```

### Test Case 2: New Company Email User

1. Login with `bob@acme.com`
2. **Expected**: Confirmation page appears with:
   - User info: Bob's name and email
   - Tenant type: "Company Workspace"
   - Tenant name: "Acme" (NOT editable)
3. Click "Confirm and Continue"
4. **Expected**: Redirected to dashboard

**Verify in Database:**
```sql
SELECT tenant_id, name, domain FROM tenants;
-- Expected: tenant_id='acme-com', name='Acme', domain='acme.com'
```

### Test Case 3: Existing User

1. Login with same email again
2. **Expected**: No confirmation page, direct login to dashboard

## Security Considerations

1. **Pending Token Expiry**: Tokens are valid for 10 minutes only
2. **Token Verification**: Backend verifies pending token signature
3. **User Existence Check**: Double-check user doesn't exist before creation
4. **OAuth Token Storage**: Tokens stored securely in signed JWT

## Error Handling

### Frontend Error Scenarios

1. **Expired Pending Token**: Show error message, redirect to login
2. **User Already Exists**: Should not happen (check endpoint prevents this)
3. **Network Errors**: Show user-friendly error, allow retry

```typescript
try {
  const response = await fetch('/auth/oauth/confirm', { ... });
  if (!response.ok) {
    const error = await response.json();
    if (error.detail.includes('expired')) {
      alert('Session expired. Please login again.');
      window.location.href = '/login';
    } else if (error.detail.includes('already exists')) {
      alert('Account already exists. Please login.');
      window.location.href = '/login';
    } else {
      alert('Registration failed. Please try again.');
    }
  }
} catch (error) {
  alert('Network error. Please check your connection.');
}
```

## Backend Restart

After updating the code, restart the backend:

```bash
cd nexus
./local-demo.sh --stop
./local-demo.sh --start --no-langgraph
```

Or if using manual start:
```bash
# Kill existing process
pkill -f "nexus serve"

# Start with environment
set -a && source .env && set +a
export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost:5432/nexus"
nexus serve --config ./configs/config.demo.yaml --auth-type database --async
```

## API Reference

### POST /auth/oauth/check

**Request:**
```json
{
  "provider": "google",
  "code": "4/0AfJohXl...",
  "state": "random_state_string"
}
```

**Response (New User):**
```json
{
  "needs_confirmation": true,
  "pending_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user_info": {
    "email": "alice@gmail.com",
    "display_name": "Alice Smith",
    "avatar_url": "https://...",
    "oauth_provider": "google",
    "oauth_code": "...",
    "oauth_state": "..."
  },
  "tenant_info": {
    "tenant_id": "alice",
    "name": "Alice's Workspace",
    "domain": "gmail.com",
    "description": "Personal workspace for Alice Smith",
    "is_personal": true,
    "can_edit_name": true
  },
  "message": "Please confirm your information to complete registration"
}
```

**Response (Existing User):**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user": { "user_id": "...", "email": "alice@gmail.com", ... },
  "is_new_user": false,
  "api_key": "nx_...",
  "tenant_id": "alice",
  "message": "OAuth authentication successful",
  "needs_confirmation": false
}
```

### POST /auth/oauth/confirm

**Request:**
```json
{
  "pending_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "tenant_name": "Alice's Projects"  // Optional, only for personal workspaces
}
```

**Response:**
```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "user": { "user_id": "...", "email": "alice@gmail.com", ... },
  "is_new_user": true,
  "api_key": "nx_...",
  "tenant_id": "alice",
  "message": "Registration completed successfully"
}
```

## Next Steps

1. **Frontend Implementation**: Implement the confirmation page component
2. **Testing**: Test with both personal and company emails
3. **UX Polish**: Add loading states, animations, error messages
4. **Optional**: Add email verification step before confirmation
5. **Optional**: Add terms of service checkbox on confirmation page
