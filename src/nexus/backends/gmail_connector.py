"""Gmail connector backend with OAuth 2.0 authentication and caching support.

This is a READ-ONLY connector that syncs Gmail messages to Nexus using OAuth credentials
and caches them locally for fast search and access. Writing/deleting emails is not supported.

Use case: Personal Gmail integration where users mount their Gmail messages into
Nexus workspace for searching, analysis, and AI operations.

Storage structure:
    Gmail/
    ├── inbox/
    │   ├── <message_id>.yaml        # Email metadata and bodies (YAML format)
    │   └── .<message_id>.html       # HTML body (if present, hidden file)
    ├── sent/
    │   ├── <message_id>.yaml
    │   └── .<message_id>.html
    └── drafts/
        ├── <message_id>.yaml
        └── .<message_id>.html

Key features:
- OAuth 2.0 authentication (user-scoped)
- Read-only access (no write/delete operations)
- Incremental syncing via Gmail History API (uses historyId)
- Email syncing with cache support via CacheConnectorMixin
- Message search and filtering
- Full-text extraction from emails
- Automatic token refresh via TokenManager
- Label/folder support (INBOX, SENT, DRAFTS, etc.)

Incremental sync:
    The connector uses Gmail's historyId for efficient incremental syncing:
    - historyId is a global mailbox state marker (not per-message or per-label)
    - First sync: Fetches all messages and returns historyId
    - Subsequent syncs: Only fetches changes (new/deleted messages) since historyId
    - historyId should be stored in mount's backend_config and passed to sync()
    - After sync, new historyId is returned in SyncResult.history_id

Authentication:
    Uses OAuth 2.0 flow via TokenManager:
    - User authorizes via browser
    - Tokens stored encrypted in database
    - Automatic refresh when expired
    - Required scope: gmail.readonly
"""

import base64
import email
import logging
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend
from nexus.backends.cache_mixin import CacheConnectorMixin
from nexus.core.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.core.permissions import OperationContext

logger = logging.getLogger(__name__)


class GmailConnectorBackend(Backend, CacheConnectorMixin):
    """
    Gmail connector backend with OAuth 2.0 authentication and caching.

    This is a READ-ONLY backend that syncs Gmail messages to Nexus and caches them
    locally for fast access and search. Each message is stored as a virtual file.

    Features:
    - OAuth 2.0 authentication (per-user credentials)
    - Read-only access (no write/delete operations)
    - Incremental syncing using Gmail History API (historyId)
    - Message caching via CacheConnectorMixin
    - Label/folder support (inbox, sent, drafts, etc.)
    - Full-text extraction from emails
    - Full-text search via cache
    - Automatic token refresh

    Incremental Sync:
    - historyId is passed via sync(history_id=...) from backend_config
    - After sync, new historyId is returned in result.history_id
    - Caller should persist historyId back to backend_config for next sync

    Limitations:
    - Read-only (no write/delete operations)
    - Rate limited by Gmail API quotas
    - Large messages may not be cached (configurable)
    """

    def __init__(
        self,
        token_manager_db: str,
        user_email: str | None = None,
        provider: str = "gmail",
        db_session: Any | None = None,
        session_factory: Any | None = None,
        max_results: int = 100,
        labels: list[str] | None = None,
    ):
        """
        Initialize Gmail connector backend.

        Args:
            token_manager_db: Path to TokenManager database (e.g., ~/.nexus/nexus.db)
            user_email: Optional user email for OAuth lookup. If None, uses authenticated
                       user from OperationContext (recommended for multi-user scenarios)
            provider: OAuth provider name from config (default: "gmail")
            db_session: SQLAlchemy session for caching (deprecated, use session_factory)
            session_factory: Session factory (e.g., metadata_store.SessionLocal) for
                           caching support. Preferred over db_session.
            max_results: Maximum messages to fetch per sync (default: 100)
            labels: List of Gmail labels to sync (default: ["INBOX"])

        Note:
            For single-user scenarios (demos), set user_email explicitly.
            For multi-user production, leave user_email=None to auto-detect from context.
        """
        logger.info(
            f"[GMAIL-INIT] Initializing Gmail connector: user_email={user_email}, "
            f"provider={provider}, max_results={max_results}"
        )

        # Import TokenManager here to avoid circular imports
        from nexus.server.auth.token_manager import TokenManager

        # Support both file paths and database URLs
        if token_manager_db.startswith(("postgresql://", "sqlite://", "mysql://")):
            self.token_manager = TokenManager(db_url=token_manager_db)
        else:
            self.token_manager = TokenManager(db_path=token_manager_db)

        self.user_email = user_email  # None means use context.user_id
        self.provider = provider

        # Store session info for caching support (CacheConnectorMixin)
        # Prefer session_factory (creates fresh sessions) over db_session
        self.session_factory = session_factory
        self.db_session = db_session  # Legacy support

        # Warn if using deprecated db_session parameter
        if db_session is not None and session_factory is None:
            import warnings

            warnings.warn(
                "The 'db_session' parameter is deprecated and will be removed in a future version. "
                "Use 'session_factory' instead for better session management.",
                DeprecationWarning,
                stacklevel=2,
            )

        self.max_results = max_results
        self.labels = labels or ["INBOX"]

        # Register OAuth provider using factory (loads from config)
        self._register_oauth_provider()

        # Cache for message metadata
        self._message_cache: dict[str, dict[str, Any]] = {}

        # Lazy import Gmail API (only when needed)
        self._gmail_service = None

    def _register_oauth_provider(self) -> None:
        """Register OAuth provider with TokenManager using OAuthProviderFactory."""
        try:
            from nexus.server.auth.oauth_factory import OAuthProviderFactory

            # Create factory (loads from oauth.yaml config)
            factory = OAuthProviderFactory()

            # Create provider instance from config
            try:
                provider_instance = factory.create_provider(
                    name=self.provider,
                )
                # Register with TokenManager
                self.token_manager.register_provider(self.provider, provider_instance)
                logger.info(f"✓ Registered OAuth provider '{self.provider}' for Gmail backend")
            except ValueError as e:
                logger.warning(
                    f"OAuth provider '{self.provider}' not available: {e}. "
                    "OAuth flow must be initiated manually via the Integrations page."
                )
        except Exception as e:
            logger.error(f"Failed to register OAuth provider: {e}")

    @property
    def name(self) -> str:
        """Backend identifier name."""
        return "gmail"

    @property
    def user_scoped(self) -> bool:
        """This backend requires per-user OAuth credentials."""
        return True

    def _has_caching(self) -> bool:
        """Check if caching is enabled (session factory or db_session available)."""
        return self.session_factory is not None or self.db_session is not None

    def _create_yaml_content(
        self, headers: dict[str, str], text_body: str, labels: list[str] | None = None
    ) -> str:
        """
        Create YAML content with literal block scalar style for text_body.

        This ensures that multi-line text bodies are represented with the | (pipe)
        notation in YAML, preserving newlines and making the output more readable.

        Args:
            headers: Email headers dict
            text_body: Email text body
            labels: Gmail labels (optional)

        Returns:
            YAML string with literal block scalar for text_body
        """
        import yaml

        # Custom representer for strings with newlines
        # This forces text_body to use literal block scalar style (|)
        class LiteralString(str):
            """String subclass to force literal block scalar style in YAML."""

            pass

        def literal_representer(dumper: yaml.Dumper, data: LiteralString) -> yaml.Node:
            """Represent LiteralString as literal block scalar (|)."""
            if "\n" in data:
                return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
            return dumper.represent_scalar("tag:yaml.org,2002:str", data)

        yaml.add_representer(LiteralString, literal_representer)

        # Normalize line endings (CRLF -> LF) before creating LiteralString
        # Email content uses RFC 822 format with CRLF, but we want clean LF for YAML
        normalized_text = text_body.replace("\r\n", "\n").replace("\r", "\n")

        # Create YAML structure with LiteralString for text_body
        yaml_data = {
            "headers": headers,
            "text_body": LiteralString(normalized_text),
        }

        # Add labels if present
        if labels:
            yaml_data["labels"] = labels

        return yaml.dump(yaml_data, default_flow_style=False, allow_unicode=True)

    def _get_gmail_service(self, context: "OperationContext | None" = None) -> "Resource":
        """Get Gmail API service with user's OAuth credentials.

        Args:
            context: Operation context (provides user_id if user_email not configured)

        Returns:
            Gmail API service instance

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
            # Default to 'default' tenant if not specified
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
                backend="gmail",
            ) from e

        # Build Gmail service with OAuth token
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("gmail", "v1", credentials=creds)

    def _fetch_messages_initial(
        self,
        service: "Resource",
        label_ids: list[str] | None = None,
        query: str | None = None,
        max_results: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch messages for initial sync (full fetch).

        Args:
            service: Gmail API service
            label_ids: List of label IDs to filter (default: None for all)
            query: Gmail search query (default: None)
            max_results: Maximum messages to fetch (default: self.max_results)

        Returns:
            Tuple of (message_list, history_id)

        Raises:
            BackendError: If fetch fails
        """
        try:
            results_per_page = min(max_results or self.max_results, 500)
            messages = []
            latest_history_id = None

            # Build request parameters
            params: dict[str, Any] = {
                "userId": "me",
                "maxResults": results_per_page,
            }
            if label_ids:
                params["labelIds"] = label_ids
            if query:
                params["q"] = query

            # Fetch messages (with pagination)
            while True:
                response = service.users().messages().list(**params).execute()
                batch = response.get("messages", [])
                messages.extend(batch)

                # Capture the latest historyId from response
                if "historyId" in response:
                    latest_history_id = response["historyId"]

                # Check if we've reached max_results
                if max_results and len(messages) >= max_results:
                    messages = messages[:max_results]
                    break

                # Check if there are more pages
                next_page_token = response.get("nextPageToken")
                if not next_page_token:
                    break

                params["pageToken"] = next_page_token

            logger.info(
                f"Initial sync: Fetched {len(messages)} messages from Gmail (historyId: {latest_history_id})"
            )
            return messages, latest_history_id

        except Exception as e:
            raise BackendError(
                f"Failed to fetch messages from Gmail: {e}",
                backend="gmail",
            ) from e

    def _fetch_messages_incremental(
        self,
        service: "Resource",
        start_history_id: str,
        label_ids: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str], str | None]:
        """Fetch message changes since last sync (incremental sync using History API).

        Args:
            service: Gmail API service
            start_history_id: History ID from last sync
            label_ids: List of label IDs to filter

        Returns:
            Tuple of (messages_added, messages_deleted, new_history_id)

        Raises:
            BackendError: If fetch fails
        """
        try:
            messages_added = []
            messages_deleted = []
            latest_history_id = None

            # Build request parameters for history list
            params: dict[str, Any] = {
                "userId": "me",
                "startHistoryId": start_history_id,
                "historyTypes": ["messageAdded", "messageDeleted"],
            }
            if label_ids:
                params["labelId"] = label_ids[0]  # History API accepts single labelId

            # Fetch history (with pagination)
            try:
                while True:
                    response = service.users().history().list(**params).execute()

                    # Process history records
                    history_records = response.get("history", [])
                    for record in history_records:
                        # Handle messages added
                        for msg_added in record.get("messagesAdded", []):
                            if label_ids:
                                # Check if message has the label we're interested in
                                msg_labels = msg_added.get("message", {}).get("labelIds", [])
                                if any(label in msg_labels for label in label_ids):
                                    messages_added.append(msg_added["message"])
                            else:
                                messages_added.append(msg_added["message"])

                        # Handle messages deleted
                        for msg_deleted in record.get("messagesDeleted", []):
                            messages_deleted.append(msg_deleted["message"]["id"])

                    # Capture the latest historyId
                    if "historyId" in response:
                        latest_history_id = response["historyId"]

                    # Check if there are more pages
                    next_page_token = response.get("nextPageToken")
                    if not next_page_token:
                        break

                    params["pageToken"] = next_page_token

                logger.info(
                    f"Incremental sync: {len(messages_added)} added, {len(messages_deleted)} deleted "
                    f"(historyId: {start_history_id} -> {latest_history_id})"
                )
                return messages_added, messages_deleted, latest_history_id

            except Exception as e:
                # If history request fails (e.g., historyId too old), fall back to full sync
                if "404" in str(e) or "historyId" in str(e).lower():
                    logger.warning(
                        f"History ID {start_history_id} is no longer valid. "
                        "Falling back to full sync."
                    )
                    return [], [], None
                raise

        except Exception as e:
            raise BackendError(
                f"Failed to fetch message history from Gmail: {e}",
                backend="gmail",
            ) from e

    def _get_message_content(
        self, service: "Resource", message_id: str
    ) -> tuple[dict[str, str], str, str, list[str], bytes]:
        """Get full message content from Gmail API.

        Args:
            service: Gmail API service
            message_id: Message ID

        Returns:
            Tuple of (headers_dict, text_body, html_body, labels, raw_bytes)

        Raises:
            BackendError: If fetch fails
        """
        try:
            # Fetch full message with metadata to get labels
            message = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )

            # Extract labels from message metadata
            labels = message.get("labelIds", [])

            # Get raw content from payload
            raw_data = message.get("raw")
            if raw_data:
                # If raw is available, use it
                raw_bytes = base64.urlsafe_b64decode(raw_data)
            else:
                # Fallback: fetch raw format separately
                raw_message = (
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="raw")
                    .execute()
                )
                raw_data = raw_message.get("raw", "")
                raw_bytes = base64.urlsafe_b64decode(raw_data)

            # Parse email
            msg = email.message_from_bytes(raw_bytes)

            # Extract headers (including cc and bcc)
            headers = {
                "from": msg.get("From", "Unknown"),
                "to": msg.get("To", "Unknown"),
                "subject": msg.get("Subject", "No Subject"),
                "date": msg.get("Date", "Unknown"),
            }

            # Add cc and bcc if present
            cc = msg.get("Cc")
            if cc:
                headers["cc"] = cc

            bcc = msg.get("Bcc")
            if bcc:
                headers["bcc"] = bcc

            # Extract text and HTML bodies
            text_body, html_body = self._extract_text_and_html_from_message(msg)

            return headers, text_body, html_body, labels, raw_bytes

        except Exception as e:
            raise BackendError(
                f"Failed to get message content for {message_id}: {e}",
                backend="gmail",
                path=message_id,
            ) from e

    def _extract_text_from_message(self, msg: email.message.Message) -> str:
        """Extract plain text from email message.

        Args:
            msg: Email message object

        Returns:
            Plain text content
        """
        text_parts = []

        # Add headers
        headers = [
            f"From: {msg.get('From', 'Unknown')}",
            f"To: {msg.get('To', 'Unknown')}",
            f"Subject: {msg.get('Subject', 'No Subject')}",
            f"Date: {msg.get('Date', 'Unknown')}",
            "",
        ]
        text_parts.append("\n".join(headers))

        # Extract body
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or "utf-8"
                            text_parts.append(payload.decode(charset, errors="ignore"))
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    text_parts.append(payload.decode(charset, errors="ignore"))
            except Exception:
                pass

        return "\n\n".join(text_parts)

    def _extract_text_and_html_from_message(self, msg: email.message.Message) -> tuple[str, str]:
        """Extract both plain text and HTML from email message.

        Args:
            msg: Email message object

        Returns:
            Tuple of (text_body, html_body)
        """
        text_body_parts = []
        html_body = ""

        # Extract body content
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                try:
                    payload = part.get_payload(decode=True)
                    if not payload:
                        continue

                    charset = part.get_content_charset() or "utf-8"
                    decoded = payload.decode(charset, errors="ignore")

                    if content_type == "text/plain":
                        text_body_parts.append(decoded)
                    elif content_type == "text/html" and not html_body:
                        # Take the first HTML part
                        html_body = decoded
                except Exception:
                    pass
        else:
            content_type = msg.get_content_type()
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    decoded = payload.decode(charset, errors="ignore")

                    if content_type == "text/plain":
                        text_body_parts.append(decoded)
                    elif content_type == "text/html":
                        html_body = decoded
            except Exception:
                pass

        text_body = "\n\n".join(text_body_parts)
        return text_body, html_body

    def sync(
        self,
        path: str | None = None,
        mount_point: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_file_size: int | None = None,
        generate_embeddings: bool = True,
        context: "OperationContext | None" = None,
        history_id: str | None = None,
    ) -> Any:
        """Sync Gmail messages to cache using incremental sync (History API).

        This is the main entry point for syncing emails from Gmail to the local cache.
        Uses historyId for efficient incremental syncing - only fetches changes since
        last sync instead of all messages.

        The historyId is a global mailbox state marker that should be:
        1. Passed in from the mount's backend_config (history_id parameter)
        2. Stored back to backend_config after sync (returned in result.history_id)

        Args:
            path: Specific label/path to sync (e.g., "INBOX", "SENT")
            mount_point: Mount point path (REQUIRED for cache to work - used to construct full virtual paths)
            include_patterns: Not used for Gmail (reserved for future)
            exclude_patterns: Not used for Gmail (reserved for future)
            max_file_size: Maximum message size to cache (default: MAX_CACHE_FILE_SIZE)
            generate_embeddings: Generate embeddings for semantic search
            context: Operation context
            history_id: Current mailbox history ID from previous sync (stored in backend_config)

        Returns:
            SyncResult with statistics, including new history_id attribute to save to backend_config

        Raises:
            ValueError: If mount_point is not provided (required for caching)
        """
        from nexus.backends.cache_mixin import SyncResult

        result = SyncResult()
        new_history_id = history_id  # Start with current history_id

        # Validate that mount_point is provided (required for cache path consistency)
        if not mount_point:
            raise ValueError(
                "mount_point is required for Gmail sync (needed for cache path consistency)"
            )

        try:
            service = self._get_gmail_service(context)

            # Determine which labels to sync
            # Treat empty string, "/", or None as "sync all labels"
            if path and path.strip() and path.strip() != "/":
                labels_to_sync = [path.strip()]
            else:
                labels_to_sync = self.labels

            logger.info(
                f"[GMAIL-SYNC] path={repr(path)}, self.labels={self.labels}, labels_to_sync={labels_to_sync}"
            )

            for label in labels_to_sync:
                logger.info(f"[GMAIL-SYNC] Processing label: {repr(label)}")
                messages_to_process = []
                messages_to_delete = []
                label_history_id = None

                if history_id:
                    # Incremental sync: fetch only changes since last sync
                    logger.info(f"Incremental sync for {label} (historyId: {history_id})")
                    messages_added, messages_deleted, label_history_id = (
                        self._fetch_messages_incremental(
                            service,
                            start_history_id=history_id,
                            label_ids=[label],
                        )
                    )

                    # If incremental sync failed (e.g., history too old), fall back to full sync
                    if label_history_id is None:
                        logger.info(f"Falling back to full sync for {label}")
                        messages, label_history_id = self._fetch_messages_initial(
                            service,
                            label_ids=[label],
                            max_results=self.max_results,
                        )
                        messages_to_process = messages
                    else:
                        messages_to_process = messages_added
                        messages_to_delete = messages_deleted
                else:
                    # Initial sync: fetch all messages
                    logger.info(f"Initial sync for {label}")
                    messages, label_history_id = self._fetch_messages_initial(
                        service,
                        label_ids=[label],
                        max_results=self.max_results,
                    )
                    messages_to_process = messages

                result.files_scanned += len(messages_to_process)

                # Process added/updated messages
                for msg_meta in messages_to_process:
                    msg_id = msg_meta["id"]
                    # Construct full virtual paths with mount point for cache consistency
                    virtual_path = f"{mount_point}/{label.lower()}/{msg_id}.yaml"
                    html_path = f"{mount_point}/{label.lower()}/.{msg_id}.html"
                    logger.info(
                        f"[GMAIL-SYNC] Constructed paths: virtual_path={virtual_path}, html_path={html_path}"
                    )

                    try:
                        # Fetch full message content
                        headers, text_body, html_body, labels, raw_bytes = (
                            self._get_message_content(service, msg_id)
                        )

                        # Check size
                        max_size = max_file_size or self.MAX_CACHE_FILE_SIZE
                        if len(raw_bytes) > max_size:
                            result.files_skipped += 1
                            logger.debug(f"Skipping large message {msg_id}: {len(raw_bytes)} bytes")
                            continue

                        # Get tenant_id from context
                        tenant_id = None
                        if context and hasattr(context, "tenant_id"):
                            tenant_id = context.tenant_id

                        # Create YAML content with headers, text body, and labels
                        # HTML body is stored separately in .{message_id}.html file
                        yaml_content = self._create_yaml_content(headers, text_body, labels)
                        yaml_bytes = yaml_content.encode("utf-8")

                        # Concatenate all content for full-text search
                        searchable_text = (
                            f"From: {headers['from']}\n"
                            f"To: {headers['to']}\n"
                            f"Subject: {headers['subject']}\n"
                            f"Date: {headers['date']}\n\n"
                            f"{text_body}"
                        )

                        # Write YAML to cache if caching is enabled
                        if self._has_caching():
                            import contextlib

                            with contextlib.suppress(Exception):
                                self._write_to_cache(
                                    path=virtual_path,
                                    content=yaml_bytes,
                                    content_text=searchable_text,
                                    content_type="full",
                                    backend_version=None,  # Gmail doesn't have versions
                                    parsed_from="gmail",
                                    parse_metadata={"message_id": msg_id, "label": label},
                                    tenant_id=tenant_id,
                                )

                            # Write HTML body to separate file if present
                            if html_body:
                                with contextlib.suppress(Exception):
                                    html_bytes = html_body.encode("utf-8")
                                    self._write_to_cache(
                                        path=html_path,
                                        content=html_bytes,
                                        content_text=html_body,
                                        content_type="full",
                                        backend_version=None,
                                        parsed_from="gmail",
                                        parse_metadata={
                                            "message_id": msg_id,
                                            "label": label,
                                            "type": "html_body",
                                        },
                                        tenant_id=tenant_id,
                                    )

                        result.files_synced += 1
                        result.bytes_synced += len(yaml_bytes)
                        if html_body:
                            result.files_synced += 1
                            result.bytes_synced += len(html_body.encode("utf-8"))

                        # Generate embeddings if requested and caching is enabled
                        if generate_embeddings and self._has_caching():
                            import contextlib

                            with contextlib.suppress(Exception):
                                self._generate_embeddings(virtual_path)
                                result.embeddings_generated += 1

                    except Exception as e:
                        result.errors.append(f"Failed to sync message {msg_id}: {e}")
                        logger.error(f"Failed to sync message {msg_id}: {e}")

                # Process deleted messages
                for msg_id in messages_to_delete:
                    # Construct full virtual paths with mount point for cache consistency
                    virtual_path = f"{mount_point}/{label.lower()}/{msg_id}.yaml"
                    html_path = f"{mount_point}/{label.lower()}/.{msg_id}.html"

                    # Invalidate cache for deleted message if caching is enabled
                    if self._has_caching():
                        import contextlib

                        with contextlib.suppress(Exception):
                            self._invalidate_cache(path=virtual_path, delete=True)
                            self._invalidate_cache(path=html_path, delete=True)
                            logger.debug(f"Removed deleted message {msg_id} from cache")

                # Update the global history ID with the latest from this label sync
                if label_history_id:
                    new_history_id = label_history_id

            # Store the new history_id in the result for caller to save to backend_config
            result.history_id = new_history_id  # type: ignore[attr-defined]
            logger.info(f"Gmail sync completed: {result} (new historyId: {new_history_id})")
            return result

        except Exception as e:
            result.errors.append(f"Failed to sync Gmail: {e}")
            result.history_id = new_history_id  # type: ignore[attr-defined]
            logger.error(f"Failed to sync Gmail: {e}")
            return result

    # ===  Backend Interface Methods ===

    def write_content(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """
        Write content to Gmail (not supported - read-only connector).

        Gmail connector is read-only. Writing emails is not supported.

        Args:
            content: Email content as bytes (RFC 822 format)
            context: Operation context

        Returns:
            Content hash (message ID)

        Raises:
            BackendError: Always raised - write operations not supported
        """
        raise BackendError(
            "Gmail connector is read-only. Writing/sending emails is not supported.",
            backend="gmail",
        )

    def read_content(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """
        Read email content from Gmail by message ID.

        Args:
            content_hash: Message ID (or virtual path for .html files)
            context: Operation context

        Returns:
            Email content as bytes (YAML for .yaml files, HTML for .html files)

        Raises:
            NexusFileNotFoundError: If message doesn't exist
            BackendError: If read operation fails
        """
        # Get virtual path for cache lookup
        virtual_path = (
            context.backend_path if context and hasattr(context, "backend_path") else content_hash
        )
        if context and hasattr(context, "virtual_path") and context.virtual_path:
            virtual_path = context.virtual_path

        # Check cache first if enabled
        if self._has_caching():
            import contextlib

            with contextlib.suppress(Exception):
                cached = self._read_from_cache(virtual_path)
                if cached and not cached.stale and cached.content_binary:
                    logger.info(f"[Gmail] Cache hit for {virtual_path}")
                    return cached.content_binary

        # Cache miss - read from Gmail backend
        logger.info(f"[Gmail] Cache miss, reading from Gmail API: {content_hash}")
        try:
            service = self._get_gmail_service(context)

            # Extract message ID from virtual path if needed
            # Format: /{label}/{msg_id}.yaml or /{label}/.{msg_id}.html
            import os

            filename = os.path.basename(virtual_path) if virtual_path else content_hash
            is_html = filename.startswith(".") and filename.endswith(".html")
            is_yaml = filename.endswith(".yaml")

            # Extract message ID
            if is_html:
                msg_id = filename[1:-5]  # Remove leading '.' and '.html'
            elif is_yaml:
                msg_id = filename[:-5]  # Remove '.yaml'
            else:
                msg_id = content_hash  # Fallback to content_hash

            # Fetch message content
            headers, text_body, html_body, labels, raw_bytes = self._get_message_content(
                service, msg_id
            )

            # Determine what to return based on file type
            if is_html:
                # Return HTML body only
                result_bytes = html_body.encode("utf-8")
            elif is_yaml:
                # Return YAML content (headers, text body, and labels)
                # HTML body is in a separate .{message_id}.html file
                yaml_content = self._create_yaml_content(headers, text_body, labels)
                result_bytes = yaml_content.encode("utf-8")
            else:
                # Fallback: return raw bytes
                result_bytes = raw_bytes

            # Cache the result if caching is enabled
            if self._has_caching():
                import contextlib

                with contextlib.suppress(Exception):
                    tenant_id = getattr(context, "tenant_id", None) if context else None

                    if is_yaml:
                        # Create searchable text for YAML files
                        searchable_text = (
                            f"From: {headers['from']}\n"
                            f"To: {headers['to']}\n"
                            f"Subject: {headers['subject']}\n"
                            f"Date: {headers['date']}\n\n"
                            f"{text_body}"
                        )
                        self._write_to_cache(
                            path=virtual_path,
                            content=result_bytes,
                            content_text=searchable_text,
                            backend_version=None,
                            tenant_id=tenant_id,
                        )
                    else:
                        self._write_to_cache(
                            path=virtual_path,
                            content=result_bytes,
                            backend_version=None,
                            tenant_id=tenant_id,
                        )

            return result_bytes

        except Exception as e:
            if "not found" in str(e).lower() or "404" in str(e):
                raise NexusFileNotFoundError(content_hash) from e
            raise BackendError(
                f"Failed to read message {content_hash}: {e}",
                backend="gmail",
                path=content_hash,
            ) from e

    def delete_content(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """
        Delete email from Gmail (not supported - read-only connector).

        Gmail connector is read-only. Deleting emails is not supported.

        Args:
            content_hash: Message ID
            context: Operation context

        Raises:
            BackendError: Always raised - delete operations not supported
        """
        raise BackendError(
            "Gmail connector is read-only. Deleting emails is not supported.",
            backend="gmail",
        )

    def content_exists(self, content_hash: str, context: "OperationContext | None" = None) -> bool:
        """
        Check if email exists in Gmail.

        Args:
            content_hash: Message ID
            context: Operation context

        Returns:
            True if message exists, False otherwise
        """
        try:
            service = self._get_gmail_service(context)

            # Try to get message metadata
            service.users().messages().get(userId="me", id=content_hash, format="minimal").execute()

            return True

        except Exception:
            return False

    def get_content_size(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get email size from Gmail.

        Args:
            content_hash: Message ID
            context: Operation context

        Returns:
            Content size in bytes

        Raises:
            NexusFileNotFoundError: If message doesn't exist
            BackendError: If operation fails
        """
        try:
            service = self._get_gmail_service(context)

            # Get message metadata
            message = (
                service.users()
                .messages()
                .get(userId="me", id=content_hash, format="minimal")
                .execute()
            )

            size = message.get("sizeEstimate", 0)
            return int(size)

        except Exception as e:
            if "not found" in str(e).lower() or "404" in str(e):
                raise NexusFileNotFoundError(content_hash) from e
            raise BackendError(
                f"Failed to get message size: {e}",
                backend="gmail",
                path=content_hash,
            ) from e

    def get_ref_count(self, content_hash: str, context: "OperationContext | None" = None) -> int:
        """Get reference count (always 1 for connector backends).

        Gmail doesn't do deduplication, so each message has exactly one reference.

        Args:
            content_hash: Message ID
            context: Operation context

        Returns:
            Always 1 (no reference counting)
        """
        return 1

    # === Directory operations (not applicable for Gmail) ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory (not applicable for Gmail).

        Gmail uses labels, not directories. This is a no-op.
        """
        # No-op: Gmail uses labels, not directories
        pass

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove directory (not applicable for Gmail).

        Gmail uses labels, not directories. This is a no-op.
        """
        # No-op: Gmail uses labels, not directories
        pass

    def is_directory(self, path: str, context: "OperationContext | None" = None) -> bool:
        """Check if path is a directory.

        For Gmail, labels act as directories.

        Args:
            path: Path to check (label name)

        Returns:
            True if path is a known label, False otherwise
        """
        # Labels act as directories
        path_clean = path.strip("/").upper()
        return path_clean in ["INBOX", "SENT", "DRAFTS", "TRASH", "SPAM", "STARRED"]

    def list_dir(self, path: str, context: "OperationContext | None" = None) -> list[str]:
        """
        List directory contents (list messages in label).

        Args:
            path: Label name (e.g., "INBOX")
            context: Operation context

        Returns:
            List of message filenames (.yaml and .html files)

        Raises:
            BackendError: If operation fails
        """
        try:
            service = self._get_gmail_service(context)

            # Fetch messages for this label
            label = path.strip("/").upper()
            messages, history_id = self._fetch_messages_initial(
                service,
                label_ids=[label] if label else None,
                max_results=self.max_results,
            )

            # Return message IDs as virtual files (YAML format)
            # Note: HTML files are created separately during sync, but we only list YAML files
            return [f"{msg['id']}.yaml" for msg in messages]

        except Exception as e:
            raise BackendError(
                f"Failed to list messages in {path}: {e}",
                backend="gmail",
                path=path,
            ) from e
