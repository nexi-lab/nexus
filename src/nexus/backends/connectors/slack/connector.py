"""Slack connector backend -- PathAddressingEngine + SlackTransport composition.

Architecture (Transport x Addressing):
    PathSlackBackend(PathAddressingEngine)
        +-- SlackTransport(Transport)
              +-- Slack API calls (I/O)
              +-- OAuth token from OperationContext

This follows the same pattern as PathGmailBackend, PathCalendarBackend:
Transport handles raw I/O; PathAddressingEngine handles addressing,
path security, and content operations.

Storage structure (2-level hierarchy):
    /
    +-- channels/                      # Public channels
    |   +-- general.yaml               # Messages as YAML
    +-- private-channels/              # Private channels
    |   +-- team-internal.yaml
    +-- dms/                           # Direct messages
        +-- U12345.yaml
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import register_connector
from nexus.backends.base.runtime_deps import PythonDep
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
from nexus.backends.connectors.slack.schemas import (
    DeleteMessageSchema,
    SendMessageSchema,
    UpdateMessageSchema,
)
from nexus.backends.connectors.slack.transport import FOLDER_TYPES, SlackTransport
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import AuthenticationError, BackendError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector(
    "slack_connector",
    description="Slack workspace with OAuth 2.0 authentication",
    category="oauth",
    runtime_deps=(PythonDep("slack_sdk", extras=("slack",)),),
    service_name="slack",
)
class PathSlackBackend(
    PathAddressingEngine,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
):
    """Slack connector: PathAddressingEngine + SlackTransport composition.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Channel-based folder structure (channels/, private-channels/, dms/)
    - Message posting via write_content
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = OAUTH_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.README_DOC,
        }
    )

    # Skill documentation settings
    SKILL_NAME = "slack"

    SCHEMAS: dict[str, type] = {
        "send_message": SendMessageSchema,
        "delete_message": DeleteMessageSchema,
        "update_message": UpdateMessageSchema,
    }

    OPERATION_TRAITS = {
        "send_message": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "delete_message": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "update_message": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
    }

    ERROR_REGISTRY = {
        **TRAIT_ERRORS,
        "CHANNEL_NOT_FOUND": ErrorDef(
            message="Channel not found or bot not a member",
            readme_section="operations",
            fix_example="channel: C01234ABCDE  # Use channel ID, not name",
        ),
        "MESSAGE_NOT_FOUND": ErrorDef(
            message="Message not found (invalid timestamp)",
            readme_section="operations",
            fix_example="ts: 1234567890.123456",
        ),
    }

    # Top-level folder types
    FOLDER_TYPES = FOLDER_TYPES

    # Provider aliases for OAuth resolution
    _PROVIDER_ALIASES: dict[str, list[str]] = {}

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "slack",
        record_store: "RecordStoreABC | None" = None,
        max_messages_per_channel: int = 100,
        metadata_store: Any = None,
        encryption_key: str | None = None,
        pool: Any = None,  # CredentialPool | None — see Issue #3723 for migration guide
    ):
        """Initialize Slack connector backend.

        Args:
            token_manager_db: Path to TokenManager database
            user_email: Optional user email for OAuth lookup
            provider: OAuth provider name from config (default: "slack")
            record_store: Optional RecordStoreABC for content caching
            max_messages_per_channel: Maximum messages to fetch per channel
            metadata_store: MetastoreABC instance for file_paths table
            pool: Optional CredentialPool for multi-account failover (Issue #3723).
        """
        # 1. Initialize OAuth (sets self.token_manager, self.provider, etc.)
        self._pool = pool  # stored for future migrate_to_pool() call (Issue #3723)
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )

        # 2. Create SlackTransport with the token manager
        slack_transport = SlackTransport(
            token_manager=self.token_manager,
            provider=provider,
            user_email=user_email,
            max_messages_per_channel=max_messages_per_channel,
        )
        self._slack_transport = slack_transport

        # 3. Initialize PathAddressingEngine
        PathAddressingEngine.__init__(
            self,
            transport=slack_transport,
            backend_name="slack",
        )

        # 4. Cache and metadata setup
        self.session_factory = record_store.session_factory if record_store else None
        self.metadata_store = metadata_store
        self.max_messages_per_channel = max_messages_per_channel

        # 5. Register OAuth provider using factory
        self._register_oauth_provider()

    # -- Properties --

    @property
    def user_scoped(self) -> bool:
        """This backend requires per-user OAuth credentials."""
        return True

    @property
    def has_token_manager(self) -> bool:
        """Slack connector manages OAuth tokens."""
        return True

    # -- OAuth provider registration --

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        import traceback

        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()

            try:
                provider_instance = factory.create_provider(name=self.provider)
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info("Registered OAuth provider '%s' for Slack backend", self.provider)
            except ValueError as e:
                logger.warning(
                    "OAuth provider '%s' not available: %s. "
                    "OAuth flow must be initiated manually via the Integrations page.",
                    self.provider,
                    e,
                )
        except Exception as e:
            error_msg = f"Failed to register OAuth provider: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)

    # =================================================================
    # Content operations -- override PathAddressingEngine for Slack
    # =================================================================

    def _bind_transport(self, context: "OperationContext | None") -> None:
        """Bind the transport to the current request context (OAuth token)."""
        self._transport = self._slack_transport.with_context(context)

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Post a message to Slack via the transport.

        Args:
            content: Message content as JSON bytes (must include 'channel' and 'text')
            content_id: Ignored
            offset: Ignored
            context: Operation context

        Returns:
            WriteResult with message timestamp
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Slack connector requires backend_path in OperationContext for write operations",
                backend="slack",
            )

        self._bind_transport(context)

        try:
            msg_ts = self._transport.store(context.backend_path, content)
            return WriteResult(content_id=msg_ts or "", version=msg_ts or "", size=len(content))
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(f"Failed to write message: {e}", backend="slack") from e

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        """Read channel messages as YAML.

        Args:
            content_id: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            Channel messages as YAML bytes
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Slack connector requires backend_path in OperationContext",
                backend="slack",
            )

        # Bind transport to request context for OAuth
        self._bind_transport(context)

        # Delegate to PathAddressingEngine (which calls transport.fetch)
        return super().read_content(content_id, context)

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        raise BackendError(
            "Slack connector does not support message deletion yet.",
            backend="slack",
        )

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        if not context or not context.backend_path:
            return False
        self._bind_transport(context)
        return super().content_exists(content_id, context)

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        if context is None or not hasattr(context, "backend_path"):
            raise ValueError("Slack connector requires backend_path in OperationContext")

        # Bind transport and delegate
        self._bind_transport(context)
        return super().get_content_size(content_id, context)

    # =================================================================
    # Version support (Slack messages are mutable)
    # =================================================================

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """Get version for a Slack channel snapshot file.

        Returns a timestamp-based version for .yaml files, None for directories.
        """
        try:
            backend_path = (
                context.backend_path
                if context and hasattr(context, "backend_path") and context.backend_path
                else path.lstrip("/")
            )

            if not backend_path.endswith(".yaml"):
                return None

            # Return timestamp-based version for staleness detection
            return str(int(datetime.now(UTC).timestamp()))
        except Exception as e:
            logger.debug("Slack version check failed: %s", e)
            return None

    # =================================================================
    # Directory operations -- override for Slack virtual directories
    # =================================================================

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        return path in self.FOLDER_TYPES

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents via SlackTransport.list_keys()."""
        try:
            path = path.strip("/")

            # Bind transport for OAuth
            self._bind_transport(context)

            # Root directory -- list folder types
            if not path:
                return [f"{folder}/" for folder in self.FOLDER_TYPES]

            # Folder type directory -- list channel YAML files
            if path in self.FOLDER_TYPES:
                keys, _prefixes = self._transport.list_keys(prefix=path, delimiter="/")
                # keys are "channels/general.yaml" -- strip folder prefix
                files = []
                folder_prefix = f"{path}/"
                for key in keys:
                    name = key[len(folder_prefix) :] if key.startswith(folder_prefix) else key
                    if name:
                        files.append(name)
                return sorted(files)

            raise FileNotFoundError(f"Directory not found: {path}")
        except FileNotFoundError:
            raise
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="slack",
                path=path,
            ) from e

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
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
        raise BackendError(
            "Slack connector has a fixed directory structure. Cannot remove directories.",
            backend="slack",
        )
