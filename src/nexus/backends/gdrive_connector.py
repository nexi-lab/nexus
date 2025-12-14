"""Google Drive connector backend with OAuth 2.0 authentication.

This is a connector backend that maps files directly to Google Drive using OAuth,
making it a user-scoped backend (each user has their own Drive).

Use case: Personal Google Drive integration where users mount their own Drive
folders into Nexus workspace.

Storage structure:
    Google Drive/
    ├── nexus-data/           # Root folder (configurable)
    │   ├── workspace/
    │   │   ├── file.txt      # Stored at actual path in Drive
    │   │   └── data/
    │   │       └── output.json
    │   └── reports/
    │       └── report.gdoc   # Google Docs file

Key features:
- OAuth 2.0 authentication (user-scoped)
- Direct path mapping (files stored at actual paths)
- Automatic token refresh via TokenManager
- Google Docs/Sheets/Slides export support
- Folder hierarchy maintained
- Shared Drive support (optional)

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - Automatic refresh when expired
"""

import io
import logging
import mimetypes
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.registry import ArgType, ConnectionArg, register_connector
from nexus.core.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


# Google Drive MIME types
GOOGLE_MIME_TYPES = {
    "application/vnd.google-apps.document": "Google Docs",
    "application/vnd.google-apps.spreadsheet": "Google Sheets",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.drawing": "Google Drawings",
    "application/vnd.google-apps.form": "Google Forms",
    "application/vnd.google-apps.folder": "Folder",
}

# Export formats for Google Workspace files
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "odt": "application/vnd.oasis.opendocument.text",
        "html": "text/html",
        "txt": "text/plain",
        "markdown": "text/markdown",  # Custom conversion
    },
    "application/vnd.google-apps.spreadsheet": {
        "pdf": "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ods": "application/vnd.oasis.opendocument.spreadsheet",
        "csv": "text/csv",
        "tsv": "text/tab-separated-values",
    },
    "application/vnd.google-apps.presentation": {
        "pdf": "application/pdf",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "odp": "application/vnd.oasis.opendocument.presentation",
        "txt": "text/plain",
    },
}


@register_connector(
    "gdrive_connector",
    description="Google Drive with OAuth 2.0 authentication",
    category="oauth",
    requires=["google-api-python-client", "google-auth-oauthlib"],
)
class GoogleDriveConnectorBackend(Backend):
    """
    Google Drive connector backend with OAuth 2.0 authentication.

    This backend stores files at their actual paths in Google Drive, making it
    user-scoped (each user has their own Drive access).

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Direct path mapping (file.txt → file.txt in Drive)
    - Google Workspace file export (Docs/Sheets/Slides)
    - Folder hierarchy maintained
    - Automatic token refresh
    - Shared Drive support (optional)

    Limitations:
    - No automatic deduplication (Drive handles this)
    - Requires OAuth tokens for each user
    - Rate limited by Google Drive API quotas
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
    ):
        """
        Initialize Google Drive connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            root_folder: Root folder name in Drive (default: "nexus-data")
            use_shared_drives: Whether to use shared drives
            shared_drive_id: Shared drive ID (if use_shared_drives=True)
            provider: OAuth provider name from config (default: "google-drive")

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
            This ensures each user accesses their own Drive.
        """
        print(f"[GDRIVE-INIT] __init__ called: user_email={user_email}, provider={provider}")

        # Import TokenManager here to avoid circular imports
        from nexus.server.auth.token_manager import TokenManager

        # Support both file paths and database URLs
        # Resolve database URL using base class method (checks TOKEN_MANAGER_DB env var)
        resolved_db = self.resolve_database_url(token_manager_db)

        if resolved_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=resolved_db)
        else:
            self.token_manager = TokenManager(db_path=resolved_db)
        self.user_email = user_email  # None means use context.user_id
        self.root_folder = root_folder
        self.use_shared_drives = use_shared_drives
        self.shared_drive_id = shared_drive_id
        self.provider = provider

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

        # Cache for folder IDs (path -> Drive folder ID)
        self._folder_cache: dict[str, str] = {}

        # Lazy import Google Drive API (only when needed)
        self._drive_service = None

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        import logging
        import traceback

        logger = logging.getLogger(__name__)

        try:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            # Create factory (loads from oauth.yaml config)
            factory = OAuthProviderFactory()

            # Create provider instance from config
            try:
                provider_instance = factory.create_provider(
                    name=self.provider,
                )
                # Register with TokenManager using the provider name from config
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info(
                    f"✓ Registered OAuth provider '{self.provider}' for Google Drive backend"
                )
                print(f"[GDRIVE-INIT] ✓ Registered OAuth provider '{self.provider}' from config")
            except ValueError as e:
                # Provider not found in config or credentials not set
                logger.warning(
                    f"OAuth provider '{self.provider}' not available: {e}. "
                    "OAuth flow must be initiated manually via the Integrations page."
                )
                print(f"[GDRIVE-INIT] ⚠ OAuth provider '{self.provider}' not available: {e}")
        except Exception as e:
            error_msg = f"Failed to register OAuth provider: {e}\n{traceback.format_exc()}"
            logger.error(error_msg)
            print(f"[GDRIVE-INIT] ✗ {error_msg}")

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "gdrive"

    def _get_drive_service(self, context: "OperationContext | None" = None) -> "Resource":
        """Get Google Drive service with user's OAuth credentials.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            Google Drive service instance

        Raises:
            BackendError: If credentials not found or user not authenticated
        """
        # Import here to avoid dependency if not using Drive
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gdrive",
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
                "Google Drive backend requires either configured user_email "
                "or authenticated user in OperationContext",
                backend="gdrive",
            )

        # Get valid access token from TokenManager (auto-refreshes if expired)
        import asyncio

        try:
            # Default to 'default' tenant if not specified to match mount configurations
            tenant_id = (
                context.tenant_id
                if context and hasattr(context, "tenant_id") and context.tenant_id
                else "default"
            )
            access_token = asyncio.run(
                self.token_manager.get_valid_token(
                    provider=self.provider,
                    user_email=user_email,
                    tenant_id=tenant_id,
                )
            )
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="gdrive",
            ) from e

        # Build Drive service with OAuth token
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("drive", "v3", credentials=creds)

    def _get_or_create_root_folder(
        self,
        service: "Resource",
        context: "OperationContext | str | None",
    ) -> str:
        """Get or create root folder in Drive.

        Args:
            service: Google Drive service
            context: Operation context

        Returns:
            Root folder ID

        Raises:
            BackendError: If folder operations fail
        """
        # Check cache first
        if context is not None and not isinstance(context, str):
            cache_key = f"root:{context.user_id}:{context.tenant_id}"
        else:
            cache_key = "root::"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            # Search for existing root folder
            query = f"name='{self.root_folder}' and mimeType='application/vnd.google-apps.folder' and trashed=false"

            if self.use_shared_drives and self.shared_drive_id:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name)",
                        corpora="drive",
                        driveId=self.shared_drive_id,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(q=query, spaces="drive", fields="files(id, name)")
                    .execute()
                )

            files = results.get("files", [])

            if files:
                # Root folder exists
                folder_id = str(files[0]["id"])
            else:
                # Create root folder
                file_metadata: dict[str, Any] = {
                    "name": self.root_folder,
                    "mimeType": "application/vnd.google-apps.folder",
                }

                if self.use_shared_drives and self.shared_drive_id:
                    file_metadata["parents"] = [self.shared_drive_id]

                folder = (
                    service.files()
                    .create(body=file_metadata, fields="id", supportsAllDrives=True)
                    .execute()
                )
                folder_id = str(folder["id"])
                logger.info(f"Created root folder '{self.root_folder}' with ID: {folder_id}")

            # Cache it
            self._folder_cache[cache_key] = folder_id
            return folder_id

        except Exception as e:
            raise BackendError(
                f"Failed to get/create root folder '{self.root_folder}': {e}",
                backend="gdrive",
            ) from e

    def _get_or_create_folder(
        self,
        service: "Resource",
        path: str,
        parent_id: str,
        context: "OperationContext | str | None",
    ) -> str:
        """Get or create a folder by path.

        Args:
            service: Google Drive service
            path: Folder path (relative to root)
            parent_id: Parent folder ID
            context: Operation context

        Returns:
            Folder ID

        Raises:
            BackendError: If folder operations fail
        """
        # Check cache
        if isinstance(context, str):
            # context is actually the parent_id in some calls
            cache_key = f":{path}"
        elif context is not None:
            cache_key = f"{context.user_id}:{context.tenant_id}:{path}"
        else:
            cache_key = f":{path}"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            # Search for existing folder
            query = f"name='{path}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(q=query, spaces="drive", fields="files(id, name)")
                    .execute()
                )

            files = results.get("files", [])

            if files:
                folder_id = str(files[0]["id"])
            else:
                # Create folder
                file_metadata: dict[str, Any] = {
                    "name": path,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                }

                folder = (
                    service.files()
                    .create(body=file_metadata, fields="id", supportsAllDrives=True)
                    .execute()
                )
                folder_id = str(folder["id"])
                logger.info(f"Created folder '{path}' with ID: {folder_id}")

            # Cache it
            self._folder_cache[cache_key] = folder_id
            return folder_id

        except Exception as e:
            raise BackendError(
                f"Failed to get/create folder '{path}': {e}", backend="gdrive"
            ) from e

    def _resolve_path_to_folder_id(
        self, service: "Resource", backend_path: str, context: "OperationContext | str | None"
    ) -> tuple[str, str]:
        """Resolve a backend path to parent folder ID and filename.

        Args:
            service: Google Drive service
            backend_path: Backend path (e.g., "/workspace/data/file.txt")
            context: Operation context

        Returns:
            Tuple of (parent_folder_id, filename)

        Raises:
            BackendError: If path resolution fails
        """
        # Get root folder
        root_id = self._get_or_create_root_folder(service, context)

        # Split path into parts
        parts = backend_path.strip("/").split("/")
        if not parts or parts == [""]:
            raise BackendError("Invalid backend path", backend="gdrive", path=backend_path)

        filename = parts[-1]
        folder_parts = parts[:-1]

        # Navigate to parent folder
        parent_id = root_id
        for folder_name in folder_parts:
            parent_id = self._get_or_create_folder(service, folder_name, parent_id, context)

        return parent_id, filename

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to Google Drive and return its content hash.

        For connector backends, this writes to a temporary location indexed by hash.
        The actual file path is determined later via backend_path in context.

        Args:
            content: File content as bytes
            context: Operation context with user_id and backend_path

        Returns:
            Content hash (SHA-256 as hex string)

        Raises:
            BackendError: If write operation fails
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        # Calculate content hash (BLAKE3, Rust-accelerated)
        content_hash = hash_content(content)

        service = self._get_drive_service(context)

        # Resolve path to parent folder and filename (backend_path is checked above)
        parent_id, filename = self._resolve_path_to_folder_id(
            service, context.backend_path, context
        )

        try:
            # Check if file already exists
            query = f"name='{filename}' and '{parent_id}' in parents and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(q=query, spaces="drive", fields="files(id, name)")
                    .execute()
                )

            files = results.get("files", [])

            # Guess MIME type
            mime_type, _ = mimetypes.guess_type(filename)
            if mime_type is None:
                mime_type = "application/octet-stream"

            from googleapiclient.http import MediaIoBaseUpload

            media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=True)

            if files:
                # Update existing file
                file_id = files[0]["id"]
                service.files().update(
                    fileId=file_id, media_body=media, supportsAllDrives=True
                ).execute()
                logger.info(f"Updated file '{filename}' in Drive (ID: {file_id})")
            else:
                # Create new file
                file_metadata = {"name": filename, "parents": [parent_id]}

                file = (
                    service.files()
                    .create(
                        body=file_metadata, media_body=media, fields="id", supportsAllDrives=True
                    )
                    .execute()
                )
                file_id = file["id"]
                logger.info(f"Created file '{filename}' in Drive (ID: {file_id})")

            return content_hash

        except Exception as e:
            raise BackendError(
                f"Failed to write file '{filename}' to Drive: {e}",
                backend="gdrive",
                path=context.backend_path,
            ) from e

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read content from Google Drive by path (not hash).

        For connector backends, content_hash is ignored - we use backend_path instead.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read operation fails
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        service = self._get_drive_service(context)

        # Resolve path to parent folder and filename (backend_path is checked above)
        parent_id, filename = self._resolve_path_to_folder_id(
            service, context.backend_path, context
        )

        try:
            # Find file by name in parent folder
            query = f"name='{filename}' and '{parent_id}' in parents and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name, mimeType)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(q=query, spaces="drive", fields="files(id, name, mimeType)")
                    .execute()
                )

            files = results.get("files", [])

            if not files:
                raise NexusFileNotFoundError(
                    context.backend_path
                )  # backend_path is str (checked above)

            file_id = files[0]["id"]
            mime_type = files[0].get("mimeType", "")

            # Check if it's a Google Workspace file
            if mime_type in GOOGLE_MIME_TYPES:
                # Export Google Workspace file
                export_format = self._get_export_format(mime_type, filename)
                request = service.files().export_media(fileId=file_id, mimeType=export_format)
            else:
                # Download regular file
                request = service.files().get_media(fileId=file_id)

            # Execute download
            import io

            fh = io.BytesIO()
            from googleapiclient.http import MediaIoBaseDownload

            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()

            return fh.getvalue()

        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to read file '{filename}' from Drive: {e}",
                backend="gdrive",
                path=context.backend_path,
            ) from e

    def _get_export_format(self, mime_type: str, filename: str) -> str:
        """Get export MIME type for Google Workspace files.

        Args:
            mime_type: Google Workspace MIME type
            filename: Filename (may contain format hint like .pdf, .docx)

        Returns:
            Export MIME type

        Example:
            >>> _get_export_format("application/vnd.google-apps.document", "report.pdf")
            "application/pdf"
        """
        # Check if filename has export format hint
        for ext, export_mime in EXPORT_FORMATS.get(mime_type, {}).items():
            if filename.endswith(f".{ext}"):
                return export_mime

        # Default export formats
        defaults = {
            "application/vnd.google-apps.document": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # DOCX
            "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # XLSX
            "application/vnd.google-apps.presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # PPTX
        }

        return defaults.get(mime_type, "application/pdf")

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> bool:  # type: ignore[override]
        """
        Delete content from Google Drive by path.

        For connector backends, content_hash is ignored - we use backend_path instead.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            True if deleted, False if not found

        Raises:
            BackendError: If delete operation fails
        """
        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Google Drive connector requires OperationContext with backend_path",
                backend="gdrive",
            )

        service = self._get_drive_service(context)

        # Resolve path to parent folder and filename (backend_path is checked above)
        parent_id, filename = self._resolve_path_to_folder_id(
            service, context.backend_path, context
        )

        try:
            # Find file by name in parent folder
            query = f"name='{filename}' and '{parent_id}' in parents and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(q=query, spaces="drive", fields="files(id, name)")
                    .execute()
                )

            files = results.get("files", [])

            if not files:
                return False

            file_id = files[0]["id"]

            # Move to trash
            service.files().update(
                fileId=file_id, body={"trashed": True}, supportsAllDrives=True
            ).execute()

            logger.info(f"Deleted file '{filename}' from Drive (ID: {file_id})")
            return True

        except Exception as e:
            raise BackendError(
                f"Failed to delete file '{filename}' from Drive: {e}",
                backend="gdrive",
                path=context.backend_path,
            ) from e

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if content exists in Google Drive by path.

        For connector backends, content_hash is ignored - we use backend_path instead.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            True if file exists, False otherwise
        """
        if context is None or not hasattr(context, "backend_path"):
            return False

        try:
            service = self._get_drive_service(context)

            # Resolve path to parent folder and filename (backend_path is checked above)
            parent_id, filename = self._resolve_path_to_folder_id(
                service,
                context.backend_path or "",
                context,
            )

            # Find file by name in parent folder
            query = f"name='{filename}' and '{parent_id}' in parents and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files().list(q=query, spaces="drive", fields="files(id)").execute()
                )

            files = results.get("files", [])
            return len(files) > 0

        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get content size from Google Drive.

        Args:
            content_hash: Content hash (file ID in Drive)
            context: Operation context (optional)

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If operation fails
        """
        try:
            service = self._get_drive_service(context)
            file_metadata = service.files().get(fileId=content_hash, fields="size").execute()

            size = file_metadata.get("size")
            if size is None:
                # Google Workspace files don't have size
                return 0
            return int(size)

        except Exception as e:
            if "File not found" in str(e):
                raise NexusFileNotFoundError(content_hash) from e
            raise BackendError(
                f"Failed to get content size: {e}",
                backend="gdrive",
                path=content_hash,
            ) from e

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get reference count (always 1 for connector backends).

        Connector backends don't do deduplication, so each file
        has exactly one reference.

        Args:
            content_hash: Content hash
            context: Operation context

        Returns:
            Always 1 (no reference counting)
        """
        # No deduplication - each file is unique
        return 1

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory in Google Drive.

        Args:
            path: Directory path relative to backend root
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            BackendError: If operation fails
        """
        path = path.strip("/")
        if not path:
            return  # Root always exists

        if context is None:
            raise BackendError(
                "Google Drive connector mkdir requires OperationContext",
                backend="gdrive",
            )

        try:
            service = self._get_drive_service(context)
            root_folder_id = self._get_or_create_root_folder(service, context)

            # Check if folder exists
            if self.is_directory(path):
                if not exist_ok:
                    raise FileExistsError(f"Directory already exists: {path}")
                return

            # Navigate through path components to create folder hierarchy
            parts = path.split("/")
            parent_id = root_folder_id

            for i, folder_name in enumerate(parts):
                if not folder_name:
                    continue

                # Check if this is the last component (the folder we want to create)
                is_last = i == len(parts) - 1

                if not is_last:
                    # Intermediate folder - create if parents=True, otherwise fail if missing
                    if parents:
                        parent_id = self._get_or_create_folder(
                            service, folder_name, parent_id, context
                        )
                    else:
                        # Try to get existing folder
                        query = f"name='{folder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
                        if self.use_shared_drives:
                            results = (
                                service.files()
                                .list(
                                    q=query,
                                    spaces="drive",
                                    fields="files(id)",
                                    includeItemsFromAllDrives=True,
                                    supportsAllDrives=True,
                                )
                                .execute()
                            )
                        else:
                            results = (
                                service.files()
                                .list(q=query, spaces="drive", fields="files(id)")
                                .execute()
                            )

                        files = results.get("files", [])
                        if not files:
                            raise FileNotFoundError(
                                f"Parent directory does not exist: {'/'.join(parts[: i + 1])}"
                            )
                        parent_id = files[0]["id"]
                else:
                    # Last folder - create it
                    parent_id = self._get_or_create_folder(service, folder_name, parent_id, context)

        except (FileExistsError, FileNotFoundError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to create directory {path}: {e}",
                backend="gdrive",
                path=path,
            ) from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove directory from Google Drive.

        Args:
            path: Directory path
            recursive: Remove non-empty directory (moves to trash)
            context: Operation context (not used, authentication handled internally)

        Raises:
            BackendError: If trying to remove root
            NexusFileNotFoundError: If directory doesn't exist
            OSError: If directory not empty and recursive=False
            BackendError: If operation fails
        """
        path = path.strip("/")
        if not path:
            raise BackendError("Cannot remove root directory", backend="gdrive", path=path)

        try:
            service = self._get_drive_service(None)
            root_folder_id = self._get_or_create_root_folder(service, None)

            # Resolve path to folder ID
            folder_id = self._resolve_path_to_folder_id(service, path, root_folder_id)
            if not folder_id:
                raise NexusFileNotFoundError(path)

            if not recursive:
                # Check if directory is empty
                query = f"'{folder_id}' in parents and trashed=false"
                results = service.files().list(q=query, fields="files(id)", pageSize=1).execute()
                if results.get("files", []):
                    raise OSError(f"Directory not empty: {path}")

            # Move to trash (Google Drive doesn't permanently delete immediately)
            service.files().update(fileId=folder_id, body={"trashed": True}).execute()

        except (NexusFileNotFoundError, OSError):
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to remove directory {path}: {e}",
                backend="gdrive",
                path=path,
            ) from e

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory in Google Drive.

        Args:
            path: Path to check

        Returns:
            True if path is a folder, False otherwise
        """
        try:
            path = path.strip("/")
            if not path:
                return True  # Root is always a directory

            service = self._get_drive_service(None)
            root_folder_id = self._get_or_create_root_folder(service, None)

            # Split path into parent and target name
            parts = path.split("/")
            target_name = parts[-1]
            parent_parts = parts[:-1]

            # Navigate to parent folder (without creating missing folders)
            parent_id = root_folder_id
            for part in parent_parts:
                if not part:
                    continue

                # Try to find existing folder (don't create it)
                query = f"name='{part}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
                if self.use_shared_drives:
                    results = (
                        service.files()
                        .list(
                            q=query,
                            spaces="drive",
                            fields="files(id)",
                            includeItemsFromAllDrives=True,
                            supportsAllDrives=True,
                        )
                        .execute()
                    )
                else:
                    results = (
                        service.files().list(q=query, spaces="drive", fields="files(id)").execute()
                    )

                files = results.get("files", [])
                if not files:
                    # Parent doesn't exist, so path doesn't exist
                    return False
                parent_id = files[0]["id"]

            # Check if target exists as a folder
            query = f"name='{target_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files().list(q=query, spaces="drive", fields="files(id)").execute()
                )

            files = results.get("files", [])
            return len(files) > 0

        except Exception:
            return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents from Google Drive.

        Args:
            path: Directory path to list (relative to backend root)
            context: Operation context for authentication

        Returns:
            List of entry names (directories have trailing '/')

        Raises:
            FileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        try:
            path = path.strip("/")

            service = self._get_drive_service(context)
            root_folder_id = self._get_or_create_root_folder(service, context)

            # Navigate to target folder
            if path:
                # Split path and navigate
                parts = path.split("/")
                current_folder_id = root_folder_id
                for part in parts:
                    if not part:
                        continue
                    current_folder_id = self._get_or_create_folder(
                        service, part, current_folder_id, context
                    )
                folder_id = current_folder_id
            else:
                # List root folder
                folder_id = root_folder_id

            # Query all files/folders in this directory
            query = f"'{folder_id}' in parents and trashed=false"

            if self.use_shared_drives:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name, mimeType)",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        pageSize=1000,
                    )
                    .execute()
                )
            else:
                results = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="files(id, name, mimeType)",
                        pageSize=1000,
                    )
                    .execute()
                )

            files = results.get("files", [])

            # Build list of entries
            entries = []
            for file in files:
                name = file["name"]
                mime_type = file.get("mimeType", "")

                # Add trailing '/' for folders
                if mime_type == "application/vnd.google-apps.folder":
                    entries.append(name + "/")
                else:
                    entries.append(name)

            return sorted(entries)

        except Exception as e:
            if "not found" in str(e).lower():
                raise FileNotFoundError(f"Directory not found: {path}") from e
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gdrive",
                path=path,
            ) from e
