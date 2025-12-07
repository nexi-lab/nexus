"""Gmail connector backend with OAuth 2.0 authentication.

This is a connector backend that syncs emails from Gmail to a mount,
storing them as structured files organized by date.

Use case: Sync Gmail emails to Nexus mount for search, analysis, and archival.

Storage structure:
    /
    ├── email-{message_id}.yaml      # Email metadata and text content (flat structure)
    ├── .email-{message_id}.html     # Email HTML content for frontend rendering (hidden)
    ├── email-{message_id}.yaml
    ├── .email-{message_id}.html
    └── ...

Key features:
- OAuth 2.0 authentication (user-scoped)
- Incremental sync using Gmail historyId (recommended)
- Fallback to date-based sync for initial sync
- Flat storage structure (all emails in root directory)
- Full email metadata and content in YAML format
- Automatic token refresh via TokenManager
- Smart HTML detection: if body_text contains HTML (detected by <!DOCTYPE html> or <html prefix),
  it's treated as HTML content and moved to the .html file, leaving body_text empty in YAML

Sync strategy:
- Use last_history_id for efficient incremental sync (only fetches changes)
- Falls back to sync_from_date for initial sync or if historyId is too old
- Store the returned historyId from get_last_history_id() for next sync

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - Automatic refresh when expired
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class GmailConnectorBackend(Backend, CacheConnectorMixin):
    """
    Gmail connector backend with OAuth 2.0 authentication.

    This backend syncs emails from Gmail API and stores them as YAML files
    organized by date. Each email is stored with full metadata and content.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Email syncing from a start date
    - Flat storage structure (all emails in root directory)
    - Full email metadata and content
    - Automatic token refresh
    - Persistent caching via CacheConnectorMixin for fast grep/search

    Limitations:
    - No automatic deduplication (each email is a unique file)
    - Requires OAuth tokens for each user
    - Rate limited by Gmail API quotas
    - Emails are stored as YAML files (not editable)
    """

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        sync_from_date: str | None = None,
        last_history_id: str | None = None,
        provider: str = "gmail",
        session_factory=None,  # type: ignore[no-untyped-def]
    ):
        """
        Initialize Gmail connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            sync_from_date: Start date for initial sync (ISO format: YYYY-MM-DD).
                           Only used if last_history_id is not provided.
                           If None, syncs from 30 days ago for initial sync.
            last_history_id: Last synced Gmail historyId for incremental sync.
                           If provided, uses Gmail history API to sync only changes since this ID.
                           If None, performs initial sync using sync_from_date.
            provider: OAuth provider name from config (default: "gmail")
            session_factory: SQLAlchemy session factory for content caching (optional).
                           If provided, enables persistent caching for fast grep/search.

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
            This ensures each user accesses their own Gmail.

            Gmail historyId is the recommended way to do incremental sync:
            - More efficient (only fetches changes, not all messages)
            - Tracks deletions and label changes, not just new messages
            - More reliable than date-based queries
            - Use the historyId from the last sync for subsequent syncs
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
        self.last_history_id = last_history_id

        # Store session factory for caching (CacheConnectorMixin)
        self.session_factory = session_factory

        # Parse sync_from_date (only used if last_history_id is not provided)
        if sync_from_date:
            try:
                self.sync_from_date = datetime.fromisoformat(sync_from_date.replace("Z", "+00:00"))
                if self.sync_from_date.tzinfo is None:
                    self.sync_from_date = self.sync_from_date.replace(tzinfo=UTC)
            except ValueError as e:
                raise BackendError(
                    f"Invalid sync_from_date format: {sync_from_date}. Use ISO format (YYYY-MM-DD)",
                    backend="gmail",
                ) from e
        else:
            # Default to 30 days ago (only used for initial sync)
            self.sync_from_date = datetime.now(UTC) - timedelta(days=30)

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

        # Cache for email data (message_id -> email data)
        self._email_cache: dict[str, dict[str, Any]] = {}

        # Store the latest historyId after sync (for next incremental sync)
        self._current_history_id: str | None = None

        # Store mount_point for updating config after sync
        self._mount_point: str | None = None

        # Lazy import Gmail API (only when needed)
        self._gmail_service = None

    def set_mount_point(self, mount_point: str) -> None:
        """Set the mount point for this backend.

        This allows the backend to update its configuration in the database
        after sync operations.

        Args:
            mount_point: Virtual path where this backend is mounted
        """
        self._mount_point = mount_point

    def get_updated_config(self) -> dict[str, Any] | None:
        """Get updated backend configuration with new last_history_id.

        This should be called after a sync operation to get the updated config
        that includes the new last_history_id for persistence.

        Returns:
            Updated backend config dict with last_history_id, or None if no update needed
        """
        if not self._current_history_id:
            return None  # No sync performed yet

        # Return updated config with new last_history_id
        # Preserve all existing config values and update last_history_id
        updated_config = {
            "token_manager_db": self.token_manager_db,
            "provider": self.provider,
        }

        if self.user_email:
            updated_config["user_email"] = self.user_email

        if self.sync_from_date:
            updated_config["sync_from_date"] = self.sync_from_date.isoformat()

        # Always include the new last_history_id (this is the key update)
        updated_config["last_history_id"] = self._current_history_id

        return updated_config

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
        return build("gmail", "v1", credentials=creds)

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

    def _get_email_path(self, message_id: str, date: datetime, file_type: str = "yaml") -> str:
        """Get file path for an email (flat structure).

        Args:
            message_id: Gmail message ID
            date: Email date (unused in flat structure, kept for compatibility)
            file_type: File type - "yaml" for metadata or "html" for HTML content

        Returns:
            Backend path (e.g., "email-{message_id}.yaml" or ".email-{message_id}.html")
        """
        if file_type == "html":
            return f".email-{message_id}.html"
        return f"email-{message_id}.yaml"

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
                    import base64

                    body_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
            else:
                # Multipart message
                for part in parts:
                    mime_type = part.get("mimeType", "")
                    body_data = part.get("body", {}).get("data")
                    if body_data:
                        import base64

                        decoded = base64.urlsafe_b64decode(body_data).decode(
                            "utf-8", errors="ignore"
                        )
                        if mime_type == "text/plain":
                            body_text = decoded
                        elif mime_type == "text/html":
                            body_html = decoded

            # Build email data structure
            email_data = {
                "id": message_id,
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

        except Exception as e:
            raise BackendError(
                f"Failed to fetch email {message_id}: {e}",
                backend="gmail",
            ) from e

    def _sync_emails(self, context: "OperationContext | None" = None) -> None:
        """Sync emails from Gmail API and store them in cache.

        Uses Gmail history API for incremental sync if last_history_id is provided,
        otherwise falls back to date-based initial sync.

        This method fetches emails and stores them in the internal cache.
        The actual files are created on-demand when read_content is called.

        After sync, the current historyId is stored in _current_history_id, which
        can be retrieved via get_updated_config() to persist in the database.

        Args:
            context: Operation context for authentication

        Raises:
            BackendError: If sync fails
        """
        try:
            service = self._get_gmail_service(context)

            # Use last_history_id if available (from database), otherwise use sync_from_date
            if self.last_history_id:
                # Incremental sync using history API
                logger.info(f"Performing incremental sync from historyId: {self.last_history_id}")
                self._sync_from_history(service, self.last_history_id)
            else:
                # Initial sync using date-based query
                logger.info(f"Performing initial sync from date: {self.sync_from_date}")
                self._sync_from_date(service)

            logger.info(
                f"Synced {len(self._email_cache)} emails from Gmail. "
                f"New historyId: {self._current_history_id}"
            )

        except Exception as e:
            raise BackendError(
                f"Failed to sync emails from Gmail: {e}",
                backend="gmail",
            ) from e

    def _sync_from_history(self, service: "Resource", start_history_id: str) -> None:
        """Sync emails using Gmail history API (incremental sync).

        Args:
            service: Gmail service instance
            start_history_id: Starting historyId for incremental sync

        Raises:
            BackendError: If sync fails
        """
        try:
            # Get profile to get current historyId
            profile = service.users().getProfile(userId="me").execute()
            current_history_id = profile.get("historyId")

            if not current_history_id:
                logger.warning("Could not get current historyId, falling back to date-based sync")
                self._sync_from_date(service)
                return

            # List history records since start_history_id
            history_records = []
            page_token = None
            while True:
                try:
                    result = (
                        service.users()
                        .history()
                        .list(
                            userId="me",
                            startHistoryId=start_history_id,
                            pageToken=page_token,
                            maxResults=500,
                        )
                        .execute()
                    )

                    history_records.extend(result.get("history", []))
                    page_token = result.get("nextPageToken")
                    if not page_token:
                        break
                except Exception as e:
                    # If historyId is too old, Gmail returns 400
                    if "400" in str(e) or "historyId" in str(e).lower():
                        logger.warning(
                            f"HistoryId {start_history_id} is too old, falling back to date-based sync"
                        )
                        self._sync_from_date(service)
                        return
                    raise

            # Process history records
            message_ids_to_fetch = set()
            message_ids_with_label_changes = set()
            message_ids_deleted = set()
            new_messages_count = 0
            label_changes_count = 0

            for record in history_records:
                # Get added messages (new emails)
                for msg_added in record.get("messagesAdded", []):
                    message_id = msg_added.get("message", {}).get("id")
                    if message_id:
                        message_ids_to_fetch.add(message_id)
                        new_messages_count += 1

                # Get messages with labels added (could be new messages or existing messages with label changes)
                for label_change in record.get("labelsAdded", []):
                    message_id = label_change.get("message", {}).get("id")
                    if message_id:
                        message_ids_to_fetch.add(message_id)
                        # Track that this message had label changes (even if already in cache)
                        if message_id not in message_ids_with_label_changes:
                            message_ids_with_label_changes.add(message_id)
                            label_changes_count += 1

                # Get messages with labels removed (existing messages with label changes)
                for label_change in record.get("labelsRemoved", []):
                    message_id = label_change.get("message", {}).get("id")
                    if message_id:
                        message_ids_to_fetch.add(message_id)
                        # Track that this message had label changes (even if already in cache)
                        if message_id not in message_ids_with_label_changes:
                            message_ids_with_label_changes.add(message_id)
                            label_changes_count += 1

                # Track deleted messages (remove from cache)
                for msg_deleted in record.get("messagesDeleted", []):
                    message_id = msg_deleted.get("message", {}).get("id")
                    if message_id:
                        message_ids_deleted.add(message_id)

            # Remove deleted messages from cache
            for message_id in message_ids_deleted:
                if message_id in self._email_cache:
                    del self._email_cache[message_id]
                    logger.debug(f"Removed deleted email {message_id} from cache")

            # Fetch full email data for each message
            # For messages with label changes, always re-fetch to get updated labelIds
            # For new messages, only fetch if not in cache
            fetched_count = 0
            updated_count = 0
            for message_id in message_ids_to_fetch:
                # Re-fetch if:
                # 1. Not in cache (new message), OR
                # 2. In cache but had label changes (need to update labelIds)
                if (
                    message_id not in self._email_cache
                    or message_id in message_ids_with_label_changes
                ):
                    try:
                        email_data = self._fetch_email(service, message_id)
                        was_cached = message_id in self._email_cache
                        self._email_cache[message_id] = email_data
                        fetched_count += 1
                        if message_id in message_ids_with_label_changes:
                            updated_count += 1
                            if was_cached:
                                logger.debug(f"Updated email {message_id} due to label changes")
                    except Exception as e:
                        logger.warning(f"Failed to fetch email {message_id}: {e}")

            # Store current historyId for next sync
            self._current_history_id = current_history_id
            logger.info(
                f"Incremental sync complete: {new_messages_count} new messages, "
                f"{label_changes_count} label changes, {len(message_ids_deleted)} deletions, "
                f"{fetched_count} messages fetched ({updated_count} updated), "
                f"current historyId: {current_history_id}"
            )

        except Exception as e:
            raise BackendError(
                f"Failed to sync from history: {e}",
                backend="gmail",
            ) from e

    def _sync_from_date(self, service: "Resource") -> None:
        """Sync emails using date-based query (initial sync).

        Args:
            service: Gmail service instance

        Raises:
            BackendError: If sync fails
        """
        try:
            # Build query for emails from sync_from_date
            query = f"after:{int(self.sync_from_date.timestamp())}"

            # List messages (fetch all pages)
            messages = []
            page_token = None

            while True:
                result = (
                    service.users()
                    .messages()
                    .list(userId="me", q=query, pageToken=page_token, maxResults=500)
                    .execute()
                )

                messages.extend(result.get("messages", []))
                page_token = result.get("nextPageToken")
                if not page_token:
                    break

            # Fetch full email data for each message
            for msg in messages:
                message_id = msg["id"]
                if message_id not in self._email_cache:
                    try:
                        email_data = self._fetch_email(service, message_id)
                        self._email_cache[message_id] = email_data
                    except Exception as e:
                        logger.warning(f"Failed to fetch email {message_id}: {e}")

            # Get current historyId for future incremental syncs
            try:
                profile = service.users().getProfile(userId="me").execute()
                self._current_history_id = profile.get("historyId")
                logger.info(f"Initial sync complete, current historyId: {self._current_history_id}")
            except Exception as e:
                logger.warning(f"Could not get current historyId: {e}")

        except Exception as e:
            raise BackendError(
                f"Failed to sync from date: {e}",
                backend="gmail",
            ) from e

    def get_last_history_id(self) -> str | None:
        """Get the last synced historyId.

        This can be used to persist the historyId for the next sync.

        Returns:
            Last synced historyId, or None if no sync has been performed
        """
        return self._current_history_id or self.last_history_id

    # === CacheConnectorMixin required methods ===

    def _read_content_from_backend(
        self, backend_path: str, context: "OperationContext | None" = None
    ) -> bytes:
        """Read content from Gmail API (required by CacheConnectorMixin).

        This is the backend-specific implementation called by the cache layer.

        Args:
            backend_path: Path to the email file (e.g., "email-123.yaml")
            context: Operation context for authentication

        Returns:
            Email content as bytes

        Raises:
            NexusFileNotFoundError: If email doesn't exist
        """
        # Extract message_id from path
        is_html = backend_path.endswith(".html")
        is_yaml = backend_path.endswith(".yaml")

        if not is_html and not is_yaml:
            raise NexusFileNotFoundError(backend_path)

        if is_html:
            if not backend_path.startswith(".email-"):
                raise NexusFileNotFoundError(backend_path)
            message_id = backend_path.replace(".email-", "").replace(".html", "")
        else:
            if not backend_path.startswith("email-"):
                raise NexusFileNotFoundError(backend_path)
            message_id = backend_path.replace("email-", "").replace(".yaml", "")

        # Check cache first
        if message_id not in self._email_cache:
            # Fetch from Gmail API
            try:
                service = self._get_gmail_service(context)
                email_data = self._fetch_email(service, message_id)
                self._email_cache[message_id] = email_data
            except Exception as e:
                raise NexusFileNotFoundError(backend_path) from e

        email_data = self._email_cache[message_id]

        # Return content based on file type
        if is_html:
            body_html = email_data.get("body_html", "")
            body_text = email_data.get("body_text", "")
            body_text_is_html = body_text.strip().startswith(("<!DOCTYPE", "<!doctype", "<html"))
            if body_html:
                return body_html.encode("utf-8")
            elif body_text_is_html:
                return body_text.encode("utf-8")
            else:
                return b""
        else:
            # Return YAML content (same logic as read_content but simplified)
            return self._format_email_as_yaml(email_data)

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

        # Remove HTML and headers from YAML output
        yaml_data = {k: v for k, v in email_data.items() if k not in ("body_html", "headers")}

        # Check if body_text is HTML
        body_text = yaml_data.get("body_text", "")
        body_text_is_html = body_text.strip().startswith(("<!DOCTYPE", "<!doctype", "<html"))

        if body_text_is_html:
            yaml_data["body_text"] = ""
        elif body_text:
            # Normalize line endings
            text = body_text.replace("\r\n", "\n")
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

    def _list_files_recursive(
        self, path: str, context: "OperationContext | None" = None
    ) -> list[str]:
        """List all files recursively (required by CacheConnectorMixin).

        Args:
            path: Starting path (empty string for root)
            context: Operation context

        Returns:
            List of backend-relative file paths
        """
        # Gmail uses flat structure - just return all files from cache
        # Trigger sync if cache is empty
        if not self._email_cache:
            self._sync_emails(context)

        # Return all email files (both .yaml and .html)
        files = []
        for email_data in self._email_cache.values():
            message_id = email_data["id"]
            files.append(f"email-{message_id}.yaml")
            files.append(f".email-{message_id}.html")

        return files

    def _create_read_context(
        self,
        backend_path: str,
        virtual_path: str,
        context: "OperationContext | None" = None,
    ) -> "OperationContext":
        """Create operation context with paths set (required by CacheConnectorMixin).

        Args:
            backend_path: Backend-relative path
            virtual_path: Full virtual path (with mount point)
            context: Original context (optional)

        Returns:
            New context with paths set
        """
        from nexus.core.permissions import OperationContext

        if context:
            # Clone existing context and add paths
            return OperationContext(
                user=context.user_id if hasattr(context, "user_id") else context.user,
                groups=context.groups if hasattr(context, "groups") else [],
                tenant_id=context.tenant_id if hasattr(context, "tenant_id") else "default",
                subject_type=context.subject_type if hasattr(context, "subject_type") else "user",
                subject_id=context.subject_id if hasattr(context, "subject_id") else None,
                backend_path=backend_path,
                virtual_path=virtual_path,
            )
        else:
            # Create minimal context with paths
            return OperationContext(
                user=self.user_email or "anonymous",
                groups=[],
                tenant_id="default",
                backend_path=backend_path,
                virtual_path=virtual_path,
            )

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

        if context is None or not hasattr(context, "backend_path") or context.backend_path is None:
            raise BackendError(
                "Gmail connector requires OperationContext with backend_path",
                backend="gmail",
            )

        # Extract message_id from path (e.g., "email-{message_id}.yaml" or ".email-{message_id}.html")
        is_html = context.backend_path.endswith(".html")
        is_yaml = context.backend_path.endswith(".yaml")

        if not is_html and not is_yaml:
            raise NexusFileNotFoundError(context.backend_path)

        if is_html:
            # Handle hidden HTML file: .email-{message_id}.html
            if not context.backend_path.startswith(".email-"):
                raise NexusFileNotFoundError(context.backend_path)
            message_id = context.backend_path.replace(".email-", "").replace(".html", "")
        else:
            # Handle YAML file: email-{message_id}.yaml
            if not context.backend_path.startswith("email-"):
                raise NexusFileNotFoundError(context.backend_path)
            message_id = context.backend_path.replace("email-", "").replace(".yaml", "")

        # Check cache first
        if message_id not in self._email_cache:
            # Try to fetch from Gmail API
            try:
                service = self._get_gmail_service(context)
                email_data = self._fetch_email(service, message_id)
                self._email_cache[message_id] = email_data
            except Exception as e:
                raise NexusFileNotFoundError(context.backend_path) from e

        email_data = self._email_cache[message_id]

        # Check if body_text contains HTML (when body_html is missing)
        body_text = email_data.get("body_text", "")
        body_html = email_data.get("body_html", "")
        body_text_is_html = body_text.strip().startswith(("<!DOCTYPE", "<!doctype", "<html"))

        if is_html:
            # Return HTML content - use body_html if available, otherwise body_text if it's HTML
            if body_html:
                return body_html.encode("utf-8")
            elif body_text_is_html:
                return body_text.encode("utf-8")
            else:
                return b""  # No HTML content available
        else:
            # Return email as YAML (without body_html and headers)
            yaml_data = {k: v for k, v in email_data.items() if k not in ("body_html", "headers")}

            # If body_text contains HTML, set it to empty in YAML (HTML is in separate .html file)
            if body_text_is_html:
                yaml_data["body_text"] = ""
            elif "body_text" in yaml_data and yaml_data["body_text"]:
                # Convert \r\n to actual newlines, and handle escaped newlines
                text = yaml_data["body_text"]

                # Replace Windows line endings with Unix line endings
                text = text.replace("\r\n", "\n")
                # Handle case where newlines are escaped as literal backslash-n
                # (shouldn't normally happen with Gmail API, but just in case)
                if "\\n" in text:
                    text = text.replace("\\n", "\n")

                yaml_data["body_text"] = text

            # Use literal block scalar style for body_text (for nice formatting)
            # Create custom dumper that uses literal style for multiline strings
            class LiteralDumper(yaml.SafeDumper):
                def write_line_break(self, data=None):
                    super().write_line_break(data)

                def choose_scalar_style(self):
                    # Force literal style for multiline strings
                    if self.event.value and "\n" in self.event.value:
                        return "|"
                    return super().choose_scalar_style()

            def literal_presenter(dumper, data):
                if isinstance(data, str) and "\n" in data:
                    # Strip trailing whitespace/newlines before dumping
                    # Return with style='|' but PyYAML will use choose_scalar_style() to enforce it
                    return dumper.represent_scalar(
                        "tag:yaml.org,2002:str", data.rstrip(), style="|"
                    )
                return dumper.represent_scalar("tag:yaml.org,2002:str", data)

            LiteralDumper.add_representer(str, literal_presenter)

            yaml_output = yaml.dump(
                yaml_data,
                Dumper=LiteralDumper,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
            return yaml_output.encode("utf-8")

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
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
        if context is None or not hasattr(context, "backend_path"):
            return False

        try:
            # Extract message_id from path (handles both .yaml and .html)
            is_html = context.backend_path.endswith(".html")
            is_yaml = context.backend_path.endswith(".yaml")

            if not is_html and not is_yaml:
                return False

            if is_html:
                # Handle hidden HTML file: .email-{message_id}.html
                if not context.backend_path.startswith(".email-"):
                    return False
                message_id = context.backend_path.replace(".email-", "").replace(".html", "")
            else:
                # Handle YAML file: email-{message_id}.yaml
                if not context.backend_path.startswith("email-"):
                    return False
                message_id = context.backend_path.replace("email-", "").replace(".yaml", "")

            # Check cache or try to fetch
            if message_id in self._email_cache:
                return True

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

        # Email files (.yaml and .html) are not directories
        if path.endswith(".yaml") and path.startswith("email-"):
            return False
        if path.endswith(".html") and path.startswith(".email-"):
            return False

        return False

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents.

        This method syncs emails from Gmail if needed, then lists all
        emails in a flat directory structure.

        Args:
            path: Directory path to list (relative to backend root)
            context: Operation context for authentication

        Returns:
            List of entry names (email files)

        Raises:
            FileNotFoundError: If directory doesn't exist
            BackendError: If operation fails
        """
        try:
            path = path.strip("/")

            # Sync emails if cache is empty
            # If cache is empty, force a sync (even if last_history_id exists, we need initial data)
            if not self._email_cache:
                self._sync_emails(context)
                # If still empty after sync, try date-based sync as fallback
                if not self._email_cache and self.last_history_id:
                    logger.info(
                        "Cache still empty after incremental sync, falling back to date-based sync"
                    )
                    # Temporarily clear last_history_id to force date-based sync
                    original_history_id = self.last_history_id
                    self.last_history_id = None
                    try:
                        self._sync_emails(context)
                    finally:
                        # Restore original history_id
                        self.last_history_id = original_history_id

            # Root directory - list all email files directly (both .yaml and .html)
            if not path:
                files = []
                for email_data in self._email_cache.values():
                    message_id = email_data["id"]
                    files.append(f"email-{message_id}.yaml")
                    files.append(f".email-{message_id}.html")
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
