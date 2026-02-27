# X (Twitter) Connector

The X (Twitter) Connector enables Nexus to interact with X/Twitter through a virtual filesystem interface, allowing AI agents and applications to read timelines, post tweets, and search content using familiar file operations.

## Features

✅ **OAuth 2.0 PKCE Authentication** - Secure authentication without requiring client secrets
✅ **Virtual Filesystem** - Maps tweets and timelines to file paths
✅ **Multi-tier Caching** - Reduces API calls and handles rate limits
✅ **Full grep/glob Support** - Search tweets like files
✅ **Read-optimized** - Aggressive caching for timeline data
✅ **Write Safety** - Only specific paths are writable
✅ **Rate Limit Handling** - Automatic detection and error reporting

---

## Quick Start

### 1. Get X API Credentials

1. Visit [X Developer Portal](https://developer.twitter.com/)
2. Create a new app (or use existing)
3. Setup OAuth 2.0:
   - Type: **Web App, Automated App or Bot**
   - Callback URI: `http://localhost`
   - Permissions: Read and Write
4. Copy your **Client ID** (Client Secret is optional for PKCE)

### 2. Setup OAuth

```bash
# Using environment variable
export NEXUS_OAUTH_X_CLIENT_ID="your-client-id"
nexus oauth setup-x --user-email "you@example.com"

# Or pass directly
nexus oauth setup-x \
    --client-id "your-client-id" \
    --user-email "you@example.com"
```

The CLI will:
1. Generate an authorization URL
2. Open your browser for consent
3. Prompt you to paste the authorization code
4. Exchange code for tokens using PKCE
5. Store encrypted tokens in database

### 3. Use the Connector

```python
from nexus import NexusFS
from nexus.backends import XConnectorBackend

# Initialize
nx = NexusFS(backend=XConnectorBackend(
    token_manager_db="~/.nexus/nexus.db",
))

# Read timeline
timeline = nx.read("/x/timeline/recent.json")

# Post tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "Hello from Nexus! 🚀"
}).encode())

# Search tweets
results = nx.grep("python", path="/x/search/")
```

---

## Virtual Filesystem Structure

```
/x/
├── timeline/
│   ├── recent.json              # Last 100 tweets (cached 5 min)
│   ├── 2025-01-22.json         # Daily archive
│   └── media/                   # Downloaded media
│       └── {tweet_id}/
│           ├── image_1.jpg
│           └── video_1.mp4
│
├── mentions/
│   └── recent.json              # Last 100 mentions (cached 5 min)
│
├── posts/
│   ├── all.json                 # All user tweets (cached 1 hour)
│   ├── new.json                 # Post new tweet (write-only)
│   ├── drafts/                  # Local drafts (not posted)
│   │   └── {draft_id}.json
│   └── {tweet_id}.json          # Individual tweet (cached 24 hours)
│
├── bookmarks/
│   └── all.json                 # Saved tweets (cached 1 hour)
│
├── search/
│   └── {query}.json             # Search results (cached 30 min)
│
└── users/
    └── {username}/
        ├── profile.json         # User profile (cached 1 hour)
        └── tweets.json          # User's tweets (cached 1 hour)
```

### Path Types

| Path | Access | Caching | Description |
|------|--------|---------|-------------|
| `/x/timeline/recent.json` | Read-only | 5 min | Home timeline |
| `/x/mentions/recent.json` | Read-only | 5 min | Mentions |
| `/x/posts/all.json` | Read-only | 1 hour | User's tweets |
| `/x/posts/{id}.json` | Read-only | 24 hours | Single tweet |
| `/x/posts/new.json` | Write-only | None | Post new tweet |
| `/x/posts/drafts/*.json` | Read-write | None | Local drafts |
| `/x/bookmarks/all.json` | Read-only | 1 hour | Bookmarked tweets |
| `/x/search/{query}.json` | Read-only | 30 min | Search results |

---

## Operations

### Read Operations

```python
# Read home timeline
timeline_json = nx.read("/x/timeline/recent.json")
timeline = json.loads(timeline_json)

for tweet in timeline["data"]:
    print(f"@{tweet['author']['username']}: {tweet['text']}")

# Read specific date
timeline = nx.read("/x/timeline/2025-01-22.json")

# Read mentions
mentions = json.loads(nx.read("/x/mentions/recent.json"))

# Read bookmarks
bookmarks = json.loads(nx.read("/x/bookmarks/all.json"))

# Read user profile
profile = json.loads(nx.read("/x/users/elonmusk/profile.json"))
```

### Write Operations

```python
# Post a tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "Hello from Nexus! 🚀"
}).encode())

# Reply to a tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "@username Thanks for the feedback!",
    "reply_to": "1234567890"  # Tweet ID to reply to
}).encode())

# Quote tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "This is amazing!",
    "quote_tweet_id": "1234567890"
}).encode())

# Save draft (local, not posted)
nx.write("/x/posts/drafts/my-draft.json", json.dumps({
    "text": "Draft tweet (not posted yet)"
}).encode())
```

### Delete Operations

```python
# Delete your own tweet
nx.delete("/x/posts/1234567890.json")

# Delete draft
nx.delete("/x/posts/drafts/abc123.json")

# Note: Cannot delete timeline, mentions, or others' tweets
```

### Glob Operations

```python
# List root directories
dirs = nx.glob("/x/*")
# → ["/x/timeline/", "/x/posts/", "/x/mentions/", ...]

# List timeline files
files = nx.glob("/x/timeline/*.json")
# → ["/x/timeline/recent.json", "/x/timeline/2025-01-22.json", ...]

# List user's tweets
tweets = nx.glob("/x/posts/*.json")
# → ["/x/posts/all.json", "/x/posts/new.json"]
```

### Grep Operations

```python
# Search cached timeline
results = nx.grep("python", path="/x/timeline/")

# Search user's tweets (via API)
results = nx.grep("error", path="/x/posts/")

# Global search (via X API)
results = nx.grep("nexus ai", path="/x/search/", max_results=10)

for match in results:
    print(f"{match['file']}:{match['line']}: {match['content']}")
```

---

## Caching Strategy

The X connector uses multi-tier caching to minimize API calls and handle rate limits:

### Cache Levels

1. **Memory Cache** (fastest, smallest)
   - In-process dictionary cache
   - Cleared on restart
   - First line of defense

2. **Disk Cache** (fast, persistent)
   - Stored in `/tmp/nexus-x-cache/`
   - Survives restarts
   - Automatically cleaned based on TTL

### Cache TTL by Endpoint

| Endpoint | TTL | Rationale |
|----------|-----|-----------|
| Timeline | 5 minutes | Frequently changing |
| Mentions | 5 minutes | Frequently changing |
| User tweets | 1 hour | Changes less often |
| Single tweet | 24 hours | Tweets don't change |
| Bookmarks | 1 hour | Semi-static |
| Search | 30 minutes | Balance freshness/cost |

### Cache Invalidation

Caches are automatically invalidated when:
- User posts a new tweet (invalidates timeline, user_tweets)
- User deletes a tweet (invalidates timeline, user_tweets)
- Cache TTL expires

### Manual Cache Control

```python
# Pre-cache timeline for faster grep
nx.read("/x/timeline/recent.json")  # Caches timeline

# Now grep is fast (uses cache)
results = nx.grep("python", path="/x/timeline/")
```

---

## Rate Limiting

The X connector handles API rate limits automatically:

### X API v2 Rate Limits (OAuth 2.0)

| Endpoint | Limit | Window |
|----------|-------|--------|
| User timeline | 180 requests | 15 minutes |
| User tweets | 900 requests | 15 minutes |
| Mentions | 180 requests | 15 minutes |
| Single tweet | 900 requests | 15 minutes |
| Search | 450 requests | 15 minutes |
| Post tweet | 200 requests | 15 minutes |
| Delete tweet | 50 requests | 15 minutes |

### Rate Limit Handling

When rate limit is exceeded:
```python
try:
    timeline = nx.read("/x/timeline/recent.json")
except BackendError as e:
    if "Rate limit exceeded" in str(e):
        # Error includes reset time
        print(f"Rate limit hit. Resets at: {e.reset_at}")
        # Use cached data if available
```

The connector:
- Detects 429 (Rate Limit) responses
- Parses `x-rate-limit-reset` header
- Raises informative error with reset time
- Logs rate limit status at DEBUG level

---

## Advanced Usage

### AI Agent Integration

```python
import anthropic
from nexus import NexusFS
from nexus.backends import XConnectorBackend

# Initialize
nx = NexusFS(backend=XConnectorBackend(
    token_manager_db="~/.nexus/nexus.db"
))
client = anthropic.Anthropic()

# Read mentions
mentions = json.loads(nx.read("/x/mentions/recent.json"))

# Process each mention with Claude
for tweet in mentions['data']:
    # Generate response
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        messages=[{
            "role": "user",
            "content": f"Reply to: {tweet['text']}"
        }]
    )

    reply_text = response.content[0].text

    # Post reply
    nx.write("/x/posts/new.json", json.dumps({
        "text": reply_text,
        "reply_to": tweet['id']
    }).encode())
```

### Batch Operations

```python
# Archive last 7 days of timeline
from datetime import datetime, timedelta

for i in range(7):
    date = datetime.now() - timedelta(days=i)
    date_str = date.strftime("%Y-%m-%d")

    # Read and save
    timeline = nx.read(f"/x/timeline/{date_str}.json")

    with open(f"archive/timeline-{date_str}.json", "wb") as f:
        f.write(timeline)

    print(f"Archived timeline for {date_str}")
```

### Sentiment Analysis

```python
import json

# Read recent tweets
timeline = json.loads(nx.read("/x/timeline/recent.json"))

# Analyze sentiment
positive_tweets = []
for tweet in timeline['data']:
    # Use your sentiment analysis model
    sentiment = analyze_sentiment(tweet['text'])

    if sentiment > 0.7:
        positive_tweets.append(tweet)

print(f"Found {len(positive_tweets)} positive tweets")
```

---

## Troubleshooting

### Authentication Errors

```
Error: X connector requires user_email or context.user_id
```

**Solution**: Ensure you've run `nexus oauth setup-x` and the credentials are stored.

```bash
# List credentials
nexus oauth list

# Test credential
nexus oauth test twitter you@example.com
```

### Rate Limit Errors

```
RateLimitError: Rate limit exceeded for /users/:id/timelines/reverse_chronological
```

**Solution**: Wait for rate limit reset or use cached data.

```python
# Check cache TTL setting
backend = XConnectorBackend(
    token_manager_db="~/.nexus/nexus.db",
    cache_ttl={"timeline": 600},  # Increase to 10 minutes
)
```

### Import Errors

```
ModuleNotFoundError: No module named 'httpx'
```

**Solution**: Install required dependencies.

```bash
pip install httpx
# or
pip install nexus-ai-fs[x]  # If extras are configured
```

### Permission Errors

```
PermissionError: Path '/x/timeline/fake.json' is read-only
```

**Solution**: Only `/x/posts/new.json` and `/x/posts/drafts/` are writable.

---

## Security Considerations

### OAuth Tokens

- Tokens are encrypted at rest using Fernet (AES-128)
- Encryption key from `NEXUS_OAUTH_ENCRYPTION_KEY` environment variable
- Tokens auto-refresh before expiry
- Refresh tokens stored securely

### PKCE Flow

- No client secret required for public clients
- Code verifier never leaves your machine
- Code challenge sent to X (one-way hash)
- Protects against authorization code interception

### Best Practices

1. **Use HTTPS** in production (redirect URI)
2. **Rotate credentials** periodically
3. **Limit scopes** to minimum required
4. **Monitor usage** via `nexus oauth list`
5. **Revoke unused tokens** via `nexus oauth revoke`

---

## API Mapping

### X API v2 → Virtual Paths

| Virtual Path | X API Endpoint | Method |
|--------------|----------------|--------|
| `/x/timeline/recent.json` | `/2/users/:id/timelines/reverse_chronological` | GET |
| `/x/mentions/recent.json` | `/2/users/:id/mentions` | GET |
| `/x/posts/all.json` | `/2/users/:id/tweets` | GET |
| `/x/posts/{id}.json` | `/2/tweets/:id` | GET |
| `/x/posts/new.json` | `/2/tweets` | POST |
| `/x/posts/{id}.json` (delete) | `/2/tweets/:id` | DELETE |
| `/x/bookmarks/all.json` | `/2/users/:id/bookmarks` | GET |
| `/x/search/{query}.json` | `/2/tweets/search/recent` | GET |
| `/x/users/{username}/profile.json` | `/2/users/by/username/:username` | GET |

---

## Limitations

⚠️ **Not a True Filesystem**
- Virtual paths don't correspond to actual files
- No `mkdir`, `rmdir`, or `chmod` support
- Fixed directory structure

⚠️ **API Constraints**
- Rate limits apply (see Rate Limiting section)
- Search only covers last 7 days (unless premium)
- Tweet history limited to 3200 most recent

⚠️ **Write Restrictions**
- Cannot edit existing tweets (Twitter doesn't support)
- Cannot modify timeline or mentions
- Limited to posting/deleting your own content

⚠️ **Media Limitations**
- Media upload not yet implemented
- Media files are cached locally (consume disk space)
- No video streaming support

---

## Future Enhancements

🔮 **Planned Features**
- [ ] Media upload support (images, videos)
- [ ] Direct messages (DMs)
- [ ] Lists management
- [ ] Spaces integration
- [ ] Analytics/metrics
- [ ] Webhook support for real-time updates
- [ ] Batch operations API
- [ ] Extended tweet history (via archive)

---

## Examples

See `examples/x_connector_example.py` for comprehensive examples including:
- Basic operations (read timeline, post tweet)
- Grep operations (search tweets)
- Glob operations (list files)
- Error handling

---

## Support

- **Documentation**: `/docs/design/x-connector.md`
- **Issues**: https://github.com/nexi-lab/nexus/issues
- **X API Docs**: https://developer.twitter.com/en/docs/twitter-api

---

## License

Same as Nexus project license.
