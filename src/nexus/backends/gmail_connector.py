"""Gmail connector backend with OAuth 2.0 authentication.

This is a connector backend that provides read-only access to Gmail emails,
organizing them by label-based folders and thread structure.

Use case: Access Gmail emails through Nexus mount for search, analysis, and archival.

Storage structure (3-level hierarchy):
    /
    ├── SENT/                          # Sent emails
    │   └── {thread_id}/               # Thread folders
    │       └── email-{msg_id}.yaml    # Email metadata and content
    ├── STARRED/                       # Starred emails in INBOX
    ├── IMPORTANT/                     # Important emails in INBOX
    └── INBOX/                         # Remaining inbox emails

Key features:
- OAuth 2.0 authentication (user-scoped)
- Priority-based label folders (SENT > STARRED > IMPORTANT > INBOX)
- Thread-based organization preserving Gmail conversations
- Efficient API usage with label-based filtering
- On-demand email fetching from Gmail API
- Full email metadata and content in YAML format (including HTML body if present)
- Automatic token refresh via TokenManager
- Database-backed caching via CacheConnectorMixin for fast search

Fetching strategy:
- Uses list_emails_by_folder() utility with label-based filtering
- Fetches emails on-demand when accessed
- Each email appears in exactly ONE folder based on highest priority label match

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - Automatic refresh when expired
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.backends.gmail_connector_utils import fetch_emails_batch, list_emails_by_folder
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

# Suppress annoying googleapiclient discovery cache warnings
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class GmailConnectorBackend(Backend, CacheConnectorMixin):
    """
    Gmail connector backend with OAuth 2.0 authentication.

    This backend syncs emails from Gmail API and organizes them as YAML files
    by Gmail labels (INBOX, SENT, STARRED, etc.).

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Email syncing from a start date
    - Label-based folder structure (INBOX/, SENT/, STARRED/, etc.)
    - Full email metadata and content
    - Automatic token refresh
    - Persistent caching via CacheConnectorMixin for fast grep/search

    Folder Structure (3-level hierarchy, priority-based, mutually exclusive):
    - / - Root directory (lists label folders)
    - /SENT/ - All sent emails (priority 1)
      - {thread_id}/ - Thread folders
        - email-{msg_id}.yaml - Individual email messages with full content
    - /STARRED/ - Starred emails in INBOX, excluding SENT (priority 2)
    - /IMPORTANT/ - Important emails in INBOX, excluding SENT and STARRED (priority 3)
    - /INBOX/ - Remaining INBOX emails (priority 4)
    - Each email appears in exactly ONE folder based on highest priority match
    - Thread grouping preserved in folder structure

    Limitations:
    - No automatic deduplication (each email is a unique file)
    - Requires OAuth tokens for each user
    - Rate limited by Gmail API quotas
    - Emails are stored as YAML files (not editable)
    """

    # Gmail system labels to expose as folders (in priority order)
    # Each email appears in exactly ONE folder based on priority
    LABEL_FOLDERS = [
        "SENT",  # Priority 1: All sent emails
        "STARRED",  # Priority 2: Starred emails in INBOX (excluding SENT)
        "IMPORTANT",  # Priority 3: Important emails in INBOX (excluding SENT, STARRED)
        "INBOX",  # Priority 4: Remaining INBOX emails
    ]

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "gmail",
        session_factory=None,  # type: ignore[no-untyped-def]
        max_message_per_label: int = 50,
    ):
        """
        Initialize Gmail connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            provider: OAuth provider name from config (default: "gmail")
            session_factory: SQLAlchemy session factory for content caching (optional).
                           If provided, enables persistent caching for fast grep/search.
            max_message_per_label: Maximum number of messages to fetch per label (default: 50).
                                  Set to None for unlimited. Useful for testing with small datasets.

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
            This ensures each user accesses their own Gmail.
        """
        # Import TokenManager here to avoid circular imports
        from nexus.server.auth.token_manager import TokenManager

        # Store original token_manager_db for config updates
        self.token_manager_db = token_manager_db

        # Support both file paths and database URLs
        if token_manager_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=token_manager_db)
        else:
            self.token_manager = TokenManager(db_path=token_manager_db)
        self.user_email = user_email  # None means use context.user_id
        self.provider = provider

        # Store session factory for caching (CacheConnectorMixin)
        self.session_factory = session_factory

        # Store max messages per label (for testing with small datasets)
        self.max_message_per_label = max_message_per_label

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

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
                logger.info(f"✓ Registered OAuth provider '{self.provider}' for Gmail backend")
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
        return "gmail"

    @property
    def user_scoped(self) -> bool:
        """This backend requires per-user OAuth credentials."""
        return True

    def _get_gmail_service(self, context: "OperationContext | None" = None) -> "Resource":
        """Get Gmail service with user's OAuth credentials.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            Gmail service instance

        Raises:
            BackendError: If credentials not found or user not authenticated
        """
        # Import here to avoid dependency if not using Gmail
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gmail",
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
                "Gmail backend requires either configured user_email "
                "or authenticated user in OperationContext",
                backend="gmail",
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

            # Handle both sync and async contexts
            try:
                # Try to get the current event loop
                asyncio.get_running_loop()
                # If we're in an async context, we can't use asyncio.run()
                # This shouldn't happen in normal usage, but handle it gracefully
                raise BackendError(
                    "Gmail connector cannot be used in async context. "
                    "Use sync methods or ensure you're not in an async event loop.",
                    backend="gmail",
                )
            except RuntimeError:
                # No running event loop, safe to use asyncio.run()
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
                backend="gmail",
            ) from e

        # Build Gmail service with OAuth token
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        service = build("gmail", "v1", credentials=creds)

        return service

    def _parse_email_date(self, date_str: str) -> datetime:
        """Parse email date string to datetime.

        Args:
            date_str: Email date string (RFC 2822 format)

        Returns:
            Datetime object in UTC
        """
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            # Fallback to current time if parsing fails
            return datetime.now(UTC)

    def _parse_gmail_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Parse Gmail API message response into email data dict.

        This is a helper method for batch operations. It extracts email metadata
        and content from Gmail API response format.

        Args:
            message: Gmail API message response dict

        Returns:
            Email data dictionary with metadata and content
        """
        import base64

        # Extract headers
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}

        # Extract date
        date_str = headers.get("Date", "")
        email_date = self._parse_email_date(date_str) if date_str else datetime.now(UTC)

        # Extract body
        body_text = ""
        body_html = ""
        parts = message.get("payload", {}).get("parts", [])
        if not parts:
            # Simple message without multipart
            body_data = message.get("payload", {}).get("body", {}).get("data")
            if body_data:
                body_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        else:
            # Multipart message
            for part in parts:
                mime_type = part.get("mimeType", "")
                body_data = part.get("body", {}).get("data")
                if body_data:
                    decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                    if mime_type == "text/plain":
                        body_text = decoded
                    elif mime_type == "text/html":
                        body_html = decoded

        # Build email data structure
        email_data = {
            "id": message["id"],
            "threadId": message.get("threadId"),
            "labelIds": message.get("labelIds", []),
            "snippet": message.get("snippet", ""),
            "date": email_date.isoformat(),
            "headers": headers,
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
            "bcc": headers.get("Bcc", ""),
            "body_text": body_text,
            "body_html": body_html,
            "sizeEstimate": message.get("sizeEstimate", 0),
            "historyId": message.get("historyId"),
        }

        # Store the historyId from the message for tracking
        if message.get("historyId"):
            self._current_history_id = str(message.get("historyId"))

        return email_data

    def _fetch_email(self, service: "Resource", message_id: str) -> dict[str, Any]:
        """Fetch full email data from Gmail API.

        Args:
            service: Gmail service instance
            message_id: Gmail message ID

        Returns:
            Email data dictionary with metadata and content

        Raises:
            BackendError: If fetch fails
        """
        try:
            # Get message
            message = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )

            # Use shared parser
            return self._parse_gmail_message(message)

        except Exception as e:
            raise BackendError(
                f"Failed to fetch email {message_id}: {e}",
                backend="gmail",
            ) from e

    # === CacheConnectorMixin required methods ===

    def _read_content_bulk_from_backend(
        self, backend_paths: list[str], context: "OperationContext | None" = None
    ) -> dict[str, bytes]:
        """Read multiple email contents from Gmail API in bulk.

        Uses Gmail's batch API to efficiently fetch multiple messages.

        Args:
            backend_paths: List of paths to email files
            context: Operation context for authentication

        Returns:
            Dict mapping path -> content bytes (only successful reads)
        """
        if not backend_paths:
            return {}

        # Extract message IDs from paths
        path_info: dict[str, str] = {}  # path -> message_id

        for backend_path in backend_paths:
            try:
                # Parse path to extract message ID
                path_parts = backend_path.split("/")
                if len(path_parts) == 3 and path_parts[0] in self.LABEL_FOLDERS:
                    filename = path_parts[2]
                elif len(path_parts) == 1:
                    filename = path_parts[0]
                else:
                    continue

                # Extract message_id from filename (only .yaml files)
                if not filename.endswith(".yaml"):
                    continue

                if not filename.startswith("email-"):
                    continue

                message_id = filename.replace("email-", "").replace(".yaml", "")
                path_info[backend_path] = message_id
            except Exception:
                continue

        if not path_info:
            return {}

        # Collect all message IDs to fetch
        messages_to_fetch = list(path_info.values())

        # Batch fetch messages from Gmail API
        email_cache: dict[str, dict[str, Any]] = {}
        if messages_to_fetch:
            try:
                service = self._get_gmail_service(context)
                fetch_emails_batch(
                    service, messages_to_fetch, self._parse_gmail_message, email_cache
                )
            except Exception:
                pass  # Suppress errors, return partial results

        # Format results
        results: dict[str, bytes] = {}
        for backend_path, message_id in path_info.items():
            if message_id in email_cache:
                try:
                    email_data = email_cache[message_id]
                    results[backend_path] = self._format_email_as_yaml(email_data)
                except Exception:
                    pass  # Skip this path on error

        return results

    def _format_email_as_yaml(self, email_data: dict[str, Any]) -> bytes:
        """Format email data as YAML bytes.

        Args:
            email_data: Email metadata dictionary

        Returns:
            Formatted YAML as bytes
        """
        if yaml is None:
            raise BackendError(
                "PyYAML not installed. Install with: pip install pyyaml",
                backend="gmail",
            )

        # Remove headers from YAML output (but keep body_html and body_text)
        yaml_data = {k: v for k, v in email_data.items() if k != "headers"}

        # Normalize line endings in text bodies
        if "body_text" in yaml_data and yaml_data["body_text"]:
            text = yaml_data["body_text"]
            text = text.replace("\r\n", "\n")
            if "\\n" in text:
                text = text.replace("\\n", "\n")
            yaml_data["body_text"] = text

        # Use custom dumper for literal block scalars
        class LiteralDumper(yaml.SafeDumper):  # type: ignore[name-defined]
            def choose_scalar_style(self):  # type: ignore[no-untyped-def]
                if self.event.value and "\n" in self.event.value:
                    return "|"
                return super().choose_scalar_style()

        def literal_presenter(dumper, data):  # type: ignore[no-untyped-def]
            if isinstance(data, str) and "\n" in data:
                return dumper.represent_scalar("tag:yaml.org,2002:str", data.rstrip(), style="|")
            return dumper.represent_scalar("tag:yaml.org,2002:str", data)

        LiteralDumper.add_representer(str, literal_presenter)

        yaml_output = yaml.dump(  # type: ignore[attr-defined]
            yaml_data,
            Dumper=LiteralDumper,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )
        return yaml_output.encode("utf-8")

    # === Backend interface methods ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content is not supported for Gmail connector (read-only).

        Args:
            content: File content as bytes
            context: Operation context

        Raises:
            BackendError: Always raised (read-only backend)
        """
        raise BackendError(
            "Gmail connector is read-only. Cannot write emails back to Gmail.",
            backend="gmail",
        )

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read email content from cache or Gmail API.

        For connector backends, content_hash is ignored - we use backend_path instead.

        Args:
            content_hash: Ignored for connector backends
            context: Operation context with backend_path

        Returns:
            Email content as YAML bytes

        Raises:
            NexusFileNotFoundError: If email doesn't exist
            BackendError: If read operation fails or PyYAML not installed
        """
        if yaml is None:
            raise BackendError(
                "PyYAML not installed. Install with: pip install pyyaml",
                backend="gmail",
            )

        if not context or not context.backend_path:
            raise BackendError(
                "Gmail connector requires backend_path in OperationContext. "
                "This backend reads files from actual paths, not CAS hashes.",
                backend="gmail",
            )

        # Strip label and thread folder prefix (e.g., "INBOX/thread_id/email-123.yaml" -> "email-123.yaml")
        path_parts = context.backend_path.split("/")
        if len(path_parts) == 3 and path_parts[0] in self.LABEL_FOLDERS:
            # Path is "LABEL/thread_id/email-123.yaml" - use just the filename
            filename = path_parts[2]
        elif len(path_parts) == 1:
            # Already just a filename
            filename = path_parts[0]
        else:
            raise NexusFileNotFoundError(context.backend_path)

        # Extract message_id from path (only .yaml files)
        if not filename.endswith(".yaml"):
            raise NexusFileNotFoundError(context.backend_path)

        if not filename.startswith("email-"):
            raise NexusFileNotFoundError(context.backend_path)

        message_id = filename.replace("email-", "").replace(".yaml", "")

        # Get cache path
        cache_path = self._get_cache_path(context) or context.backend_path

        # Check cache first (if caching enabled)
        if self._has_caching():
            cached = self._read_from_cache(cache_path, original=True)
            if cached and not cached.stale and cached.content_binary:
                return cached.content_binary

        # Fetch from Gmail API
        try:
            service = self._get_gmail_service(context)
            email_data = self._fetch_email(service, message_id)
        except Exception as e:
            raise NexusFileNotFoundError(context.backend_path) from e

        # Format as YAML (includes both body_text and body_html)
        content = self._format_email_as_yaml(email_data)

        # Cache the result
        if self._has_caching():
            try:
                tenant_id = getattr(context, "tenant_id", None)
                self._write_to_cache(
                    path=cache_path,
                    content=content,
                    backend_version=None,  # Emails are immutable, no versioning needed
                    tenant_id=tenant_id,
                )
            except Exception:
                pass  # Don't fail on cache write errors

        return content

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete is not supported for Gmail connector (read-only).

        Args:
            content_hash: Content hash
            context: Operation context

        Raises:
            BackendError: Always raised (read-only backend)
        """
        raise BackendError(
            "Gmail connector is read-only. Cannot delete emails from Gmail.",
            backend="gmail",
        )

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if email exists.

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            True if email exists, False otherwise
        """
        if not context or not context.backend_path:
            return False

        try:
            # Strip label and thread folder prefix (e.g., "INBOX/thread_id/email-123.yaml" -> "email-123.yaml")
            path_parts = context.backend_path.split("/")
            if len(path_parts) == 3 and path_parts[0] in self.LABEL_FOLDERS:
                # Path is "LABEL/thread_id/email-123.yaml" - use just the filename
                filename = path_parts[2]
            elif len(path_parts) == 1:
                # Already just a filename
                filename = path_parts[0]
            else:
                return False

            # Extract message_id from path (only .yaml files)
            if not filename.endswith(".yaml"):
                return False

            if not filename.startswith("email-"):
                return False

            message_id = filename.replace("email-", "").replace(".yaml", "")

            # Try to fetch from Gmail API
            try:
                service = self._get_gmail_service(context)
                service.users().messages().get(userId="me", id=message_id).execute()
                return True
            except Exception:
                return False

        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get email content size.

        Args:
            content_hash: Content hash (ignored)
            context: Operation context with backend_path

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If email doesn't exist
            BackendError: If operation fails
        """
        if context is None or not hasattr(context, "backend_path"):
            raise ValueError("Gmail connector requires backend_path in OperationContext")

        # Read content to get size
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

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory (not supported for Gmail connector).

        Args:
            path: Directory path
            parents: Create parent directories if needed
            exist_ok: Don't raise error if directory exists
            context: Operation context

        Raises:
            BackendError: Always raised (read-only backend)
        """
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
        """Remove directory (not supported for Gmail connector).

        Args:
            path: Directory path
            recursive: Remove non-empty directory
            context: Operation context

        Raises:
            BackendError: Always raised (read-only backend)
        """
        raise BackendError(
            "Gmail connector is read-only. Cannot remove directories.",
            backend="gmail",
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

        # Check if it's a label folder (SENT/, STARRED/, etc.)
        if path in self.LABEL_FOLDERS:
            return True

        # Check if it's a thread folder (SENT/thread_id, etc.)
        path_parts = path.split("/")
        if len(path_parts) == 2 and path_parts[0] in self.LABEL_FOLDERS:
            return True

        # Email files (.yaml) are not directories
        if path.endswith(".yaml"):
            return False

        return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents.

        This method fetches emails from Gmail and lists:
        - Root directory: Label folders (SENT/, STARRED/, IMPORTANT/, INBOX/)
        - Label folders: Thread folders (thread_id_1/, thread_id_2/, ...)
        - Thread folders: Email files for that thread (email-msg_id.yaml)

        Args:
            path: Directory path to list (relative to backend root)
            context: Operation context for authentication

        Returns:
            List of entry names (folders or email files)

        Raises:
            FileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        try:
            path = path.strip("/")

            # Root directory - list label folders
            if not path:
                return [f"{label}/" for label in self.LABEL_FOLDERS]

            # Get Gmail service
            service = self._get_gmail_service(context)

            # Label folder - list thread folders for this label
            if path in self.LABEL_FOLDERS:
                # Fetch emails from Gmail API
                emails = list_emails_by_folder(
                    service,
                    max_results=self.max_message_per_label,
                    folder_filter=[path],
                    silent=True,
                )

                # Extract unique thread IDs
                threads = set()
                for email in emails:
                    if email.get("folder") == path:
                        thread_id = email.get("threadId")
                        if thread_id:
                            threads.add(thread_id)
                return sorted([f"{thread_id}/" for thread_id in threads])

            # Label/Thread folder - list email files for this thread
            path_parts = path.split("/")
            if len(path_parts) == 2 and path_parts[0] in self.LABEL_FOLDERS:
                target_folder = path_parts[0]
                target_thread = path_parts[1]

                # Fetch emails from Gmail API
                emails = list_emails_by_folder(
                    service,
                    max_results=self.max_message_per_label,
                    folder_filter=[target_folder],
                    silent=True,
                )

                # Filter for files in this thread (only .yaml files)
                files = []
                for email in emails:
                    if (
                        email.get("folder") == target_folder
                        and email.get("threadId") == target_thread
                    ):
                        message_id = email["id"]
                        files.append(f"email-{message_id}.yaml")
                return sorted(files)

            # Invalid path
            raise FileNotFoundError(f"Directory not found: {path}")

        except FileNotFoundError:
            raise
        except Exception as e:
            raise BackendError(
                f"Failed to list directory {path}: {e}",
                backend="gmail",
                path=path,
            ) from e
