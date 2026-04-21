"""Slack Transport -- raw key->bytes I/O over the Slack API.

Implements the Transport protocol for Slack, mapping:
- fetch(key) -> conversations.history -> YAML bytes
- list_keys(prefix) -> conversations.list -> channel keys
- exists(key) -> channel/message existence check

Read-only: store/remove/copy_key/create_dir raise BackendError.

Auth: SlackTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``
so the transport can resolve the per-user OAuth token.

Key schema:
    "channels/general.yaml"         -> channel_type=public_channel, name=general
    "private-channels/team.yaml"    -> channel_type=private_channel, name=team
    "dms/U12345.yaml"              -> channel_type=im, id=U12345
    list_keys("")                   -> common_prefixes = ["channels/", ...]
    list_keys("channels/")          -> ["channels/general.yaml", ...]
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from copy import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.slack.utils import (
    list_channels,
    list_messages_from_channel,
)
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Top-level virtual folder types mapping to Slack channel types.
FOLDER_TYPES = ["channels", "private-channels", "dms"]

# Mapping folder name -> Slack API channel type filter.
_FOLDER_TO_CHANNEL_TYPE: dict[str, str] = {
    "channels": "public_channel",
    "private-channels": "private_channel",
    "dms": "im",
}


class SlackTransport:
    """Slack API transport implementing the Transport protocol.

    Attributes:
        transport_name: ``"slack"`` -- used by PathAddressingEngine to build
            the backend name (``"path-slack"``).
    """

    transport_name: str = "slack"

    def __init__(
        self,
        token_manager: Any,
        provider: str = "slack",
        user_email: str | None = None,
        max_messages_per_channel: int = 100,
    ) -> None:
        self._token_manager = token_manager
        self._provider = provider
        self._user_email = user_email
        self._max_messages_per_channel = max_messages_per_channel
        self._context: OperationContext | None = None

        # In-memory caches.
        self._channel_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Context binding (per-request OAuth token resolution)
    # ------------------------------------------------------------------

    def with_context(self, context: OperationContext | None) -> SlackTransport:
        """Return a shallow copy bound to *context*."""
        clone = copy(self)
        clone._context = context
        return clone

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_slack_client(self) -> Any:
        """Get Slack WebClient with the bound user's OAuth credentials."""
        try:
            from slack_sdk import WebClient
        except ImportError:
            raise BackendError(
                "slack-sdk not installed. Install with: pip install slack-sdk",
                backend="slack",
            ) from None

        from nexus.backends.connectors.oauth_base import resolve_oauth_access_token
        from nexus.contracts.exceptions import AuthenticationError

        if self._user_email:
            user_email: str | None = self._user_email
        elif self._context and self._context.user_id:
            user_email = self._context.user_id
        else:
            user_email = None

        zone_id = (
            self._context.zone_id
            if self._context and hasattr(self._context, "zone_id") and self._context.zone_id
            else "root"
        )
        try:
            access_token = resolve_oauth_access_token(
                self._token_manager,
                connector_name="slack_connector",
                provider=self._provider,
                user_email=user_email,
                zone_id=zone_id,
            )
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="slack",
            ) from e

        return WebClient(token=access_token)

    def _get_channel_by_name(self, channel_name: str) -> dict[str, Any] | None:
        """Get channel info by name or ID, using cache."""
        # Check cache first
        for _channel_id, channel in self._channel_cache.items():
            if channel.get("name") == channel_name or channel.get("id") == channel_name:
                return channel

        # Fetch from API
        client = self._get_slack_client()
        channels = list_channels(client, silent=True)

        # Update cache
        for channel in channels:
            self._channel_cache[channel["id"]] = channel

        # Find matching channel
        for channel in channels:
            if channel.get("name") == channel_name or channel.get("id") == channel_name:
                return channel

        return None

    @staticmethod
    def _parse_key(key: str) -> tuple[str | None, str | None]:
        """Parse a transport key into ``(folder_type, channel_name)``.

        Expected format: ``"channels/general.yaml"``

        Returns ``(None, None)`` for unparseable keys.
        """
        key = key.strip("/")
        parts = key.split("/")

        if len(parts) == 2 and parts[0] in FOLDER_TYPES:
            folder_type = parts[0]
            filename = parts[1]
            if filename.endswith(".yaml"):
                channel_name = filename.removesuffix(".yaml")
                return folder_type, channel_name
            return folder_type, None
        elif len(parts) == 1 and parts[0] in FOLDER_TYPES:
            return parts[0], None

        return None, None

    @staticmethod
    def _format_messages_as_yaml(messages: list[dict[str, Any]]) -> bytes:
        """Format messages as YAML bytes."""
        yaml_output = yaml.dump(
            messages,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )
        return yaml_output.encode("utf-8")

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Post a message to Slack.

        The data must be JSON with 'channel' and 'text' fields.
        """
        try:
            message_data = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise BackendError(f"Invalid JSON content: {e}", backend="slack") from e

        channel = message_data.get("channel")
        text = message_data.get("text")
        thread_ts = message_data.get("thread_ts")

        if not channel or not text:
            raise BackendError(
                "Message must include 'channel' and 'text' fields",
                backend="slack",
            )

        client = self._get_slack_client()

        params: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts

        try:
            result = client.chat_postMessage(**params)
        except Exception as e:
            raise BackendError(f"Failed to post message: {e}", backend="slack") from e

        if not result.get("ok"):
            error = result.get("error", "unknown_error")
            raise BackendError(f"Failed to post message: {error}", backend="slack")

        msg_ts: str = result["ts"]
        return msg_ts

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch a channel's messages as YAML bytes by transport key."""
        folder_type, channel_name = self._parse_key(key)
        if not folder_type or not channel_name:
            raise NexusFileNotFoundError(key)

        channel = self._get_channel_by_name(channel_name)
        if not channel:
            raise NexusFileNotFoundError(key)

        channel_id = channel["id"]

        client = self._get_slack_client()
        messages = list_messages_from_channel(
            client=client,
            channel_id=channel_id,
            channel_name=channel_name,
            limit=self._max_messages_per_channel,
            silent=True,
        )

        # Add channel context to each message
        for msg in messages:
            msg["channel_id"] = channel_id
            msg["channel_name"] = channel_name

        # Handle empty channels
        if not messages:
            try:
                info = client.conversations_info(channel=channel_id)
                if info.get("ok") and not info.get("channel", {}).get("is_member"):
                    messages = [
                        {
                            "_metadata": {
                                "channel_id": channel_id,
                                "channel_name": channel_name,
                                "status": "bot_not_member",
                                "message": (
                                    f"Bot is not a member of #{channel_name}. "
                                    "Please invite the bot to this channel using: "
                                    "/invite @YourBotName"
                                ),
                            }
                        }
                    ]
            except Exception as e:
                logger.debug("Failed to get channel info for %s: %s", channel_name, e)
                messages = [
                    {
                        "_metadata": {
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "status": "no_messages",
                            "message": (
                                f"No messages found in #{channel_name}. This could mean "
                                "the channel is empty or the bot doesn't have access."
                            ),
                        }
                    }
                ]

        content = self._format_messages_as_yaml(messages)
        # Version is a timestamp for mutable Slack content
        version = str(int(datetime.now(UTC).timestamp()))
        return content, version

    def remove(self, key: str) -> None:
        raise BackendError(
            "Slack transport does not support message deletion via remove().",
            backend="slack",
        )

    def exists(self, key: str) -> bool:
        """Check whether a channel key exists."""
        folder_type, channel_name = self._parse_key(key)
        if not folder_type:
            # Could be a folder-level check
            stripped = key.strip("/")
            return stripped in FOLDER_TYPES or stripped == ""
        if not channel_name:
            return folder_type in FOLDER_TYPES
        channel = self._get_channel_by_name(channel_name)
        return channel is not None

    def get_size(self, key: str) -> int:
        """Return estimated size of the channel content."""
        # Fetching and measuring would be expensive; return estimate.
        return 4096

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List channel keys under *prefix*.

        - ``list_keys("")`` -> ``([], ["channels/", "private-channels/", "dms/"])``
        - ``list_keys("channels/")`` -> ``(["channels/general.yaml", ...], [])``
        """
        prefix = prefix.strip("/")

        # Root -> return folder types as common prefixes
        if not prefix:
            return [], [f"{folder}/" for folder in FOLDER_TYPES]

        # Folder type -> list channels as YAML files
        if prefix in FOLDER_TYPES:
            channel_type = _FOLDER_TO_CHANNEL_TYPE.get(prefix)
            if not channel_type:
                return [], []

            client = self._get_slack_client()
            channels = list_channels(client, types=channel_type, silent=True)

            # Update cache
            for channel in channels:
                self._channel_cache[channel["id"]] = channel

            keys = [f"{prefix}/{channel.get('name', channel['id'])}.yaml" for channel in channels]
            return sorted(keys), []

        return [], []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        raise BackendError(
            "Slack transport does not support copy.",
            backend="slack",
        )

    def create_dir(self, key: str) -> None:
        raise BackendError(
            "Slack transport does not support directory creation. Channel structure is virtual.",
            backend="slack",
        )

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream channel content (small payloads -- fetch then chunk)."""
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
            "Slack transport does not support chunked store.",
            backend="slack",
        )
