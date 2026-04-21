"""Gmail Transport — raw key→bytes I/O over the Gmail API.

Implements the Transport protocol for Gmail, mapping:
- fetch(key) → messages.get(id=msg_id) → YAML bytes
- store(key, data) → messages.send / drafts.create
- remove(key) → messages.trash (recoverable)
- list_keys(prefix) → messages.list(labelIds=[prefix]) → file keys
- exists(key) → messages.get(id=msg_id, fields="id")
- get_size(key) → messages.get(fields="sizeEstimate")

Auth: GmailTransport carries a TokenManager + provider.  Before each
request the caller must bind an OperationContext via ``with_context()``
so the transport can resolve the per-user OAuth token.

Key schema:
    "INBOX/threadAbc-msgXyz.yaml"   → label=INBOX, msg_id=msgXyz
    "SENT/threadAbc-msgXyz.yaml"    → label=SENT,  msg_id=msgXyz
    "SENT/_new.yaml"                → send new email
    "SENT/_reply.yaml"              → reply to thread
    "SENT/_forward.yaml"            → forward message
    "DRAFTS/_new.yaml"              → create draft
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
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.connectors.cli.display_path import sanitize_filename
from nexus.backends.connectors.gmail.utils import (
    fetch_emails_batch,
    list_emails_by_folder,
)
from nexus.contracts.exceptions import AuthenticationError, BackendError, NexusFileNotFoundError

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

# Suppress noisy discovery-cache warnings from google-api-python-client.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# Gmail system labels exposed as virtual directories (priority order).
LABEL_FOLDERS = ["SENT", "STARRED", "IMPORTANT", "INBOX", "DRAFTS", "TRASH"]

# Explicit write-sentinel basenames recognised by ``store()``.
# Anything *else* starting with ``_`` is just a sanitized readable
# filename (e.g. ``_unnamed``, ``_CON``) and must fall through to the
# normal id-anchor parser.
_WRITE_SENTINELS = frozenset({"_new", "_reply", "_forward"})


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

    def with_context(
        self,
        context: OperationContext | None,
        *,
        user_email_override: str | None = None,
    ) -> GmailTransport:
        """Return a shallow copy bound to *context* (for OAuth token resolution).

        Args:
            context: Per-request OperationContext (used to resolve user_email
                when user_email_override is not set).
            user_email_override: If provided, this email is used directly for
                token lookup — bypasses context.user_id resolution. Used by the
                credential pool to select a specific account for each request.
        """
        clone = copy(self)
        clone._context = context
        if user_email_override is not None:
            clone._user_email = user_email_override
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

        from nexus.backends.connectors.oauth_base import resolve_oauth_access_token
        from nexus.contracts.exceptions import AuthenticationError

        # Pass both the mount-configured user_email (if any) and the nexus
        # user_id from the request context into the shared resolver.  The
        # resolver picks the email verbatim when it looks like an email,
        # otherwise it looks up the OAuth-linked email for the nexus user
        # — fixing the API-key auth case where context.user_id is a
        # subject id like "admin", not a gmail address (Issue #3822 part 2).
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
                connector_name="gmail_connector",
                provider=self._provider,
                user_email=user_email,
                zone_id=zone_id,
                nexus_user_id=nexus_user_id,
            )
        except AuthenticationError:
            raise
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
        """Parse a transport key into ``(label, thread_id | None, message_id | sentinel)``.

        Accepted formats (both legacy and human-readable):
        - ``"LABEL/threadId-msgId.yaml"``                       (legacy)
        - ``"LABEL/{date}_{subject}__threadId-msgId.yaml"``     (readable)
        - ``"LABEL/_new.yaml"`` etc.                             (write sentinel)

        The readable form embeds ``__threadId-msgId`` as a trailing anchor so
        ``fetch/exists/remove`` stay id-driven.  Anything between label and the
        trailing anchor is display-only.

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
        label = parts[0] if len(parts) == 2 else None

        # Explicit write-sentinel names — must match exactly, so readable
        # filenames that happen to start with ``_`` (e.g. subject
        # sanitized to ``_unnamed`` or reserved Windows name ``CON``
        # prefixed to ``_CON``) still parse as id-anchored keys.
        if base in _WRITE_SENTINELS:
            return label, None, base

        # Human-readable form: prefer the trailing "__threadId-msgId" anchor.
        id_anchor = base
        if "__" in base:
            id_anchor = base.rsplit("__", 1)[-1]

        if "-" not in id_anchor:
            return None, None, None

        thread_id, message_id = id_anchor.split("-", 1)
        return label, thread_id, message_id

    # ------------------------------------------------------------------
    # Human-readable key formatting (Issue #3256 — SDK-transport port)
    # ------------------------------------------------------------------

    @staticmethod
    def _date_prefix_for_key(date_str: str) -> str:
        """Extract ``YYYY-MM-DD`` from ISO-8601 or RFC-2822 date string."""
        if not date_str:
            return ""
        if len(date_str) >= 10 and date_str[4:5] == "-":
            return date_str[:10]
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(date_str)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

    @classmethod
    def _format_readable_key(
        cls,
        label: str,
        thread_id: str,
        msg_id: str,
        meta: dict[str, Any] | None,
    ) -> str:
        """Build ``LABEL/{date}_{subject}__{tid}-{mid}.yaml`` when meta is
        available, else fall back to the legacy hex-only key."""
        anchor = f"{thread_id}-{msg_id}"
        if not meta:
            return f"{label}/{anchor}.yaml"
        parts: list[str] = []
        date_prefix = cls._date_prefix_for_key(meta.get("date", "") or "")
        if date_prefix:
            parts.append(date_prefix)
        subject = (meta.get("subject") or "").strip()
        if subject:
            parts.append(sanitize_filename(subject, max_len=80))
        if not parts:
            return f"{label}/{anchor}.yaml"
        return f"{label}/{'_'.join(parts)}__{anchor}.yaml"

    def _batch_fetch_headers(self, service: Any, msg_ids: list[str]) -> dict[str, dict[str, str]]:
        """Batch-fetch Subject/Date/From for *msg_ids* using format=metadata.

        Returns ``{msg_id: {"subject":..., "date":..., "from":...}}``.

        Retries missing ids (including rate-limit 429 losses) up to three
        times with exponential backoff; anything still missing after the
        final attempt falls back to the legacy hex-only key in the caller.
        """
        if not msg_ids:
            return {}
        out: dict[str, dict[str, str]] = {}

        def _cb(request_id: str, response: Any, exception: Exception | None) -> None:
            if exception or not response:
                return
            headers = {
                h.get("name", ""): h.get("value", "")
                for h in response.get("payload", {}).get("headers", [])
            }
            out[request_id] = {
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "from": headers.get("From", ""),
            }

        batch_size = 50
        remaining = list(msg_ids)
        for attempt in range(3):
            if not remaining:
                break
            if attempt > 0:
                import time as _t

                _t.sleep(0.5 * (2**attempt))
            for i in range(0, len(remaining), batch_size):
                chunk = remaining[i : i + batch_size]
                batch = service.new_batch_http_request()
                for mid in chunk:
                    req = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=mid,
                            format="metadata",
                            metadataHeaders=["Subject", "Date", "From"],
                        )
                    )
                    batch.add(req, callback=_cb, request_id=mid)
                try:
                    batch.execute()
                except Exception as e:
                    logger.debug("Gmail metadata batch attempt %d failed: %s", attempt + 1, e)
            remaining = [m for m in msg_ids if m not in out]
        return out

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

        yaml_output: str = yaml.dump(
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
            # Translate HTTP 404 to NexusFileNotFoundError so callers (e.g. the
            # credential pool) can distinguish "message deleted" from "credential
            # failure" and avoid incorrectly penalising healthy credentials.
            try:
                from googleapiclient.errors import HttpError

                if isinstance(e, HttpError) and e.resp and e.resp.status == 404:
                    raise NexusFileNotFoundError(message_id) from e
            except ImportError:
                pass
            raise BackendError(
                f"Failed to fetch email {message_id}: {e}",
                backend="gmail",
            ) from e

    # ------------------------------------------------------------------
    # YAML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_yaml_content(data: bytes) -> dict[str, Any]:
        """Parse YAML bytes, extracting ``agent_intent`` / ``confirm`` from comments."""
        text = data.decode("utf-8")
        result: dict[str, Any] = {}

        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("# agent_intent:"):
                result["agent_intent"] = line.replace("# agent_intent:", "").strip()
            elif line.startswith("# confirm:"):
                result["confirm"] = line.replace("# confirm:", "").strip().lower() == "true"
            elif line.startswith("# user_confirmed:"):
                result["user_confirmed"] = (
                    line.replace("# user_confirmed:", "").strip().lower() == "true"
                )

        yaml_content = yaml.safe_load(text) or {}
        if isinstance(yaml_content, dict):
            result.update(yaml_content)
        return result

    # ------------------------------------------------------------------
    # MIME building helpers
    # ------------------------------------------------------------------

    def _resolve_from_address(self) -> str:
        """Resolve the sender email address from config or context."""
        if self._user_email:
            return self._user_email
        if self._context and self._context.user_id:
            return self._context.user_id
        return "me"

    def _build_mime_message(self, data: dict[str, Any]) -> EmailMessage:
        """Build a new MIME message from parsed YAML data.

        Handles To, Cc, Bcc, Subject, plain-text body, optional HTML
        alternative, and inline base64 attachments.
        """
        import mimetypes as _mt

        msg = EmailMessage()
        msg["From"] = data.get("from", self._resolve_from_address())

        # Recipients
        to_list = data.get("to", [])
        if isinstance(to_list, str):
            to_list = [to_list]
        msg["To"] = ", ".join(to_list)

        cc_list = data.get("cc") or []
        if isinstance(cc_list, str):
            cc_list = [cc_list]
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)

        bcc_list = data.get("bcc") or []
        if isinstance(bcc_list, str):
            bcc_list = [bcc_list]
        if bcc_list:
            msg["Bcc"] = ", ".join(bcc_list)

        msg["Subject"] = data.get("subject", "")

        # Body
        body_text = data.get("body", "")
        html_body = data.get("html_body")

        if html_body:
            msg.make_alternative()
            msg.add_alternative(body_text, subtype="plain")
            msg.add_alternative(html_body, subtype="html")
        else:
            msg.set_content(body_text)

        # Attachments (inline base64 data)
        attachments = data.get("attachments") or []
        for att in attachments:
            if isinstance(att, dict):
                att_data_b64 = att.get("data")
                if not att_data_b64:
                    continue  # path-based attachments not yet supported
                file_bytes = base64.b64decode(att_data_b64)
                filename = att.get("filename") or "attachment"
                content_type = att.get("content_type")
                if not content_type:
                    content_type, _ = _mt.guess_type(filename)
                    content_type = content_type or "application/octet-stream"
                maintype, _, subtype = content_type.partition("/")
                msg.add_attachment(
                    file_bytes,
                    maintype=maintype,
                    subtype=subtype or "octet-stream",
                    filename=filename,
                )

        return msg

    def _build_reply_mime(
        self,
        data: dict[str, Any],
        original: dict[str, Any],
    ) -> EmailMessage:
        """Build a reply MIME message with proper threading headers.

        Sets In-Reply-To, References, and ``Re:`` subject prefix.
        Returns the message along with the threadId for Gmail API.
        """
        msg = EmailMessage()
        msg["From"] = data.get("from", self._resolve_from_address())

        # Determine reply recipients
        reply_all = data.get("reply_all", False)
        original_from = original.get("from", original.get("headers", {}).get("From", ""))
        original_to = original.get("to", original.get("headers", {}).get("To", ""))
        original_cc = original.get("cc", original.get("headers", {}).get("Cc", ""))

        if reply_all:
            # Reply-all: To = original From + original To, Cc = original Cc
            all_to = [original_from]
            if original_to:
                to_parts = [t.strip() for t in original_to.split(",") if t.strip()]
                all_to.extend(to_parts)
            # Remove self from recipients
            my_addr = self._resolve_from_address().lower()
            all_to = [t for t in all_to if my_addr not in t.lower()]
            msg["To"] = ", ".join(all_to) if all_to else original_from

            if original_cc:
                cc_parts = [c.strip() for c in original_cc.split(",") if c.strip()]
                cc_parts = [c for c in cc_parts if my_addr not in c.lower()]
                if cc_parts:
                    msg["Cc"] = ", ".join(cc_parts)
        else:
            msg["To"] = original_from

        # Additional recipients
        additional_to = data.get("additional_to") or []
        if isinstance(additional_to, str):
            additional_to = [additional_to]
        if additional_to:
            existing_to = msg.get("To", "")
            msg.replace_header("To", ", ".join([existing_to] + additional_to))

        # Subject with Re: prefix
        original_subject = original.get("subject", original.get("headers", {}).get("Subject", ""))
        if not original_subject.startswith("Re: "):
            msg["Subject"] = f"Re: {original_subject}"
        else:
            msg["Subject"] = original_subject

        # Threading headers
        original_message_id = original.get("headers", {}).get("Message-ID", "")
        if original_message_id:
            msg["In-Reply-To"] = original_message_id
            msg["References"] = original_message_id

        # Body
        body_text = data.get("body", "")
        html_body = data.get("html_body")

        if html_body:
            msg.make_alternative()
            msg.add_alternative(body_text, subtype="plain")
            msg.add_alternative(html_body, subtype="html")
        else:
            msg.set_content(body_text)

        return msg

    def _build_forward_mime(
        self,
        data: dict[str, Any],
        original: dict[str, Any],
    ) -> EmailMessage:
        """Build a forward MIME message with quoted original content.

        Prepends user comment (if any) and adds ``---------- Forwarded message ----------``
        separator with original headers.
        """
        msg = EmailMessage()
        msg["From"] = data.get("from", self._resolve_from_address())

        # Recipients
        to_list = data.get("to", [])
        if isinstance(to_list, str):
            to_list = [to_list]
        msg["To"] = ", ".join(to_list)

        cc_list = data.get("cc") or []
        if isinstance(cc_list, str):
            cc_list = [cc_list]
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)

        # Subject with Fwd: prefix
        original_subject = original.get("subject", original.get("headers", {}).get("Subject", ""))
        if not original_subject.startswith("Fwd: "):
            msg["Subject"] = f"Fwd: {original_subject}"
        else:
            msg["Subject"] = original_subject

        # Build forwarded body
        comment = data.get("comment", "")
        original_from = original.get("from", original.get("headers", {}).get("From", ""))
        original_to = original.get("to", original.get("headers", {}).get("To", ""))
        original_date = original.get("date", original.get("headers", {}).get("Date", ""))
        original_body = original.get("body_text", "")

        forward_separator = (
            "\n---------- Forwarded message ----------\n"
            f"From: {original_from}\n"
            f"Date: {original_date}\n"
            f"Subject: {original_subject}\n"
            f"To: {original_to}\n\n"
            f"{original_body}"
        )

        body = f"{comment}\n{forward_separator}" if comment else forward_separator

        msg.set_content(body)
        return msg

    @staticmethod
    def _encode_mime_raw(msg: EmailMessage) -> str:
        """Base64url-encode a MIME message for the Gmail API ``raw`` field."""
        raw_bytes = msg.as_bytes()
        return base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    # ------------------------------------------------------------------
    # Gmail write helpers
    # ------------------------------------------------------------------

    def _send_new_email(
        self,
        service: "Resource",
        data: dict[str, Any],
    ) -> str:
        """Send a new email via messages.send()."""
        msg = self._build_mime_message(data)
        raw = self._encode_mime_raw(msg)
        try:
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return str(result.get("id", ""))
        except Exception as e:
            raise BackendError(
                f"Failed to send email: {e}",
                backend="gmail",
            ) from e

    def _send_reply(
        self,
        service: "Resource",
        data: dict[str, Any],
    ) -> str:
        """Send a reply to an existing thread via messages.send() with threadId."""
        message_id = data.get("message_id", "")
        thread_id = data.get("thread_id", "")

        if not message_id:
            raise BackendError(
                "Reply requires 'message_id' of the message to reply to.",
                backend="gmail",
            )
        if not thread_id:
            raise BackendError(
                "Reply requires 'thread_id' of the thread to reply to.",
                backend="gmail",
            )

        # Fetch original message for threading headers
        original = self._fetch_email(service, message_id)
        msg = self._build_reply_mime(data, original)
        raw = self._encode_mime_raw(msg)

        try:
            result = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": raw, "threadId": thread_id})
                .execute()
            )
            return str(result.get("id", ""))
        except Exception as e:
            raise BackendError(
                f"Failed to send reply: {e}",
                backend="gmail",
            ) from e

    def _send_forward(
        self,
        service: "Resource",
        data: dict[str, Any],
    ) -> str:
        """Forward an email via messages.send()."""
        message_id = data.get("message_id", "")
        if not message_id:
            raise BackendError(
                "Forward requires 'message_id' of the message to forward.",
                backend="gmail",
            )

        # Fetch original message for quoting
        original = self._fetch_email(service, message_id)
        msg = self._build_forward_mime(data, original)
        raw = self._encode_mime_raw(msg)

        try:
            result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
            return str(result.get("id", ""))
        except Exception as e:
            raise BackendError(
                f"Failed to forward email: {e}",
                backend="gmail",
            ) from e

    def _create_draft(
        self,
        service: "Resource",
        data: dict[str, Any],
    ) -> str:
        """Create a draft via drafts.create()."""
        msg = self._build_mime_message(data)
        raw = self._encode_mime_raw(msg)

        draft_body: dict[str, Any] = {"message": {"raw": raw}}
        # If this is a reply draft, include threadId
        thread_id = data.get("thread_id")
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        try:
            result = service.users().drafts().create(userId="me", body=draft_body).execute()
            return str(result.get("id", ""))
        except Exception as e:
            raise BackendError(
                f"Failed to create draft: {e}",
                backend="gmail",
            ) from e

    # ------------------------------------------------------------------
    # Transport protocol methods
    # ------------------------------------------------------------------

    def store(self, key: str, data: bytes, content_type: str = "") -> str | None:
        """Send, reply, forward, or draft an email based on the key sentinel.

        Dispatch rules:
        - ``SENT/_new.yaml``     → send new email
        - ``SENT/_reply.yaml``   → reply to thread
        - ``SENT/_forward.yaml`` → forward message
        - ``DRAFTS/_new.yaml``   → create draft
        """
        label, _thread_id, sentinel = self._parse_key(key)
        if not label or not sentinel or not sentinel.startswith("_"):
            raise BackendError(
                f"Invalid write key: {key}. "
                "Use SENT/_new.yaml, SENT/_reply.yaml, SENT/_forward.yaml, or DRAFTS/_new.yaml",
                backend="gmail",
            )

        parsed = self._parse_yaml_content(data)
        service = self._get_gmail_service()

        dispatch = {
            ("SENT", "_new"): self._send_new_email,
            ("SENT", "_reply"): self._send_reply,
            ("SENT", "_forward"): self._send_forward,
            ("DRAFTS", "_new"): self._create_draft,
        }

        handler = dispatch.get((label, sentinel))
        if handler is None:
            raise BackendError(
                f"Unsupported write operation: label={label}, sentinel={sentinel}. "
                "Supported: SENT/_new, SENT/_reply, SENT/_forward, DRAFTS/_new",
                backend="gmail",
            )

        return handler(service, parsed)

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
        """Trash a Gmail message (recoverable — not permanent delete).

        Calls ``messages.trash(id=message_id)`` which moves the message
        to the Trash label.  Messages in Trash are auto-deleted after 30 days.
        """
        _label, _thread_id, message_id = self._parse_key(key)
        if not message_id or (message_id and message_id.startswith("_")):
            raise BackendError(
                f"Invalid key for trash: {key}. Expected LABEL/threadId-msgId.yaml",
                backend="gmail",
            )

        service = self._get_gmail_service()
        try:
            service.users().messages().trash(userId="me", id=message_id).execute()
            logger.info("Trashed Gmail message: %s", message_id)
        except Exception as e:
            raise BackendError(
                f"Failed to trash message {message_id}: {e}",
                backend="gmail",
            ) from e

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
        except AuthenticationError:
            raise
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

        # DRAFTS folder → use drafts.list API
        if prefix == "DRAFTS":
            service = self._get_gmail_service()
            try:
                result = (
                    service.users()
                    .drafts()
                    .list(userId="me", maxResults=self._max_message_per_label)
                    .execute()
                )
                pairs: list[tuple[str, str]] = []
                for draft in result.get("drafts", []):
                    draft_id = draft.get("id", "")
                    msg = draft.get("message", {})
                    thread_id = msg.get("threadId", draft_id)
                    msg_id = msg.get("id", draft_id)
                    pairs.append((thread_id, msg_id))
                meta = self._batch_fetch_headers(service, [m for _, m in pairs])
                keys = [self._format_readable_key("DRAFTS", t, m, meta.get(m)) for t, m in pairs]
                return sorted(keys), []
            except AuthenticationError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Failed to list Gmail drafts: {e}",
                    backend="gmail",
                    path=prefix,
                ) from e

        # TRASH folder → use messages.list with TRASH label
        if prefix == "TRASH":
            service = self._get_gmail_service()
            try:
                result = (
                    service.users()
                    .messages()
                    .list(userId="me", labelIds=["TRASH"], maxResults=self._max_message_per_label)
                    .execute()
                )
                pairs = [
                    (msg.get("threadId", msg["id"]), msg["id"])
                    for msg in result.get("messages", [])
                ]
                meta = self._batch_fetch_headers(service, [m for _, m in pairs])
                keys = [self._format_readable_key("TRASH", t, m, meta.get(m)) for t, m in pairs]
                return sorted(keys), []
            except AuthenticationError:
                raise
            except Exception as e:
                raise BackendError(
                    f"Failed to list Gmail trash: {e}",
                    backend="gmail",
                    path=prefix,
                ) from e

        # Other label folders → list messages via categorized listing
        if prefix in LABEL_FOLDERS:
            service = self._get_gmail_service()
            emails = list_emails_by_folder(
                service,
                max_results=self._max_message_per_label,
                folder_filter=[prefix],
                silent=True,
            )
            pairs = [
                (email.get("threadId") or email["id"], email["id"])
                for email in emails
                if email.get("folder") == prefix
            ]
            meta = self._batch_fetch_headers(service, [m for _, m in pairs])
            keys = [self._format_readable_key(prefix, t, m, meta.get(m)) for t, m in pairs]
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
