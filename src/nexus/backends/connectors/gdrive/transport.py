"""Google Drive Transport — raw key→bytes I/O over the Drive API.

Implements the Transport protocol for Google Drive, mapping:
- store(key, data) → files.create / files.update (upsert by path)
- fetch(key) → files.get_media / files.export_media → bytes
- remove(key) → files.update(trashed=True)
- list_keys(prefix) → files.list in resolved folder
- exists(key) → files.list(q=name+parent)
- get_size(key) → files.get(fields="size")

Read-write: supports upload, download, delete, and directory operations.

Auth: DriveTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``
so the transport can resolve the per-user OAuth token.

Key schema (paths relative to root_folder):
    "workspace/data/file.txt"   → file.txt in workspace/data/ folder
    "report.pdf"                → report.pdf in root folder
    list_keys("")               → root folder contents
    list_keys("workspace/")     → workspace folder contents
"""

from __future__ import annotations

import io
import logging
import mimetypes
import time
from collections.abc import Iterator
from copy import copy
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Suppress noisy discovery-cache warnings from google-api-python-client.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)


# ---------------------------------------------------------------------------
# Google Drive MIME type constants (Drive API concerns)
# ---------------------------------------------------------------------------

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
        "markdown": "text/markdown",
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


class DriveTransport:
    """Google Drive API transport implementing the Transport protocol.

    Attributes:
        transport_name: ``"gdrive"`` -- used by PathAddressingEngine to build
            the backend name (``"path-gdrive"``).
    """

    transport_name: str = "gdrive"
    _LIST_KEYS_MAX_PAGES: int | None = 200
    _LIST_KEYS_MAX_ITEMS: int | None = 50000
    _LIST_KEYS_MAX_ELAPSED_SECONDS: float = 120.0
    _LIST_KEYS_FAIL_ON_TRUNCATION: bool = True

    def __init__(
        self,
        token_manager: Any,
        provider: str = "google-drive",
        user_email: str | None = None,
        root_folder: str = "nexus-data",
        use_shared_drives: bool = False,
        shared_drive_id: str | None = None,
    ) -> None:
        self._token_manager = token_manager
        self._provider = provider
        self._user_email = user_email
        self._root_folder = root_folder
        self._use_shared_drives = use_shared_drives
        self._shared_drive_id = shared_drive_id
        self._context: OperationContext | None = None

        # Cache for folder IDs (cache_key -> Drive folder ID)
        self._folder_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Context binding (not part of Transport protocol; Drive-specific)
    # ------------------------------------------------------------------

    def with_context(self, context: OperationContext | None) -> DriveTransport:
        """Return a shallow copy bound to *context* (for OAuth token resolution)."""
        clone = copy(self)
        clone._context = context
        # Share folder cache across clones for efficiency
        clone._folder_cache = self._folder_cache
        return clone

    # ------------------------------------------------------------------
    # Internal helpers — OAuth / service building
    # ------------------------------------------------------------------

    def _get_drive_service(self) -> Resource:
        """Build an authenticated Drive v3 ``Resource`` using the bound context."""
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gdrive",
            ) from None

        from nexus.backends.connectors.oauth_base import resolve_oauth_access_token

        # Let the shared resolver pick mount-configured email vs context
        # user_id (the resolver uses list_credentials to map a nexus
        # user_id → linked OAuth email when the context id isn't itself
        # an email, e.g. API-key auth as "admin").
        user_email: str | None = self._user_email
        nexus_user_id: str | None = (
            self._context.user_id if self._context and self._context.user_id else None
        )
        zone_id = (
            self._context.zone_id
            if self._context and hasattr(self._context, "zone_id") and self._context.zone_id
            else "root"
        )
        try:
            access_token = resolve_oauth_access_token(
                self._token_manager,
                connector_name="gdrive_connector",
                provider=self._provider or "google-drive",
                user_email=user_email,
                zone_id=zone_id,
                nexus_user_id=nexus_user_id,
            )
        except AuthenticationError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="gdrive",
            ) from e

        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("drive", "v3", credentials=creds)

    # ------------------------------------------------------------------
    # Internal helpers — folder resolution
    # ------------------------------------------------------------------

    def _list_files(
        self,
        service: Resource,
        query: str,
        fields: str = "files(id, name)",
        page_size: int = 1000,
        *,
        all_pages: bool = False,
        max_pages: int | None = None,
        max_items: int | None = None,
        max_elapsed_seconds: float | None = None,
        fail_on_truncation: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute files.list with shared-drive support and optional pagination."""
        list_fields = fields
        if all_pages and "nextPageToken" not in list_fields:
            list_fields = f"nextPageToken, {list_fields}"

        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        pages_fetched = 0
        truncation_reason: str | None = None
        started_at = time.monotonic()

        while True:
            if (
                max_elapsed_seconds is not None
                and (time.monotonic() - started_at) >= max_elapsed_seconds
            ):
                truncation_reason = f"max_elapsed_seconds={max_elapsed_seconds} reached"
                break

            page_size_for_call = page_size
            if max_items is not None:
                remaining = max_items - len(collected)
                if remaining <= 0:
                    truncation_reason = f"max_items={max_items} reached"
                    break
                page_size_for_call = min(page_size, remaining)

            kwargs: dict[str, Any] = {
                "q": query,
                "spaces": "drive",
                "fields": list_fields,
                "pageSize": page_size_for_call,
            }
            if page_token:
                kwargs["pageToken"] = page_token

            if self._use_shared_drives and self._shared_drive_id:
                kwargs.update(
                    {
                        "corpora": "drive",
                        "driveId": self._shared_drive_id,
                        "includeItemsFromAllDrives": True,
                        "supportsAllDrives": True,
                    }
                )
            elif self._use_shared_drives:
                kwargs.update(
                    {
                        "includeItemsFromAllDrives": True,
                        "supportsAllDrives": True,
                    }
                )

            results = service.files().list(**kwargs).execute()
            pages_fetched += 1
            page_files = list(results.get("files", []))
            collected.extend(page_files)

            if not all_pages:
                break

            next_page_token = results.get("nextPageToken")
            if not next_page_token:
                break
            if max_pages is not None and pages_fetched >= max_pages:
                truncation_reason = f"max_pages={max_pages} reached"
                break
            if max_items is not None and len(collected) >= max_items:
                truncation_reason = f"max_items={max_items} reached"
                break
            if next_page_token in seen_page_tokens:
                truncation_reason = "repeated nextPageToken detected"
                break
            seen_page_tokens.add(next_page_token)
            page_token = next_page_token

        if truncation_reason:
            message = f"Drive listing incomplete for query={query}: {truncation_reason}"
            if fail_on_truncation:
                raise BackendError(message, backend="gdrive")
            logger.warning(message)

        return collected

    @staticmethod
    def _escape_query_literal(value: str) -> str:
        """Escape string literal for Google Drive query expressions."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def _get_or_create_root_folder(self, service: Resource) -> str:
        """Get or create root folder in Drive.

        Returns:
            Root folder ID
        """
        # Build cache key from context
        if self._context is not None and hasattr(self._context, "user_id"):
            zone_id = getattr(self._context, "zone_id", "") or ""
            cache_key = f"root:{self._context.user_id}:{zone_id}"
        else:
            cache_key = "root::"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            escaped_root_folder = self._escape_query_literal(self._root_folder)
            query = (
                f"name='{escaped_root_folder}' "
                f"and mimeType='application/vnd.google-apps.folder' "
                f"and trashed=false"
            )
            files = self._list_files(service, query)

            if files:
                folder_id = str(files[0]["id"])
            else:
                # Create root folder
                file_metadata: dict[str, Any] = {
                    "name": self._root_folder,
                    "mimeType": "application/vnd.google-apps.folder",
                }
                if self._use_shared_drives and self._shared_drive_id:
                    file_metadata["parents"] = [self._shared_drive_id]

                folder = (
                    service.files()
                    .create(body=file_metadata, fields="id", supportsAllDrives=True)
                    .execute()
                )
                folder_id = str(folder["id"])
                logger.info("Created root folder '%s' with ID: %s", self._root_folder, folder_id)

            self._folder_cache[cache_key] = folder_id
            return folder_id

        except Exception as e:
            raise BackendError(
                f"Failed to get/create root folder '{self._root_folder}': {e}",
                backend="gdrive",
            ) from e

    def _get_or_create_folder(
        self,
        service: Resource,
        name: str,
        parent_id: str,
    ) -> str:
        """Get or create a folder by name under parent_id.

        Returns:
            Folder ID
        """
        if self._context is not None and hasattr(self._context, "user_id"):
            zone_id = getattr(self._context, "zone_id", "") or ""
            cache_key = f"{self._context.user_id}:{zone_id}:{parent_id}/{name}"
        else:
            cache_key = f"::{parent_id}/{name}"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            escaped_name = self._escape_query_literal(name)
            escaped_parent_id = self._escape_query_literal(parent_id)
            query = (
                f"name='{escaped_name}' and '{escaped_parent_id}' in parents "
                f"and mimeType='application/vnd.google-apps.folder' "
                f"and trashed=false"
            )
            files = self._list_files(service, query)

            if files:
                folder_id = str(files[0]["id"])
            else:
                file_metadata: dict[str, Any] = {
                    "name": name,
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": [parent_id],
                }
                folder = (
                    service.files()
                    .create(body=file_metadata, fields="id", supportsAllDrives=True)
                    .execute()
                )
                folder_id = str(folder["id"])
                logger.info("Created folder '%s' with ID: %s", name, folder_id)

            self._folder_cache[cache_key] = folder_id
            return folder_id

        except Exception as e:
            raise BackendError(
                f"Failed to get/create folder '{name}': {e}",
                backend="gdrive",
            ) from e

    def _find_folder(
        self,
        service: Resource,
        name: str,
        parent_id: str,
    ) -> str | None:
        """Find an existing folder by name under parent (read-only, never creates).

        Returns:
            Folder ID if found, None otherwise.
        """
        escaped_name = self._escape_query_literal(name)
        escaped_parent_id = self._escape_query_literal(parent_id)
        query = (
            f"name='{escaped_name}' and '{escaped_parent_id}' in parents "
            f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
        )
        files = self._list_files(service, query)
        if files:
            return str(files[0]["id"])
        return None

    def _resolve_path_to_folder_id(
        self,
        service: Resource,
        path: str,
        *,
        create_parents: bool = True,
    ) -> tuple[str, str]:
        """Resolve a path to (parent_folder_id, filename).

        Args:
            service: Drive API service
            path: Path relative to root (e.g. "workspace/data/file.txt")
            create_parents: If True, create missing parent folders; if False,
                raise NexusFileNotFoundError on missing parents.

        Returns:
            Tuple of (parent_folder_id, filename)
        """
        root_id = self._get_or_create_root_folder(service)

        parts = path.strip("/").split("/")
        if not parts or parts == [""]:
            raise BackendError("Invalid path", backend="gdrive", path=path)

        filename = parts[-1]
        folder_parts = parts[:-1]

        parent_id = root_id
        for folder_name in folder_parts:
            if create_parents:
                parent_id = self._get_or_create_folder(service, folder_name, parent_id)
            else:
                found = self._find_folder(service, folder_name, parent_id)
                if found is None:
                    raise NexusFileNotFoundError(
                        path="/".join(parts[: folder_parts.index(folder_name) + 1]),
                    )
                parent_id = found

        return parent_id, filename

    def _find_file_in_parent(
        self,
        service: Resource,
        filename: str,
        parent_id: str,
        fields: str = "files(id, name)",
    ) -> list[dict[str, Any]]:
        """Find files by name in a parent folder."""
        escaped_filename = self._escape_query_literal(filename)
        escaped_parent_id = self._escape_query_literal(parent_id)
        query = f"name='{escaped_filename}' and '{escaped_parent_id}' in parents and trashed=false"
        return self._list_files(service, query, fields=fields)

    # ------------------------------------------------------------------
    # Internal helpers — export format detection
    # ------------------------------------------------------------------

    @staticmethod
    def _get_export_format(mime_type: str, filename: str) -> str:
        """Get export MIME type for Google Workspace files.

        Checks filename extension for format hints, falls back to defaults.
        """
        for ext, export_mime in EXPORT_FORMATS.get(mime_type, {}).items():
            if filename.endswith(f".{ext}"):
                return export_mime

        defaults = {
            "application/vnd.google-apps.document": (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            "application/vnd.google-apps.spreadsheet": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "application/vnd.google-apps.presentation": (
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        }
        return defaults.get(mime_type, "application/pdf")

    def _download_file(
        self, service: Resource, file_id: str, mime_type: str, filename: str
    ) -> bytes:
        """Download a file from Drive, handling Google Workspace export."""
        if mime_type in GOOGLE_MIME_TYPES:
            export_format = self._get_export_format(mime_type, filename)
            request = service.files().export_media(fileId=file_id, mimeType=export_format)
        else:
            request = service.files().get_media(fileId=file_id)

        from googleapiclient.http import MediaIoBaseDownload

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()

        return fh.getvalue()

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Upload or update a file in Drive.

        - If a file with the same name exists in the parent folder, it is updated.
        - Otherwise a new file is created.

        Returns:
            None (Drive does not expose version IDs in this flow).
        """
        service = self._get_drive_service()
        parent_id, filename = self._resolve_path_to_folder_id(service, key, create_parents=True)

        # Check if file already exists
        existing = self._find_file_in_parent(service, filename, parent_id)

        # Determine MIME type
        mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=True)

        if existing:
            file_id = existing[0]["id"]
            service.files().update(
                fileId=file_id, media_body=media, supportsAllDrives=True
            ).execute()
            logger.info("Updated file '%s' in Drive (ID: %s)", filename, file_id)
        else:
            file_metadata: dict[str, Any] = {"name": filename, "parents": [parent_id]}
            result = (
                service.files()
                .create(body=file_metadata, media_body=media, fields="id", supportsAllDrives=True)
                .execute()
            )
            file_id = result["id"]
            logger.info("Created file '%s' in Drive (ID: %s)", filename, file_id)

        return None

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Download a file from Drive by path.

        Returns:
            (content_bytes, None) -- Drive transport does not track versions.
        """
        service = self._get_drive_service()
        parent_id, filename = self._resolve_path_to_folder_id(service, key, create_parents=False)

        files = self._find_file_in_parent(
            service, filename, parent_id, fields="files(id, name, mimeType)"
        )
        if not files:
            raise NexusFileNotFoundError(key)

        file_id = files[0]["id"]
        mime_type = files[0].get("mimeType", "")

        content = self._download_file(service, file_id, mime_type, filename)
        return content, None

    def remove(self, key: str) -> None:
        """Move a file to trash in Drive."""
        service = self._get_drive_service()
        parent_id, filename = self._resolve_path_to_folder_id(service, key, create_parents=False)

        files = self._find_file_in_parent(service, filename, parent_id)
        if not files:
            raise NexusFileNotFoundError(key)

        file_id = files[0]["id"]
        service.files().update(
            fileId=file_id, body={"trashed": True}, supportsAllDrives=True
        ).execute()
        logger.info("Deleted file '%s' from Drive (ID: %s)", filename, file_id)

    def exists(self, key: str) -> bool:
        """Check whether a file exists at the given path in Drive."""
        try:
            service = self._get_drive_service()
            parent_id, filename = self._resolve_path_to_folder_id(
                service, key, create_parents=False
            )
            files = self._find_file_in_parent(service, filename, parent_id, fields="files(id)")
            return len(files) > 0
        except AuthenticationError:
            raise
        except (NexusFileNotFoundError, BackendError):
            return False
        except Exception:
            return False

    def get_size(self, key: str) -> int:
        """Return file size in bytes.

        Note: Google Workspace files (Docs/Sheets/Slides) report size=0.
        """
        try:
            service = self._get_drive_service()
            parent_id, filename = self._resolve_path_to_folder_id(
                service, key, create_parents=False
            )
            files = self._find_file_in_parent(
                service, filename, parent_id, fields="files(id, size)"
            )
            if not files:
                raise NexusFileNotFoundError(key)

            size = files[0].get("size")
            return int(size) if size is not None else 0

        except NexusFileNotFoundError:
            raise
        except Exception as e:
            raise NexusFileNotFoundError(key) from e

    def _resolve_list_prefix(
        self,
        service: Resource,
        prefix: str,
    ) -> tuple[str | None, str]:
        """Resolve list prefix to (folder_id, path_prefix)."""
        if not prefix:
            return self._get_or_create_root_folder(service), ""

        root_id = self._get_or_create_root_folder(service)
        parts = prefix.split("/")
        current_id = root_id
        for part in parts:
            if not part:
                continue
            found = self._find_folder(service, part, current_id)
            if found is None:
                return None, ""
            current_id = found
        return current_id, prefix + "/"

    def _list_page_under_folder(
        self,
        service: Resource,
        folder_id: str,
        path_prefix: str,
        *,
        page_size: int = 1000,
        page_token: str | None = None,
    ) -> tuple[list[str], list[str], str | None]:
        """List a single Drive page under a folder and return nextPageToken."""
        escaped_folder_id = self._escape_query_literal(folder_id)
        query = f"'{escaped_folder_id}' in parents and trashed=false"
        kwargs: dict[str, Any] = {
            "q": query,
            "spaces": "drive",
            "fields": "nextPageToken, files(id, name, mimeType)",
            "pageSize": page_size,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        if self._use_shared_drives and self._shared_drive_id:
            kwargs.update(
                {
                    "corpora": "drive",
                    "driveId": self._shared_drive_id,
                    "includeItemsFromAllDrives": True,
                    "supportsAllDrives": True,
                }
            )
        elif self._use_shared_drives:
            kwargs.update(
                {
                    "includeItemsFromAllDrives": True,
                    "supportsAllDrives": True,
                }
            )

        result = service.files().list(**kwargs).execute()
        blob_keys: list[str] = []
        common_prefixes: list[str] = []
        for file_entry in result.get("files", []):
            name = file_entry["name"]
            file_mime = file_entry.get("mimeType", "")
            if file_mime == "application/vnd.google-apps.folder":
                common_prefixes.append(f"{path_prefix}{name}/")
            else:
                blob_keys.append(f"{path_prefix}{name}")
        return blob_keys, common_prefixes, result.get("nextPageToken")

    def list_keys_page(
        self,
        prefix: str,
        *,
        page_token: str | None = None,
        page_size: int = 1000,
    ) -> tuple[list[str], list[str], str | None]:
        """List a single page of keys and return continuation token."""
        service = self._get_drive_service()
        prefix = prefix.strip("/")
        folder_id, path_prefix = self._resolve_list_prefix(service, prefix)
        if folder_id is None:
            return [], [], None
        blob_keys, common_prefixes, next_page_token = self._list_page_under_folder(
            service,
            folder_id,
            path_prefix,
            page_size=page_size,
            page_token=page_token,
        )
        return sorted(set(blob_keys)), sorted(set(common_prefixes)), next_page_token

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List files and folders under *prefix*.

        - ``list_keys("")`` -> contents of root folder
        - ``list_keys("workspace/")`` -> contents of workspace folder

        Returns:
            ``(blob_keys, common_prefixes)`` where folders are common_prefixes
            with trailing ``/``, and files are blob_keys with full relative path.
        """
        service = self._get_drive_service()
        prefix = prefix.strip("/")
        blob_keys: list[str] = []
        common_prefixes: list[str] = []
        folder_id, path_prefix = self._resolve_list_prefix(service, prefix)
        if folder_id is None:
            return [], []

        next_page_token: str | None = None
        pages_fetched = 0
        started_at = time.monotonic()
        truncation_reason: str | None = None
        seen_page_tokens: set[str] = set()

        while True:
            page_blobs, page_prefixes, next_page_token = self._list_page_under_folder(
                service,
                folder_id,
                path_prefix,
                page_size=1000,
                page_token=next_page_token,
            )
            blob_keys.extend(page_blobs)
            common_prefixes.extend(page_prefixes)
            pages_fetched += 1

            if not next_page_token:
                break
            if next_page_token in seen_page_tokens:
                truncation_reason = "repeated nextPageToken detected"
                break
            seen_page_tokens.add(next_page_token)
            if (
                self._LIST_KEYS_MAX_ELAPSED_SECONDS is not None
                and (time.monotonic() - started_at) >= self._LIST_KEYS_MAX_ELAPSED_SECONDS
            ):
                truncation_reason = (
                    f"max_elapsed_seconds={self._LIST_KEYS_MAX_ELAPSED_SECONDS} reached"
                )
                break
            if self._LIST_KEYS_MAX_PAGES is not None and pages_fetched >= self._LIST_KEYS_MAX_PAGES:
                truncation_reason = f"max_pages={self._LIST_KEYS_MAX_PAGES} reached"
                break
            if (
                self._LIST_KEYS_MAX_ITEMS is not None
                and (len(blob_keys) + len(common_prefixes)) >= self._LIST_KEYS_MAX_ITEMS
            ):
                truncation_reason = f"max_items={self._LIST_KEYS_MAX_ITEMS} reached"
                break

        if truncation_reason and next_page_token:
            message = (
                f"Drive listing incomplete for prefix='{prefix}': {truncation_reason}. "
                f"next_page_token={next_page_token}. "
                "Use list_keys_page(prefix=..., page_token=...) to continue."
            )
            if self._LIST_KEYS_FAIL_ON_TRUNCATION:
                raise BackendError(message, backend="gdrive")
            logger.warning(message)

        return sorted(set(blob_keys)), sorted(set(common_prefixes))

    def copy_key(self, src_key: str, dst_key: str) -> None:
        """Copy a file within Drive (server-side copy)."""
        service = self._get_drive_service()

        # Resolve source
        src_parent_id, src_filename = self._resolve_path_to_folder_id(
            service, src_key, create_parents=False
        )
        src_files = self._find_file_in_parent(service, src_filename, src_parent_id)
        if not src_files:
            raise NexusFileNotFoundError(src_key)

        src_file_id = src_files[0]["id"]

        # Resolve destination (create parent folders if needed)
        dst_parent_id, dst_filename = self._resolve_path_to_folder_id(
            service, dst_key, create_parents=True
        )

        # Perform server-side copy
        copy_metadata: dict[str, Any] = {
            "name": dst_filename,
            "parents": [dst_parent_id],
        }
        service.files().copy(
            fileId=src_file_id,
            body=copy_metadata,
            supportsAllDrives=True,
        ).execute()

    def create_dir(self, key: str) -> None:
        """Create a folder in Drive.

        The *key* should be a path like ``"workspace/reports/"``.
        """
        path = key.rstrip("/")
        if not path:
            return

        service = self._get_drive_service()
        root_id = self._get_or_create_root_folder(service)

        parts = path.split("/")
        parent_id = root_id
        for folder_name in parts:
            if folder_name:
                parent_id = self._get_or_create_folder(service, folder_name, parent_id)

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream file content (download then chunk)."""
        data, _ = self.fetch(key, version_id)
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    def store_chunked(
        self,
        key: str,
        chunks: Iterator[bytes],
        content_type: str = "",
    ) -> str | None:
        """Write a file from an iterator of byte chunks."""
        data = b"".join(chunks)
        return self.store(key, data, content_type)

    # ------------------------------------------------------------------
    # Drive-specific helpers (not part of Transport protocol)
    # ------------------------------------------------------------------

    def resolve_folder_id(self, path: str) -> str | None:
        """Resolve a directory path to its Drive folder ID (read-only).

        Returns None if any part of the path does not exist.
        """
        service = self._get_drive_service()
        root_id = self._get_or_create_root_folder(service)

        path = path.strip("/")
        if not path:
            return root_id

        parts = path.split("/")
        current_id = root_id
        for part in parts:
            if not part:
                continue
            found = self._find_folder(service, part, current_id)
            if found is None:
                return None
            current_id = found
        return current_id

    def is_folder(self, path: str) -> bool:
        """Check whether a path is a folder in Drive.

        Auth-required failures from ``_get_drive_service()`` are
        re-raised so callers can surface the ``AuthenticationError``
        recovery signal — swallowing them as ``False`` would flip a
        401-class auth condition into false "not a directory" semantics
        upstream (e.g., ``NexusFS._check_is_directory``), which then
        cascades into 404 instead of 401.
        """
        path = path.strip("/")
        if not path:
            return True

        try:
            service = self._get_drive_service()
            root_id = self._get_or_create_root_folder(service)

            parts = path.split("/")
            current_id = root_id
            for part in parts:
                if not part:
                    continue
                found = self._find_folder(service, part, current_id)
                if found is None:
                    return False
                current_id = found
            return True
        except AuthenticationError:
            raise
        except Exception:
            return False

    def remove_folder(self, path: str, recursive: bool = False) -> None:
        """Move a folder to trash in Drive.

        Args:
            path: Folder path relative to root
            recursive: If False, raises error on non-empty folders
        """
        path = path.strip("/")
        if not path:
            raise BackendError("Cannot remove root directory", backend="gdrive")

        service = self._get_drive_service()
        root_id = self._get_or_create_root_folder(service)

        # Resolve folder ID
        parts = path.split("/")
        current_id = root_id
        for part in parts:
            if not part:
                continue
            found = self._find_folder(service, part, current_id)
            if found is None:
                raise NexusFileNotFoundError(path)
            current_id = found
        folder_id = current_id

        if not recursive:
            # Check if directory is empty
            query = f"'{folder_id}' in parents and trashed=false"
            children = self._list_files(service, query, fields="files(id)", page_size=1)
            if children:
                raise BackendError(
                    f"Directory not empty: {path}",
                    backend="gdrive",
                )

        # Move to trash
        service.files().update(
            fileId=folder_id, body={"trashed": True}, supportsAllDrives=True
        ).execute()

    def mkdir_path(
        self,
        path: str,
        parents: bool = False,
    ) -> None:
        """Create a folder at the given path.

        Args:
            path: Folder path relative to root
            parents: If True, create intermediate folders; if False, fail on missing parents.
        """
        path = path.strip("/")
        if not path:
            return

        service = self._get_drive_service()
        root_id = self._get_or_create_root_folder(service)

        parts = path.split("/")
        parent_id = root_id

        for i, folder_name in enumerate(parts):
            if not folder_name:
                continue

            is_last = i == len(parts) - 1

            if not is_last:
                if parents:
                    parent_id = self._get_or_create_folder(service, folder_name, parent_id)
                else:
                    found = self._find_folder(service, folder_name, parent_id)
                    if found is None:
                        raise NexusFileNotFoundError("/".join(parts[: i + 1]))
                    parent_id = found
            else:
                parent_id = self._get_or_create_folder(service, folder_name, parent_id)
