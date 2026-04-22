"""Google Drive connector — PathAddressingEngine + DriveTransport composition.

Architecture (Transport x Addressing):
    PathGDriveBackend(PathAddressingEngine)
        +-- DriveTransport(Transport)
              +-- Drive API calls (I/O)
              +-- OAuth token from OperationContext
              +-- Folder ID caching + resolution

This follows the same pattern as PathGmailBackend and PathCalendarBackend:
Transport handles raw I/O; PathAddressingEngine handles addressing,
path security, and content operations.

Storage structure:
    Google Drive/
    +-- nexus-data/           # Root folder (configurable)
    |   +-- workspace/
    |   |   +-- file.txt      # Stored at actual path in Drive
    |   |   +-- data/
    |   |       +-- output.json
    |   +-- reports/
    |       +-- report.gdoc   # Google Docs file (auto-exported)
"""

from __future__ import annotations

import logging
<<<<<<< HEAD
<<<<<<< HEAD
import time
from typing import TYPE_CHECKING, Any, ClassVar
=======
from typing import TYPE_CHECKING, ClassVar
>>>>>>> c25f7f03d (feat: reapply 36 lost commits — async→sync, dead code cleanup, Rust dispatch simplification)
=======
from typing import TYPE_CHECKING, Any, ClassVar
>>>>>>> 5d325f31e (fix: nuclear restore 168 files + rustfmt)

from nexus.backends.base.backend import HandlerStatusResponse
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
from nexus.backends.connectors.gdrive.transport import DriveTransport
from nexus.backends.connectors.gws.schemas import (
    DeleteFileSchema,
    UpdateFileSchema,
    UploadFileSchema,
)
from nexus.backends.connectors.oauth import OAuthConnectorMixin
from nexus.contracts.backend_features import OAUTH_BACKEND_FEATURES, BackendFeature
from nexus.contracts.exceptions import AuthenticationError, BackendError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


@register_connector("gdrive_connector")
class PathGDriveBackend(
    PathAddressingEngine,
    OAuthConnectorMixin,
    ReadmeDocMixin,
    ValidatedMixin,
    TraitBasedMixin,
):
    """Google Drive connector: PathAddressingEngine + DriveTransport composition.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Direct path mapping (files stored at actual paths in Drive)
    - Google Workspace file export (Docs/Sheets/Slides)
    - Folder hierarchy maintained via DriveTransport
    - Automatic token refresh
    - Shared Drive support (optional)
    """

    _BACKEND_FEATURES: ClassVar[frozenset[BackendFeature]] = OAUTH_BACKEND_FEATURES | frozenset(
        {
            BackendFeature.README_DOC,
        }
    )

    # =========================================================================
    # Mixin Configuration
    # =========================================================================

    # ReadmeDocMixin config
    SKILL_NAME = "gdrive"
    # Drive stores arbitrary user files at arbitrary paths, so a real
    # ``.readme/`` directory in the user's Drive must shadow the
    # auto-generated virtual tree (Issue #3728 finding #9).
    VIRTUAL_README_DEFERS_TO_BACKEND: bool = True

    # ValidatedMixin config
    SCHEMAS = {
        "upload_file": UploadFileSchema,
        "update_file": UpdateFileSchema,
        "delete_file": DeleteFileSchema,
    }

    # TraitBasedMixin config
    OPERATION_TRAITS = {
        "upload_file": OpTraits(
            reversibility=Reversibility.FULL,
            confirm=ConfirmLevel.INTENT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "update_file": OpTraits(
            reversibility=Reversibility.PARTIAL,
            confirm=ConfirmLevel.EXPLICIT,
            checkpoint=True,
            intent_min_length=10,
        ),
        "delete_file": OpTraits(
            reversibility=Reversibility.PARTIAL,  # Can restore from trash
            confirm=ConfirmLevel.USER,
            checkpoint=True,
            intent_min_length=10,
        ),
    }

    # Error registry for self-correcting messages
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "MISSING_FILE_ID": ErrorDef(
            message="Update and delete operations require a file_id",
            readme_section="update-file",
            fix_example="file_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms",
        ),
    }

    user_scoped = True

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
        "root_folder": ConnectionArg(
            type=ArgType.STRING,
            description="Root folder name in Google Drive",
            required=False,
            default="nexus-data",
        ),
        "use_shared_drives": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Whether to use shared drives",
            required=False,
            default=False,
        ),
        "shared_drive_id": ConnectionArg(
            type=ArgType.STRING,
            description="Shared drive ID (if use_shared_drives=True)",
            required=False,
        ),
        "provider": ConnectionArg(
            type=ArgType.STRING,
            description="OAuth provider name from config",
            required=False,
            default="google-drive",
        ),
    }

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        root_folder: str = "nexus-data",
        use_shared_drives: bool = False,
        shared_drive_id: str | None = None,
        provider: str = "google-drive",
        encryption_key: str | None = None,
        pool: Any = None,  # CredentialPool | None — see Issue #3723 for migration guide
    ):
        # 1. Initialize OAuth (sets self.token_manager, self.provider, etc.)
        self._pool = pool  # stored for future migrate_to_pool() call (Issue #3723)
        self._init_oauth(
            token_manager_db,
            user_email=user_email,
            provider=provider,
            encryption_key=encryption_key,
        )

        # 2. Create DriveTransport with the token manager
        drive_transport = DriveTransport(
            token_manager=self.token_manager,
            provider=provider,
            user_email=user_email,
            root_folder=root_folder,
            use_shared_drives=use_shared_drives,
            shared_drive_id=shared_drive_id,
        )
        self._drive_transport = drive_transport

        # 3. Initialize PathAddressingEngine (no prefix — key IS the path)
        PathAddressingEngine.__init__(
            self,
            transport=drive_transport,
            backend_name="gdrive",
        )

        # 4. Store config for check_connection and other uses
        self.root_folder = root_folder
        self.use_shared_drives = use_shared_drives
        self.shared_drive_id = shared_drive_id

        # 5. Register OAuth provider using factory
        self._register_oauth_provider()

    # -- Properties --

    @property
    def has_token_manager(self) -> bool:
        """GDrive connector manages OAuth tokens."""
        return True

    # -- OAuth provider registration --

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

    # =================================================================
    # Health check
    # =================================================================

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        """Verify Google Drive connection is healthy.

        Checks that OAuth tokens are valid and the Drive API is accessible.
        """
        import time

        start = time.perf_counter()

        # Determine user email
        if self.user_email:
            user_email = self.user_email
        elif context and context.user_id:
            user_email = context.user_id
        else:
            return HandlerStatusResponse(
                success=False,
                error_message="No user context provided for user-scoped backend",
                latency_ms=(time.perf_counter() - start) * 1000,
                details={"backend": self.name, "user_scoped": True},
            )

        try:
            from nexus.lib.sync_bridge import run_sync

            zone_id = (
                context.zone_id
                if context and hasattr(context, "zone_id") and context.zone_id
                else "root"
            )
            access_token = run_sync(
                self.token_manager.get_valid_token(
                    provider=self.provider,
                    user_email=user_email,
                    zone_id=zone_id,
                )
            )

            if not access_token:
                return HandlerStatusResponse(
                    success=False,
                    error_message=f"No valid OAuth token for user {user_email}",
                    latency_ms=(time.perf_counter() - start) * 1000,
                    details={
                        "backend": self.name,
                        "user_email": user_email,
                        "zone_id": zone_id,
                    },
                )

            # Verify by calling Drive API (lightweight about() call)
            try:
                from google.oauth2.credentials import Credentials
                from googleapiclient.discovery import build

                creds = Credentials(token=access_token)
                service = build("drive", "v3", credentials=creds)
                about = service.about().get(fields="user").execute()
                drive_user = about.get("user", {}).get("emailAddress", "unknown")
            except Exception as api_error:
                return HandlerStatusResponse(
                    success=False,
                    error_message=f"Drive API check failed: {api_error}",
                    latency_ms=(time.perf_counter() - start) * 1000,
                    details={
                        "backend": self.name,
                        "user_email": user_email,
                        "token_valid": True,
                        "api_error": str(api_error),
                    },
                )

            latency_ms = (time.perf_counter() - start) * 1000
            return HandlerStatusResponse(
                success=True,
                latency_ms=latency_ms,
                details={
                    "backend": self.name,
                    "user_email": user_email,
                    "drive_user": drive_user,
                    "zone_id": zone_id,
                    "root_folder": self.root_folder,
                },
            )

        except Exception as e:
            return HandlerStatusResponse(
                success=False,
                error_message=str(e),
                latency_ms=(time.perf_counter() - start) * 1000,
                details={"backend": self.name, "user_email": user_email},
            )

    # =================================================================
    # Transport context binding
    # =================================================================

    def _bind_transport(self, context: "OperationContext | None") -> None:
        """Bind the transport to the current request context (OAuth token)."""
        self._transport = self._drive_transport.with_context(context)

    # =================================================================
    # Content operations — override PathAddressingEngine
    # =================================================================

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content to Google Drive.

        Delegates to DriveTransport which handles folder resolution,
        file upsert (create or update), and shared drive support.
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        self._bind_transport(context)

        # Delegate to PathAddressingEngine which calls transport.store
        super().write_content(content, content_id, offset=offset, context=context)

        # Return hash-based result (Drive doesn't expose version IDs)
        content_hash = hash_content(content)
        return WriteResult(content_id=content_hash, version=content_hash, size=len(content))

    def read_content(self, content_id: str, context: "OperationContext | None" = None) -> bytes:
        """Read content from Google Drive by path.

        Binds transport to context for OAuth, then delegates to
        PathAddressingEngine which calls transport.fetch.
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        self._bind_transport(context)
        return super().read_content(content_id, context)

    def delete_content(self, content_id: str, context: "OperationContext | None" = None) -> None:
        """Delete content from Google Drive (move to trash).

        Binds transport to context for OAuth, then delegates to
        PathAddressingEngine which calls transport.remove.
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        self._bind_transport(context)
        super().delete_content(content_id, context)

    def content_exists(self, content_id: str, context: "OperationContext | None" = None) -> bool:
        if context is None or not hasattr(context, "backend_path"):
            return False
        self._bind_transport(context)
        return super().content_exists(content_id, context)

    def get_content_size(self, content_id: str, context: "OperationContext | None" = None) -> int:
        if context is None or not hasattr(context, "backend_path"):
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )
        self._bind_transport(context)
        return super().get_content_size(content_id, context)

    # =================================================================
    # Directory operations — override for Drive folder semantics
    # =================================================================

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory (folder) in Google Drive.

        Uses DriveTransport.is_folder() which resolves the path through
        the folder hierarchy.
        """
        bound = self._drive_transport.with_context(context)
        return bound.is_folder(path)

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """List directory contents from Google Drive.

        Returns file names and folder names (with trailing '/').
        """
        try:
            path = path.strip("/")
            self._bind_transport(context)
            bound = self._drive_transport.with_context(context)
            service = bound._get_drive_service()
            folder_id, path_prefix = bound._resolve_list_prefix(service, path)
            if folder_id is None:
                return []

            blob_keys: list[str] = []
            common_prefixes: list[str] = []
            next_page_token: str | None = None
            seen_page_tokens: set[str] = set()
            pages_fetched = 0
            started_at = time.monotonic()

            while True:
                page_blobs, page_prefixes, next_page_token = bound._list_page_under_folder(
                    service,
                    folder_id,
                    path_prefix,
                    page_token=next_page_token,
                    page_size=1000,
                )
                blob_keys.extend(page_blobs)
                common_prefixes.extend(page_prefixes)
                pages_fetched += 1

                if not next_page_token:
                    break
                if next_page_token in seen_page_tokens:
                    raise BackendError(
                        f"Drive listing incomplete for '{path}': "
                        f"repeated next_page_token={next_page_token}",
                        backend="gdrive",
                        path=path,
                    )
                seen_page_tokens.add(next_page_token)

                limit_reason: str | None = None
                if (
                    bound._LIST_KEYS_MAX_ELAPSED_SECONDS is not None
                    and (time.monotonic() - started_at) >= bound._LIST_KEYS_MAX_ELAPSED_SECONDS
                ):
                    limit_reason = (
                        f"max_elapsed_seconds={bound._LIST_KEYS_MAX_ELAPSED_SECONDS} reached"
                    )
                elif (
                    bound._LIST_KEYS_MAX_PAGES is not None
                    and pages_fetched >= bound._LIST_KEYS_MAX_PAGES
                ):
                    limit_reason = f"max_pages={bound._LIST_KEYS_MAX_PAGES} reached"
                elif (
                    bound._LIST_KEYS_MAX_ITEMS is not None
                    and (len(blob_keys) + len(common_prefixes)) >= bound._LIST_KEYS_MAX_ITEMS
                ):
                    limit_reason = f"max_items={bound._LIST_KEYS_MAX_ITEMS} reached"

                if limit_reason:
                    message = (
                        f"Drive listing incomplete for '{path}': {limit_reason}. "
                        f"next_page_token={next_page_token}"
                    )
                    if bound._LIST_KEYS_FAIL_ON_TRUNCATION:
                        raise BackendError(message, backend="gdrive", path=path)
                    logger.warning(message)
                    break

            # Build entry list: folder names with trailing /, file names without
            entries: list[str] = []

            for prefix_path in common_prefixes:
                name = (
                    prefix_path[len(path_prefix) :].rstrip("/")
                    if path_prefix
                    else prefix_path.rstrip("/")
                )
                if name:
                    entries.append(name + "/")

            for blob_key in blob_keys:
                name = blob_key[len(path_prefix) :] if path_prefix else blob_key
                if name:
                    entries.append(name)

            return sorted(entries)

        except FileNotFoundError:
            raise
        except AuthenticationError:
            # Issue #3822: propagate auth-required signal unchanged so
            # callers can read ``.provider`` / ``.user_email`` / ``.auth_url``
            # and drive the OAuth flow.  Wrapping into BackendError here is
            # what made ``fs.ls`` silently return [].
            raise
        except Exception as e:
            if "not found" in str(e).lower():
                raise FileNotFoundError(f"Directory not found: {path}") from e
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gdrive",
                path=path,
            ) from e

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory in Google Drive.

        Delegates folder creation to DriveTransport.mkdir_path().
        """
        path = path.strip("/")
        if not path:
            return

        self._bind_transport(context)

        bound = self._drive_transport.with_context(context)

        # Check if folder already exists
        if bound.is_folder(path):
            if not exist_ok:
                raise BackendError(
                    f"Directory already exists: {path}",
                    backend="gdrive",
                )
            return

        bound.mkdir_path(path, parents=parents)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove directory from Google Drive (move to trash).

        Delegates to DriveTransport.remove_folder().
        """
        bound = self._drive_transport.with_context(context)
        bound.remove_folder(path, recursive=recursive)
