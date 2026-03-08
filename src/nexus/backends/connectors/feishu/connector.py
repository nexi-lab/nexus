"""Feishu/Lark connector backend with app bot + user OAuth authentication.

Provides read-write access to Feishu workspace messages, organized by
group chats and P2P conversations.

Mount prefix: /chat/feishu/

Storage structure (2-level hierarchy):
    /
    ├── groups/                                  # Group chats
    │   ├── {Name} [{chat_id}].yaml              # Hybrid naming for stability
    │   └── ...
    └── p2p/                                     # 1-on-1 chats
        └── {Name} [{chat_id}].yaml

Hybrid naming:
    Filenames use "{Name} [{chat_id}].yaml" so that links remain stable
    even if the chat is renamed. The chat_id in brackets is the persistent
    identifier used for API resolution.

Authentication:
    Dual auth model:
    - App bot credentials (app_id + app_secret) -> tenant_access_token
    - Optional per-user OAuth via OAuthConnectorMixin (copilot mode)
"""

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import IMMUTABLE_VERSION, CacheConnectorMixin
from nexus.backends.connectors.feishu.utils import (
    get_chat_info,
    get_chat_members,
    list_chats,
    list_messages_from_chat,
    send_message,
)
from nexus.backends.oauth_mixin import OAuthConnectorMixin
from nexus.backends.registry import register_connector
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult
from nexus.core.protocols.capabilities import OAUTH_CONNECTOR_CAPABILITIES, ConnectorCapability

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector(
    "feishu_connector",
    description="Feishu/Lark messaging connector with app + user OAuth",
    category="oauth",
    requires=["lark-oapi"],
    service_name="feishu",
)
class FeishuConnectorBackend(Backend, CacheConnectorMixin, OAuthConnectorMixin):
    """Feishu/Lark connector backend.

    Supports both pure bot mode (tenant_access_token) and copilot mode
    (user_access_token via OAuthConnectorMixin).

    Mount prefix: /chat/feishu/

    Folder Structure (2-level hierarchy with hybrid naming):
    - / - Root directory
    - /groups/ - Group chats
      - /groups/{Name} [{chat_id}].yaml - Chat messages
    - /p2p/ - Direct messages
      - /p2p/{Name} [{chat_id}].yaml - DM messages
    """

    _CAPABILITIES = OAUTH_CONNECTOR_CAPABILITIES | frozenset(
        {
            ConnectorCapability.DIRECTORY_LISTING,
            ConnectorCapability.CACHE_BULK_READ,
            ConnectorCapability.CACHE_SYNC,
        }
    )

    FOLDER_TYPES = ["groups", "p2p"]

    use_metadata_listing = True

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "feishu",
        record_store: "RecordStoreABC | None" = None,
        max_messages_per_chat: int = 50,
        metadata_store: Any = None,
    ):
        """Initialize Feishu connector backend.

        Args:
            app_id: Feishu app ID
            app_secret: Feishu app secret
            token_manager_db: Path to TokenManager database
            user_email: Optional user email for copilot mode (user-scoped access)
            provider: OAuth provider name (default: "feishu")
            record_store: Optional RecordStoreABC for content caching
            max_messages_per_chat: Max messages to fetch per chat (default: 50)
            metadata_store: MetastoreABC instance for file_paths table (optional)
        """
        self._init_oauth(token_manager_db, user_email=user_email, provider=provider)

        self.app_id = app_id
        self.app_secret = app_secret
        self.max_messages_per_chat = max_messages_per_chat

        # Store session factory for caching (CacheConnectorMixin)
        self.session_factory = record_store.session_factory if record_store else None

        # Store metadata store for file_paths table
        self.metadata_store = metadata_store

        # Cache for chats: chat_id -> chat_info
        self._chat_cache: dict[str, dict[str, Any]] = {}

        # P2P chat registry: chat_id -> {"name": ..., "chat_id": ...}
        # Populated from webhook events since list_chats API doesn't return P2P chats
        self._p2p_registry: dict[str, dict[str, Any]] = {}

        # VFS mount prefix for matching inbound events
        self._mount_prefix = "/chat/feishu/"

        # Initialize app-level lark client
        self._app_client = self._build_app_client()

        # Register OAuth provider
        self._register_oauth_provider()

        # Register for webhook cache invalidation
        self._register_cache_invalidation()

    def _build_app_client(self) -> Any:
        """Build lark_oapi.Client with app credentials (tenant_access_token)."""
        try:
            import lark_oapi as lark

            return lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        except ImportError:
            raise BackendError(
                "lark-oapi not installed. Install with: pip install lark-oapi",
                backend="feishu",
            ) from None

    def _register_oauth_provider(self) -> None:
        """Register Feishu OAuth provider with TokenManager."""
        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()
            try:
                provider_instance = factory.create_provider(name=self.provider)
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info("Registered OAuth provider '%s' for Feishu backend", self.provider)
            except ValueError as e:
                logger.warning(
                    "OAuth provider '%s' not available: %s. OAuth flow must be initiated manually.",
                    self.provider,
                    e,
                )
        except Exception as e:
            logger.error("Failed to register Feishu OAuth provider: %s", e)

    def _register_cache_invalidation(self) -> None:
        """Register with the Feishu webhook for cache invalidation on inbound events."""
        try:
            from nexus.server.api.v2.routers.feishu_webhook import register_cache_invalidator

            register_cache_invalidator(self.handle_event)
        except ImportError:
            logger.debug("Feishu webhook router not available; cache invalidation disabled")

    def handle_event(self, file_event: Any) -> None:
        """Handle an inbound FileEvent by registering P2P chats and invalidating cache.

        Called by the webhook router when a Feishu event (e.g. new message) arrives.
        For P2P events, registers the chat in the P2P registry.
        For all events, invalidates cached YAML so next read_content() fetches fresh data.

        Args:
            file_event: FileEvent from the webhook router
        """
        path = file_event.path
        if not path.startswith(self._mount_prefix):
            return

        # Extract the backend-relative path (e.g. "groups/oc_xxx.yaml" or "p2p/oc_xxx.yaml")
        backend_path = path[len(self._mount_prefix) :]

        # Detect P2P events and register the chat
        if backend_path.startswith("p2p/"):
            parts = backend_path.split("/")
            if len(parts) >= 2:
                filename = parts[1]
                chat_id = self._parse_chat_id_from_filename(filename)
                if not chat_id:
                    # Webhook uses bare chat_id as filename (e.g. "p2p/oc_xxx.yaml")
                    chat_id = filename.removesuffix(".yaml")
                if chat_id and chat_id not in self._p2p_registry:
                    self._discover_p2p_chat(chat_id)

        if not self._has_caching():
            return

        try:
            self._invalidate_cache(path=backend_path)
            logger.info("Cache invalidated for %s (event: %s)", backend_path, file_event.type)
        except Exception as e:
            logger.warning("Failed to invalidate cache for %s: %s", backend_path, e)

    def _discover_p2p_chat(self, chat_id: str) -> None:
        """Discover and register a P2P chat by fetching its info and member names.

        Called when a P2P event is received from the webhook. Since P2P chats
        have name=None from the API, we resolve the peer name from chat members.

        Args:
            chat_id: P2P chat ID to discover
        """
        try:
            client = self._app_client
            info = get_chat_info(client, chat_id)
            if not info:
                # Still register with fallback name
                self._p2p_registry[chat_id] = {
                    "chat_id": chat_id,
                    "name": chat_id,
                    "chat_mode": "p2p",
                }
                return

            # For P2P chats, resolve the peer (non-bot) member name
            name = info.get("name") or None
            if not name or name == chat_id:
                members = get_chat_members(client, chat_id)
                # Filter out bot members (bot IDs start with "cli_")
                human_members = [
                    m
                    for m in members
                    if m.get("member_id") and not m["member_id"].startswith("cli_")
                ]
                name = human_members[0].get("name", chat_id) if human_members else chat_id

            self._p2p_registry[chat_id] = {
                "chat_id": chat_id,
                "name": name,
                "chat_mode": "p2p",
            }
            logger.info("Discovered P2P chat: %s (%s)", name, chat_id)

        except Exception as e:
            logger.warning("Failed to discover P2P chat %s: %s", chat_id, e)
            self._p2p_registry[chat_id] = {
                "chat_id": chat_id,
                "name": chat_id,
                "chat_mode": "p2p",
            }

    def _get_feishu_client(self, context: "OperationContext | None" = None) -> Any:
        """Get Feishu client configured for the appropriate auth mode.

        In bot mode (no user_email / no context user), returns the app-level client
        using tenant_access_token. In copilot mode, returns a client configured
        with the user's access_token.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            lark_oapi.Client instance
        """
        # Determine if we should use user-scoped access
        effective_user = self.user_email
        if not effective_user and context and context.user_id:
            effective_user = context.user_id

        if not effective_user:
            # Pure bot mode — use app-level client
            return self._app_client

        # Copilot mode — get user access token
        from nexus.lib.sync_bridge import run_sync

        try:
            zone_id = (
                context.zone_id
                if context and hasattr(context, "zone_id") and context.zone_id
                else "root"
            )
            # Validate user has a valid token (raises if not)
            run_sync(
                self.token_manager.get_valid_token(
                    provider=self.provider,
                    user_email=effective_user,
                    zone_id=zone_id,
                )
            )
        except Exception as e:
            logger.warning(
                "Failed to get user token for %s, falling back to bot mode: %s",
                effective_user,
                e,
            )
            return self._app_client

        # User token validated — still use app client (lark-oapi handles token internally)
        return self._app_client

    @property
    def name(self) -> str:
        return "feishu"

    @property
    def user_scoped(self) -> bool:
        """Returns True if running in copilot mode (user-scoped access)."""
        return bool(self.user_email)

    @property
    def has_token_manager(self) -> bool:
        return True

    # --- Hybrid naming helpers ---

    # Pattern: "Name [oc_xxx].yaml" -> captures chat_id from brackets
    _HYBRID_RE = re.compile(r"^.+\[([^\]]+)\]\.yaml$")

    @staticmethod
    def _format_hybrid_filename(name: str, chat_id: str) -> str:
        """Build hybrid filename: '{Name} [{chat_id}].yaml'."""
        return f"{name} [{chat_id}].yaml"

    @classmethod
    def _parse_chat_id_from_filename(cls, filename: str) -> str | None:
        """Extract chat_id from hybrid filename.

        Args:
            filename: e.g. 'General [oc_123].yaml'

        Returns:
            chat_id (e.g. 'oc_123') or None if pattern doesn't match
        """
        m = cls._HYBRID_RE.match(filename)
        return m.group(1) if m else None

    def _ensure_chat_cache(self, context: "OperationContext | None" = None) -> None:
        """Ensure chat cache is populated."""
        if self._chat_cache:
            return
        client = self._get_feishu_client(context)
        chats = list_chats(client, silent=True)
        for chat in chats:
            self._chat_cache[chat["chat_id"]] = chat

    def _format_messages_as_yaml(self, messages: list[dict[str, Any]]) -> bytes:
        """Format messages as YAML bytes."""
        import yaml

        yaml_output = yaml.dump(
            messages,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )
        return yaml_output.encode("utf-8")

    # === Backend interface methods ===

    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content (send message to Feishu chat).

        Content format (YAML or JSON):
            msg_type: text
            content:
              text: "Hello from the AI agent!"

        Args:
            content: Message content as YAML/JSON bytes
            context: Operation context with backend_path indicating target chat

        Returns:
            WriteResult with message_id as content_hash
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Feishu connector requires backend_path in OperationContext for write operations",
                backend="feishu",
            )

        try:
            # Try YAML first, fall back to JSON
            content_str = content.decode("utf-8")
            try:
                import yaml

                message_data = yaml.safe_load(content_str)
            except Exception:
                message_data = json.loads(content_str)

            msg_type = message_data.get("msg_type", "text")
            msg_content = message_data.get("content", {})

            # Ensure content is a JSON string for the API
            if isinstance(msg_content, dict):
                msg_content = json.dumps(msg_content, ensure_ascii=False)

            # Resolve chat_id from hybrid filename: "groups/Name [oc_xxx].yaml"
            path_parts = context.backend_path.strip("/").split("/")
            if len(path_parts) != 2:
                raise BackendError(
                    f"Invalid write path: {context.backend_path}. "
                    "Expected: groups/Name [id].yaml or p2p/Name [id].yaml",
                    backend="feishu",
                )

            folder_type, filename = path_parts
            if not filename.endswith(".yaml"):
                raise BackendError(
                    f"Invalid filename: {filename}. Must end with .yaml",
                    backend="feishu",
                )

            chat_id = self._parse_chat_id_from_filename(filename)
            if not chat_id:
                raise BackendError(
                    f"Cannot resolve chat_id from filename: {filename}. "
                    "Expected format: 'Name [chat_id].yaml'",
                    backend="feishu",
                )

            # Send message
            client = self._get_feishu_client(context)
            result = send_message(client, chat_id, msg_type, msg_content)

            return WriteResult(
                content_hash=result["message_id"],
                size=len(content),
            )

        except BackendError:
            raise
        except json.JSONDecodeError as e:
            raise BackendError(f"Invalid content format: {e}", backend="feishu") from e
        except Exception as e:
            raise BackendError(f"Failed to send Feishu message: {e}", backend="feishu") from e

    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read chat messages as YAML.

        For connector backends, content_hash is ignored — we use backend_path.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            Chat messages as YAML bytes
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Feishu connector requires backend_path in OperationContext",
                backend="feishu",
            )

        backend_path = context.backend_path
        path_parts = backend_path.strip("/").split("/")

        if len(path_parts) != 2:
            raise NexusFileNotFoundError(backend_path)

        folder_type, filename = path_parts

        if folder_type not in self.FOLDER_TYPES:
            raise NexusFileNotFoundError(backend_path)

        if not filename.endswith(".yaml"):
            raise NexusFileNotFoundError(backend_path)

        # Extract chat_id from hybrid filename: "Name [oc_xxx].yaml"
        chat_id = self._parse_chat_id_from_filename(filename)
        if not chat_id:
            raise NexusFileNotFoundError(backend_path)

        # Check cache first
        cache_path = self._get_cache_path(context) or backend_path
        if self._has_caching():
            cached = self._read_from_cache(cache_path, original=True)
            if cached and not cached.stale and cached.content_binary:
                return cached.content_binary

        # Fetch messages
        client = self._get_feishu_client(context)
        messages = list_messages_from_chat(
            client=client,
            chat_id=chat_id,
            limit=self.max_messages_per_chat,
            silent=True,
        )

        if not messages:
            messages = [
                {
                    "_metadata": {
                        "chat_id": chat_id,
                        "status": "no_messages",
                        "message": f"No messages found in chat '{chat_id}'.",
                    }
                }
            ]

        content = self._format_messages_as_yaml(messages)

        # Cache the result
        if self._has_caching():
            try:
                zone_id = getattr(context, "zone_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=IMMUTABLE_VERSION,
                    zone_id=zone_id,
                )
            except Exception as e:
                logger.debug("Feishu cache write failed for %s: %s", cache_path, e)

        return content

    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "Feishu connector does not support message deletion.",
            backend="feishu",
        )

    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        if not context or not context.backend_path:
            return False
        try:
            self.read_content(content_hash, context)
            return True
        except Exception:
            return False

    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        if context is None or not hasattr(context, "backend_path"):
            raise ValueError("Feishu connector requires backend_path in OperationContext")

        if hasattr(context, "virtual_path") and context.virtual_path:
            cached_size = self._get_size_from_cache(context.virtual_path)
            if cached_size is not None:
                return cached_size

        content = self.read_content(content_hash, context)
        return len(content)

    def get_ref_count(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> int:
        return 1

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        try:
            if context and hasattr(context, "backend_path") and context.backend_path:
                backend_path = context.backend_path
            else:
                backend_path = path.lstrip("/")

            if not backend_path.endswith(".yaml"):
                return None
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
        raise BackendError(
            "Feishu connector has a fixed directory structure. Cannot create directories.",
            backend="feishu",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "Feishu connector has a fixed directory structure. Cannot remove directories.",
            backend="feishu",
        )

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        path = path.strip("/")

        if not path:
            return True  # Root is a directory

        path_parts = path.split("/")

        if len(path_parts) == 1:
            return path in self.FOLDER_TYPES

        # .yaml files at depth 2 are files
        if len(path_parts) == 2:
            return False

        return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents.

        - Root: ["groups/", "p2p/"]
        - groups/: List of group chat YAML files
        - p2p/: List of P2P chat YAML files
        """
        try:
            path = path.strip("/")

            if not path:
                return [f"{folder}/" for folder in self.FOLDER_TYPES]

            path_parts = path.split("/")

            if len(path_parts) == 1:
                folder_type = path_parts[0]
                if folder_type not in self.FOLDER_TYPES:
                    raise FileNotFoundError(f"Directory not found: {path}")

                if folder_type == "p2p":
                    # P2P chats are not returned by list_chats API.
                    # Return chats discovered from webhook events.
                    return [
                        self._format_hybrid_filename(
                            info.get("name", info["chat_id"]), info["chat_id"]
                        )
                        for info in self._p2p_registry.values()
                    ]

                # Group chats — use list_chats API
                client = self._get_feishu_client(context)
                chats = list_chats(client, silent=True)

                # Update cache
                for chat in chats:
                    self._chat_cache[chat["chat_id"]] = chat

                return [
                    self._format_hybrid_filename(chat.get("name", chat["chat_id"]), chat["chat_id"])
                    for chat in chats
                ]

            raise FileNotFoundError(f"Directory not found: {path}")

        except FileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="feishu",
                path=path,
            ) from e
