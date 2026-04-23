"""Gmail connector backend — PathAddressingEngine + GmailTransport composition.

Architecture (Transport × Addressing):
    PathGmailBackend(PathAddressingEngine)
        └── GmailTransport(Transport)
              ├── Gmail API calls (I/O)
              └── OAuth token from OperationContext

This follows the same pattern as PathS3Backend, PathGCSBackend:
Transport handles raw I/O; PathAddressingEngine handles addressing,
path security, and content operations.

Storage structure (2-level hierarchy):
    /
    ├── SENT/                          # Sent emails
    │   ├── {thread_id}-{msg_id}.yaml  # Email metadata + content
    │   ├── _new.yaml                  # Write here to send new email
    │   ├── _reply.yaml                # Write here to reply to thread
    │   └── _forward.yaml              # Write here to forward message
    ├── STARRED/                       # Starred emails in INBOX
    ├── IMPORTANT/                     # Important emails in INBOX
    ├── INBOX/                         # Remaining inbox emails
    ├── DRAFTS/                        # Email drafts
    │   └── _new.yaml                  # Write here to create draft
    └── TRASH/                         # Trashed emails
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from nexus.backends.base.path_addressing_engine import PathAddressingEngine
from nexus.backends.base.registry import ArgType, ConnectionArg, register_connector
from nexus.backends.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    OpTraits,
    ReadmeDocMixin,
    Reversibility,
    TraitBasedMixin,
    ValidatedMixin,
)
from nexus.backends.connectors.gmail.errors import ERROR_REGISTRY
from nexus.backends.connectors.gmail.schemas import (
    DraftEmailSchema,
    ForwardEmailSchema,
    ReplyEmailSchema,
    SendEmailSchema,
)
from nexus.backends.connectors.gmail.transport import LABEL_FOLDERS, GmailTransport
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES, BackendFeature
from nexus.contracts.constants import IMMUTABLE_VERSION
from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


@register_connector("gmail_connector")
class PathGmailBackend(
    PathAddressingEngine,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
    CheckpointMixin,
):
    """Gmail connector: PathAddressingEngine + GmailTransport composition.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Label-based folder structure (INBOX/, SENT/, STARRED/, etc.)
    - Batch download via Gmail batch API
    - Immutable version for cache optimization
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = OAUTH_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.README_DOC,
        }
    )

    # Gmail system labels exposed as folders (in priority order)
    LABEL_FOLDERS = LABEL_FOLDERS

    # Skill documentation settings
    SKILL_NAME = "gmail"

    # ValidatedMixin config — maps operation name → Pydantic schema
    SCHEMAS = {
        "send_email": SendEmailSchema,
        "reply_email": ReplyEmailSchema,
        "forward_email": ForwardEmailSchema,
        "create_draft": DraftEmailSchema,
    }

    # Maps (label, sentinel) → operation name for path-based dispatch
    _OPERATION_MAP: ClassVar[dict[tuple[str, str], str]] = {
        ("SENT", "_new"): "send_email",
        ("SENT", "_reply"): "reply_email",
        ("SENT", "_forward"): "forward_email",
        ("DRAFTS", "_new"): "create_draft",
    }

    # Operation traits for trait-based validation
    OPERATION_TRAITS = {
        "send_email": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "reply_email": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "forward_email": OpTraits(
            reversibility=Reversibility.NONE,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "create_draft": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            checkpoint=True,
            intent_min_length=10,
        ),
    }

    # Error registry for self-correcting messages
    ERROR_REGISTRY = ERROR_REGISTRY

    # Connection arguments for registry-based instantiation
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
        "provider": ConnectionArg(
            type=ArgType.STRING,
            description="OAuth provider name from config",
            required=False,
            default="gmail",
        ),
        "max_message_per_label": ConnectionArg(
            type=ArgType.INTEGER,
            description="Maximum number of messages to fetch per label folder",
            required=False,
            default=200,
        ),
    }

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "gmail",
        record_store: "RecordStoreABC | None" = None,
        max_message_per_label: int = 200,
        metadata_store: Any = None,
        encryption_key: str | None = None,
        pool: "Any | None" = None,
    ):
        # 1. Initialize OAuth (sets self.token_manager, self.provider, etc.)
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )

        # 2. Create GmailTransport with the token manager
        gmail_transport = GmailTransport(
            token_manager=self.token_manager,
            provider=provider,
            user_email=user_email,
            max_message_per_label=max_message_per_label,
        )
        self._gmail_transport = gmail_transport

        # 3. Initialize PathAddressingEngine (no prefix, no bucket)
        PathAddressingEngine.__init__(
            self,
            transport=gmail_transport,
            backend_name="gmail",
        )

        # 4. Cache and metadata setup
        self.session_factory = record_store.session_factory if record_store else None
        self.metadata_store = metadata_store
        self.max_message_per_label = max_message_per_label

        # 5. Initialize CheckpointMixin state
        self._checkpoints: dict[str, Any] = {}

        # 6. Credential pool (multi-account failover, Issue #3723)
        self._pool = pool

        # 7. Register OAuth provider using factory
        self._register_oauth_provider()

    # -- Properties --

    @property
    def user_scoped(self) -> bool:
        return True

    @property
    def has_token_manager(self) -> bool:
        return True

    # -- _register_oauth_provider (same as OAuthConnectorBase) --

    _PROVIDER_ALIASES: dict[str, list[str]] = {
        "google": ["gmail", "gcalendar", "google-drive", "google-cloud-storage"],
    }

    def _register_oauth_provider(self) -> None:
        try:
            import importlib as _il

            OAuthProviderFactory = _il.import_module(
                "nexus.bricks.auth.oauth.factory"
            ).OAuthProviderFactory

            factory = OAuthProviderFactory()
            candidates = [self.provider]
            backend_name = getattr(self, "name", "")
            if backend_name and backend_name != self.provider:
                candidates.append(backend_name)
            for alias, targets in self._PROVIDER_ALIASES.items():
                if self.provider == alias:
                    candidates.extend(targets)

            for candidate in candidates:
                try:
                    provider_instance = factory.create_provider(name=candidate)
                    self.token_manager.register_provider(self.provider, provider_instance)
                    logger.info(
                        "Registered OAuth provider '%s' (resolved from '%s') for %s backend",
                        candidate,
                        self.provider,
                        self.name,
                    )
                    return
                except ValueError:
                    continue

            logger.warning(
                "OAuth provider '%s' not available (tried: %s). "
                "OAuth flow must be initiated manually via the Integrations page.",
                self.provider,
                ", ".join(candidates),
            )
        except Exception as e:
            logger.error("Failed to register OAuth provider: %s", e)

    # -- Skill docs --

    def generate_readme(self, mount_path: str) -> str:
        import importlib.resources as resources

        try:
            readme_md_content = (
                resources.files("nexus.backends.connectors.gmail")
                .joinpath("README.md")
                .read_text(encoding="utf-8")
            )
            readme_md_content = readme_md_content.replace("`/mnt/gmail/`", f"`{mount_path}`")
            readme_md_content = readme_md_content.replace(
                "/mnt/gmail/", mount_path.rstrip("/") + "/"
            )
            return readme_md_content
        except Exception as e:
            logger.warning(f"Failed to load static README.md: {e}, using auto-generated")
            return super().generate_readme(mount_path)

    # NOTE (Issue #3728): ``write_readme`` override removed along with the
    # base class method.  Gmail's static README.md (if present) is now read
    # via ``generate_readme`` above, and the virtual ``.readme/`` overlay in
    # ``schema_generator`` serves the result on-demand.

    # =================================================================
    # Content operations — override PathAddressingEngine for Gmail
    # =================================================================

    def _bind_transport(self, context: "OperationContext | None") -> None:
        """Bind the transport to the current request context (OAuth token)."""
        self._transport = self._gmail_transport.with_context(context)

    def _resolve_operation(self, path: str) -> tuple[str, str, str]:
        """Resolve a backend path to ``(operation_name, label, sentinel)``.

        Raises BackendError if the path does not match any write operation.
        """
        label, _thread_id, sentinel = GmailTransport._parse_key(path)
        if not label or not sentinel or not sentinel.startswith("_"):
            raise BackendError(
                f"Invalid write path: {path}. "
                "Expected: SENT/_new.yaml, SENT/_reply.yaml, SENT/_forward.yaml, "
                "or DRAFTS/_new.yaml",
                backend="gmail",
            )

        operation = self._OPERATION_MAP.get((label, sentinel))
        if operation is None:
            raise BackendError(
                f"No operation for path: {path}. "
                "Supported write paths: SENT/_new.yaml, SENT/_reply.yaml, "
                "SENT/_forward.yaml, DRAFTS/_new.yaml",
                backend="gmail",
            )
        return operation, label, sentinel

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Handle send/reply/forward/draft with validation + checkpoints.

        The write path is determined by ``context.backend_path``:
        - ``SENT/_new.yaml``     → send_email
        - ``SENT/_reply.yaml``   → reply_email
        - ``SENT/_forward.yaml`` → forward_email
        - ``DRAFTS/_new.yaml``   → create_draft
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Gmail connector requires backend_path in OperationContext.",
                backend="gmail",
            )

        self._bind_transport(context)

        path = context.backend_path.strip("/")
        operation, label, sentinel = self._resolve_operation(path)

        # Parse YAML content for validation
        data = GmailTransport._parse_yaml_content(content)

        # Trait-based validation (intent, confirm)
        warnings = self.validate_traits(operation, data)
        for w in warnings:
            logger.warning("Gmail %s warning: %s", operation, w)

        # Schema validation
        self.validate_schema(operation, data)

        # Create checkpoint
        checkpoint = self.create_checkpoint(operation, metadata={"path": path})

        try:
            blob_path = self._get_key_path(f"{label}/{sentinel}.yaml")
            result_id = self._transport.store(blob_path, content) or ""

            if checkpoint:
                self.complete_checkpoint(
                    checkpoint.checkpoint_id,
                    {"message_id": result_id, "operation": operation},
                )
            logger.info("Gmail %s completed: %s", operation, result_id)
            return WriteResult(content_id=result_id, version=result_id, size=len(content))
        except Exception as e:
            if checkpoint:
                self.clear_checkpoint(checkpoint.checkpoint_id)
            if isinstance(e, (AuthenticationError, BackendError, NexusFileNotFoundError)):
                raise
            raise BackendError(
                f"Failed to execute {operation}: {e}",
                backend="gmail",
            ) from e

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        if not context or not context.backend_path:
            raise BackendError(
                "Gmail connector requires backend_path in OperationContext.",
                backend="gmail",
            )

        if self._pool is None:
            # No pool — single-account behaviour (unchanged)
            self._bind_transport(context)
            return super().read_content(content_id, context)

        # Pool-based: rotate credentials on rate-limit.
        # user_email_override routes the transport to the selected profile's
        # account (e.g. a different service account with the same data access).
        # bypass_exceptions prevents path-level errors (deleted messages, stale
        # paths) from being misclassified as credential failures.
        # Note: pool correctness (only accounts with appropriate access) is the
        # operator's responsibility when building the CredentialPool.
        from nexus.bricks.auth.classifiers.google import classify_google_error
        from nexus.bricks.auth.profile import AuthProfile

        backend_path: str = context.backend_path  # narrowed: checked non-None above

        def _call(profile: AuthProfile) -> bytes:
            # Use a *local* transport so concurrent pool calls don't race on
            # self._transport (PathAddressingEngine stores it as instance state).
            transport = self._gmail_transport.with_context(
                context, user_email_override=profile.account_identifier
            )
            blob_path = self._get_key_path(backend_path)
            content, _ = transport.fetch(blob_path, None)
            return content

        return bytes(
            self._pool.execute_sync(
                _call,
                classify_google_error,
                bypass_exceptions=(NexusFileNotFoundError,),
            )
        )

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        """Trash a Gmail message (recoverable — not permanent delete).

        The message is moved to Gmail Trash and auto-deleted after 30 days.
        """
        if not context or not context.backend_path:
            raise BackendError(
                "Gmail connector requires backend_path in OperationContext.",
                backend="gmail",
            )

        self._bind_transport(context)

        path = context.backend_path.strip("/")
        _label, _thread_id, message_id = GmailTransport._parse_key(path)

        if not message_id or (message_id and message_id.startswith("_")):
            raise BackendError(
                f"Invalid path for trash: {path}. Expected LABEL/threadId-msgId.yaml",
                backend="gmail",
            )

        blob_path = self._get_key_path(path)
        try:
            self._transport.remove(blob_path)
            logger.info("Trashed Gmail message via connector: %s", message_id)
        except Exception as e:
            if isinstance(e, (AuthenticationError, BackendError, NexusFileNotFoundError)):
                raise
            raise BackendError(
                f"Failed to trash message: {e}",
                backend="gmail",
            ) from e

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        if not context or not context.backend_path:
            return False
        self._bind_transport(context)
        return super().content_exists(content_id, context)

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        if context is None or not hasattr(context, "backend_path"):
            raise ValueError("Gmail connector requires backend_path in OperationContext")

        # Bind transport and delegate
        self._bind_transport(context)
        return super().get_content_size(content_id, context)

    # =================================================================
    # Version support (emails are immutable)
    # =================================================================

    def get_version(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> str | None:
        try:
            backend_path = (
                context.backend_path
                if context and hasattr(context, "backend_path") and context.backend_path
                else path.lstrip("/")
            )

            if not backend_path.endswith(".yaml"):
                return None

            path_parts = backend_path.split("/")
            if len(path_parts) == 2 and path_parts[0] in self.LABEL_FOLDERS:
                filename = path_parts[1]
            elif len(path_parts) == 1:
                filename = path_parts[0]
            else:
                return None

            if "-" not in filename.removesuffix(".yaml"):
                return None

            return IMMUTABLE_VERSION
        except Exception as e:
            logger.debug("Gmail version check failed: %s", e)
            return None

    def batch_get_versions(
        self,
        backend_paths: list[str],
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, str | None]:
        results: dict[str, str | None] = {}
        for backend_path in backend_paths:
            ctx = contexts.get(backend_path) if contexts else None
            results[backend_path] = self.get_version(backend_path, context=ctx)
        return results

    # =================================================================
    # Directory operations — override for Gmail virtual directories
    # =================================================================

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        from nexus.backends.connectors.gmail.transport import _GMAIL_CATEGORY_FOLDERS

        path = path.strip("/")
        if not path:
            return True
        if path in self.LABEL_FOLDERS:
            return True
        # INBOX/<category> virtual sub-directories — must stay in sync
        # with list_dir()'s acceptance set so stat/traversal behaviour
        # doesn't diverge from listability.
        parts = path.split("/")
        return len(parts) == 2 and parts[0] == "INBOX" and parts[1] in _GMAIL_CATEGORY_FOLDERS

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents via GmailTransport.list_keys()."""
        try:
            from nexus.backends.connectors.gmail.transport import _GMAIL_CATEGORY_FOLDERS

            path = path.strip("/")

            # Root directory — list label folders (no API call needed)
            if not path:
                return [f"{label}/" for label in self.LABEL_FOLDERS]

            path_parts = path.split("/")
            is_label = path in self.LABEL_FOLDERS
            is_inbox_category = (
                len(path_parts) == 2
                and path_parts[0] == "INBOX"
                and path_parts[1] in _GMAIL_CATEGORY_FOLDERS
            )
            if not (is_label or is_inbox_category):
                raise FileNotFoundError(f"Directory not found: {path}")

            if self._pool is None:
                # Single-account path
                self._bind_transport(context)
                keys, prefixes = self._transport.list_keys(prefix=path, delimiter="/")
            else:
                # Pool-based: rotate credentials on rate-limit.
                # user_email_override routes the transport to the selected profile's
                # account. bypass_exceptions prevents directory-not-found from
                # poisoning healthy credentials.
                from nexus.bricks.auth.classifiers.google import classify_google_error
                from nexus.bricks.auth.profile import AuthProfile

                def _list(profile: AuthProfile) -> tuple[list[str], list[str]]:
                    transport = self._gmail_transport.with_context(
                        context, user_email_override=profile.account_identifier
                    )
                    return transport.list_keys(prefix=path, delimiter="/")

                keys, prefixes = self._pool.execute_sync(
                    _list,
                    classify_google_error,
                    bypass_exceptions=(NexusFileNotFoundError,),
                )

            # Strip label prefix from keys: "LABEL/thread-msg.yaml" → "thread-msg.yaml"
            label_prefix = f"{path}/"
            leaves: list[str] = []
            dirs: list[str] = []
            for key in keys:
                name = key[len(label_prefix) :] if key.startswith(label_prefix) else key
                if name:
                    leaves.append(name)
            # Forward virtual category prefixes (INBOX/PRIMARY/, INBOX/UPDATES/, ...)
            for pref in prefixes:
                name = pref[len(label_prefix) :] if pref.startswith(label_prefix) else pref
                if name:
                    dirs.append(name)
            # Message filenames are `{YYYY-MM-DD}_{subject}__{ids}.yaml`, so
            # reverse-lex = reverse-chronological = newest first — matches
            # every real email client.  Directory entries (category
            # sub-labels) stay alphabetical.
            return sorted(dirs) + sorted(leaves, reverse=True)

        except FileNotFoundError:
            raise
        except AuthenticationError:
            # Propagate auth-required signal unchanged so callers can drive the
            # OAuth flow (Issue #3822). Wrapping here silently returns [].
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gmail",
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
            "Gmail connector is read-only. Cannot create directories.",
            backend="gmail",
        )

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        raise BackendError(
            "Gmail connector is read-only. Cannot remove directories.",
            backend="gmail",
        )

    # =================================================================
    # Batch download (connector-specific — uses Gmail batch API)
    # =================================================================

    def _bulk_download_contents(
        self,
        paths: list[str],
        contexts: dict[str, "OperationContext"] | None = None,
    ) -> dict[str, bytes]:
        # Extract message IDs from paths
        path_to_message_id: dict[str, str] = {}
        for path in paths:
            _label, _thread_id, message_id = GmailTransport._parse_key(path)
            if message_id:
                path_to_message_id[path] = message_id

        if not path_to_message_id:
            return {}

        # Bind transport to context for OAuth
        context = None
        if contexts and paths:
            context = contexts.get(paths[0])
        self._bind_transport(context)

        # Use Gmail transport's batch fetch (not on Transport protocol)
        message_ids = list(path_to_message_id.values())
        # _bind_transport already set self._transport to a context-bound clone;
        # but fetch_batch is Gmail-specific, so cast via _gmail_transport.
        bound = self._gmail_transport.with_context(context)
        msg_id_to_content = bound.fetch_batch(message_ids)

        # Map back to paths
        results: dict[str, bytes] = {}
        for path, message_id in path_to_message_id.items():
            if message_id in msg_id_to_content:
                results[path] = msg_id_to_content[message_id]
        return results
