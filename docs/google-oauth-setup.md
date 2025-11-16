# Getting Google OAuth Credentials

This guide shows you how to get Google OAuth credentials (Client ID and Client Secret) for the Google Drive connector backend.

## Quick Steps

1. **Go to Google Cloud Console**: https://console.cloud.google.com/apis/credentials
2. **Create project** → Enable Drive API → Create OAuth credentials
3. **Download JSON** → Extract Client ID & Secret
4. **Use with Nexus** → `nexus oauth setup-gdrive`

## Detailed Instructions

### Step 1: Access Google Cloud Console

Visit: https://console.cloud.google.com/apis/credentials

If you don't have a Google Cloud account, you'll need to:
- Sign up (free)
- Agree to terms of service
- No billing required for OAuth credentials

### Step 2: Create a New Project

1. Click **"Select a project"** dropdown (top left)
2. Click **"New Project"**
3. Enter project details:
   - **Project name**: `Nexus Google Drive Demo`
   - **Organization**: Leave as default (No organization)
4. Click **"Create"**
5. Wait for project creation (takes ~30 seconds)
6. Select the new project from the dropdown

### Step 3: Enable Google Drive API

1. Go to **"APIs & Services"** → **"Library"**
   - Or visit: https://console.cloud.google.com/apis/library
2. Search for: `Google Drive API`
3. Click on **"Google Drive API"** result
4. Click **"Enable"** button
5. Wait for API to be enabled

### Step 4: Configure OAuth Consent Screen

**⚠️ Required before creating OAuth credentials**

1. Go to **"APIs & Services"** → **"OAuth consent screen"**
   - Or visit: https://console.cloud.google.com/apis/credentials/consent
2. Select **User Type**:
   - Choose **"External"** (for personal Google accounts)
   - Click **"Create"**
3. Fill in OAuth consent screen:
   - **App name**: `Nexus Drive Demo`
   - **User support email**: Your email
   - **Developer contact email**: Your email
   - Leave other fields blank
4. Click **"Save and Continue"**
5. **Scopes** page:
   - Click **"Add or Remove Scopes"**
   - Filter for: `.../auth/drive`
   - Select: `https://www.googleapis.com/auth/drive`
   - Click **"Update"**
   - Click **"Save and Continue"**
6. **Test users** page:
   - Click **"Add Users"**
   - Enter your Google email address
   - Click **"Add"**
   - Click **"Save and Continue"**
7. Click **"Back to Dashboard"**

### Step 5: Create OAuth 2.0 Credentials

1. Go to **"APIs & Services"** → **"Credentials"**
   - Or visit: https://console.cloud.google.com/apis/credentials
2. Click **"Create Credentials"** → **"OAuth client ID"**
3. Select **Application type**:
   - Choose **"Desktop app"**
   - **Name**: `Nexus Desktop Client`
4. Click **"Create"**
5. You'll see a popup with your credentials:
   - **Client ID**: `123456789-abc.apps.googleusercontent.com`
   - **Client Secret**: `GOCSPX-abc123xyz456...`
6. Click **"OK"** to close the popup

### Step 6: Download Credentials (Optional but Recommended)

1. Find your OAuth 2.0 Client in the credentials list
2. Click the **download icon** (⬇) on the right
3. A JSON file will be downloaded: `client_secret_123456789-abc.json`

**JSON Structure:**
```json
{
  "installed": {
    "client_id": "123456789-abc.apps.googleusercontent.com",
    "client_secret": "GOCSPX-abc123xyz456...",
    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token"
  }
}
```

### Step 7: Extract Credentials

**Option A: From JSON file (if downloaded)**
```bash
# Extract Client ID
cat ~/Downloads/client_secret_*.json | jq -r '.installed.client_id'

# Extract Client Secret
cat ~/Downloads/client_secret_*.json | jq -r '.installed.client_secret'
```

**Option B: Copy from Console**
1. Go to **"Credentials"** page
2. Click on your OAuth client name
3. Copy **"Client ID"** and **"Client secret"** from the page

### Step 8: Set Environment Variables

```bash
# Set credentials (use actual values from Step 7)
export GOOGLE_CLIENT_ID="123456789-abc.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="GOCSPX-abc123xyz456..."
export GOOGLE_USER_EMAIL="your-email@gmail.com"

# Verify they're set
echo "Client ID: $GOOGLE_CLIENT_ID"
echo "Client Secret: ${GOOGLE_CLIENT_SECRET:0:20}..."
echo "User Email: $GOOGLE_USER_EMAIL"
```

### Step 9: Setup OAuth with Nexus

```bash
# Run Nexus OAuth setup
nexus oauth setup-gdrive \
    --client-id "$GOOGLE_CLIENT_ID" \
    --client-secret "$GOOGLE_CLIENT_SECRET" \
    --user-email "$GOOGLE_USER_EMAIL"
```

**What happens next:**
1. Browser opens with Google OAuth consent page
2. You grant permission to access your Drive
3. You'll see an authorization code (copy it)
4. Paste the code into the terminal
5. Nexus stores encrypted OAuth tokens in database

### Step 10: Verify Setup

```bash
# Test OAuth credentials
nexus oauth test google "$GOOGLE_USER_EMAIL"

# List all OAuth credentials
nexus oauth list

# Run the demo
./examples/cli/gdrive_connector_demo.sh
```

## Troubleshooting

### "Access blocked: This app's request is invalid"

**Problem:** OAuth consent screen not configured

**Solution:**
- Complete Step 4 (Configure OAuth Consent Screen)
- Add your email as a test user
- Make sure app is in "Testing" status

### "redirect_uri_mismatch"

**Problem:** Redirect URI not authorized

**Solution:**
- The CLI uses `http://localhost` (Desktop app redirect URI)
- This is automatically configured when you select **"Desktop app"** type
- Make sure you selected **"Desktop app"** not "Web application"
- If you still see this error, verify your OAuth client type in Google Console

### "invalid_client"

**Problem:** Wrong Client ID or Secret

**Solution:**
- Double-check copied credentials
- Make sure no extra spaces or newlines
- Re-download the JSON file if needed

### "API has not been used in project"

**Problem:** Drive API not enabled

**Solution:**
- Complete Step 3 (Enable Google Drive API)
- Wait a few minutes for API to activate
- Try again

### "Daily Limit Exceeded"

**Problem:** Too many OAuth flows in a day

**Solution:**
- Wait 24 hours
- Use existing OAuth tokens: `nexus oauth list`
- For production, request quota increase

## Security Best Practices

### 🔒 Keep Credentials Secure

```bash
# ❌ DON'T commit to git
echo "client_secret_*.json" >> .gitignore

# ❌ DON'T share credentials publicly
# Client Secret should be kept private

# ✅ DO use environment variables
export GOOGLE_CLIENT_SECRET="..."  # Not in code

# ✅ DO use separate credentials per environment
# Development: One set of credentials
# Production: Different set of credentials
```

### 🔑 Credential Types

| Type | Security | Use Case |
|------|----------|----------|
| **OAuth Client Credentials** | Public (Client ID) + Secret | Nexus CLI setup |
| **OAuth Tokens** | Private (access + refresh) | Stored encrypted by Nexus |

**Important:**
- **Client ID**: Can be public (embedded in apps)
- **Client Secret**: Keep private (like a password)
- **OAuth Tokens**: Never share (stored encrypted by Nexus)

### 🔄 Rotating Credentials

To rotate credentials (recommended annually):

```bash
# 1. Create new OAuth client in Google Console
# 2. Download new credentials
# 3. Revoke old tokens
nexus oauth revoke google "$GOOGLE_USER_EMAIL"

# 4. Setup with new credentials
nexus oauth setup-gdrive \
    --client-id "$NEW_CLIENT_ID" \
    --client-secret "$NEW_CLIENT_SECRET" \
    --user-email "$GOOGLE_USER_EMAIL"

# 5. Delete old OAuth client in Google Console
```

## For Production Use

### Production Checklist

- [ ] Create separate Google Cloud project for production
- [ ] Use organization Google Workspace account (not personal)
- [ ] Configure OAuth consent screen with privacy policy
- [ ] Request production OAuth verification (if needed)
- [ ] Set up monitoring for quota usage
- [ ] Implement credential rotation schedule
- [ ] Use secrets management (Vault, AWS Secrets Manager)
- [ ] Enable audit logging

### OAuth Consent Screen Verification

For apps with >100 users, Google requires verification:

1. Go to OAuth consent screen
2. Click **"Publish App"**
3. Submit for verification (takes 1-2 weeks)
4. Provide:
   - Privacy policy URL
   - Terms of service URL
   - App homepage
   - Video demo

**Until verified:**
- Shows "unverified app" warning to users
- Limited to 100 test users
- Works fine for internal/development use

## Related Documentation

- [Google Drive Backend Guide](./google-drive-backend.md)
- [OAuth Token Management](./oauth.md)
- [Server Authentication](./server-authentication.md)
- [Google Cloud Console](https://console.cloud.google.com)
- [Google OAuth Documentation](https://developers.google.com/identity/protocols/oauth2)

## Quick Reference

```bash
# Get credentials
https://console.cloud.google.com/apis/credentials

# Setup OAuth
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export GOOGLE_USER_EMAIL="your@email.com"

nexus oauth setup-gdrive \
    --client-id "$GOOGLE_CLIENT_ID" \
    --client-secret "$GOOGLE_CLIENT_SECRET" \
    --user-email "$GOOGLE_USER_EMAIL"

# Test
nexus oauth test google "$GOOGLE_USER_EMAIL"

# List
nexus oauth list

# Revoke
nexus oauth revoke google "$GOOGLE_USER_EMAIL"
```
