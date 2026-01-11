# Testing the Slack Connector

This guide walks you through testing the Slack connector implementation.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Detailed Setup](#detailed-setup)
4. [Running Tests](#running-tests)
5. [Integration Testing](#integration-testing)
6. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### 1. Install Dependencies

```bash
# Install slack-sdk (required for Slack connector)
pip install slack-sdk

# Or install with dev dependencies
pip install -e ".[dev]"
```

### 2. Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **"Create New App"** → **"From scratch"**
3. Give it a name (e.g., "Nexus Connector Test")
4. Select your workspace

### 3. Configure OAuth & Permissions

1. In your app settings, go to **"OAuth & Permissions"**
2. Under **"Redirect URLs"**, add:
   ```
   http://localhost:5173/oauth/callback
   ```

3. Under **"Scopes" → "User Token Scopes"**, add:
   - `channels:read` - View basic channel info
   - `channels:history` - View messages in public channels
   - `chat:write` - Post messages
   - `users:read` - View users in workspace
   - `im:read` - View direct messages (optional)
   - `im:history` - View DM history (optional)
   - `groups:read` - View private channels (optional)
   - `groups:history` - View private channel messages (optional)

4. Click **"Save Changes"**

### 4. Install App to Workspace

1. Go to **"Install App"** in the left sidebar
2. Click **"Install to Workspace"**
3. Authorize the app
4. Copy the **"User OAuth Token"** (starts with `xoxp-`)

### 5. Get OAuth Credentials

1. Go to **"Basic Information"**
2. Copy **"Client ID"** under "App Credentials"
3. Copy **"Client Secret"** under "App Credentials"

---

## Quick Start

### Option 1: Quick Test with Token (Fastest)

```bash
# Set environment variables
export SLACK_TOKEN="xoxp-your-user-token-here"
export SLACK_USER_EMAIL="your@email.com"

# Run quick test
python scripts/test_slack_connector.py --test quick
```

This will:
- ✅ Test authentication
- ✅ List channels
- ✅ Read recent messages
- ✅ No OAuth flow required

### Option 2: Full OAuth Flow

```bash
# Set OAuth credentials
export NEXUS_OAUTH_SLACK_CLIENT_ID="your-client-id"
export NEXUS_OAUTH_SLACK_CLIENT_SECRET="your-client-secret"
export SLACK_USER_EMAIL="your@email.com"

# Run all tests
python scripts/test_slack_connector.py
```

---

## Detailed Setup

### Environment Variables

Create a `.env` file in the nexus directory:

```bash
# Slack OAuth credentials
NEXUS_OAUTH_SLACK_CLIENT_ID="YOUR_CLIENT_ID_HERE"
NEXUS_OAUTH_SLACK_CLIENT_SECRET="YOUR_CLIENT_SECRET_HERE"

# For quick testing (optional)
SLACK_TOKEN="xoxp-YOUR-SLACK-TOKEN-HERE"
SLACK_USER_EMAIL="your@email.com"

# Database (optional, defaults to ~/.nexus/nexus.db)
TOKEN_MANAGER_DB="sqlite:///~/.nexus/nexus.db"
```

Load it:

```bash
source .env
```

---

## Running Tests

### 1. Unit Tests (No Slack Workspace Required)

```bash
# Run all Slack connector unit tests
pytest tests/unit/backends/test_slack_connector.py -v

# Run specific test class
pytest tests/unit/backends/test_slack_connector.py::TestSlackConnectorInitialization -v

# Run with coverage
pytest tests/unit/backends/test_slack_connector.py --cov=nexus.backends.slack_connector
```

**Expected output:**
```
22 passed in 4.63s
```

### 2. Integration Tests (Requires Slack Workspace)

#### Quick Test (SDK Only)

```bash
python scripts/test_slack_connector.py --test quick
```

**What it tests:**
- ✅ Slack SDK authentication
- ✅ Listing channels
- ✅ Reading messages

#### Connector Initialization

```bash
python scripts/test_slack_connector.py --test init
```

**What it tests:**
- ✅ SlackConnectorBackend initialization
- ✅ TokenManager setup
- ✅ OAuth provider registration

#### List Channels

```bash
python scripts/test_slack_connector.py --test list_channels
```

**What it tests:**
- ✅ List channel types (channels/, private-channels/, dms/)
- ✅ List public channels
- ✅ List private channels (if accessible)

#### Read Messages

```bash
python scripts/test_slack_connector.py --test read_messages
```

**What it tests:**
- ✅ List messages in a channel
- ✅ Read individual message content
- ✅ Parse JSON message data

#### Post Messages

```bash
python scripts/test_slack_connector.py --test post_message
```

**What it tests:**
- ✅ Write messages to channels
- ✅ Return message timestamp

⚠️ **Warning:** This will post a test message to your Slack workspace!

#### Run All Tests

```bash
python scripts/test_slack_connector.py
```

---

## Integration Testing

### Testing with NexusFS API

```python
from nexus import NexusFS
from nexus.backends.slack_connector import SlackConnectorBackend

# Initialize connector
connector = SlackConnectorBackend(
    token_manager_db="~/.nexus/nexus.db",
    user_email="your@email.com",
    provider="slack",
    max_messages_per_channel=100,
)

# Initialize NexusFS
nx = NexusFS(backend=connector)

# List channels
channels = nx.ls("/channels/")
print("Channels:", channels)

# Read message
messages = nx.ls("/channels/general/")
if messages:
    content = nx.read(f"/channels/general/{messages[0]}")
    print("Message:", content.decode())

# Post message
message_data = {
    "channel": "C1234567890",  # Replace with real channel ID
    "text": "Hello from Nexus!"
}
import json
nx.write("/channels/general/new.json", json.dumps(message_data).encode())
```

### Testing with Nexus CLI

```bash
# Mount Slack connector (future feature)
nexus mount slack /slack

# List channels
nexus ls /slack/channels/

# Read messages
nexus cat /slack/channels/general/1234567890.123456.json

# Post message
echo '{"channel":"C1234567890","text":"Hello!"}' | nexus write /slack/channels/general/new.json
```

---

## Troubleshooting

### Issue: "slack-sdk not installed"

```bash
pip install slack-sdk
```

### Issue: "OAuth token invalid"

1. Go to https://api.slack.com/apps
2. Select your app → **"OAuth & Permissions"**
3. Click **"Reinstall to Workspace"**
4. Copy the new User OAuth Token
5. Update `SLACK_TOKEN` environment variable

### Issue: "Missing permissions"

Check that your app has these scopes:
- `channels:read`
- `channels:history`
- `chat:write`
- `users:read`

If not:
1. Go to **"OAuth & Permissions"**
2. Add missing scopes under **"User Token Scopes"**
3. Click **"Reinstall to Workspace"**

### Issue: "Rate limit exceeded"

The connector has built-in rate limiting with exponential backoff. If you see this:
1. Reduce `max_messages_per_channel` (default: 100)
2. Add delays between operations
3. Wait a few minutes before retrying

### Issue: "Channel not found"

Make sure:
1. Your bot/app is invited to the channel
2. You're using the correct channel name (without #)
3. You have permission to access the channel

### Issue: "Cannot post message"

Check:
1. App has `chat:write` scope
2. Bot is invited to the channel: `/invite @YourBot`
3. Channel is not archived
4. You're using the channel ID, not name

---

## Example Test Output

```
================================================================================
QUICK TEST: Slack SDK Direct
================================================================================

1. Testing authentication...
   ✓ Authenticated as: john.doe
   ✓ Team: MyWorkspace

2. Listing channels...
   ✓ Found 5 channels:
      - #general (C1234567890)
      - #random (C1234567891)
      - #dev (C1234567892)
      - #announcements (C1234567893)
      - #support (C1234567894)

3. Reading messages from #general...
   ✓ Found 3 recent messages:
      - [1234567890.123456] Hey team, how's everyone doing?...
      - [1234567891.123456] Good morning! Ready for the standup?...
      - [1234567892.123456] Just pushed the latest changes...

✅ Quick test passed!

================================================================================
TEST SUMMARY
================================================================================
✅ PASSED: quick
✅ PASSED: init
✅ PASSED: list_channels
✅ PASSED: read_messages
✅ PASSED: post_message

🎉 All tests passed!
```

---

## Next Steps

1. ✅ Run unit tests: `pytest tests/unit/backends/test_slack_connector.py`
2. ✅ Run quick integration test: `python scripts/test_slack_connector.py --test quick`
3. ✅ Test with your workspace: `python scripts/test_slack_connector.py`
4. 🚀 Integrate into your application

For more information, see:
- [Slack API Documentation](https://api.slack.com/docs)
- [slack-sdk Documentation](https://slack.dev/python-slack-sdk/)
- [Nexus OAuth Documentation](../docs/oauth.md)
