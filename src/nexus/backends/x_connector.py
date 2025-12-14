"""X (Twitter) connector backend with OAuth 2.0 PKCE authentication.

This connector maps X (Twitter) API to a virtual filesystem, allowing
AI agents and applications to interact with X using familiar file operations.

Virtual filesystem structure:
    /x/timeline/         - Home timeline tweets
    /x/mentions/         - Mentions
    /x/posts/           - User's tweets
    /x/bookmarks/       - Saved tweets
    /x/search/          - Search results
    /x/users/           - User profiles

Features:
- OAuth 2.0 PKCE authentication (per-user credentials)
- Virtual path mapping (tweets → JSON files)
- Multi-tier caching with TTL
- Rate limit handling
- Read-optimized with smart cache invalidation

Example:
    >>> from nexus import NexusFS
    >>> from nexus.backends import XConnectorBackend
    >>>
    >>> nx = NexusFS(backend=XConnectorBackend(
    ...     token_manager_db="~/.nexus/nexus.db",
    ...     cache_ttl=300,
    ... ))
    >>>
    >>> # Read timeline
    >>> timeline = nx.read("/x/timeline/recent.json")
    >>>
    >>> # Post tweet
    >>> nx.write("/x/posts/new.json", json.dumps({"text": "Hello!")).encode())
"""

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


# Cache TTL configuration (in seconds)
CACHE_TTL = {
    # Frequently changing data (short TTL)
    "timeline": 300,  # 5 minutes
    "mentions": 300,  # 5 minutes
    "search": 1800,  # 30 minutes
    # Semi-static data (medium TTL)
    "user_tweets": 3600,  # 1 hour
    "bookmarks": 3600,  # 1 hour
    "user_profile": 3600,  # 1 hour
    # Static data (long TTL)
    "single_tweet": 86400,  # 24 hours
    # No caching
    "create_tweet": 0,  # Write operation
    "delete_tweet": 0,  # Write operation
}


@register_connector(
    "x_connector",
    description="X (Twitter) API with OAuth 2.0 PKCE",
    category="api",
    requires=["requests-oauthlib"],
)
class XConnectorBackend(Backend):
    """
    X (Twitter) connector backend with OAuth 2.0 PKCE authentication.

    Maps X API to virtual filesystem:
    - /x/timeline/ → Home timeline tweets
    - /x/posts/ → User's tweets
    - /x/mentions/ → Mentions
    - /x/bookmarks/ → Saved tweets
    - /x/search/ → Search results

    Features:
    - OAuth 2.0 PKCE authentication (per-user credentials)
    - Virtual path mapping (tweets → JSON files)
    - Multi-tier caching with TTL
    - Rate limit handling
    - Read-optimized

    Limitations:
    - No true file storage (virtual filesystem only)
    - API rate limits apply
    - Read-only for most paths
    - Fixed virtual directory structure
    """

    user_scoped = True

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "token_manager_db": ConnectionArg(
            type=ArgType.PATH,
            description="Path to TokenManager database or database URL",
            required=True,
        ),
        "user_email": ConnectionArg(
            type=ArgType.STRING,
            description="User email for OAuth lookup (None for multi-user from context)",
            required=False,
        ),
        "cache_ttl": ConnectionArg(
            type=ArgType.STRING,
            description="Custom cache TTL configuration (JSON dict)",
            required=False,
        ),
        "cache_dir": ConnectionArg(
            type=ArgType.PATH,
            description="Cache directory path",
            required=False,
            default="/tmp/nexus-x-cache",
        ),
        "provider": ConnectionArg(
            type=ArgType.STRING,
            description="OAuth provider name",
            required=False,
            default="twitter",
        ),
    }

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        cache_ttl: dict[str, int] | None = None,
        cache_dir: str | None = None,
        provider: str = "twitter",
    ):
        """
        Initialize X connector backend.

        Args:
            token_manager_db: Path to TokenManager database or database URL
            user_email: User email for OAuth (None = use from context)
            cache_ttl: Custom cache TTL per endpoint type
            cache_dir: Cache directory (default: /tmp/nexus-x-cache)
            provider: OAuth provider name (default: "twitter")
        """
        # Initialize TokenManager
        from nexus.server.auth.token_manager import TokenManager

        # Resolve database URL using base class method (checks TOKEN_MANAGER_DB env var)
        resolved_db = self.resolve_database_url(token_manager_db)

        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db)
        else:
            self.token_manager = TokenManager(db_path=resolved_db)

        self.user_email = user_email
        self.cache_ttl = cache_ttl or CACHE_TTL
        self.cache_dir = cache_dir or "/tmp/nexus-x-cache"
        self.provider = provider

        # Create cache directory
        os.makedirs(self.cache_dir, exist_ok=True)

        # In-memory cache: (cache_key, user_id) -> (content, timestamp)
        self._memory_cache: dict[tuple[str, str], tuple[bytes, float]] = {}

        # User ID cache: user_email -> x_user_id
        self._user_id_cache: dict[str, str] = {}

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "x"

    async def _get_api_client_async(
        self, context: "OperationContext | None"
    ) -> Any:  # Returns XAPIClient but avoid circular import
        """Get authenticated X API client (async version).

        Args:
            context: Operation context with user_id

        Returns:
            XAPIClient instance

        Raises:
            BackendError: If authentication fails
        """
        from nexus.backends.x_api_client import XAPIClient

        # Determine user email
        user_email = self.user_email or (context.user_id if context else None)

        if not user_email:
            raise BackendError(
                "X connector requires user_email or context.user_id",
                backend="x",
            )

        # Get OAuth token
        tenant_id: str = (
            context.tenant_id if context and hasattr(context, "tenant_id") else "default"
        ) or "default"

        try:
            access_token = await self.token_manager.get_valid_token(
                provider=self.provider,
                user_email=user_email,
                tenant_id=tenant_id,
            )
        except Exception as e:
            raise BackendError(
                f"Failed to get OAuth token for {user_email}: {e}",
                backend="x",
            ) from e

        # Create API client
        return XAPIClient(access_token=access_token)

    def _get_api_client(
        self, context: "OperationContext | None"
    ) -> Any:  # Returns XAPIClient but avoid circular import
        """Get authenticated X API client (sync wrapper).

        Args:
            context: Operation context with user_id

        Returns:
            XAPIClient instance

        Raises:
            BackendError: If authentication fails
        """
        return asyncio.run(self._get_api_client_async(context))

    async def _get_user_id(self, context: "OperationContext | None") -> str:
        """Get X user ID for authenticated user.

        Args:
            context: Operation context

        Returns:
            X user ID

        Raises:
            BackendError: If user ID cannot be determined
        """
        user_email = self.user_email or (context.user_id if context else None)

        if not user_email:
            raise BackendError("Cannot determine user email", backend="x")

        # Check cache
        if user_email in self._user_id_cache:
            return self._user_id_cache[user_email]

        # Fetch from API (use async version to avoid nested asyncio.run)
        client = await self._get_api_client_async(context)
        try:
            user_data = await client.get_me()
            user_id: str = user_data["data"]["id"]

            # Cache it
            self._user_id_cache[user_email] = user_id

            return user_id
        finally:
            await client.close()

    def _generate_cache_key(
        self,
        endpoint: str,
        params: dict[str, Any],
        user_id: str,
    ) -> str:
        """
        Generate deterministic cache key for API request.

        Args:
            endpoint: API endpoint type (e.g., "timeline", "mentions")
            params: Request parameters
            user_id: User identifier

        Returns:
            Cache key string

        Example:
            x:user123:timeline:a1b2c3d4
        """
        # Sort params for deterministic hash
        param_str = json.dumps(params, sort_keys=True)
        param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]

        return f"x:{user_id}:{endpoint}:{param_hash}"

    def _get_cached(
        self,
        cache_key: str,
        user_id: str,
        max_age: float,
    ) -> bytes | None:
        """
        Get cached content if available and not expired.

        Args:
            cache_key: Cache key
            user_id: User identifier
            max_age: Maximum age in seconds

        Returns:
            Cached content bytes or None if cache miss/expired
        """
        # Check memory cache
        mem_key = (cache_key, user_id)
        if mem_key in self._memory_cache:
            content, timestamp = self._memory_cache[mem_key]
            if time.time() - timestamp < max_age:
                logger.debug(f"[X-CACHE] Memory hit: {cache_key}")
                return content

        # Check disk cache
        cache_file = Path(self.cache_dir) / f"{cache_key}.json"
        if cache_file.exists():
            stat = cache_file.stat()
            if time.time() - stat.st_mtime < max_age:
                content = cache_file.read_bytes()
                logger.debug(f"[X-CACHE] Disk hit: {cache_key}")

                # Promote to memory cache
                self._memory_cache[mem_key] = (content, stat.st_mtime)

                return content

        logger.debug(f"[X-CACHE] Miss: {cache_key}")
        return None

    def _set_cached(
        self,
        cache_key: str,
        user_id: str,
        content: bytes,
    ) -> None:
        """
        Store content in cache.

        Args:
            cache_key: Cache key
            user_id: User identifier
            content: Content to cache
        """
        timestamp = time.time()

        # Store in memory cache
        mem_key = (cache_key, user_id)
        self._memory_cache[mem_key] = (content, timestamp)

        # Store in disk cache
        cache_file = Path(self.cache_dir) / f"{cache_key}.json"
        cache_file.write_bytes(content)

        logger.debug(f"[X-CACHE] Cached: {cache_key} ({len(content)} bytes)")

    def _invalidate_caches(
        self,
        user_id: str,
        endpoint_types: list[str],
    ) -> None:
        """
        Invalidate caches for specific endpoint types.

        Args:
            user_id: User identifier
            endpoint_types: List of endpoint types to invalidate
        """
        # Invalidate memory cache
        keys_to_delete = []
        for cache_key, cached_user_id in self._memory_cache:
            if cached_user_id == user_id:
                for endpoint_type in endpoint_types:
                    if f":{endpoint_type}:" in cache_key:
                        keys_to_delete.append((cache_key, cached_user_id))
                        break

        for key in keys_to_delete:
            del self._memory_cache[key]
            logger.debug(f"[X-CACHE] Invalidated: {key[0]}")

        # Invalidate disk cache
        cache_dir = Path(self.cache_dir)
        for cache_file in cache_dir.glob(f"x:{user_id}:*.json"):
            for endpoint_type in endpoint_types:
                if f":{endpoint_type}:" in cache_file.name:
                    cache_file.unlink()
                    logger.debug(f"[X-CACHE] Deleted: {cache_file.name}")
                    break

    def _resolve_path(
        self,
        backend_path: str,
    ) -> tuple[str, dict[str, Any]]:
        """
        Resolve virtual path to X API endpoint.

        Args:
            backend_path: Virtual backend path (e.g., "timeline/recent.json" or "/x/timeline/recent.json")

        Returns:
            Tuple of (endpoint_type, params)

        Raises:
            BackendError: If path is invalid
        """
        parts = backend_path.strip("/").split("/")

        # Handle paths both with and without /x/ prefix
        # When mounted at /mnt/x, backend_path will be "timeline/recent.json"
        # When used directly, backend_path might be "/x/timeline/recent.json"
        if parts and parts[0] == "x":
            # Remove the "x" prefix to normalize
            parts = parts[1:]

        if not parts:
            raise BackendError(f"Invalid X path: {backend_path}", backend="x")

        # Now parts[0] is the namespace (timeline, mentions, posts, etc.)
        namespace = parts[0]

        # Timeline paths: timeline/ or timeline/recent.json
        if namespace == "timeline":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "recent.json"):
                return ("timeline", {"max_results": 100})
            elif len(parts) == 2 and parts[1].endswith(".json"):
                # Daily archive format: 2025-01-22.json
                date_str = parts[1].replace(".json", "")
                return (
                    "timeline",
                    {
                        "start_time": f"{date_str}T00:00:00Z",
                        "end_time": f"{date_str}T23:59:59Z",
                        "max_results": 100,
                    },
                )

        # Mentions paths: mentions/ or mentions/recent.json
        elif namespace == "mentions":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "recent.json"):
                return ("mentions", {"max_results": 100})

        # Posts paths: posts/ or posts/all.json or posts/<id>.json
        elif namespace == "posts":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "all.json"):
                return ("user_tweets", {"max_results": 100})
            elif (
                len(parts) == 2
                and parts[1].endswith(".json")
                and parts[1] not in ("all.json", "new.json")
            ):
                # Individual tweet: posts/1234567890.json
                tweet_id = parts[1].replace(".json", "")
                return ("single_tweet", {"id": tweet_id})
            elif len(parts) == 2 and parts[1] == "new.json":
                # This is for writing - handled separately
                return ("new_tweet", {})

        # Bookmarks paths: bookmarks/ or bookmarks/all.json
        elif namespace == "bookmarks":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "all.json"):
                return ("bookmarks", {"max_results": 100})

        # Search paths: search/<query>.json
        elif namespace == "search":
            if len(parts) == 2:
                query = parts[1].replace(".json", "").replace("_", " ")
                return ("search", {"query": query, "max_results": 100})

        # User paths: users/<username>/ or users/<username>/profile.json
        elif namespace == "users":
            if len(parts) < 2:
                raise BackendError("User path requires username", backend="x")

            username = parts[1]

            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "profile.json"):
                return ("user_profile", {"username": username})
            elif len(parts) == 3 and parts[2] == "tweets.json":
                return ("user_tweets_by_username", {"username": username})

        raise BackendError(f"Unknown virtual path: {backend_path}", backend="x")

    def _is_writable(self, path: str) -> bool:
        """Check if virtual path is writable."""
        # Normalize path - remove leading /x/ if present
        normalized = path.strip("/")
        if normalized.startswith("x/"):
            normalized = normalized[2:]

        WRITABLE_PATHS = [
            "posts/new.json",
            "posts/drafts/",
        ]

        for writable in WRITABLE_PATHS:
            if normalized.startswith(writable) or normalized == writable:
                return True

        return False

    async def _fetch_from_api(
        self,
        client: Any,  # XAPIClient
        endpoint_type: str,
        params: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """
        Fetch data from X API.

        Args:
            client: X API client
            endpoint_type: Endpoint type (e.g., "timeline", "mentions")
            params: Request parameters
            user_id: X user ID

        Returns:
            API response data
        """
        # All client methods return dict[str, Any] from the X API
        result: dict[str, Any]
        if endpoint_type == "timeline":
            result = await client.get_user_timeline(user_id, **params)
        elif endpoint_type == "mentions":
            result = await client.get_mentions(user_id, **params)
        elif endpoint_type == "user_tweets":
            result = await client.get_user_tweets(user_id, **params)
        elif endpoint_type == "single_tweet":
            result = await client.get_tweet(params["id"])
        elif endpoint_type == "bookmarks":
            result = await client.get_bookmarks(user_id, **params)
        elif endpoint_type == "search":
            result = await client.search_recent_tweets(**params)
        elif endpoint_type == "user_profile":
            result = await client.get_user_by_username(params["username"])
        elif endpoint_type == "user_tweets_by_username":
            # First get user ID from username
            user_data = await client.get_user_by_username(params["username"])
            target_user_id: str = user_data["data"]["id"]
            result = await client.get_user_tweets(target_user_id, max_results=100)
        else:
            raise BackendError(f"Unknown endpoint type: {endpoint_type}", backend="x")
        return result

    def _transform_response(
        self,
        endpoint_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Transform X API response to simplified format.

        Args:
            endpoint_type: Endpoint type
            data: Raw API response

        Returns:
            Transformed response
        """
        # For now, return as-is with metadata
        # In the future, can flatten/simplify structure
        result = dict(data)

        # Add metadata
        result["_meta"] = {
            "endpoint": endpoint_type,
            "cached_at": datetime.now().isoformat(),
            "cache_ttl": self.cache_ttl.get(endpoint_type, 300),
        }

        return result

    # === Backend Interface Implementation ===

    async def _read_content_async(
        self,
        context: "OperationContext",
        endpoint_type: str,
        params: dict[str, Any],
    ) -> bytes:
        """Async implementation of read_content."""
        user_email_for_cache: str = self.user_email or context.user_id or "anonymous"

        # Fetch from API
        client = await self._get_api_client_async(context)
        try:
            # Get user ID
            user_id = await self._get_user_id(context)

            # Fetch data
            data = await self._fetch_from_api(client, endpoint_type, params, user_id)

            # Transform response
            transformed = self._transform_response(endpoint_type, data)

            # Serialize to JSON
            content = json.dumps(transformed, indent=2).encode("utf-8")

            # Cache response
            cache_key = self._generate_cache_key(endpoint_type, params, user_email_for_cache)
            self._set_cached(cache_key, user_email_for_cache, content)

            return content

        finally:
            await client.close()

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """
        Read content from X API via virtual path.

        For X connector, content_hash is ignored - we use backend_path from context.

        Args:
            content_hash: Ignored for X connector
            context: Operation context with backend_path

        Returns:
            File content as bytes (JSON)

        Raises:
            BackendError: If operation fails
        """
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        path = context.backend_path
        user_email_for_cache: str = self.user_email or context.user_id or "anonymous"

        # Resolve virtual path to API endpoint
        endpoint_type, params = self._resolve_path(path)

        # Generate cache key
        cache_key = self._generate_cache_key(endpoint_type, params, user_email_for_cache)

        # Check cache
        ttl = self.cache_ttl.get(endpoint_type, 300)
        cached = self._get_cached(cache_key, user_email_for_cache, ttl)
        if cached:
            return cached

        # Fetch from API using single asyncio.run
        return asyncio.run(self._read_content_async(context, endpoint_type, params))

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> str:
        """
        Write content (post tweet or save draft).

        Args:
            content: File content as bytes (JSON)
            context: Operation context with backend_path

        Returns:
            Content hash (tweet ID for posted tweets)

        Raises:
            BackendError: If operation fails
            PermissionError: If path is read-only
        """
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        path = context.backend_path

        # Check if path is writable
        if not self._is_writable(path):
            raise PermissionError(
                f"Path '{path}' is read-only. "
                f"Writable paths: /x/posts/new.json, /x/posts/drafts/*.json"
            )

        # Parse content
        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError:
            # Treat as plain text tweet
            data = {"text": content.decode("utf-8")}

        # Handle drafts (store locally)
        if path.startswith("/x/posts/drafts/"):
            draft_id = hashlib.sha256(content).hexdigest()[:16]
            draft_file = Path(self.cache_dir) / "drafts" / f"{draft_id}.json"
            draft_file.parent.mkdir(exist_ok=True)
            draft_file.write_bytes(content)
            return draft_id

        # Post tweet using single asyncio.run
        async def _post_tweet() -> str:
            client = await self._get_api_client_async(context)
            try:
                response = await client.create_tweet(
                    text=data.get("text", ""),
                    reply_to=data.get("reply_to"),
                    quote_tweet_id=data.get("quote_tweet_id"),
                    media_ids=data.get("media_ids"),
                    poll_options=data.get("poll_options"),
                    poll_duration_minutes=data.get("poll_duration_minutes"),
                )

                # Invalidate caches
                post_user_email = self.user_email or (context.user_id if context else None)
                if post_user_email:
                    self._invalidate_caches(post_user_email, ["timeline", "user_tweets"])

                # Return tweet ID
                tweet_id: str = response["data"]["id"]
                return tweet_id
            finally:
                await client.close()

        return asyncio.run(_post_tweet())

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Delete content (delete tweet or draft).

        Args:
            content_hash: Ignored for X connector
            context: Operation context with backend_path

        Raises:
            BackendError: If operation fails
            PermissionError: If path cannot be deleted
        """
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        path = context.backend_path

        # Normalize path - handle both /x/posts/... and posts/...
        normalized_path = path.strip("/")
        if normalized_path.startswith("x/"):
            normalized_path = normalized_path[2:]

        # Delete own tweet: posts/1234567890.json
        if normalized_path.startswith("posts/") and normalized_path.endswith(".json"):
            tweet_id = normalized_path.replace("posts/", "").replace(".json", "")

            # Skip special files
            if tweet_id in ("new", "all"):
                raise BackendError(f"Cannot delete special file: {path}", backend="x")

            # Check if it's a draft
            if "drafts/" in normalized_path:
                draft_file = Path(self.cache_dir) / "drafts" / f"{tweet_id}.json"
                if draft_file.exists():
                    draft_file.unlink()
                return

            # Delete tweet via API using single asyncio.run
            async def _delete_tweet() -> None:
                client = await self._get_api_client_async(context)
                try:
                    await client.delete_tweet(tweet_id)

                    # Invalidate caches
                    del_user_email = self.user_email or (context.user_id if context else None)
                    if del_user_email:
                        self._invalidate_caches(del_user_email, ["timeline", "user_tweets"])
                finally:
                    await client.close()

            try:
                asyncio.run(_delete_tweet())
            except BackendError as e:
                if "403" in str(e) or "Forbidden" in str(e):
                    raise PermissionError(
                        f"Cannot delete tweet {tweet_id}: Not owned by user"
                    ) from e
                raise
            return

        # Cannot delete other paths
        raise PermissionError(f"Path '{path}' cannot be deleted")

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """
        Check if content exists.

        For X connector, checks if virtual path exists.

        Args:
            content_hash: Ignored for X connector
            context: Operation context with backend_path

        Returns:
            True if path exists
        """
        if not context or not hasattr(context, "backend_path"):
            return False

        try:
            # Try to resolve path
            self._resolve_path(context.backend_path or "")
            return True
        except BackendError:
            return False

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """
        Get content size.

        For X connector, returns estimated JSON size.

        Args:
            content_hash: Ignored for X connector
            context: Operation context

        Returns:
            Content size in bytes (estimated)
        """
        # Return approximate size (tweets are usually small)
        return 1024  # 1 KB estimate

    def get_ref_count(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """
        Get reference count (always 1 for X connector).

        Args:
            content_hash: Ignored for X connector
            context: Operation context

        Returns:
            Always 1 (no reference counting)
        """
        return 1

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Create directory (not supported for X connector).

        Raises:
            NotImplementedError: X connector has fixed virtual structure
        """
        raise NotImplementedError(
            "X connector has a fixed virtual structure. "
            "mkdir() is not supported. "
            "Available paths: /x/timeline/, /x/posts/, /x/mentions/, etc."
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """
        Remove directory (not supported for X connector).

        Raises:
            NotImplementedError: X connector has fixed virtual structure
        """
        raise NotImplementedError(
            "X connector has a fixed virtual structure. rmdir() is not supported."
        )

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Path to check
            context: Operation context (unused)

        Returns:
            True if path is a virtual directory
        """
        path = path.strip("/")

        VIRTUAL_DIRS = {
            "x",
            "x/timeline",
            "x/mentions",
            "x/posts",
            "x/posts/drafts",
            "x/bookmarks",
            "x/lists",
            "x/search",
            "x/users",
        }

        return path in VIRTUAL_DIRS

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """
        List virtual directory contents.

        Args:
            path: Directory path to list
            context: Operation context

        Returns:
            List of entry names (directories have trailing '/')

        Raises:
            FileNotFoundError: If directory doesn't exist
        """
        path = path.strip("/")

        # Normalize - handle both /x/... and just namespace paths
        if path.startswith("x/"):
            path = path[2:]

        # Root
        if not path:
            return [
                "timeline/",
                "mentions/",
                "posts/",
                "bookmarks/",
                "lists/",
                "search/",
                "users/",
            ]

        # Timeline
        if path == "timeline":
            entries = ["recent.json", "media/"]

            # Add cached daily archives
            cache_path = Path(self.cache_dir) / "timeline"
            if cache_path.exists():
                for file in cache_path.glob("*.json"):
                    if file.name != "recent.json":
                        entries.append(file.name)

            return sorted(entries)

        # Posts
        if path == "posts":
            entries = ["all.json", "new.json", "drafts/"]

            # Add cached tweets (from user_tweets endpoint)
            # Note: This would require an API call to list all tweets
            # For now, return static entries

            return sorted(entries)

        # Mentions
        if path == "mentions":
            return ["recent.json"]

        # Bookmarks
        if path == "bookmarks":
            return ["all.json"]

        # Search
        if path == "search":
            # Return cached search results
            cache_path = Path(self.cache_dir)
            entries = []
            for file in cache_path.glob("x:*:search:*.json"):
                # Extract query from filename (simplified)
                entries.append(file.name.replace("x:", "").split(":")[2] + ".json")

            return sorted(set(entries))  # Remove duplicates

        # Users
        if path == "users":
            # Cannot list all users, return empty
            return []

        raise FileNotFoundError(f"Directory not found: {path}")

    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """
        Match paths using glob patterns.

        For X connector, glob works on:
        1. Virtual directory structure (fixed paths)
        2. Dynamic tweet IDs (via API calls)
        3. Cached files (local storage)

        Args:
            pattern: Glob pattern
            path: Base path (default: "/")
            context: Operation context

        Returns:
            List of matching paths

        Examples:
            >>> nx.glob("/x/*")  # List top-level directories
            >>> nx.glob("/x/posts/*.json")  # List user's tweets
            >>> nx.glob("/x/timeline/*.json")  # List cached timeline files
        """
        # Handle root-level globs
        if pattern == "/x/*" or pattern == "/x/*/":
            return [
                "/x/timeline/",
                "/x/mentions/",
                "/x/posts/",
                "/x/bookmarks/",
                "/x/lists/",
                "/x/search/",
                "/x/users/",
            ]

        # Handle timeline glob
        if pattern.startswith("/x/timeline/"):
            available = ["/x/timeline/recent.json", "/x/timeline/media/"]

            # Add cached daily archives
            cache_path = Path(self.cache_dir) / "timeline"
            if cache_path.exists():
                for file in cache_path.glob("*.json"):
                    if file.name != "recent.json":
                        available.append(f"/x/timeline/{file.name}")

            # Filter by pattern
            return [p for p in available if fnmatch.fnmatch(p, pattern)]

        # Handle posts glob (requires API call)
        if pattern.startswith("/x/posts/") and "*.json" in pattern:
            # Would need API call to list all tweets
            # For now, return static entries
            return [
                "/x/posts/all.json",
                "/x/posts/new.json",
            ]

        # Handle search glob
        if pattern.startswith("/x/search/"):
            cache_path = Path(self.cache_dir)
            matches = []
            for file in cache_path.glob("x:*:search:*.json"):
                # Extract query from filename
                virtual_path = f"/x/search/{file.stem.split(':')[-1]}.json"
                if fnmatch.fnmatch(virtual_path, pattern):
                    matches.append(virtual_path)
            return sorted(set(matches))

        # Default: return empty
        return []

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
        Search content using pattern.

        For X connector, grep is implemented as:
        1. Cached files: Search locally in cached JSON
        2. Timeline/posts: Use X API search within context
        3. Global search: Use X API search

        Args:
            pattern: Regex pattern to search for
            path: Base path to search (default: "/")
            file_pattern: Optional glob pattern to filter files
            ignore_case: Case-insensitive search
            max_results: Maximum number of results
            context: Operation context

        Returns:
            List of match dicts (file, line, content, match)

        Examples:
            >>> nx.grep("python", path="/x/timeline/")
            >>> nx.grep("error", path="/x/posts/")
            >>> nx.grep("nexus ai", path="/x/search/")
        """
        path = path.strip("/")

        # Compile regex pattern
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        # Global search via X API
        if path == "x" or path.startswith("x/search"):
            return self._grep_global(pattern, max_results, context)

        # Search cached files
        if path.startswith("x/timeline") or path.startswith("x/bookmarks"):
            return self._grep_cached(regex, path, max_results)

        # Search user's tweets via API
        if path.startswith("x/posts"):
            return self._grep_user_tweets(regex, max_results, context)

        # Fallback: search cached files
        return self._grep_cached(regex, path, max_results)

    def _grep_cached(
        self,
        regex: re.Pattern[str],
        path: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        """Search cached JSON files."""
        results: list[dict[str, Any]] = []
        cache_path = Path(self.cache_dir)

        # Find relevant cache files
        pattern_map = {
            "x/timeline": "x:*:timeline:*.json",
            "x/bookmarks": "x:*:bookmarks:*.json",
            "x/mentions": "x:*:mentions:*.json",
        }

        glob_pattern = pattern_map.get(path, "*.json")

        for file in cache_path.glob(glob_pattern):
            if len(results) >= max_results:
                break

            try:
                content = file.read_text()
                data = json.loads(content)

                # Search in JSON content
                json_str = json.dumps(data, indent=2)
                for line_num, line in enumerate(json_str.splitlines(), start=1):
                    match = regex.search(line)
                    if match:
                        results.append(
                            {
                                "file": f"/{path}/{file.name}",
                                "line": line_num,
                                "content": line.strip(),
                                "match": match.group(0),
                                "source": "cache",
                            }
                        )

                        if len(results) >= max_results:
                            break

            except Exception:
                continue

        return results

    def _grep_user_tweets(
        self,
        regex: re.Pattern[str],
        max_results: int,
        context: Any,
    ) -> list[dict[str, Any]]:
        """Search user's tweets via API."""

        async def _search_user_tweets() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            client = await self._get_api_client_async(context)
            try:
                user_id = await self._get_user_id(context)

                # Fetch user's tweets
                response = await client.get_user_tweets(user_id, max_results=100)

                # Search through tweets
                for tweet in response.get("data", []):
                    if len(results) >= max_results:
                        break

                    text = tweet.get("text", "")
                    lines = text.split("\n")

                    for line_num, line in enumerate(lines, start=1):
                        match = regex.search(line)
                        if match:
                            results.append(
                                {
                                    "file": f"/x/posts/{tweet['id']}.json",
                                    "line": line_num,
                                    "content": line.strip(),
                                    "match": match.group(0),
                                    "source": "x_api",
                                }
                            )

                            if len(results) >= max_results:
                                break
            finally:
                await client.close()

            return results

        try:
            return asyncio.run(_search_user_tweets())
        except Exception as e:
            logger.warning(f"grep_user_tweets failed: {e}")
            return []

    def _grep_global(
        self,
        pattern: str,
        max_results: int,
        context: Any,
    ) -> list[dict[str, Any]]:
        """Global search using X API."""

        async def _search_global() -> list[dict[str, Any]]:
            results: list[dict[str, Any]] = []
            client = await self._get_api_client_async(context)
            try:
                # Use X search API
                response = await client.search_recent_tweets(pattern, max_results=max_results)

                # Transform to grep result format
                for tweet in response.get("data", []):
                    if len(results) >= max_results:
                        break

                    text = tweet.get("text", "")
                    lines = text.split("\n")

                    for line_num, line in enumerate(lines, start=1):
                        if pattern.lower() in line.lower():
                            results.append(
                                {
                                    "file": f"/x/posts/{tweet['id']}.json",
                                    "line": line_num,
                                    "content": line.strip(),
                                    "match": pattern,
                                    "source": "x_api",
                                }
                            )

                            if len(results) >= max_results:
                                break
            finally:
                await client.close()

            return results

        try:
            return asyncio.run(_search_global())
        except Exception as e:
            logger.warning(f"grep_global failed: {e}")
            return []
