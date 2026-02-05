"""Slack connector backend with OAuth 2.0 authentication.

This is a connector backend that provides read-write access to Slack workspace messages,
organizing them by channel-based folders and thread structure.

Use case: Access Slack messages through Nexus mount for search, analysis, and automation.

Storage structure (2-level hierarchy):
    /
    ├── channels/                      # Public channels
    │   ├── general/
    │   │   └── {ts}-{message_id}.json # Message content as JSON
    │   └── random/
    ├── private-channels/              # Private channels
    │   └── team-internal/
    └── dms/                          # Direct messages
        └── {user_id}/

Key features:
- OAuth 2.0 authentication (user-scoped)
- Channel-based organization (public, private, DMs)
- Thread-based conversation structure
- Read and write operations (read messages, post messages)
- On-demand message fetching from Slack API
- Full message metadata and content in JSON format
- Automatic token management via TokenManager
- Database-backed caching via CacheConnectorMixin for fast search

Fetching strategy:
- Uses conversations.list() and conversations.history() APIs
- Fetches messages on-demand when accessed
- Each message is a unique JSON file with timestamp-based naming

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - No token expiration (unless revoked)
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import IMMUTABLE_VERSION, CacheConnectorMixin
from nexus.backends.slack_connector_utils import (
    list_channels,
    list_messages_from_channel,
)
from nexus.core.exceptions import BackendError
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class SlackConnectorBackend(Backend, CacheConnectorMixin):
    """
    Slack connector backend with OAuth 2.0 authentication.

    This backend syncs messages from Slack API and organizes them as JSON files
    by channels (public, private, DMs).

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Message syncing from channels
    - Channel-based folder structure
    - Full message metadata and content
    - No token expiration (Slack OAuth v2)
    - Persistent caching via CacheConnectorMixin for fast grep/search

    Folder Structure (2-level hierarchy):
    - / - Root directory (lists channel type folders)
    - /channels/ - Public channels
      - /channels/general/ - Messages in #general channel
        - {ts}-{msg_id}.json - Message files (timestamp-based naming)
    - /private-channels/ - Private channels
    - /dms/ - Direct messages

    Limitations:
    - Requires OAuth tokens for each user
    - Rate limited by Slack API quotas
    - Messages stored as JSON files
    """

    # Top-level folder types
    FOLDER_TYPES = ["channels", "private-channels", "dms"]

    # Enable metadata-based listing (use file_paths table)
    use_metadata_listing = True

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "slack",
        session_factory: Any = None,
        max_messages_per_channel: int = 100,
        metadata_store: Any = None,
    ):
        """
        Initialize Slack connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            provider: OAuth provider name from config (default: "slack")
            session_factory: SQLAlchemy session factory for content caching (optional).
                           If provided, enables persistent caching for fast grep/search.
            max_messages_per_channel: Maximum number of messages to fetch per channel (default: 100).
                                     Set to None for unlimited.
            metadata_store: MetadataStore instance for writing to file_paths table (optional).
                          Required for metadata-based listing (fast database queries).

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
        """
        # Import TokenManager here to avoid circular imports
        from nexus.server.auth.token_manager import TokenManager

        # Store original token_manager_db for config updates
        self.token_manager_db = token_manager_db

        # Resolve database URL using base class method (checks TOKEN_MANAGER_DB env var)
        resolved_db = self.resolve_database_url(token_manager_db)

        # Support both file paths and database URLs
        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db)
        else:
            self.token_manager = TokenManager(db_path=resolved_db)

        self.user_email = user_email  # None means use context.user_id
        self.provider = provider

        # Store session factory for caching (CacheConnectorMixin)
        self.session_factory = session_factory

        # Store max messages per channel
        self.max_messages_per_channel = max_messages_per_channel

        # Store metadata store for file_paths table
        self.metadata_store = metadata_store

        # Cache for channels: channel_id -> channel_info
        self._channel_cache: dict[str, dict[str, Any]] = {}

        # Cache for user info: user_id -> user_info
        self._user_cache: dict[str, dict[str, Any]] = {}

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        import traceback

        try:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            # Create factory (loads from oauth.yaml config)
            factory = OAuthProviderFactory()

            # Create provider instance from config
            try:
                provider_instance = factory.create_provider(name=self.provider)
                # Register with TokenManager using the provider name from config
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info(f"✓ Registered OAuth provider '{self.provider}' for Slack backend")
            except ValueError as e:
                # Provider not found in config or credentials not set
                logger.warning(
                    f"OAuth provider '{self.provider}' not available: {e}. "
                    "OAuth flow must be initiated manually via the Integrations page."
                )
        except Exception as e:
            error_msg = f"Failed to register OAuth provider: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "slack"

    @property
    def user_scoped(self) -> bool:
        """This backend requires per-user OAuth credentials."""
        return True

    def _get_slack_client(self, context: "OperationContext | None" = None) -> Any:
        """Get Slack WebClient with user's OAuth credentials.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            Slack WebClient instance

        Raises:
            BackendError: If credentials not found or user not authenticated
        """
        # Import here to avoid dependency if not using Slack
        try:
            from slack_sdk import WebClient
        except ImportError:
            raise BackendError(
                "slack-sdk not installed. Install with: pip install slack-sdk",
                backend="slack",
            ) from None

        # Determine which user's tokens to use
        if self.user_email:
            # Explicit user_email configured (single-user/demo mode)
            user_email = self.user_email
        elif context and context.user_id:
            # Multi-user mode: use authenticated user from API key
            user_email = context.user_id
        else:
            raise BackendError(
                "Slack backend requires either configured user_email "
                "or authenticated user in OperationContext",
                backend="slack",
            )

        # Get valid access token from TokenManager
        import asyncio

        try:
            # Default to 'default' tenant if not specified
            zone_id = (
                context.zone_id
                if context and hasattr(context, "zone_id") and context.zone_id
                else "default"
            )

            # Handle both sync and async contexts
            try:
                # Try to get the current event loop
                asyncio.get_running_loop()
                # If we're in an async context, we can't use asyncio.run()
                raise BackendError(
                    "Slack connector cannot be used in async context. "
                    "Use sync methods or ensure you're not in an async event loop.",
                    backend="slack",
                )
            except RuntimeError:
                # No running event loop, safe to use asyncio.run()
                access_token = asyncio.run(
                    self.token_manager.get_valid_token(
                        provider=self.provider,
                        user_email=user_email,
                        zone_id=zone_id,
                    )
                )
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="slack",
            ) from e

        # Create Slack WebClient with OAuth token
        client = WebClient(token=access_token)
        return client

    def _parse_message_timestamp(self, ts: str) -> datetime:
        """Parse Slack message timestamp to datetime.

        Args:
            ts: Slack timestamp (e.g., "1234567890.123456")

        Returns:
            Datetime object in UTC
        """
        try:
            # Slack timestamps are Unix timestamps with microseconds
            timestamp = float(ts)
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except Exception:
            # Fallback to current time if parsing fails
            return datetime.now(UTC)

    def _format_message_as_json(self, message: dict[str, Any]) -> bytes:
        """Format message data as JSON bytes.

        Args:
            message: Message metadata dictionary

        Returns:
            Formatted JSON as bytes
        """
        # Pretty-print JSON for readability
        json_output = json.dumps(message, indent=2, ensure_ascii=False)
        return json_output.encode("utf-8")

    def _format_messages_as_yaml(self, messages: list[dict[str, Any]]) -> bytes:
        """Format messages as YAML bytes.

        Args:
            messages: List of message dictionaries

        Returns:
            Formatted YAML as bytes
        """
        import yaml

        # Convert messages to YAML format
        yaml_output = yaml.dump(
            messages,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )
        return yaml_output.encode("utf-8")

    def _get_channel_by_name(
        self, channel_name: str, context: "OperationContext | None" = None
    ) -> dict[str, Any] | None:
        """Get channel info by name or ID.

        Args:
            channel_name: Channel name (without #) or channel ID (for DMs)
            context: Operation context

        Returns:
            Channel dict or None if not found
        """
        # Check cache first
        for _channel_id, channel in self._channel_cache.items():
            # Match by name or ID (DMs use ID as filename)
            if channel.get("name") == channel_name or channel.get("id") == channel_name:
                return channel

        # Fetch from API
        client = self._get_slack_client(context)
        channels = list_channels(client, silent=True)

        # Update cache
        for channel in channels:
            self._channel_cache[channel["id"]] = channel

        # Find matching channel by name or ID
        for channel in channels:
            if channel.get("name") == channel_name or channel.get("id") == channel_name:
                return channel

        return None

    # === Backend interface methods ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content (post message to Slack).

        For Slack, writing content means posting a message to a channel.
        The content should be JSON with message details.

        Args:
            content: Message content as JSON bytes
            context: Operation context with backend_path indicating target channel

        Returns:
            Content hash (message timestamp)

        Raises:
            BackendError: If write operation fails
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Slack connector requires backend_path in OperationContext for write operations",
                backend="slack",
            )

        try:
            # Parse message data
            message_data = json.loads(content.decode("utf-8"))

            # Extract channel and text
            channel = message_data.get("channel")
            text = message_data.get("text")
            thread_ts = message_data.get("thread_ts")  # For threaded replies

            if not channel or not text:
                raise BackendError(
                    "Message must include 'channel' and 'text' fields",
                    backend="slack",
                )

            # Get Slack client
            client = self._get_slack_client(context)

            # Post message
            params = {"channel": channel, "text": text}
            if thread_ts:
                params["thread_ts"] = thread_ts

            result = client.chat_postMessage(**params)

            if not result.get("ok"):
                error = result.get("error", "unknown_error")
                raise BackendError(f"Failed to post message: {error}", backend="slack")

            # Return message timestamp as content hash
            return result["ts"]

        except json.JSONDecodeError as e:
            raise BackendError(f"Invalid JSON content: {e}", backend="slack") from e
        except Exception as e:
            raise BackendError(f"Failed to write message: {e}", backend="slack") from e

    def read_content(
        self, content_hash: str, context: "OperationContext | None" = None
    ) -> "HandlerResponse[bytes]":
        """
        Read channel content as YAML file from cache or Slack API.

        For connector backends, content_hash is ignored - we use backend_path instead.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            HandlerResponse with channel messages as YAML bytes in data field

        Raises:
            NexusFileNotFoundError: If channel doesn't exist
            BackendError: If read operation fails
        """
        import time

        start_time = time.perf_counter()

        if not context or not context.backend_path:
            return HandlerResponse.error(
                message="Slack connector requires backend_path in OperationContext",
                code=400,
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name="slack",
                path=content_hash,
            )

        backend_path = context.backend_path

        # Parse path: channels/general.yaml
        path_parts = backend_path.strip("/").split("/")

        if len(path_parts) != 2:
            return HandlerResponse.not_found(
                path=backend_path,
                message=f"Invalid Slack path format: {backend_path}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name="slack",
            )

        folder_type, filename = path_parts

        if folder_type not in self.FOLDER_TYPES:
            return HandlerResponse.not_found(
                path=backend_path,
                message=f"Invalid folder type: {folder_type}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name="slack",
            )

        if not filename.endswith(".yaml"):
            return HandlerResponse.not_found(
                path=backend_path,
                message=f"Not a valid YAML file: {filename}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name="slack",
            )

        # Extract channel name from filename
        channel_name = filename.replace(".yaml", "")

        # Get cache path
        cache_path = self._get_cache_path(context) or backend_path

        # Check cache first (if caching enabled)
        if self._has_caching():
            cached = self._read_from_cache(cache_path, original=True)
            if cached and not cached.stale and cached.content_binary:
                return HandlerResponse.ok(
                    data=cached.content_binary,
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name="slack",
                    path=backend_path,
                )

        # Fetch from Slack API
        try:
            # Get channel info
            channel = self._get_channel_by_name(channel_name, context)
            if not channel:
                return HandlerResponse.not_found(
                    path=backend_path,
                    message=f"Channel not found: {channel_name}",
                    execution_time_ms=(time.perf_counter() - start_time) * 1000,
                    backend_name="slack",
                )

            channel_id = channel["id"]

            # Fetch all messages from channel
            client = self._get_slack_client(context)
            messages = list_messages_from_channel(
                client=client,
                channel_id=channel_id,
                channel_name=channel_name,
                limit=self.max_messages_per_channel,
                silent=True,
            )

            # Add channel context to each message
            for msg in messages:
                msg["channel_id"] = channel_id
                msg["channel_name"] = channel_name

            # If no messages, add metadata about why
            if not messages:
                # Check if this is likely a "not_in_channel" issue
                # by trying to get channel info
                try:
                    info = client.conversations_info(channel=channel_id)
                    if info.get("ok") and not info.get("channel", {}).get("is_member"):
                        messages = [
                            {
                                "_metadata": {
                                    "channel_id": channel_id,
                                    "channel_name": channel_name,
                                    "status": "bot_not_member",
                                    "message": f"Bot is not a member of #{channel_name}. Please invite the bot to this channel using: /invite @YourBotName",
                                }
                            }
                        ]
                except Exception:
                    # If we can't get channel info, just note that no messages were found
                    messages = [
                        {
                            "_metadata": {
                                "channel_id": channel_id,
                                "channel_name": channel_name,
                                "status": "no_messages",
                                "message": f"No messages found in #{channel_name}. This could mean the channel is empty or the bot doesn't have access.",
                            }
                        }
                    ]

        except Exception as e:
            return HandlerResponse.not_found(
                path=backend_path,
                message=f"Failed to fetch channel messages: {e}",
                execution_time_ms=(time.perf_counter() - start_time) * 1000,
                backend_name="slack",
            )

        # Format as YAML
        content = self._format_messages_as_yaml(messages)

        # Cache the result
        if self._has_caching():
            try:
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=IMMUTABLE_VERSION,  # Messages are immutable
                    zone_id=zone_id,
                )
            except Exception:
                pass  # Don't fail on cache write errors

        return HandlerResponse.ok(
            data=content,
            execution_time_ms=(time.perf_counter() - start_time) * 1000,
            backend_name="slack",
            path=backend_path,
        )

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete is not supported for Slack connector (read-only for now).

        Args:
            content_hash: Content hash
            context: Operation context

        Raises:
            BackendError: Always raised (not implemented yet)
        """
        raise BackendError(
            "Slack connector does not support message deletion yet.",
            backend="slack",
        )

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if message exists.

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            True if message exists, False otherwise
        """
        if not context or not context.backend_path:
            return False

        try:
            # Try to read the message
            self.read_content(content_hash, context)
            return True
        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get message content size (cache-first, efficient).

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If message doesn't exist
            BackendError: If operation fails
        """
        if context is None or not hasattr(context, "backend_path"):
            raise ValueError("Slack connector requires backend_path in OperationContext")

        # OPTIMIZATION: Check cache first
        if hasattr(context, "virtual_path") and context.virtual_path:
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                return cached_size

        # Fallback: Read content to get size
        content = self.read_content(content_hash, context)
        return len(content)

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get reference count (always 1 for connector backends).

        Args:
            content_hash: Content hash
            context: Operation context

        Returns:
            Always 1 (no reference counting)
        """
        return 1

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """
        Get version for a Slack message file.

        Slack messages are immutable (read-only) - once sent, they never change.
        Therefore, we return a fixed version "immutable" for all message files.

        Args:
            path: Virtual file path (or backend_path from context)
            context: Operation context with optional backend_path

        Returns:
            "immutable" for message files, None for directories/non-files
        """
        try:
            # Get backend path
            if context and hasattr(context, "backend_path") and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            # Check if this is a message file (ends with .json)
            if not backend_path.endswith(".json"):
                return None  # Not a file (likely a directory)

            # Return fixed version for immutable Slack messages
            return IMMUTABLE_VERSION

        except Exception:
            return None

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory (not supported for Slack connector).

        Args:
            path: Directory path
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context

        Raises:
            BackendError: Always raised (read-only structure)
        """
        raise BackendError(
            "Slack connector has a fixed directory structure. Cannot create directories.",
            backend="slack",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove directory (not supported for Slack connector).

        Args:
            path: Directory path
            recursive: Remove non-empty directory
            context: Operation context

        Raises:
            BackendError: Always raised (read-only structure)
        """
        raise BackendError(
            "Slack connector has a fixed directory structure. Cannot remove directories.",
            backend="slack",
        )

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory.

        Args:
            path: Path to check
            context: Operation context

        Returns:
            True if path is a directory, False if it's a file
        """
        path = path.strip("/")

        if not path:
            return True  # Root is always a directory

        path_parts = path.split("/")

        # Top-level folders (channels, private-channels, dms)
        if len(path_parts) == 1:
            return path in self.FOLDER_TYPES

        # Channel YAML files (channels/general.yaml) - these are files now
        if len(path_parts) == 2:
            return False  # All paths at this level are YAML files

        return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents.

        This method fetches channels/messages from Slack and lists:
        - Root directory: Folder types (channels/, private-channels/, dms/)
        - Folder type directory: Channel folders
        - Channel directory: Message files

        Args:
            path: Directory path to list (relative to backend root)
            context: Operation context for authentication

        Returns:
            List of entry names (folders or message files)

        Raises:
            FileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        try:
            path = path.strip("/")

            # Root directory - list folder types
            if not path:
                return [f"{folder}/" for folder in self.FOLDER_TYPES]

            path_parts = path.split("/")

            # Folder type directory - list channels as YAML files
            if len(path_parts) == 1:
                folder_type = path_parts[0]
                if folder_type not in self.FOLDER_TYPES:
                    raise FileNotFoundError(f"Directory not found: {path}")

                # Get Slack client
                client = self._get_slack_client(context)

                # Determine channel types based on folder
                if folder_type == "channels":
                    channel_types = "public_channel"
                elif folder_type == "private-channels":
                    channel_types = "private_channel"
                elif folder_type == "dms":
                    channel_types = "im"
                else:
                    return []

                # Fetch channels
                channels = list_channels(client, types=channel_types, silent=True)

                # Update cache
                for channel in channels:
                    self._channel_cache[channel["id"]] = channel

                # Return channel names as YAML files (not directories)
                return [f"{channel.get('name', channel['id'])}.yaml" for channel in channels]

            # Invalid path - channels are now files, not directories
            raise FileNotFoundError(f"Directory not found: {path}")

        except FileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="slack",
                path=path,
            ) from e
