# Slack Secrets Management

This guide explains how to securely store and access Slack credentials.

## Your Slack App Credentials

From your screenshot:
```
App ID:              A0A84C8PGFN
Client ID:           8308475064551.10276416798532
Client Secret:       8235dc7061624feadd355861ef49731
Signing Secret:      3cc6dbee6f6bb71d9c72410b8253ba7b
Verification Token:  vq78gjgL1psmW8oLJ2Ecb3c7
```

## 🔐 Option 1: Local Development (.env file)

### Quick Setup

1. **Copy example to .env:**
   ```bash
   cd /Users/jinjingzhou/nexi-lab/nexus
   cp .env.slack.example .env
   ```

2. **Edit .env with your credentials:**
   ```bash
   # The file is already pre-filled with your credentials!
   # Just add your User OAuth Token
   ```

3. **Load environment variables:**
   ```bash
   source .env
   ```

4. **Test:**
   ```bash
   python scripts/test_slack_connector.py --test quick
   ```

✅ **The .env file is in .gitignore** - safe from accidental commits!

---

## ☁️ Option 2: Google Secret Manager (Production)

### Prerequisites

```bash
# Install gcloud CLI
curl https://sdk.cloud.google.com | bash
exec -l $SHELL

# Authenticate
gcloud auth login

# Set project
gcloud config set project YOUR_PROJECT_ID
```

### Save Secrets to GCP

```bash
# Run the setup script
cd /Users/jinjingzhou/nexi-lab/nexus
./scripts/setup_slack_secrets.sh
```

This creates 5 secrets in Google Secret Manager:
- ✅ `nexus-slack-client-id`
- ✅ `nexus-slack-client-secret`
- ✅ `nexus-slack-app-id`
- ✅ `nexus-slack-signing-secret`
- ✅ `nexus-slack-verification-token`

### Load Secrets from GCP

**Option A: Export to environment**
```bash
# Load and export all secrets
eval $(python scripts/load_secrets_from_gcp.py --export)

# Verify
echo $NEXUS_OAUTH_SLACK_CLIENT_ID
```

**Option B: Write to .env file**
```bash
# Generate .env from GCP secrets
python scripts/load_secrets_from_gcp.py --output .env

# Load it
source .env
```

**Option C: Use in Python code**
```python
from google.cloud import secretmanager

def get_slack_credentials():
    client = secretmanager.SecretManagerServiceClient()
    project_id = "your-project-id"

    # Get client ID
    name = f"projects/{project_id}/secrets/nexus-slack-client-id/versions/latest"
    response = client.access_secret_version(request={"name": name})
    client_id = response.payload.data.decode("UTF-8")

    # Get client secret
    name = f"projects/{project_id}/secrets/nexus-slack-client-secret/versions/latest"
    response = client.access_secret_version(request={"name": name})
    client_secret = response.payload.data.decode("UTF-8")

    return client_id, client_secret
```

---

## 🐳 Option 3: Docker Secrets

### docker-compose.yml

```yaml
version: '3.8'

services:
  nexus:
    image: nexus:latest
    environment:
      - NEXUS_OAUTH_SLACK_CLIENT_ID=${NEXUS_OAUTH_SLACK_CLIENT_ID}
      - NEXUS_OAUTH_SLACK_CLIENT_SECRET=${NEXUS_OAUTH_SLACK_CLIENT_SECRET}
    env_file:
      - .env
```

---

## 📋 Environment Variables Reference

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `NEXUS_OAUTH_SLACK_CLIENT_ID` | ✅ Yes | OAuth client ID | `8308475064551.10276416798532` |
| `NEXUS_OAUTH_SLACK_CLIENT_SECRET` | ✅ Yes | OAuth client secret | `8235dc7061624feadd...` |
| `SLACK_APP_ID` | ⚠️ Optional | App identifier | `A0A84C8PGFN` |
| `SLACK_SIGNING_SECRET` | ⚠️ Optional | For webhook verification | `3cc6dbee6f6bb71d...` |
| `SLACK_VERIFICATION_TOKEN` | ⚠️ Optional | Legacy verification | `vq78gjgL1psmW8oLJ2...` |
| `SLACK_TOKEN` | 🧪 Testing | User OAuth token | `xoxp-123456789-...` |
| `SLACK_USER_EMAIL` | 🧪 Testing | Your email | `your@email.com` |

---

## 🔒 Security Best Practices

### ✅ DO

- ✅ Use Google Secret Manager for production
- ✅ Use .env files for local development
- ✅ Keep .env in .gitignore
- ✅ Rotate secrets regularly
- ✅ Use minimal OAuth scopes
- ✅ Enable 2FA on your Slack workspace

### ❌ DON'T

- ❌ Commit secrets to git
- ❌ Share secrets in Slack/email
- ❌ Store secrets in code
- ❌ Use production secrets in development
- ❌ Grant unnecessary permissions

---

## 🧪 Testing Your Setup

### 1. Check environment variables

```bash
# Verify they're loaded
env | grep SLACK
env | grep NEXUS_OAUTH
```

### 2. Test with quick script

```bash
python scripts/test_slack_connector.py --test quick
```

### 3. Test OAuth flow

```bash
python scripts/test_slack_connector.py
```

---

## 🔄 Rotating Secrets

### When to Rotate

- ✅ Every 90 days (recommended)
- ✅ After team member leaves
- ✅ If secret is exposed
- ✅ After security incident

### How to Rotate

1. **Go to Slack app settings:**
   ```
   https://api.slack.com/apps/A0A84C8PGFN
   → Basic Information → App Credentials
   ```

2. **Click "Regenerate" for Client Secret**

3. **Update secrets:**
   ```bash
   # Update in GCP
   echo -n "NEW_SECRET" | gcloud secrets versions add nexus-slack-client-secret --data-file=-

   # Update in .env
   nano .env  # Edit NEXUS_OAUTH_SLACK_CLIENT_SECRET
   ```

4. **Reload and test:**
   ```bash
   source .env
   python scripts/test_slack_connector.py --test quick
   ```

---

## 📚 Additional Resources

- [Google Secret Manager Docs](https://cloud.google.com/secret-manager/docs)
- [Slack Security Best Practices](https://api.slack.com/authentication/best-practices)
- [OAuth 2.0 Security](https://oauth.net/2/security/)

---

## 🆘 Troubleshooting

### Issue: "gcloud: command not found"

```bash
# Install gcloud CLI
curl https://sdk.cloud.google.com | bash
exec -l $SHELL
```

### Issue: "Error retrieving secret"

```bash
# Make sure you're authenticated
gcloud auth login

# Check project
gcloud config get-value project

# Enable Secret Manager API
gcloud services enable secretmanager.googleapis.com
```

### Issue: "Permission denied"

```bash
# Grant yourself access
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="user:your@email.com" \
  --role="roles/secretmanager.secretAccessor"
```

### Issue: ".env not loading"

```bash
# Make sure to source it
source .env

# Or use direnv (auto-loads .env)
brew install direnv
echo 'eval "$(direnv hook bash)"' >> ~/.bashrc
direnv allow .
```

---

## ✅ Quick Checklist

- [ ] Credentials saved to .env file
- [ ] .env in .gitignore
- [ ] Environment variables loaded (`source .env`)
- [ ] Quick test passing
- [ ] (Optional) Secrets saved to Google Secret Manager
- [ ] (Optional) GCP access configured
- [ ] Documentation reviewed
