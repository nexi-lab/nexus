# Setting Up Slack App for Nexus Connector

This guide shows you how to create a Slack app using the provided manifest.

## Quick Setup (2 minutes)

### Option 1: Using App Manifest (Recommended)

1. **Go to Slack Apps**
   - Visit https://api.slack.com/apps
   - Click **"Create New App"**

2. **Select "From an app manifest"**
   - Choose your workspace
   - Click **"Next"**

3. **Paste the manifest**
   - Choose **YAML** or **JSON** format
   - Copy the content from either:
     - [`configs/slack-app-manifest.yaml`](../configs/slack-app-manifest.yaml) (YAML)
     - [`configs/slack-app-manifest.json`](../configs/slack-app-manifest.json) (JSON)
   - Paste into the manifest editor
   - Click **"Next"**

4. **Review and create**
   - Review the permissions
   - Click **"Create"**
   - Click **"Install to Workspace"**
   - Authorize the app

5. **Get your credentials**
   - Go to **"Basic Information"**
   - Under "App Credentials":
     - Copy **Client ID**
     - Copy **Client Secret**
   - Go to **"OAuth & Permissions"**
   - Copy **User OAuth Token** (starts with `xoxp-`)

6. **Set environment variables**
   ```bash
   export NEXUS_OAUTH_SLACK_CLIENT_ID="123456789.123456789"
   export NEXUS_OAUTH_SLACK_CLIENT_SECRET="abcdef123456789"
   export SLACK_TOKEN="xoxp-123456789-123456789-..."
   export SLACK_USER_EMAIL="your@email.com"
   ```

✅ **Done!** You're ready to test.

---

## Option 2: Manual Setup

If you prefer to configure manually:

### 1. Create App

1. Go to https://api.slack.com/apps
2. Click **"Create New App"** → **"From scratch"**
3. Name: `Nexus Connector`
4. Select your workspace
5. Click **"Create App"**

### 2. Configure OAuth & Permissions

1. Go to **"OAuth & Permissions"** in the left sidebar

2. **Add Redirect URLs:**
   - Click **"Add New Redirect URL"**
   - Add: `http://localhost:5173/oauth/callback`
   - Add: `http://localhost:2026/oauth/callback`
   - Click **"Save URLs"**

3. **Add User Token Scopes:**

   Under **"User Token Scopes"**, add:

   **Channel Access:**
   - `channels:read` - View basic channel info
   - `channels:history` - View messages in public channels
   - `channels:write` - Manage public channels

   **Private Channel Access:**
   - `groups:read` - View basic info about private channels
   - `groups:history` - View messages in private channels
   - `groups:write` - Manage private channels

   **Direct Messages:**
   - `im:read` - View basic info about DMs
   - `im:history` - View messages in DMs
   - `im:write` - Send direct messages

   **Group DMs:**
   - `mpim:read` - View basic info about group DMs
   - `mpim:history` - View messages in group DMs
   - `mpim:write` - Send messages to group DMs

   **Core Functionality:**
   - `chat:write` - Post messages
   - `users:read` - View users in workspace
   - `users:read.email` - View email addresses
   - `team:read` - View workspace information

4. **Add Bot Token Scopes (Optional):**

   Under **"Bot Token Scopes"**, add:
   - `channels:read`
   - `channels:history`
   - `chat:write`
   - `users:read`

### 3. Install App

1. Go to **"Install App"** in the left sidebar
2. Click **"Install to Workspace"**
3. Review permissions
4. Click **"Allow"**
5. Copy the **User OAuth Token** (starts with `xoxp-`)

### 4. Get Credentials

1. Go to **"Basic Information"** in the left sidebar
2. Scroll to **"App Credentials"**
3. Copy:
   - **App ID**
   - **Client ID**
   - **Client Secret**
   - **Signing Secret** (optional)

---

## Minimal Scopes (Read-Only)

If you only want to read messages (no posting), use these minimal scopes:

```yaml
oauth_config:
  scopes:
    user:
      - channels:read
      - channels:history
      - groups:read         # For private channels
      - groups:history      # For private channels
      - im:read            # For DMs
      - im:history         # For DMs
      - users:read
```

---

## Testing Your Setup

### Quick Test with Token

```bash
# Set your token
export SLACK_TOKEN="xoxp-your-token"

# Test with Python
python3 << 'EOF'
from slack_sdk import WebClient

token = "xoxp-your-token"  # Replace with your token
client = WebClient(token=token)

# Test auth
auth = client.auth_test()
print(f"✓ Authenticated as: {auth['user']}")
print(f"✓ Team: {auth['team']}")

# List channels
channels = client.conversations_list(limit=5)
print(f"✓ Found {len(channels['channels'])} channels")
EOF
```

### Test with Nexus Connector

```bash
export NEXUS_OAUTH_SLACK_CLIENT_ID="your-client-id"
export NEXUS_OAUTH_SLACK_CLIENT_SECRET="your-client-secret"
export SLACK_USER_EMAIL="your@email.com"

python scripts/test_slack_connector.py --test quick
```

---

## Manifest Files

We provide two manifest formats:

1. **YAML** (recommended): [`configs/slack-app-manifest.yaml`](../configs/slack-app-manifest.yaml)
2. **JSON**: [`configs/slack-app-manifest.json`](../configs/slack-app-manifest.json)

Both contain the same configuration. Use whichever format you prefer.

---

## Troubleshooting

### Issue: "invalid_client_id"

**Solution:**
1. Go to **Basic Information** → **App Credentials**
2. Copy the correct **Client ID**
3. Update `NEXUS_OAUTH_SLACK_CLIENT_ID`

### Issue: "invalid_scope"

**Solution:**
1. Go to **OAuth & Permissions**
2. Add missing scopes under **User Token Scopes**
3. Click **"Reinstall to Workspace"**

### Issue: "not_in_channel"

**Solution:**
Invite the bot to the channel:
```
/invite @Nexus Bot
```

### Issue: "missing_scope: channels:history"

**Solution:**
1. Go to **OAuth & Permissions**
2. Add `channels:history` under **User Token Scopes**
3. Click **"Reinstall to Workspace"**
4. Get new token

---

## Security Best Practices

1. **Never commit tokens** to version control
   ```bash
   # Add to .gitignore
   echo ".env" >> .gitignore
   echo "*.token" >> .gitignore
   ```

2. **Use environment variables**
   ```bash
   # Store in .env file (not committed)
   NEXUS_OAUTH_SLACK_CLIENT_ID="..."
   NEXUS_OAUTH_SLACK_CLIENT_SECRET="..."

   # Load with
   source .env
   ```

3. **Rotate tokens regularly**
   - Go to **OAuth & Permissions**
   - Click **"Reinstall to Workspace"**
   - Update your tokens

4. **Use minimal scopes**
   - Only request scopes you need
   - Remove unused scopes
   - Review permissions regularly

---

## Next Steps

After setting up your app:

1. ✅ Run unit tests: `pytest tests/unit/backends/test_slack_connector.py`
2. ✅ Run quick test: `python scripts/test_slack_connector.py --test quick`
3. ✅ Test full integration: `python scripts/test_slack_connector.py`
4. 🚀 Integrate into your application

---

## Additional Resources

- [Slack API Documentation](https://api.slack.com/docs)
- [OAuth Scopes Reference](https://api.slack.com/scopes)
- [App Manifest Reference](https://api.slack.com/reference/manifests)
- [Nexus Testing Guide](testing-slack-connector.md)
