"""X (Twitter) connector backend -- PathAddressingEngine + XTransport composition.

Architecture (Transport x Addressing):
    PathXBackend(PathAddressingEngine)
        +-- XTransport(Transport)
              +-- X API v2 calls (I/O)
              +-- OAuth PKCE token from OperationContext

This follows the same pattern as PathGmailBackend, PathCalendarBackend:
Transport handles raw I/O; PathAddressingEngine handles addressing,
path security, and content operations.

Virtual filesystem structure:
    /timeline/         - Home timeline tweets
    /mentions/         - Mentions
    /posts/            - User's tweets
    /bookmarks/        - Saved tweets
    /search/           - Search results
    /users/            - User profiles
"""

from __future__ import annotations

import importlib as _il
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    ReadmeDocMixin,
    Reversibility,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.base_errors import TRAIT_ERRORS
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.backends.connectors.x.schemas import CreateTweetSchema, DeleteTweetSchema
from nexus.backends.connectors.x.transport import VIRTUAL_DIRS, XTransport
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import AuthenticationError, BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

# Brick import via importlib to avoid non-layer tier violation
glob_fast = _il.import_module("nexus.bricks.search.primitives").glob_fast

logger = logging.getLogger(__name__)


@register_connector("x_connector")
class PathXBackend(
    PathAddressingEngine,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
):
    """X (Twitter) connector: PathAddressingEngine + XTransport composition.

    Features:
    - OAuth 2.0 PKCE authentication (per-user credentials)
    - Virtual path mapping (tweets -> JSON files)
    - Multi-tier caching with TTL (in-memory + disk)
    - Rate limit handling
    - Read + write (tweet creation and deletion)
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = OAUTH_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.README_DOC,
        }
    )

    # Skill documentation settings
    SKILL_NAME = "x"

    SCHEMAS: dict[str, type] = {
        "create_tweet": CreateTweetSchema,
        "delete_tweet": DeleteTweetSchema,
    }

    OPERATION_TRAITS = {
        "create_tweet": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.USER),
        "delete_tweet": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
    }

    ERROR_REGISTRY = {
        **TRAIT_ERRORS,
        "TWEET_TOO_LONG": ErrorDef(
            message="Tweet exceeds 280 characters",
            readme_section="operations",
            fix_example="text: <max 280 chars>",
        ),
    }

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
        *,
        memory_cache_maxsize: int = 1024,
        user_id_cache_maxsize: int = 256,
        encryption_key: str | None = None,
    ):
        """Initialize X connector backend.

        Args:
            token_manager_db: Path to TokenManager database or database URL
            user_email: User email for OAuth (None = use from context)
            cache_ttl: Custom cache TTL per endpoint type
            cache_dir: Cache directory (default: /tmp/nexus-x-cache)
            provider: OAuth provider name (default: "twitter")
            memory_cache_maxsize: Max entries in the in-memory content LRU cache
            user_id_cache_maxsize: Max entries in the user ID LRU cache
        """
        # 1. Initialize OAuth
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )

        # 2. Create XTransport with the token manager
        x_transport = XTransport(
            token_manager=self.token_manager,
            provider=provider,
            user_email=user_email,
            cache_ttl=cache_ttl,
            cache_dir=cache_dir,
            memory_cache_maxsize=memory_cache_maxsize,
            user_id_cache_maxsize=user_id_cache_maxsize,
        )
        self._x_transport = x_transport

        # 3. Initialize PathAddressingEngine
        PathAddressingEngine.__init__(
            self,
            transport=x_transport,
            backend_name="x",
        )

        # Store cache_dir for glob/grep access
        self._cache_dir = cache_dir or "/tmp/nexus-x-cache"

    # -- Properties --

    @property
    def has_token_manager(self) -> bool:
        """X connector manages OAuth tokens."""
        return True

    # =================================================================
    # Content operations -- override PathAddressingEngine for X
    # =================================================================

    def _bind_transport(self, context: "OperationContext | None") -> None:
        """Bind the transport to the current request context (OAuth token)."""
        self._transport = self._x_transport.with_context(context)

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read content from X API via virtual path.

        For X connector, content_hash is ignored -- we use backend_path from context.
        """
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        self._bind_transport(context)
        return super().read_content(content_hash, context)

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content (post tweet or save draft).

        Args:
            content: File content as bytes (JSON)
            content_id: Ignored
            offset: Ignored
            context: Operation context with backend_path
        """
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        self._bind_transport(context)

        try:
            result_id = self._transport.store(context.backend_path, content)
            return WriteResult(
                content_id=result_id or "",
                version=result_id or "",
                size=len(content),
            )
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(f"Failed to write content: {e}", backend="x") from e

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Delete content (delete tweet or draft)."""
        if not context or not hasattr(context, "backend_path") or not context.backend_path:
            raise BackendError(
                "X connector requires context with backend_path",
                backend="x",
            )

        self._bind_transport(context)
        self._transport.remove(context.backend_path)

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        if not context or not hasattr(context, "backend_path"):
            return False
        self._bind_transport(context)
        return super().content_exists(content_hash, context)

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Return approximate content size (tweets are small)."""
        return 1024

    # =================================================================
    # Directory operations -- override for X virtual directories
    # =================================================================

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "X connector has a fixed virtual structure. "
            "mkdir() is not supported. "
            "Available paths: /timeline/, /posts/, /mentions/, etc.",
            backend="x",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "X connector has a fixed virtual structure. rmdir() is not supported.",
            backend="x",
        )

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        path = path.strip("/")
        if path.startswith("x/"):
            path = path[2:]
        return path in VIRTUAL_DIRS

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """List virtual directory contents via XTransport.list_keys()."""
        path = path.strip("/")

        # Normalize -- handle both /x/... and just namespace paths
        if path.startswith("x/"):
            path = path[2:]

        self._bind_transport(context)

        keys, prefixes = self._transport.list_keys(prefix=path, delimiter="/")

        # Strip prefix from keys to return just filenames
        entries: list[str] = []
        folder_prefix = f"{path}/" if path else ""
        for key in keys:
            name = (
                key[len(folder_prefix) :]
                if folder_prefix and key.startswith(folder_prefix)
                else key
            )
            if name:
                entries.append(name)
        for prefix_entry in prefixes:
            name = (
                prefix_entry[len(folder_prefix) :]
                if folder_prefix and prefix_entry.startswith(folder_prefix)
                else prefix_entry
            )
            if name:
                entries.append(name)

        if not entries and path not in VIRTUAL_DIRS:
            raise FileNotFoundError(f"Directory not found: {path}")

        return sorted(entries)

    # =================================================================
    # Glob and grep -- X-specific search
    # =================================================================

    def glob(
        self,
        pattern: str,
        path: str = "/",
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """Match paths using glob patterns against virtual structure."""
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
            cache_path = Path(self._cache_dir) / "timeline"
            if cache_path.exists():
                for file in cache_path.glob("*.json"):
                    if file.name != "recent.json":
                        available.append(f"/x/timeline/{file.name}")
            filtered: list[str] = glob_fast.glob_filter(available, include_patterns=[pattern])
            return filtered

        # Handle posts glob
        if pattern.startswith("/x/posts/") and "*.json" in pattern:
            return ["/x/posts/all.json", "/x/posts/new.json"]

        # Handle search glob
        if pattern.startswith("/x/search/"):
            cache_path = Path(self._cache_dir)
            matches = []
            bound_transport = self._x_transport.with_context(context)
            scope_token = bound_transport._scope_cache_token(
                bound_transport._cache_scope_identity()
            )
            for file in cache_path.glob(f"x:{scope_token}:search:*.json"):
                stem = file.stem
                try:
                    _, endpoint, query_hash = stem.rsplit(":", 2)
                except ValueError:
                    continue
                if endpoint != "search":
                    continue
                virtual_path = f"/x/search/{query_hash}.json"
                matches.append(virtual_path)
            filtered = glob_fast.glob_filter(matches, include_patterns=[pattern])
            return sorted(set(filtered))

        return []

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 100,
        context: Any = None,
        before_context: int = 0,  # noqa: ARG002
        after_context: int = 0,  # noqa: ARG002
        invert_match: bool = False,  # noqa: ARG002
    ) -> list[dict[str, Any]]:
        """Search content using pattern (API-backed and cache-backed)."""
        path = path.strip("/")

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
            return self._grep_cached(regex, path, max_results, context=context)

        # Search user's tweets via API
        if path.startswith("x/posts"):
            return self._grep_user_tweets(regex, max_results, context)

        # Fallback: search cached files
        return self._grep_cached(regex, path, max_results, context=context)

    def _grep_cached(
        self,
        regex: re.Pattern[str],
        path: str,
        max_results: int,
        context: Any = None,
    ) -> list[dict[str, Any]]:
        """Search cached JSON files."""
        import json

        results: list[dict[str, Any]] = []
        cache_path = Path(self._cache_dir)
        bound_transport = self._x_transport.with_context(context)
        scope_token = bound_transport._scope_cache_token(bound_transport._cache_scope_identity())

        pattern_map = {
            "x/timeline": f"x:{scope_token}:timeline:*.json",
            "x/bookmarks": f"x:{scope_token}:bookmarks:*.json",
            "x/mentions": f"x:{scope_token}:mentions:*.json",
        }
        glob_pattern = pattern_map.get(path, f"x:{scope_token}:*.json")

        for file in cache_path.glob(glob_pattern):
            if len(results) >= max_results:
                break
            try:
                content = file.read_text()
                data = json.loads(content)
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
            except Exception as e:
                logger.debug("Failed to search cached tweet file %s: %s", file, e)
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
            transport = self._x_transport.with_context(context)
            client = await transport._get_api_client_async()
            try:
                user_id = await transport._get_user_id(client=client)
                response = await client.get_user_tweets(user_id, max_results=100)
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
            from nexus.lib.sync_bridge import run_sync

            return run_sync(_search_user_tweets())
        except Exception as e:
            logger.warning("grep_user_tweets failed: %s", e)
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
            transport = self._x_transport.with_context(context)
            client = await transport._get_api_client_async()
            try:
                response = await client.search_recent_tweets(pattern, max_results=max_results)
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
            from nexus.lib.sync_bridge import run_sync

            return run_sync(_search_global())
        except Exception as e:
            logger.warning("grep_global failed: %s", e)
            return []
