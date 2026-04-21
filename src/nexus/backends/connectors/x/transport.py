"""X (Twitter) Transport -- raw key->bytes I/O over the X API v2.

Implements the Transport protocol for X (Twitter), mapping:
- fetch(key) -> tweets/timeline/mentions -> JSON bytes
- store(key, data) -> tweets.create -> tweet ID
- remove(key) -> tweets.delete
- list_keys(prefix) -> virtual directory listing

Auth: XTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``
so the transport can resolve the per-user OAuth token.

Key schema:
    "timeline/recent.json"     -> home timeline
    "mentions/recent.json"     -> mentions
    "posts/all.json"           -> user's tweets
    "posts/1234567890.json"    -> single tweet by ID
    "posts/new.json"           -> create new tweet (write target)
    "bookmarks/all.json"       -> saved tweets
    "search/query.json"        -> search results
    "users/username/profile.json" -> user profile
    list_keys("")              -> common_prefixes = ["timeline/", ...]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from copy import copy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cachetools import LRUCache

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import BackendError

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Cache TTL configuration (in seconds)
CACHE_TTL: dict[str, int] = {
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

# Virtual directory structure
VIRTUAL_DIRS: frozenset[str] = frozenset(
    {
        "",
        "timeline",
        "mentions",
        "posts",
        "posts/drafts",
        "bookmarks",
        "lists",
        "search",
        "users",
    }
)


class XTransport:
    """X (Twitter) API v2 transport implementing the Transport protocol.

    Attributes:
        transport_name: ``"x"`` -- used by PathAddressingEngine to build
            the backend name (``"path-x"``).
    """

    transport_name: str = "x"

    def __init__(
        self,
        token_manager: Any,
        provider: str = "twitter",
        user_email: str | None = None,
        cache_ttl: dict[str, int] | None = None,
        cache_dir: str | None = None,
        *,
        memory_cache_maxsize: int = 1024,
        user_id_cache_maxsize: int = 256,
    ) -> None:
        self._token_manager = token_manager
        self._provider = provider
        self._user_email = user_email
        self._cache_ttl = cache_ttl or CACHE_TTL
        self._cache_dir = cache_dir or "/tmp/nexus-x-cache"
        self._context: OperationContext | None = None

        # Create cache directory
        os.makedirs(self._cache_dir, exist_ok=True)

        # In-memory cache: (cache_key, user_id) -> (content, timestamp)
        self._memory_cache: LRUCache[tuple[str, str], tuple[bytes, float]] = LRUCache(
            maxsize=memory_cache_maxsize
        )

        # User ID cache: user_email -> x_user_id
        self._user_id_cache: LRUCache[str, str] = LRUCache(maxsize=user_id_cache_maxsize)

    # ------------------------------------------------------------------
    # Context binding (per-request OAuth token resolution)
    # ------------------------------------------------------------------

    def with_context(self, context: OperationContext | None) -> XTransport:
        """Return a shallow copy bound to *context*."""
        clone = copy(self)
        clone._context = context
        return clone

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_principal_async(self) -> tuple[str, str]:
        """Resolve ``(oauth_email, zone_id)`` for the current context.

        Mirrors the connector-agnostic resolver in ``oauth_base`` but kept
        inline because the X token manager is async.  Returns the OAuth
        email + zone that downstream calls (token fetch, cache keying,
        per-user id lookup) MUST agree on, so cached payloads cannot bleed
        across zones or across accounts that share a nexus subject ID
        (e.g. ``"admin"``).

        Raises :class:`AuthenticationError` when no unambiguous identity
        can be resolved.
        """
        from nexus.backends.connectors.oauth_base import (
            _looks_like_email,
            build_auth_recovery_hint,
        )
        from nexus.contracts.exceptions import AuthenticationError

        mount_email = self._user_email if _looks_like_email(self._user_email) else None
        nexus_user_id = (
            self._context.user_id
            if self._context
            and self._context.user_id
            and not _looks_like_email(self._context.user_id)
            else None
        ) or (self._user_email if not _looks_like_email(self._user_email) else None)
        ctx_email = (
            self._context.user_id
            if self._context and _looks_like_email(self._context.user_id)
            else None
        )
        zone_id: str = (
            self._context.zone_id
            if self._context and hasattr(self._context, "zone_id")
            else ROOT_ZONE_ID
        ) or ROOT_ZONE_ID

        resolved_email: str | None = mount_email or ctx_email
        if resolved_email is None and nexus_user_id is not None:
            list_fn = getattr(self._token_manager, "list_credentials", None)
            if list_fn is not None:
                # NB: intentionally do NOT catch exceptions — credential-
                # index failures (DB/session/encryption) must propagate
                # as backend errors instead of being downgraded to false
                # "auth required".  Matches oauth_base._resolve_linked_oauth_email.
                creds = await list_fn(zone_id=zone_id, user_id=nexus_user_id)
                matches: list[str] = []
                for cred in creds or []:
                    if cred.get("provider") != self._provider:
                        continue
                    email = cred.get("user_email")
                    if _looks_like_email(email):
                        matches.append(str(email))
                unique = sorted(set(matches))
                if len(unique) > 1:
                    raise AuthenticationError(
                        f"Multiple OAuth accounts linked to nexus user "
                        f"{nexus_user_id!r} for provider {self._provider!r}: "
                        f"{unique}. Pin the mount to an explicit user_email "
                        "to disambiguate.",
                        provider=self._provider,
                        user_email=None,
                        recovery_hint={
                            "action": "select_account",
                            "provider": self._provider,
                            "candidates": unique,
                        },
                    )
                if unique:
                    resolved_email = unique[0]

        if not resolved_email:
            raise AuthenticationError(
                f"x_connector requires authorization (provider={self._provider}). "
                "Configure user_email on the mount or complete the OAuth flow "
                "for the authenticated nexus user.",
                provider=self._provider,
                user_email=None,
                recovery_hint=build_auth_recovery_hint(
                    connector_name="x_connector",
                    provider=self._provider,
                ),
            )
        return resolved_email, zone_id

    def _resolve_principal(self) -> tuple[str, str]:
        """Sync wrapper around :meth:`_resolve_principal_async`."""
        from nexus.lib.sync_bridge import run_sync

        return run_sync(self._resolve_principal_async())

    @staticmethod
    def _cache_principal(resolved_email: str, zone_id: str) -> str:
        """Return the cache principal string — zone + resolved OAuth email.

        Pipe is safe: zone_id is a restricted identifier and an email cannot
        contain one, so the tuple is unambiguous once rejoined.
        """
        return f"{zone_id}|{resolved_email}"

    async def _get_api_client_async(self) -> Any:
        """Get authenticated X API client."""
        from nexus.backends.connectors.oauth_base import build_auth_recovery_hint
        from nexus.backends.connectors.x.api_client import XAPIClient
        from nexus.contracts.exceptions import AuthenticationError

        resolved_email, zone_id = await self._resolve_principal_async()
        try:
            access_token = await self._token_manager.get_valid_token(
                provider=self._provider,
                user_email=resolved_email,
                zone_id=zone_id,
            )
        except AuthenticationError as _auth_exc:
            raise AuthenticationError(
                str(_auth_exc),
                provider=self._provider,
                user_email=resolved_email,
                recovery_hint=build_auth_recovery_hint(
                    connector_name="x_connector",
                    provider=self._provider,
                    user_email=resolved_email,
                ),
            ) from _auth_exc
        except Exception as e:
            raise BackendError(
                f"Failed to get OAuth token for {resolved_email}: {e}",
                backend="x",
            ) from e

        return XAPIClient(access_token=access_token)

    def _get_api_client(self) -> Any:
        """Get authenticated X API client (sync wrapper)."""
        from nexus.lib.sync_bridge import run_sync

        return run_sync(self._get_api_client_async())

    async def _get_user_id(self) -> str:
        """Get X user ID for authenticated user.

        Key the user-id cache on the resolved ``(zone_id, oauth_email)``
        principal (not the raw nexus subject ID) so two zones sharing a
        subject cannot pin each other's X user id.
        """
        resolved_email, zone_id = await self._resolve_principal_async()
        cache_key = self._cache_principal(resolved_email, zone_id)

        if cache_key in self._user_id_cache:
            return self._user_id_cache[cache_key]

        client = await self._get_api_client_async()
        try:
            user_data = await client.get_me()
            user_id: str = user_data["data"]["id"]
            self._user_id_cache[cache_key] = user_id
            return user_id
        finally:
            await client.close()

    def _generate_cache_key(
        self,
        endpoint: str,
        params: dict[str, Any],
        user_id: str,
    ) -> str:
        """Generate deterministic cache key for API request."""
        param_str = json.dumps(params, sort_keys=True)
        param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
        return f"x:{user_id}:{endpoint}:{param_hash}"

    def _get_cached(
        self,
        cache_key: str,
        user_id: str,
        max_age: float,
    ) -> bytes | None:
        """Get cached content if available and not expired."""
        # Check memory cache
        mem_key = (cache_key, user_id)
        if mem_key in self._memory_cache:
            content, timestamp = self._memory_cache[mem_key]
            if time.time() - timestamp < max_age:
                logger.debug("[X-CACHE] Memory hit: %s", cache_key)
                return content

        # Check disk cache
        cache_file = Path(self._cache_dir) / f"{cache_key}.json"
        if cache_file.exists():
            stat = cache_file.stat()
            if time.time() - stat.st_mtime < max_age:
                content = cache_file.read_bytes()
                logger.debug("[X-CACHE] Disk hit: %s", cache_key)
                self._memory_cache[mem_key] = (content, stat.st_mtime)
                return content

        logger.debug("[X-CACHE] Miss: %s", cache_key)
        return None

    def _set_cached(
        self,
        cache_key: str,
        user_id: str,
        content: bytes,
    ) -> None:
        """Store content in cache."""
        timestamp = time.time()
        mem_key = (cache_key, user_id)
        self._memory_cache[mem_key] = (content, timestamp)

        cache_file = Path(self._cache_dir) / f"{cache_key}.json"
        cache_file.write_bytes(content)
        logger.debug("[X-CACHE] Cached: %s (%d bytes)", cache_key, len(content))

    def _invalidate_caches(
        self,
        user_id: str,
        endpoint_types: list[str],
    ) -> None:
        """Invalidate caches for specific endpoint types."""
        keys_to_delete = []
        for cache_key, cached_user_id in self._memory_cache:
            if cached_user_id == user_id:
                for endpoint_type in endpoint_types:
                    if f":{endpoint_type}:" in cache_key:
                        keys_to_delete.append((cache_key, cached_user_id))
                        break

        for key in keys_to_delete:
            del self._memory_cache[key]
            logger.debug("[X-CACHE] Invalidated: %s", key[0])

        cache_dir = Path(self._cache_dir)
        for cache_file in cache_dir.glob(f"x:{user_id}:*.json"):
            for endpoint_type in endpoint_types:
                if f":{endpoint_type}:" in cache_file.name:
                    cache_file.unlink()
                    logger.debug("[X-CACHE] Deleted: %s", cache_file.name)
                    break

    def _resolve_path(self, backend_path: str) -> tuple[str, dict[str, Any]]:
        """Resolve virtual path to X API endpoint.

        Returns:
            Tuple of (endpoint_type, params)
        """
        parts = backend_path.strip("/").split("/")

        # Handle paths with "x" prefix
        if parts and parts[0] == "x":
            parts = parts[1:]

        if not parts:
            raise BackendError(f"Invalid X path: {backend_path}", backend="x")

        namespace = parts[0]

        if namespace == "timeline":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "recent.json"):
                return ("timeline", {"max_results": 100})
            elif len(parts) == 2 and parts[1].endswith(".json"):
                date_str = parts[1].replace(".json", "")
                return (
                    "timeline",
                    {
                        "start_time": f"{date_str}T00:00:00Z",
                        "end_time": f"{date_str}T23:59:59Z",
                        "max_results": 100,
                    },
                )

        elif namespace == "mentions":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "recent.json"):
                return ("mentions", {"max_results": 100})

        elif namespace == "posts":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "all.json"):
                return ("user_tweets", {"max_results": 100})
            elif (
                len(parts) == 2
                and parts[1].endswith(".json")
                and parts[1] not in ("all.json", "new.json")
            ):
                tweet_id = parts[1].replace(".json", "")
                return ("single_tweet", {"id": tweet_id})
            elif len(parts) == 2 and parts[1] == "new.json":
                return ("new_tweet", {})

        elif namespace == "bookmarks":
            if len(parts) == 1 or (len(parts) == 2 and parts[1] == "all.json"):
                return ("bookmarks", {"max_results": 100})

        elif namespace == "search":
            if len(parts) == 2:
                query = parts[1].replace(".json", "").replace("_", " ")
                return ("search", {"query": query, "max_results": 100})

        elif namespace == "users":
            if len(parts) < 2:
                raise BackendError("User path requires username", backend="x")
            username = parts[1]
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "profile.json"):
                return ("user_profile", {"username": username})
            elif len(parts) == 3 and parts[2] == "tweets.json":
                return ("user_tweets_by_username", {"username": username})

        raise BackendError(f"Unknown virtual path: {backend_path}", backend="x")

    async def _fetch_from_api(
        self,
        client: Any,
        endpoint_type: str,
        params: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        """Fetch data from X API."""
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
        """Transform X API response to simplified format with metadata."""
        result = dict(data)
        result["_meta"] = {
            "endpoint": endpoint_type,
            "cached_at": datetime.now().isoformat(),
            "cache_ttl": self._cache_ttl.get(endpoint_type, 300),
        }
        return result

    @staticmethod
    def _is_writable(path: str) -> bool:
        """Check if virtual path is writable."""
        normalized = path.strip("/")
        if normalized.startswith("x/"):
            normalized = normalized[2:]

        writable_paths = [
            "posts/new.json",
            "posts/drafts/",
        ]
        for writable in writable_paths:
            if normalized.startswith(writable) or normalized == writable:
                return True
        return False

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Post a tweet or save a draft."""
        if not self._is_writable(key):
            raise BackendError(
                f"Path '{key}' is read-only. Writable paths: posts/new.json, posts/drafts/*.json",
                backend="x",
            )

        # Parse content
        try:
            tweet_data = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            tweet_data = {"text": data.decode("utf-8")}

        # Handle drafts (store locally)
        normalized = key.strip("/")
        if normalized.startswith("x/"):
            normalized = normalized[2:]
        if normalized.startswith("posts/drafts/"):
            draft_id = hashlib.sha256(data).hexdigest()[:16]
            draft_file = Path(self._cache_dir) / "drafts" / f"{draft_id}.json"
            draft_file.parent.mkdir(exist_ok=True)
            draft_file.write_bytes(data)
            return draft_id

        # Post tweet via API
        async def _post_tweet() -> str:
            client = await self._get_api_client_async()
            try:
                response = await client.create_tweet(
                    text=tweet_data.get("text", ""),
                    reply_to=tweet_data.get("reply_to"),
                    quote_tweet_id=tweet_data.get("quote_tweet_id"),
                    media_ids=tweet_data.get("media_ids"),
                    poll_options=tweet_data.get("poll_options"),
                    poll_duration_minutes=tweet_data.get("poll_duration_minutes"),
                )

                # Invalidate caches under the same principal we keyed
                # reads under — any other form would leave stale entries.
                resolved_email, zone_id = await self._resolve_principal_async()
                self._invalidate_caches(
                    self._cache_principal(resolved_email, zone_id),
                    ["timeline", "user_tweets"],
                )

                tweet_id: str = response["data"]["id"]
                return tweet_id
            finally:
                await client.close()

        from nexus.lib.sync_bridge import run_sync

        return run_sync(_post_tweet())

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch X content as JSON bytes by transport key."""
        endpoint_type, params = self._resolve_path(key)

        # Resolve the OAuth principal BEFORE touching the cache — keying on
        # the raw ``context.user_id`` (e.g. ``"admin"``) would let two zones
        # or two accounts sharing a nexus subject serve each other's cached
        # responses.  ``_cache_principal`` composes zone + email so each
        # tenant's cache is physically distinct.
        resolved_email, zone_id = self._resolve_principal()
        principal = self._cache_principal(resolved_email, zone_id)

        # Check cache
        cache_key = self._generate_cache_key(endpoint_type, params, principal)
        ttl = self._cache_ttl.get(endpoint_type, 300)
        cached = self._get_cached(cache_key, principal, ttl)
        if cached:
            return cached, None

        # Fetch from API
        async def _read() -> bytes:
            client = await self._get_api_client_async()
            try:
                user_id = await self._get_user_id()
                data = await self._fetch_from_api(client, endpoint_type, params, user_id)
                transformed = self._transform_response(endpoint_type, data)
                content = json.dumps(transformed, indent=2).encode("utf-8")
                self._set_cached(cache_key, principal, content)
                return content
            finally:
                await client.close()

        from nexus.lib.sync_bridge import run_sync

        content = run_sync(_read())
        return content, None

    def remove(self, key: str) -> None:
        """Delete a tweet or draft."""
        normalized = key.strip("/")
        if normalized.startswith("x/"):
            normalized = normalized[2:]

        if normalized.startswith("posts/") and normalized.endswith(".json"):
            tweet_id = normalized.replace("posts/", "").replace(".json", "")

            if tweet_id in ("new", "all"):
                raise BackendError(
                    f"Cannot delete special file: {key}",
                    backend="x",
                )

            # Handle drafts
            if "drafts/" in normalized:
                draft_file = Path(self._cache_dir) / "drafts" / f"{tweet_id}.json"
                if draft_file.exists():
                    draft_file.unlink()
                return

            # Delete tweet via API
            async def _delete_tweet() -> None:
                client = await self._get_api_client_async()
                try:
                    await client.delete_tweet(tweet_id)
                    resolved_email, zone_id = await self._resolve_principal_async()
                    self._invalidate_caches(
                        self._cache_principal(resolved_email, zone_id),
                        ["timeline", "user_tweets"],
                    )
                finally:
                    await client.close()

            try:
                from nexus.lib.sync_bridge import run_sync

                run_sync(_delete_tweet())
            except BackendError as e:
                if "403" in str(e) or "Forbidden" in str(e):
                    raise BackendError(
                        f"Cannot delete tweet {tweet_id}: Not owned by user",
                        backend="x",
                    ) from e
                raise
            return

        raise BackendError(f"Path '{key}' cannot be deleted", backend="x")

    def exists(self, key: str) -> bool:
        """Check whether a virtual path exists."""
        try:
            self._resolve_path(key)
            return True
        except BackendError:
            # Check if it's a virtual directory
            stripped = key.strip("/")
            if stripped.startswith("x/"):
                stripped = stripped[2:]
            return stripped in VIRTUAL_DIRS

    def get_size(self, key: str) -> int:
        """Return estimated size of the content."""
        return 1024

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List virtual keys under *prefix*.

        - ``list_keys("")`` -> ``([], ["timeline/", "mentions/", ...])``
        - ``list_keys("timeline/")`` -> ``(["timeline/recent.json"], [])``
        """
        prefix = prefix.strip("/")
        if prefix.startswith("x/"):
            prefix = prefix[2:]

        # Root
        if not prefix:
            return [], [
                "timeline/",
                "mentions/",
                "posts/",
                "bookmarks/",
                "lists/",
                "search/",
                "users/",
            ]

        if prefix == "timeline":
            entries = ["timeline/recent.json"]
            cache_path = Path(self._cache_dir) / "timeline"
            if cache_path.exists():
                for file in cache_path.glob("*.json"):
                    if file.name != "recent.json":
                        entries.append(f"timeline/{file.name}")
            return sorted(entries), ["timeline/media/"]

        if prefix == "posts":
            return sorted(["posts/all.json", "posts/new.json"]), ["posts/drafts/"]

        if prefix == "mentions":
            return ["mentions/recent.json"], []

        if prefix == "bookmarks":
            return ["bookmarks/all.json"], []

        if prefix == "search":
            cache_path = Path(self._cache_dir)
            entries = []
            for file in cache_path.glob("x:*:search:*.json"):
                entries.append(f"search/{file.name.replace('x:', '').split(':')[2]}.json")
            return sorted(set(entries)), []

        if prefix == "users":
            return [], []

        return [], []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        raise BackendError(
            "X transport does not support copy.",
            backend="x",
        )

    def create_dir(self, key: str) -> None:
        raise BackendError(
            "X transport does not support directory creation. Structure is virtual.",
            backend="x",
        )

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream content (small payloads -- fetch then chunk)."""
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        raise BackendError(
            "X transport does not support chunked store.",
            backend="x",
        )
