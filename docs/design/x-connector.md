# X (Twitter) Connector Design

**Status**: Proposal
**Author**: Nexus Team
**Date**: 2025-01-22
**Version**: 1.0

---

## Table of Contents

1. [Overview](#overview)
2. [Virtual Filesystem Design](#virtual-filesystem-design)
3. [API Endpoint Mapping](#api-endpoint-mapping)
4. [Authentication Architecture](#authentication-architecture)
5. [Rate Limiting & Caching](#rate-limiting--caching)
6. [Filesystem Operations (grep, glob, write)](#filesystem-operations-grep-glob-write)
7. [Implementation Details](#implementation-details)
8. [Usage Examples](#usage-examples)
9. [Comparison with File Storage Connectors](#comparison-with-file-storage-connectors)

---

## Overview

### Purpose

The X (Twitter) Connector enables Nexus to interface with the X/Twitter platform by mapping social media concepts (tweets, timelines, users) to a virtual filesystem interface. This allows AI agents and applications to interact with X using familiar file operations.

### Key Challenges

| Challenge | Solution |
|-----------|----------|
| **Social media vs files** | Virtual filesystem with API-defined structure |
| **API rate limits** | Multi-tier caching with TTL |
| **Read-heavy workload** | Smart cache invalidation strategies |
| **Structured data** | JSON files representing tweets/timelines |
| **Media handling** | Lazy download with local caching |
| **OAuth 2.0 PKCE** | Reuse TokenManager with new provider |

### Design Principles

1. **Intuitive Mapping**: Virtual paths mirror X concepts (`/timeline/`, `/posts/`, `/mentions/`)
2. **Transparent Caching**: Hide complexity of rate limits from users
3. **Read-Optimized**: Most operations are cached reads
4. **Write Safety**: Limited write paths prevent accidental operations
5. **Media Support**: Automatic handling of images/videos

---

## Virtual Filesystem Design

### Directory Structure

```
/x/                                    # Root namespace
├── timeline/                          # Home timeline (chronological feed)
│   ├── recent.json                   # Last 100 tweets (cached 5 min)
│   ├── 2025-01-22.json              # Daily archive
│   └── media/                        # Downloaded media
│       └── {tweet_id}/
│           ├── image_1.jpg
│           ├── image_2.jpg
│           └── video_1.mp4
│
├── mentions/                          # Tweets mentioning authenticated user
│   ├── recent.json                   # Last 100 mentions
│   └── unread.json                   # Unread mentions only
│
├── posts/                             # User's own tweets
│   ├── all.json                      # All user tweets (paginated)
│   ├── {tweet_id}.json              # Individual tweet details
│   └── drafts/                       # Local drafts (not posted)
│       └── {draft_id}.json
│
├── bookmarks/                         # Saved tweets
│   ├── all.json                      # All bookmarks
│   └── media/
│       └── {tweet_id}/
│
├── lists/                             # User's lists
│   ├── index.json                    # List of all lists
│   └── {list_id}/
│       ├── info.json                 # List metadata
│       ├── tweets.json               # Tweets in list
│       └── members.json              # List members
│
├── search/                            # Search results (ephemeral)
│   ├── {query_hash}.json            # Search results for query
│   └── cache/                        # Cached search results
│
└── users/                             # User profiles
    └── {username}/
        ├── profile.json              # User metadata
        ├── tweets.json               # User's timeline
        ├── followers.json            # Follower list
        ├── following.json            # Following list
        └── media/                    # User's media
```

### Path Type Classification

| Path Type | Access Mode | Caching | Examples |
|-----------|-------------|---------|----------|
| **Timeline** | Read-only | 5 min | `/timeline/recent.json` |
| **Mentions** | Read-only | 5 min | `/mentions/recent.json` |
| **User Posts** | Read-only | 1 hour | `/posts/all.json` |
| **Single Tweet** | Read-only | 24 hours | `/posts/1234567890.json` |
| **Drafts** | Read-write | None | `/posts/drafts/new.json` |
| **New Posts** | Write-only | None | `/posts/new.json` |
| **Bookmarks** | Read-only | 1 hour | `/bookmarks/all.json` |
| **Search** | Read-only | 30 min | `/search/python.json` |
| **User Profiles** | Read-only | 1 hour | `/users/elonmusk/profile.json` |
| **Media** | Read-only | 24 hours | `/timeline/media/{id}/image.jpg` |

### Virtual Path Resolution

```python
def resolve_path(backend_path: str) -> tuple[str, dict]:
    """
    Map virtual path to X API endpoint and parameters.

    Returns:
        (endpoint_type, params) tuple

    Examples:
        /timeline/recent.json
            -> ("user_timeline", {"max_results": 100})

        /posts/1234567890.json
            -> ("single_tweet", {"id": "1234567890"})

        /mentions/unread.json
            -> ("mentions", {"unread": True})

        /search/python.json
            -> ("search_recent", {"query": "python", "max_results": 100})

        /users/elonmusk/profile.json
            -> ("user_by_username", {"username": "elonmusk"})
    """
```

---

## API Endpoint Mapping

### X API v2 Endpoints

#### Read Operations

| Virtual Path | X API Endpoint | Rate Limit | Cache TTL |
|--------------|----------------|------------|-----------|
| `/timeline/recent.json` | `GET /2/users/:id/timelines/reverse_chronological` | 180/15min | 5 min |
| `/mentions/recent.json` | `GET /2/users/:id/mentions` | 180/15min | 5 min |
| `/posts/all.json` | `GET /2/users/:id/tweets` | 900/15min | 1 hour |
| `/posts/{id}.json` | `GET /2/tweets/:id` | 900/15min | 24 hours |
| `/bookmarks/all.json` | `GET /2/users/:id/bookmarks` | 180/15min | 1 hour |
| `/search/{query}.json` | `GET /2/tweets/search/recent` | 450/15min | 30 min |
| `/users/{username}/profile.json` | `GET /2/users/by/username/:username` | 900/15min | 1 hour |
| `/users/{username}/tweets.json` | `GET /2/users/:id/tweets` | 900/15min | 1 hour |
| `/users/{username}/followers.json` | `GET /2/users/:id/followers` | 15/15min | 1 hour |
| `/users/{username}/following.json` | `GET /2/users/:id/following` | 15/15min | 1 hour |

#### Write Operations

| Virtual Path | X API Endpoint | Rate Limit | Notes |
|--------------|----------------|------------|-------|
| `/posts/new.json` | `POST /2/tweets` | 200/15min | Create tweet |
| `/posts/{id}.json` (DELETE) | `DELETE /2/tweets/:id` | 50/15min | Delete own tweet |
| `/bookmarks/add.json` | `POST /2/users/:id/bookmarks` | 1000/24hr | Bookmark tweet |
| `/bookmarks/{id}.json` (DELETE) | `DELETE /2/users/:id/bookmarks/:tweet_id` | 1000/24hr | Remove bookmark |

### API Request Parameters

#### Timeline Request

```json
{
  "endpoint": "GET /2/users/:id/timelines/reverse_chronological",
  "params": {
    "max_results": 100,
    "tweet.fields": [
      "created_at",
      "author_id",
      "text",
      "attachments",
      "public_metrics",
      "referenced_tweets"
    ],
    "expansions": [
      "author_id",
      "attachments.media_keys",
      "referenced_tweets.id"
    ],
    "media.fields": [
      "url",
      "preview_image_url",
      "type",
      "width",
      "height"
    ],
    "user.fields": [
      "username",
      "name",
      "profile_image_url",
      "verified"
    ]
  }
}
```

#### Post Tweet Request

```json
{
  "endpoint": "POST /2/tweets",
  "body": {
    "text": "Hello from Nexus! 🚀",
    "reply_settings": "everyone",  // or "mentionedUsers", "following"
    "poll": {  // Optional
      "options": ["Option 1", "Option 2"],
      "duration_minutes": 1440
    },
    "media": {  // Optional
      "media_ids": ["1234567890"]
    },
    "quote_tweet_id": "1234567890"  // Optional
  }
}
```

#### Search Request

```json
{
  "endpoint": "GET /2/tweets/search/recent",
  "params": {
    "query": "python lang:en -is:retweet",
    "max_results": 100,
    "start_time": "2025-01-15T00:00:00Z",
    "end_time": "2025-01-22T23:59:59Z",
    "sort_order": "recency"  // or "relevancy"
  }
}
```

### Response Transformation

X API responses are transformed to simplified JSON for file storage:

```python
# Raw X API Response
{
  "data": [
    {
      "id": "1234567890",
      "text": "Hello world!",
      "author_id": "9876543210",
      "created_at": "2025-01-22T12:00:00.000Z",
      "public_metrics": {
        "like_count": 42,
        "retweet_count": 10
      }
    }
  ],
  "includes": {
    "users": [
      {
        "id": "9876543210",
        "username": "nexusai",
        "name": "Nexus AI"
      }
    ]
  }
}

# Transformed for Nexus (flattened)
{
  "tweets": [
    {
      "id": "1234567890",
      "text": "Hello world!",
      "author": {
        "id": "9876543210",
        "username": "nexusai",
        "name": "Nexus AI"
      },
      "created_at": "2025-01-22T12:00:00.000Z",
      "metrics": {
        "likes": 42,
        "retweets": 10
      },
      "media": [],
      "urls": []
    }
  ],
  "meta": {
    "result_count": 1,
    "next_token": "abc123...",
    "cached_at": "2025-01-22T12:05:00.000Z",
    "cache_ttl": 300
  }
}
```

---

## Authentication Architecture

### OAuth 2.0 PKCE Flow

X API v2 requires **OAuth 2.0 Authorization Code Flow with PKCE** (Proof Key for Code Exchange) for user-context authentication.

#### Key Endpoints

```python
AUTHORIZATION_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
REVOKE_URL = "https://api.twitter.com/2/oauth2/revoke"
```

#### Required Scopes

```python
SCOPES = [
    "tweet.read",        # Read tweets
    "tweet.write",       # Post tweets
    "tweet.moderate.write",  # Delete tweets
    "users.read",        # Read user profiles
    "follows.read",      # Read followers/following
    "follows.write",     # Follow/unfollow users
    "offline.access",    # Refresh tokens (persistent access)
    "space.read",        # Read Spaces
    "mute.read",         # Read muted accounts
    "mute.write",        # Mute/unmute accounts
    "like.read",         # Read likes
    "like.write",        # Like/unlike tweets
    "list.read",         # Read lists
    "list.write",        # Create/modify lists
    "bookmark.read",     # Read bookmarks
    "bookmark.write",    # Add/remove bookmarks
]
```

### OAuth Provider Implementation

```python
class XOAuthProvider(OAuthProvider):
    """OAuth 2.0 PKCE provider for X (Twitter) API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str | None = None,  # Optional for PKCE
        redirect_uri: str = "http://localhost:5173/auth/callback",
        scopes: list[str] | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes or DEFAULT_SCOPES

    async def get_authorization_url(
        self,
        state: str
    ) -> tuple[str, dict[str, Any]]:
        """
        Generate authorization URL with PKCE challenge.

        Returns:
            (auth_url, pkce_data) - URL for user and PKCE verifier to store
        """
        # Generate PKCE code verifier (43-128 chars, URL-safe)
        code_verifier = base64.urlsafe_b64encode(
            os.urandom(32)
        ).decode('utf-8').rstrip('=')

        # Generate PKCE code challenge (SHA256 hash)
        challenge = hashlib.sha256(
            code_verifier.encode('utf-8')
        ).digest()
        code_challenge = base64.urlsafe_b64encode(
            challenge
        ).decode('utf-8').rstrip('=')

        # Build authorization URL
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        auth_url = f"{AUTHORIZATION_URL}?{urlencode(params)}"

        # Return URL and verifier (to be stored temporarily)
        return auth_url, {"code_verifier": code_verifier}

    async def exchange_code_for_token(
        self,
        code: str,
        pkce_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Exchange authorization code for access token using PKCE verifier.

        Args:
            code: Authorization code from callback
            pkce_data: Dict containing code_verifier

        Returns:
            Token response with access_token, refresh_token, expires_in
        """
        code_verifier = pkce_data.get("code_verifier")
        if not code_verifier:
            raise ValueError("Missing code_verifier in PKCE data")

        # Prepare token request
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }

        # Add client_secret if using confidential client
        if self.client_secret:
            data["client_secret"] = self.client_secret

        # Request tokens
        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, data=data) as response:
                if response.status != 200:
                    error = await response.text()
                    raise AuthenticationError(
                        f"Token exchange failed: {error}"
                    )
                return await response.json()

    async def refresh_token(
        self,
        refresh_token: str
    ) -> dict[str, Any]:
        """Refresh access token using refresh token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id,
        }

        if self.client_secret:
            data["client_secret"] = self.client_secret

        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, data=data) as response:
                if response.status != 200:
                    error = await response.text()
                    raise AuthenticationError(
                        f"Token refresh failed: {error}"
                    )
                return await response.json()

    async def revoke_token(self, token: str) -> None:
        """Revoke access or refresh token."""
        data = {
            "token": token,
            "client_id": self.client_id,
        }

        if self.client_secret:
            data["client_secret"] = self.client_secret

        async with aiohttp.ClientSession() as session:
            async with session.post(REVOKE_URL, data=data) as response:
                if response.status != 200:
                    error = await response.text()
                    raise AuthenticationError(
                        f"Token revocation failed: {error}"
                    )
```

### CLI Authentication Setup

```bash
# Setup X OAuth credentials
nexus oauth setup-x \
    --client-id "your-client-id" \
    --client-secret "your-secret" \  # Optional for PKCE
    --user-email "you@example.com"

# This will:
# 1. Store OAuth credentials in TokenManager
# 2. Open browser for user authorization
# 3. Handle callback and exchange code for tokens
# 4. Store encrypted tokens in database
# 5. Test token with basic API call
```

### Token Storage Schema

```sql
-- Extends existing oauth_tokens table
CREATE TABLE oauth_tokens (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(50) NOT NULL,      -- 'twitter' or 'x'
    user_email VARCHAR(255) NOT NULL,
    tenant_id VARCHAR(255) NOT NULL DEFAULT 'default',

    -- OAuth tokens (encrypted)
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    token_type VARCHAR(50) DEFAULT 'Bearer',

    -- Token metadata
    expires_at TIMESTAMP,
    scopes TEXT[],                      -- Array of granted scopes

    -- X-specific metadata
    x_user_id VARCHAR(255),             -- X user ID
    x_username VARCHAR(255),            -- X username

    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),

    UNIQUE(provider, user_email, tenant_id)
);
```

---

## Rate Limiting & Caching

### Rate Limit Strategy

#### Per-Endpoint Limits

```python
RATE_LIMITS = {
    "user_timeline": RateLimit(
        requests=180,
        window_seconds=15*60,
        tier="user_context"
    ),
    "user_tweets": RateLimit(
        requests=900,
        window_seconds=15*60,
        tier="user_context"
    ),
    "single_tweet": RateLimit(
        requests=900,
        window_seconds=15*60,
        tier="user_context"
    ),
    "mentions": RateLimit(
        requests=180,
        window_seconds=15*60,
        tier="user_context"
    ),
    "search_recent": RateLimit(
        requests=450,
        window_seconds=15*60,
        tier="user_context"
    ),
    "create_tweet": RateLimit(
        requests=200,
        window_seconds=15*60,
        tier="user_context"
    ),
    "delete_tweet": RateLimit(
        requests=50,
        window_seconds=15*60,
        tier="user_context"
    ),
}
```

#### Rate Limit Handler

```python
class RateLimitHandler:
    """Track and enforce X API rate limits."""

    def __init__(self):
        # Track per-endpoint limits: endpoint -> (remaining, reset_time)
        self._limits: dict[str, tuple[int, float]] = {}
        # Lock for thread-safe updates
        self._lock = asyncio.Lock()

    async def check_limit(self, endpoint: str) -> bool:
        """
        Check if endpoint has available quota.

        Raises:
            RateLimitError: If rate limit exceeded
        """
        async with self._lock:
            if endpoint in self._limits:
                remaining, reset_time = self._limits[endpoint]

                if remaining == 0 and time.time() < reset_time:
                    wait_seconds = reset_time - time.time()
                    raise RateLimitError(
                        endpoint=endpoint,
                        reset_at=datetime.fromtimestamp(reset_time),
                        wait_seconds=wait_seconds,
                        message=f"Rate limit exceeded. Resets in {wait_seconds:.0f}s"
                    )

            return True

    async def update_from_headers(
        self,
        endpoint: str,
        headers: dict[str, str]
    ) -> None:
        """Update rate limit state from X API response headers."""
        async with self._lock:
            # X API v2 rate limit headers
            remaining = int(headers.get("x-rate-limit-remaining", 0))
            reset_time = int(headers.get("x-rate-limit-reset", 0))
            limit = int(headers.get("x-rate-limit-limit", 0))

            self._limits[endpoint] = (remaining, reset_time)

            logger.debug(
                f"Rate limit for {endpoint}: "
                f"{remaining}/{limit} remaining, "
                f"resets at {datetime.fromtimestamp(reset_time)}"
            )

    def get_status(self, endpoint: str) -> dict[str, Any]:
        """Get current rate limit status for endpoint."""
        if endpoint not in self._limits:
            return {"status": "unknown"}

        remaining, reset_time = self._limits[endpoint]
        return {
            "remaining": remaining,
            "reset_at": datetime.fromtimestamp(reset_time).isoformat(),
            "wait_seconds": max(0, reset_time - time.time()),
        }
```

### Multi-Tier Caching Architecture

#### Cache Levels

```python
# Level 1: In-Memory Cache (fastest, smallest)
# - TTL: 1-5 minutes
# - Size: 100 MB max
# - Stores: Recent timeline, mentions, frequently accessed tweets

# Level 2: Local Disk Cache (fast, medium)
# - TTL: 1-24 hours
# - Size: 1 GB max
# - Stores: User tweets, profiles, media files

# Level 3: Database Cache (persistent, largest)
# - TTL: 24 hours - 7 days
# - Size: Unlimited
# - Stores: All fetched data for analytics/backup
```

#### Cache Implementation

```python
class TieredCache:
    """Multi-tier caching for X API responses."""

    def __init__(
        self,
        memory_size_mb: int = 100,
        disk_size_mb: int = 1024,
        db_path: str | None = None,
    ):
        # Level 1: In-memory LRU cache
        self._memory_cache = LRUCache(maxsize=memory_size_mb * 1024 * 1024)

        # Level 2: Disk cache (using diskcache library)
        self._disk_cache = Cache(
            directory="/tmp/nexus-x-cache",
            size_limit=disk_size_mb * 1024 * 1024,
        )

        # Level 3: Database cache (PostgreSQL/SQLite)
        self._db_cache = DatabaseCache(db_path) if db_path else None

    async def get(
        self,
        key: str,
        max_age: float | None = None
    ) -> bytes | None:
        """
        Get cached content from tiered cache.

        Checks Level 1 → Level 2 → Level 3 in order.
        Promotes cache hits to higher tiers.
        """
        # Try Level 1: Memory
        if key in self._memory_cache:
            content, timestamp = self._memory_cache[key]
            if max_age is None or (time.time() - timestamp) < max_age:
                logger.debug(f"Cache hit (L1-memory): {key}")
                return content

        # Try Level 2: Disk
        disk_result = self._disk_cache.get(key)
        if disk_result:
            content, timestamp = disk_result
            if max_age is None or (time.time() - timestamp) < max_age:
                logger.debug(f"Cache hit (L2-disk): {key}")
                # Promote to Level 1
                self._memory_cache[key] = (content, timestamp)
                return content

        # Try Level 3: Database
        if self._db_cache:
            db_result = await self._db_cache.get(key)
            if db_result:
                content, timestamp = db_result
                if max_age is None or (time.time() - timestamp) < max_age:
                    logger.debug(f"Cache hit (L3-database): {key}")
                    # Promote to Level 2 and Level 1
                    self._disk_cache.set(key, (content, timestamp))
                    self._memory_cache[key] = (content, timestamp)
                    return content

        logger.debug(f"Cache miss: {key}")
        return None

    async def set(
        self,
        key: str,
        content: bytes,
        tier: int = 1
    ) -> None:
        """
        Store content in cache tier(s).

        Args:
            tier: Which tier to use (1=memory, 2=disk, 3=database)
                  Content is stored in specified tier and all lower tiers
        """
        timestamp = time.time()

        # Store in Level 1 (memory)
        if tier >= 1:
            self._memory_cache[key] = (content, timestamp)

        # Store in Level 2 (disk)
        if tier >= 2:
            self._disk_cache.set(key, (content, timestamp))

        # Store in Level 3 (database)
        if tier >= 3 and self._db_cache:
            await self._db_cache.set(key, content, timestamp)
```

#### Cache Key Generation

```python
def generate_cache_key(
    endpoint: str,
    params: dict[str, Any],
    user_id: str
) -> str:
    """
    Generate deterministic cache key for API request.

    Format: "x:{user_id}:{endpoint}:{params_hash}"

    Examples:
        x:user123:timeline:a1b2c3d4
        x:user456:tweet:1234567890
        x:user789:search:e5f6g7h8
    """
    # Sort params for deterministic hash
    param_str = json.dumps(params, sort_keys=True)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]

    return f"x:{user_id}:{endpoint}:{param_hash}"
```

#### Cache TTL Configuration

```python
CACHE_TTL = {
    # Frequently changing data (short TTL)
    "timeline": 300,           # 5 minutes
    "mentions": 300,           # 5 minutes
    "search": 1800,            # 30 minutes

    # Semi-static data (medium TTL)
    "user_tweets": 3600,       # 1 hour
    "bookmarks": 3600,         # 1 hour
    "user_profile": 3600,      # 1 hour
    "followers": 3600,         # 1 hour
    "following": 3600,         # 1 hour

    # Static data (long TTL)
    "single_tweet": 86400,     # 24 hours
    "user_media": 86400,       # 24 hours

    # No caching
    "create_tweet": 0,         # Write operation
    "delete_tweet": 0,         # Write operation
}
```

---

## Filesystem Operations (grep, glob, write)

### Challenge: Virtual Filesystem vs Real Operations

The X connector presents a unique challenge: **most paths are virtual and read-only**, but Nexus provides filesystem operations like `grep()`, `glob()`, `write()`, and `delete()`. How do we map these operations to X's API?

### Operation Mapping Strategy

| Operation | Real Filesystem | X Connector (Virtual) | Implementation |
|-----------|----------------|----------------------|----------------|
| **read()** | Read file bytes | Fetch from API/cache | Direct mapping |
| **write()** | Write file bytes | Post tweet / Save draft | Limited paths only |
| **delete()** | Delete file | Delete tweet | Only user's own tweets |
| **list()** | List directory | List virtual files | API call + virtual structure |
| **glob()** | Pattern match files | Match virtual paths + API | Hybrid approach |
| **grep()** | Search file contents | Search via X API | Map to search API |
| **mkdir()** | Create directory | ❌ Not supported | Virtual structure is fixed |
| **stat()** | Get file metadata | Get tweet metadata | Map tweet data to stat |

---

### grep() - Content Search

**Challenge**: How do you grep through virtual files that don't exist until fetched?

**Solution**: Map grep patterns to X API search capabilities

#### Implementation Strategy

```python
def grep(
    self,
    pattern: str,
    path: str = "/",
    file_pattern: str | None = None,
    ignore_case: bool = False,
    max_results: int = 100,
    context: Any = None,
) -> list[dict[str, Any]]:
    """
    Search content using X API search.

    For X connector, grep is implemented as:
    1. Cached files: Search locally in cached JSON
    2. Timeline/posts: Use X API search within context
    3. Unknown paths: Fall back to cache-only search

    Examples:
        # Search timeline for pattern
        nx.grep("python", path="/x/timeline/")
        → Searches cached timeline JSONs for "python"

        # Search user's tweets
        nx.grep("error", path="/x/posts/")
        → Calls X API: GET /2/users/:id/tweets with query filter

        # Search globally (uses X search API)
        nx.grep("nexus ai", path="/x/search/")
        → Calls X API: GET /2/tweets/search/recent
    """
```

#### Grep Modes

| Path Pattern | Grep Behavior | API Endpoint Used |
|--------------|---------------|-------------------|
| `/x/timeline/**` | Search cached timeline JSON files | Local cache only |
| `/x/posts/**` | Search user's own tweets | `GET /2/users/:id/tweets` with text filter |
| `/x/mentions/**` | Search mentions | `GET /2/users/:id/mentions` with text filter |
| `/x/search/**` | Global tweet search | `GET /2/tweets/search/recent` |
| `/x/bookmarks/**` | Search bookmarked tweets | Local cache only |

#### Example: Grep Implementation

```python
def grep(self, pattern: str, path: str = "/", **kwargs) -> list[dict]:
    """Search tweets using pattern."""
    path = path.strip("/")

    # Compile regex pattern
    regex = re.compile(pattern, re.IGNORECASE if kwargs.get("ignore_case") else 0)

    # Determine search strategy based on path
    if path.startswith("x/timeline") or path.startswith("x/bookmarks"):
        # Search cached JSON files only
        return self._grep_cached(regex, path, kwargs.get("max_results", 100))

    elif path.startswith("x/posts"):
        # Search user's tweets via API
        return self._grep_user_tweets(regex, kwargs)

    elif path.startswith("x/search") or path == "x":
        # Global search via X API
        return self._grep_global(pattern, kwargs)

    else:
        # Fallback: search cached files
        return self._grep_cached(regex, path, kwargs.get("max_results", 100))

def _grep_global(self, pattern: str, params: dict) -> list[dict]:
    """Global tweet search using X API."""
    client = self._get_api_client(params.get("context"))

    # Use X search API
    response = asyncio.run(client.search_tweets(
        query=pattern,
        max_results=params.get("max_results", 100),
    ))

    # Transform to grep result format
    results = []
    for tweet in response["data"]:
        # Find line number where pattern appears
        lines = tweet["text"].split("\n")
        for line_num, line in enumerate(lines, start=1):
            if re.search(pattern, line, re.IGNORECASE):
                results.append({
                    "file": f"/x/posts/{tweet['id']}.json",
                    "line": line_num,
                    "content": line,
                    "match": re.search(pattern, line, re.IGNORECASE).group(0),
                    "source": "x_api",
                })

    return results
```

#### Grep Limitations

⚠️ **Important Constraints**:

1. **Cache-dependent**: Grep only searches cached content, not all possible tweets
2. **API search limitations**: X API search only covers last 7 days (unless using premium)
3. **Rate limits apply**: Each grep may consume API quota
4. **No full-text search**: X API search has specific query syntax (not pure regex)

**Recommendation**: For extensive grep operations, use `read()` to cache timelines first:

```python
# Pre-cache timeline
nx.read("/x/timeline/recent.json")

# Now grep is fast and doesn't hit API
results = nx.grep("python", path="/x/timeline/")
```

---

### glob() - Pattern Matching

**Challenge**: How do you glob virtual files that are dynamically generated?

**Solution**: Hybrid approach - combine virtual structure with API calls

#### Implementation Strategy

```python
def glob(
    self,
    pattern: str,
    path: str = "/",
    context: Any = None,
) -> list[str]:
    """
    Match paths using glob patterns.

    For X connector, glob works on:
    1. Virtual directory structure (fixed paths)
    2. Dynamic tweet IDs (via API calls)
    3. Cached media files (local storage)

    Examples:
        # List all top-level directories
        nx.glob("/x/*")
        → ["/x/timeline/", "/x/posts/", "/x/mentions/", ...]

        # List all cached tweets
        nx.glob("/x/posts/*.json")
        → ["/x/posts/1234567890.json", "/x/posts/9876543210.json", ...]

        # List all cached media
        nx.glob("/x/timeline/media/*/*.jpg")
        → ["/x/timeline/media/123/image.jpg", ...]
    """
```

#### Glob Behavior by Path

| Pattern | Behavior | Data Source |
|---------|----------|-------------|
| `/x/*` | List virtual root directories | Hardcoded structure |
| `/x/posts/*.json` | List user's tweet IDs | API call: `GET /2/users/:id/tweets` |
| `/x/timeline/*.json` | List available timeline files | `recent.json` + cached dates |
| `/x/timeline/media/*/*.jpg` | List cached media images | Local disk cache |
| `/x/users/*/profile.json` | List cached user profiles | Local disk cache |
| `/x/search/*.json` | List cached search results | Local disk cache |

#### Example: Glob Implementation

```python
def glob(self, pattern: str, path: str = "/", context: Any = None) -> list[str]:
    """Match virtual paths using glob patterns."""
    import fnmatch

    # Parse glob pattern
    parts = pattern.strip("/").split("/")

    # Handle root-level globs
    if pattern == "/x/*" or pattern == "/x/*/":
        # Return virtual root directories
        return [
            "/x/timeline/",
            "/x/mentions/",
            "/x/posts/",
            "/x/bookmarks/",
            "/x/lists/",
            "/x/search/",
            "/x/users/",
        ]

    # Handle posts glob: /x/posts/*.json
    if pattern.startswith("/x/posts/") and "*.json" in pattern:
        # Fetch user's tweet IDs via API
        client = self._get_api_client(context)
        response = asyncio.run(client.get_user_tweets(max_results=100))

        # Return virtual paths for each tweet
        return [
            f"/x/posts/{tweet['id']}.json"
            for tweet in response.get("data", [])
        ]

    # Handle media glob: /x/timeline/media/*/*.jpg
    if "media" in pattern:
        # List cached media files from disk
        cache_dir = "/tmp/nexus-x-cache/media/"
        if os.path.exists(cache_dir):
            all_files = []
            for root, dirs, files in os.walk(cache_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    # Convert to virtual path
                    virtual_path = full_path.replace(cache_dir, "/x/timeline/media/")
                    if fnmatch.fnmatch(virtual_path, pattern):
                        all_files.append(virtual_path)
            return sorted(all_files)

    # Handle timeline glob: /x/timeline/*.json
    if pattern.startswith("/x/timeline/"):
        available = ["recent.json"]

        # Add cached daily archives
        cache_dir = "/tmp/nexus-x-cache/timeline/"
        if os.path.exists(cache_dir):
            for file in os.listdir(cache_dir):
                if file.endswith(".json"):
                    available.append(file)

        # Filter by glob pattern
        return [
            f"/x/timeline/{file}"
            for file in available
            if fnmatch.fnmatch(f"/x/timeline/{file}", pattern)
        ]

    # Default: return empty (path doesn't exist)
    return []
```

#### Glob Performance

- **Virtual paths**: Instant (hardcoded structure)
- **API-backed paths** (`/x/posts/*.json`): 1 API call + caching
- **Media paths**: Fast (local disk scan)
- **Cached paths**: Fast (local cache scan)

---

### write() - Content Modification

**Challenge**: Most paths are read-only. How do we prevent invalid writes?

**Solution**: Strict path validation with clear error messages

#### Writable Paths

| Path Pattern | Write Behavior | Notes |
|--------------|----------------|-------|
| `/x/posts/new.json` | ✅ Post new tweet | Creates tweet via API |
| `/x/posts/drafts/*.json` | ✅ Save draft | Stores locally (not posted) |
| `/x/timeline/**` | ❌ Read-only | Cannot modify timeline |
| `/x/mentions/**` | ❌ Read-only | Cannot modify mentions |
| `/x/bookmarks/**` | ❌ Read-only | Use dedicated API instead |
| `/x/users/**` | ❌ Read-only | Cannot modify other users |

#### Example: Write Validation

```python
def write_content(self, content: bytes, context: OperationContext | None = None) -> str:
    """Write content (post tweet or save draft)."""
    if not context or not context.backend_path:
        raise BackendError("X connector requires context with backend_path")

    path = context.backend_path

    # Check if path is writable
    if not self._is_writable(path):
        raise PermissionError(
            f"Path '{path}' is read-only. "
            f"Writable paths: /x/posts/new.json, /x/posts/drafts/*.json"
        )

    # Route to appropriate handler
    if path == "/x/posts/new.json":
        return self._create_tweet(content, context)
    elif path.startswith("/x/posts/drafts/"):
        return self._save_draft(content, context)
    else:
        raise BackendError(f"Unknown writable path: {path}")

def _is_writable(self, path: str) -> bool:
    """Check if virtual path is writable."""
    WRITABLE_PATHS = [
        "/x/posts/new.json",
        "/x/posts/drafts/",
    ]

    for writable in WRITABLE_PATHS:
        if path.startswith(writable) or path == writable:
            return True

    return False
```

#### Write Examples

```python
# ✅ Valid: Post new tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "Hello world!"
}).encode())

# ✅ Valid: Save draft
nx.write("/x/posts/drafts/my-draft.json", json.dumps({
    "text": "Draft tweet (not posted yet)"
}).encode())

# ❌ Invalid: Cannot write to timeline
nx.write("/x/timeline/fake.json", b"...")
# → Raises: PermissionError: Path '/x/timeline/fake.json' is read-only

# ❌ Invalid: Cannot write to mentions
nx.write("/x/mentions/fake.json", b"...")
# → Raises: PermissionError: Path '/x/mentions/fake.json' is read-only
```

---

### delete() - Content Removal

**Challenge**: Can only delete user's own tweets, not arbitrary content

**Solution**: Path validation + API permission checks

#### Deletable Paths

| Path Pattern | Delete Behavior | Notes |
|--------------|----------------|-------|
| `/x/posts/{tweet_id}.json` | ✅ Delete own tweet | Calls `DELETE /2/tweets/:id` |
| `/x/posts/drafts/{draft_id}.json` | ✅ Delete draft | Removes local file |
| `/x/timeline/**` | ❌ Cannot delete | Not owned by user |
| `/x/mentions/**` | ❌ Cannot delete | Not owned by user |
| `/x/bookmarks/**` | Use `remove_bookmark()` | Different API endpoint |

#### Example: Delete Implementation

```python
def delete_content(self, content_hash: str, context: OperationContext | None = None) -> bool:
    """Delete tweet or draft."""
    if not context or not context.backend_path:
        raise BackendError("X connector requires context with backend_path")

    path = context.backend_path

    # Delete own tweet: /x/posts/1234567890.json
    if path.startswith("/x/posts/") and path.endswith(".json"):
        tweet_id = path.replace("/x/posts/", "").replace(".json", "")

        # Skip if it's a special file
        if tweet_id in ("new", "all"):
            raise BackendError(f"Cannot delete special file: {path}")

        # Check if it's a draft
        if "drafts/" in path:
            return self._delete_draft(tweet_id, context)

        # Delete tweet via API
        client = self._get_api_client(context)
        try:
            asyncio.run(client.delete_tweet(tweet_id))

            # Invalidate caches
            self._invalidate_caches(context.user_id, ["timeline", "user_tweets"])

            return True
        except Exception as e:
            if "403" in str(e):
                raise PermissionError(
                    f"Cannot delete tweet {tweet_id}: Not owned by user"
                ) from e
            raise BackendError(f"Failed to delete tweet: {e}") from e

    # Cannot delete other paths
    raise PermissionError(f"Path '{path}' cannot be deleted")
```

---

### list() - Directory Listing

**Challenge**: Virtual directories need to be populated dynamically

**Solution**: Mix hardcoded structure with API calls

#### Example: List Implementation

```python
def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
    """List virtual directory contents."""
    path = path.strip("/")

    # Root: /x/
    if not path or path == "x":
        return [
            "timeline/",
            "mentions/",
            "posts/",
            "bookmarks/",
            "lists/",
            "search/",
            "users/",
        ]

    # Timeline: /x/timeline/
    if path == "x/timeline":
        entries = ["recent.json", "media/"]

        # Add cached daily archives
        cache_dir = "/tmp/nexus-x-cache/timeline/"
        if os.path.exists(cache_dir):
            for file in os.listdir(cache_dir):
                if file.endswith(".json") and file != "recent.json":
                    entries.append(file)

        return sorted(entries)

    # Posts: /x/posts/ (fetch from API)
    if path == "x/posts":
        client = self._get_api_client(context)
        response = asyncio.run(client.get_user_tweets(max_results=100))

        entries = ["all.json", "drafts/"]
        entries.extend([
            f"{tweet['id']}.json"
            for tweet in response.get("data", [])
        ])

        return sorted(entries)

    # Other paths: return empty or error
    raise FileNotFoundError(f"Directory not found: {path}")
```

---

### stat() - File Metadata

**Challenge**: Virtual files don't have traditional file metadata

**Solution**: Map tweet metadata to stat structure

#### Example: Stat Implementation

```python
def stat(self, path: str, context: OperationContext | None = None) -> dict[str, Any]:
    """Get virtual file metadata."""
    # For individual tweet: /x/posts/1234567890.json
    if path.startswith("/x/posts/") and path.endswith(".json"):
        tweet_id = path.replace("/x/posts/", "").replace(".json", "")

        # Fetch tweet metadata
        client = self._get_api_client(context)
        tweet = asyncio.run(client.get_tweet(tweet_id))

        # Map to stat structure
        created_at = datetime.fromisoformat(
            tweet["created_at"].replace("Z", "+00:00")
        )

        return {
            "size": len(json.dumps(tweet)),  # JSON size
            "created": created_at.timestamp(),
            "modified": created_at.timestamp(),  # Tweets don't change
            "mode": 0o444,  # Read-only
            "type": "file",
            "extra": {
                "tweet_id": tweet_id,
                "author": tweet["author_id"],
                "likes": tweet["public_metrics"]["like_count"],
                "retweets": tweet["public_metrics"]["retweet_count"],
            }
        }

    # Default: return minimal stat
    return {
        "size": 0,
        "created": time.time(),
        "modified": time.time(),
        "mode": 0o555,  # Read-only directory
        "type": "directory",
    }
```

---

### mkdir() - Not Supported

Virtual filesystem structure is fixed and cannot be modified.

```python
def mkdir(self, path: str, **kwargs) -> None:
    """Not supported for X connector."""
    raise NotImplementedError(
        "X connector has a fixed virtual structure. "
        "mkdir() is not supported. "
        "Available paths: /x/timeline/, /x/posts/, /x/mentions/, etc."
    )
```

---

### Summary: Operation Support Matrix

| Operation | Supported | Notes |
|-----------|-----------|-------|
| **read()** | ✅ Full support | All paths, cached + API |
| **write()** | ⚠️ Limited | Only `/x/posts/new.json` and drafts |
| **delete()** | ⚠️ Limited | Only user's own tweets |
| **list()** | ✅ Full support | Virtual + dynamic via API |
| **glob()** | ✅ Full support | Hybrid: virtual + API + cache |
| **grep()** | ✅ Full support | Maps to X search API + cache search |
| **stat()** | ✅ Full support | Maps tweet metadata to stat |
| **mkdir()** | ❌ Not supported | Fixed virtual structure |
| **rmdir()** | ❌ Not supported | Fixed virtual structure |
| **chmod()** | ❌ Not supported | Permissions are fixed |

---

## Implementation Details

### Backend Class Structure

```python
class XConnectorBackend(Backend):
    """X (Twitter) connector backend with OAuth 2.0 PKCE."""

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        cache_ttl: dict[str, int] | None = None,
        download_media: bool = True,
        max_memory_cache_mb: int = 100,
        max_disk_cache_mb: int = 1024,
        provider: str = "twitter",
    ):
        """
        Initialize X connector backend.

        Args:
            token_manager_db: Path to TokenManager database
            user_email: User email for OAuth (None = use from context)
            cache_ttl: Custom cache TTL per endpoint type
            download_media: Auto-download media to local cache
            max_memory_cache_mb: Memory cache size limit
            max_disk_cache_mb: Disk cache size limit
            provider: OAuth provider name
        """
        # Initialize TokenManager
        from nexus.server.auth.token_manager import TokenManager

        if token_manager_db.startswith(("postgresql://", "sqlite://")):
            self.token_manager = TokenManager(db_url=token_manager_db)
        else:
            self.token_manager = TokenManager(db_path=token_manager_db)

        self.user_email = user_email
        self.cache_ttl = cache_ttl or CACHE_TTL
        self.download_media = download_media
        self.provider = provider

        # Initialize caching and rate limiting
        self._cache = TieredCache(
            memory_size_mb=max_memory_cache_mb,
            disk_size_mb=max_disk_cache_mb,
            db_path=token_manager_db,
        )
        self._rate_limiter = RateLimitHandler()

        # X API client (lazy initialization)
        self._api_client = None
        self._user_id_cache: dict[str, str] = {}  # email -> x_user_id

    @property
    def name(self) -> str:
        return "x"

    @property
    def user_scoped(self) -> bool:
        return True

    def _get_api_client(
        self,
        context: OperationContext | None
    ) -> XAPIClient:
        """Get authenticated X API client."""
        # Determine user email
        user_email = self.user_email or (
            context.user_id if context else None
        )

        if not user_email:
            raise BackendError(
                "X connector requires user_email or context.user_id"
            )

        # Get OAuth token
        tenant_id = context.tenant_id if context else "default"
        access_token = asyncio.run(
            self.token_manager.get_valid_token(
                provider=self.provider,
                user_email=user_email,
                tenant_id=tenant_id,
            )
        )

        # Create API client
        return XAPIClient(
            access_token=access_token,
            rate_limiter=self._rate_limiter,
        )

    def read_content(
        self,
        content_hash: str,
        context: OperationContext | None = None
    ) -> bytes:
        """Read content from X API via virtual path."""
        if not context or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path"
            )

        path = context.backend_path
        user_id = context.user_id or self.user_email

        # Resolve virtual path to API endpoint
        endpoint, params = self._resolve_path(path)

        # Generate cache key
        cache_key = generate_cache_key(endpoint, params, user_id)

        # Check cache
        ttl = self.cache_ttl.get(endpoint, 300)
        cached = asyncio.run(self._cache.get(cache_key, max_age=ttl))
        if cached:
            return cached

        # Fetch from API
        client = self._get_api_client(context)
        data = asyncio.run(self._fetch_from_api(client, endpoint, params))

        # Transform and serialize
        transformed = self._transform_response(endpoint, data)
        content = json.dumps(transformed, indent=2).encode("utf-8")

        # Cache response
        cache_tier = self._get_cache_tier(endpoint)
        asyncio.run(self._cache.set(cache_key, content, tier=cache_tier))

        return content

    def write_content(
        self,
        content: bytes,
        context: OperationContext | None = None
    ) -> str:
        """Write content (post tweet or save draft)."""
        if not context or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path"
            )

        path = context.backend_path

        # Validate writable path
        if not self._is_writable(path):
            raise BackendError(
                f"Path {path} is read-only. "
                f"Use /posts/ to create tweets or /posts/drafts/ for drafts"
            )

        # Parse content
        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError:
            # Treat as plain text tweet
            data = {"text": content.decode("utf-8")}

        # Handle drafts
        if path.startswith("/posts/drafts/"):
            draft_id = self._save_draft(data, context)
            return draft_id

        # Post tweet
        client = self._get_api_client(context)
        response = asyncio.run(client.create_tweet(**data))

        # Invalidate caches
        self._invalidate_caches(context.user_id, ["timeline", "user_tweets"])

        return response["data"]["id"]

    async def _fetch_from_api(
        self,
        client: XAPIClient,
        endpoint: str,
        params: dict[str, Any]
    ) -> dict[str, Any]:
        """Fetch data from X API with rate limit handling."""
        # Check rate limit
        await self._rate_limiter.check_limit(endpoint)

        # Make API request
        try:
            response = await client.request(endpoint, params)
            return response
        except RateLimitError:
            # Re-raise rate limit errors
            raise
        except Exception as e:
            raise BackendError(
                f"X API request failed: {e}",
                backend="x",
                path=f"{endpoint}?{params}"
            ) from e
```

### Path Resolution

```python
def _resolve_path(
    self,
    backend_path: str
) -> tuple[str, dict[str, Any]]:
    """
    Resolve virtual path to X API endpoint.

    Returns:
        (endpoint_type, params) tuple
    """
    parts = backend_path.strip("/").split("/")

    if not parts:
        raise BackendError("Invalid path: empty")

    namespace = parts[0]

    # Timeline paths
    if namespace == "timeline":
        if len(parts) == 1 or parts[1] == "recent.json":
            return ("user_timeline", {"max_results": 100})
        elif parts[1].endswith(".json"):
            # Daily archive format: 2025-01-22.json
            date_str = parts[1].replace(".json", "")
            return ("user_timeline", {
                "start_time": f"{date_str}T00:00:00Z",
                "end_time": f"{date_str}T23:59:59Z",
                "max_results": 100,
            })

    # Mentions paths
    elif namespace == "mentions":
        if len(parts) == 1 or parts[1] == "recent.json":
            return ("mentions", {"max_results": 100})
        elif parts[1] == "unread.json":
            return ("mentions", {"max_results": 100, "unread": True})

    # Posts paths
    elif namespace == "posts":
        if len(parts) == 1 or parts[1] == "all.json":
            return ("user_tweets", {"max_results": 100})
        elif parts[1].endswith(".json") and parts[1] != "all.json":
            # Individual tweet: /posts/1234567890.json
            tweet_id = parts[1].replace(".json", "")
            return ("single_tweet", {"id": tweet_id})

    # Bookmarks paths
    elif namespace == "bookmarks":
        return ("bookmarks", {"max_results": 100})

    # Search paths
    elif namespace == "search":
        if len(parts) > 1:
            query = parts[1].replace(".json", "").replace("_", " ")
            return ("search_recent", {
                "query": query,
                "max_results": 100,
            })

    # User paths
    elif namespace == "users":
        if len(parts) < 2:
            raise BackendError("User path requires username")

        username = parts[1]

        if len(parts) == 2 or parts[2] == "profile.json":
            return ("user_by_username", {"username": username})
        elif parts[2] == "tweets.json":
            return ("user_tweets", {"username": username})
        elif parts[2] == "followers.json":
            return ("followers", {"username": username})
        elif parts[2] == "following.json":
            return ("following", {"username": username})

    raise BackendError(f"Unknown virtual path: {backend_path}")
```

---

## Usage Examples

### Basic Operations

```python
from nexus import NexusFS
from nexus.backends import XConnectorBackend

# Initialize X connector
nx = NexusFS(
    backend=XConnectorBackend(
        token_manager_db="~/.nexus/nexus.db",
        cache_ttl={"timeline": 300, "mentions": 300},
        download_media=True,
    )
)

# Read home timeline
timeline_json = nx.read("/x/timeline/recent.json")
timeline = json.loads(timeline_json)

print(f"Timeline has {len(timeline['tweets'])} tweets")
for tweet in timeline['tweets'][:5]:
    print(f"  @{tweet['author']['username']}: {tweet['text'][:50]}...")

# Read mentions
mentions_json = nx.read("/x/mentions/recent.json")
mentions = json.loads(mentions_json)
print(f"You have {len(mentions['tweets'])} mentions")

# Post a tweet
nx.write("/x/posts/new.json", json.dumps({
    "text": "Hello from Nexus! Building AI agents on X. 🚀",
    "reply_settings": "following"
}).encode())

# Search tweets
search_json = nx.read("/x/search/python_ai.json")
results = json.loads(search_json)
print(f"Found {len(results['tweets'])} tweets about 'python ai'")

# Get user profile
profile_json = nx.read("/x/users/elonmusk/profile.json")
profile = json.loads(profile_json)
print(f"User: {profile['name']} (@{profile['username']})")
print(f"Followers: {profile['public_metrics']['followers_count']}")
```

### Advanced: AI Agent Integration

```python
from nexus import NexusFS
from nexus.backends import XConnectorBackend
import anthropic

# Initialize Nexus with X connector
nx = NexusFS(backend=XConnectorBackend(
    token_manager_db="~/.nexus/nexus.db"
))

# Initialize Claude
client = anthropic.Anthropic()

# Read user's mentions
mentions = json.loads(nx.read("/x/mentions/recent.json"))

# Process each mention with AI
for tweet in mentions['tweets']:
    # Skip if already replied
    if tweet.get('replied_to'):
        continue

    # Generate response using Claude
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        messages=[{
            "role": "user",
            "content": f"Generate a helpful reply to: {tweet['text']}"
        }]
    )

    reply_text = response.content[0].text

    # Post reply
    nx.write("/x/posts/new.json", json.dumps({
        "text": reply_text,
        "reply": {
            "in_reply_to_tweet_id": tweet['id']
        }
    }).encode())

    print(f"Replied to @{tweet['author']['username']}")
```

### Media Download

```python
# Download all media from timeline
timeline = json.loads(nx.read("/x/timeline/recent.json"))

for tweet in timeline['tweets']:
    if 'media' in tweet and tweet['media']:
        tweet_id = tweet['id']

        # List media files
        media_files = nx.list(f"/x/timeline/media/{tweet_id}/")

        for filename in media_files:
            # Read media file
            media_path = f"/x/timeline/media/{tweet_id}/{filename}"
            content = nx.read(media_path)

            # Save locally
            with open(f"downloads/{filename}", "wb") as f:
                f.write(content)

            print(f"Downloaded {filename} ({len(content)} bytes)")
```

### Daily Archive

```python
from datetime import datetime, timedelta

# Archive last 7 days of timeline
for i in range(7):
    date = datetime.now() - timedelta(days=i)
    date_str = date.strftime("%Y-%m-%d")

    # Read daily timeline
    timeline_json = nx.read(f"/x/timeline/{date_str}.json")

    # Save to local archive
    archive_path = f"archive/timeline-{date_str}.json"
    with open(archive_path, "wb") as f:
        f.write(timeline_json)

    print(f"Archived timeline for {date_str}")
```

---

## Comparison with File Storage Connectors

### Google Drive Connector vs X Connector

| Aspect | Google Drive | X Connector |
|--------|--------------|-------------|
| **Data Model** | Real files & folders | Virtual files (JSON) |
| **Path Structure** | User-defined hierarchy | API-defined structure |
| **Authentication** | OAuth 2.0 (implicit) | OAuth 2.0 PKCE |
| **Primary Operations** | Full file CRUD | Read timelines, post tweets |
| **Caching** | Backend-handled | Explicit multi-tier cache |
| **Rate Limits** | Transparent (Google handles) | Explicit tracking required |
| **Write Operations** | Full write support | Limited (posts only) |
| **Directory Support** | Native folders | Virtual directories |
| **Media Handling** | Native storage | Downloaded & cached |
| **Deduplication** | Not needed | Critical (same tweet, multiple views) |
| **Use Cases** | Document storage/collaboration | Social media automation |

### Design Trade-offs

| Feature | Decision | Rationale |
|---------|----------|-----------|
| **Virtual vs Real Paths** | Virtual | X API has fixed structure, not user-defined |
| **Caching Strategy** | Multi-tier aggressive | Avoid rate limits, improve performance |
| **Write Restrictions** | Limited paths | Prevent accidental operations |
| **JSON Format** | Simplified/flattened | Easier for AI agents to consume |
| **Media Download** | Lazy/on-demand | Avoid storage bloat |
| **Cache Invalidation** | TTL-based | Simple, predictable behavior |

---

## Next Steps

1. **Phase 1**: Implement OAuth PKCE provider
2. **Phase 2**: Basic read operations (timeline, mentions)
3. **Phase 3**: Write operations (post tweets)
4. **Phase 4**: Media handling and advanced features
5. **Phase 5**: Testing, documentation, examples

**Status**: Ready for implementation
**Estimated Timeline**: 5 weeks
**Dependencies**: TokenManager, Backend interface, X API credentials
