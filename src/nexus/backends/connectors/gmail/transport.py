"""Gmail Transport — raw key→bytes I/O over the Gmail API.

Implements the Transport protocol for Gmail, mapping:
- fetch(key) → messages.get(id=msg_id) → YAML bytes
- list_keys(prefix) → messages.list(labelIds=[prefix]) → file keys
- exists(key) → messages.get(id=msg_id, fields="id")
- get_size(key) → messages.get(fields="sizeEstimate")

Read-only: store/remove/copy_key/create_dir raise BackendError.

Auth: GmailTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``
so the transport can resolve the per-user OAuth token.

Key schema:
    "INBOX/threadAbc-msgXyz.yaml"   → label=INBOX, msg_id=msgXyz
    "SENT/threadAbc-msgXyz.yaml"    → label=SENT,  msg_id=msgXyz
    list_keys("INBOX/")             → all message keys under INBOX
    list_keys("")                    → common_prefixes = ["SENT/", ...]
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from contextlib import suppress
from copy import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.gmail.utils import (
    fetch_emails_batch,
    list_emails_by_folder,
)
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Suppress noisy discovery-cache warnings from google-api-python-client.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# Gmail system labels exposed as virtual directories (priority order).
LABEL_FOLDERS = ["SENT", "STARRED", "IMPORTANT", "INBOX"]


class GmailTransport:
    """Gmail API transport implementing the Transport protocol.

    Attributes:
        transport_name: ``"gmail"`` — used by PathAddressingEngine to build
            the backend name (``"path-gmail"``).
    """

    transport_name: str = "gmail"

    def __init__(
        self,
        token_manager: Any,
        provider: str = "gmail",
        user_email: str | None = None,
        max_message_per_label: int = 200,
    ) -> None:
        self._token_manager = token_manager
        self._provider = provider
        self._user_email = user_email
        self._max_message_per_label = max_message_per_label
        self._context: OperationContext | None = None

    # ------------------------------------------------------------------
    # Context binding (not part of Transport protocol; Gmail-specific)
    # ------------------------------------------------------------------

    def with_context(self, context: OperationContext | None) -> GmailTransport:
        """Return a shallow copy bound to *context* (for OAuth token resolution)."""
        clone = copy(self)
        clone._context = context
        return clone

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_gmail_service(self) -> Resource:
        """Build an authenticated Gmail ``Resource`` using the bound context."""
        try:
            from googleapiclient.discovery import build
        except ImportError:
            raise BackendError(
                "google-api-python-client not installed. "
                "Install with: pip install google-api-python-client",
                backend="gmail",
            ) from None

        # Resolve user email
        if self._user_email:
            user_email = self._user_email
        elif self._context and self._context.user_id:
            user_email = self._context.user_id
        else:
            raise BackendError(
                "Gmail transport requires either configured user_email "
                "or authenticated user in OperationContext",
                backend="gmail",
            )

        # Get valid access token (auto-refreshes if expired)
        from nexus.lib.sync_bridge import run_sync

        try:
            zone_id = (
                self._context.zone_id
                if self._context and hasattr(self._context, "zone_id") and self._context.zone_id
                else "root"
            )
            access_token = run_sync(
                self._token_manager.get_valid_token(
                    provider=self._provider,
                    user_email=user_email,
                    zone_id=zone_id,
                )
            )
        except Exception as e:
            raise BackendError(
                f"Failed to get valid OAuth token for user {user_email}: {e}",
                backend="gmail",
            ) from e

        from google.oauth2.credentials import Credentials

        creds = Credentials(token=access_token)
        return build("gmail", "v1", credentials=creds)

    # -- Key parsing helpers --

    @staticmethod
    def _parse_key(key: str) -> tuple[str | None, str | None, str | None]:
        """Parse a transport key into ``(label, thread_id, message_id)``.

        Expected format: ``"LABEL/threadId-msgId.yaml"``

        Returns ``(None, None, None)`` for unparseable keys.
        """
        key = key.strip("/")
        parts = key.split("/")

        if len(parts) == 2 and parts[0] in LABEL_FOLDERS:
            filename = parts[1]
        elif len(parts) == 1:
            filename = parts[0]
        else:
            return None, None, None

        if not filename.endswith(".yaml"):
            return parts[0] if len(parts) == 2 else None, None, None

        base = filename.removesuffix(".yaml")
        if "-" not in base:
            return None, None, None

        thread_id, message_id = base.split("-", 1)
        label = parts[0] if len(parts) == 2 else None
        return label, thread_id, message_id

    # -- Email parsing / formatting (extracted from old connector) --

    @staticmethod
    def _parse_email_date(date_str: str) -> datetime:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:
            return datetime.now(UTC)

    @staticmethod
    def _extract_body_from_parts(
        parts: list[dict[str, Any]],
        body_text: str = "",
        body_html: str = "",
    ) -> tuple[str, str]:
        for part in parts:
            mime_type = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data")

            if "parts" in part:
                body_text, body_html = GmailTransport._extract_body_from_parts(
                    part["parts"], body_text, body_html
                )
            elif body_data:
                try:
                    decoded = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
                    if mime_type == "text/plain" and not body_text:
                        body_text = decoded
                    elif mime_type == "text/html" and not body_html:
                        body_html = decoded
                except Exception as e:
                    logger.debug(
                        "Failed to decode email body part (mime_type=%s): %s", mime_type, e
                    )
                    continue

        return body_text, body_html

    def _parse_gmail_message(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        date_str = headers.get("Date", "")
        email_date = self._parse_email_date(date_str) if date_str else datetime.now(UTC)

        body_text = ""
        body_html = ""
        payload = message.get("payload", {})
        parts = payload.get("parts", [])

        if not parts:
            body_data = payload.get("body", {}).get("data")
            if body_data:
                with suppress(Exception):
                    body_text = base64.urlsafe_b64decode(body_data).decode("utf-8", errors="ignore")
        else:
            body_text, body_html = self._extract_body_from_parts(parts)

        return {
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

    @staticmethod
    def _format_email_as_yaml(email_data: dict[str, Any]) -> bytes:
        yaml_data = {k: v for k, v in email_data.items() if k not in ("headers", "body_html")}

        if "body_text" in yaml_data and yaml_data["body_text"]:
            text = yaml_data["body_text"]
            text = text.replace("\r\n", "\n")
            if "\\n" in text:
                text = text.replace("\\n", "\n")
            yaml_data["body_text"] = text

        class LiteralDumper(yaml.SafeDumper):
            def choose_scalar_style(self) -> Any:
                if (
                    self.event
                    and hasattr(self.event, "value")
                    and self.event.value
                    and "\n" in self.event.value
                ):
                    return "|"
                return super().choose_scalar_style()

        def literal_presenter(dumper: yaml.SafeDumper, data: str) -> Any:
            if isinstance(data, str) and "\n" in data:
                return dumper.represent_scalar("tag:yaml.org,2002:str", data.rstrip(), style="|")
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

    def _fetch_email(self, service: Resource, message_id: str) -> dict[str, Any]:
        try:
            message = (
                service.users().messages().get(userId="me", id=message_id, format="full").execute()
            )
            return self._parse_gmail_message(message)
        except Exception as e:
            raise BackendError(
                f"Failed to fetch email {message_id}: {e}",
                backend="gmail",
            ) from e

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        raise BackendError(
            "Gmail transport is read-only. Cannot store content.",
            backend="gmail",
        )

    def fetch(self, key: str, version_id: str | None = None) -> tuple[bytes, str | None]:
        """Fetch a single email as YAML bytes by transport key."""
        _label, _thread_id, message_id = self._parse_key(key)
        if not message_id:
            raise NexusFileNotFoundError(key)

        service = self._get_gmail_service()
        email_data = self._fetch_email(service, message_id)
        content = self._format_email_as_yaml(email_data)
        return content, None

    def remove(self, key: str) -> None:
        raise BackendError(
            "Gmail transport is read-only. Cannot remove content.",
            backend="gmail",
        )

    def exists(self, key: str) -> bool:
        """Check whether a message key exists in Gmail."""
        _label, _thread_id, message_id = self._parse_key(key)
        if not message_id:
            # Could be a label directory check
            stripped = key.strip("/")
            return stripped in LABEL_FOLDERS or stripped == ""

        try:
            service = self._get_gmail_service()
            service.users().messages().get(userId="me", id=message_id, format="minimal").execute()
            return True
        except Exception:
            return False

    def get_size(self, key: str) -> int:
        """Return the sizeEstimate for a message."""
        _label, _thread_id, message_id = self._parse_key(key)
        if not message_id:
            raise NexusFileNotFoundError(key)

        try:
            service = self._get_gmail_service()
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="minimal", fields="sizeEstimate")
                .execute()
            )
            return int(msg.get("sizeEstimate", 0))
        except Exception as e:
            raise NexusFileNotFoundError(key) from e

    def list_keys(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
        """List email keys under *prefix*.

        - ``list_keys("")`` → ``([], ["SENT/", "STARRED/", "IMPORTANT/", "INBOX/"])``
        - ``list_keys("INBOX/")`` → ``(["INBOX/thread-msg.yaml", ...], [])``
        """
        prefix = prefix.strip("/")

        # Root → return label folders as common prefixes
        if not prefix:
            return [], [f"{label}/" for label in LABEL_FOLDERS]

        # Label folder → list messages
        if prefix in LABEL_FOLDERS:
            service = self._get_gmail_service()
            emails = list_emails_by_folder(
                service,
                max_results=self._max_message_per_label,
                folder_filter=[prefix],
                silent=True,
            )
            keys = []
            for email in emails:
                if email.get("folder") == prefix:
                    thread_id = email.get("threadId")
                    msg_id = email["id"]
                    keys.append(f"{prefix}/{thread_id}-{msg_id}.yaml")
            return sorted(keys), []

        return [], []

    def copy_key(self, src_key: str, dst_key: str) -> None:
        raise BackendError(
            "Gmail transport does not support copy.",
            backend="gmail",
        )

    def create_dir(self, key: str) -> None:
        raise BackendError(
            "Gmail transport does not support directory creation. Labels are virtual.",
            backend="gmail",
        )

    def stream(
        self,
        key: str,
        chunk_size: int = 8192,
        version_id: str | None = None,
    ) -> Iterator[bytes]:
        """Stream email content (small payloads — fetch then chunk)."""
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
            "Gmail transport is read-only. Cannot store content.",
            backend="gmail",
        )

    # ------------------------------------------------------------------
    # Batch helpers (used by PathGmailBackend._bulk_download_contents)
    # ------------------------------------------------------------------

    def fetch_batch(
        self,
        message_ids: list[str],
    ) -> dict[str, bytes]:
        """Batch-fetch emails and return ``{message_id: yaml_bytes}``."""
        if not message_ids:
            return {}

        service = self._get_gmail_service()
        email_cache: dict[str, dict[str, Any]] = {}

        try:
            fetch_emails_batch(
                service=service,
                message_ids=message_ids,
                parse_message_func=self._parse_gmail_message,
                email_cache=email_cache,
            )
        except Exception as e:
            logger.debug("Gmail batch fetch failed: %s", e)
            return {}

        results: dict[str, bytes] = {}
        for msg_id, email_data in email_cache.items():
            try:
                results[msg_id] = self._format_email_as_yaml(email_data)
            except Exception as e:
                logger.debug("Failed to format email %s as YAML: %s", msg_id, e)
        return results
