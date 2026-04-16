"""Concrete Google Workspace CLI connector classes.

Each class is a PathCLIBackend subclass with baked-in schemas, traits, and
CLI configuration. Instantiate directly or via ``create_connector_from_yaml()``
with the corresponding YAML config.

Phase 3 (Issue #3148).
Human-readable display paths added in Issue #3256.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from nexus.backends.base.registry import register_connector
from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
)
from nexus.backends.connectors.base_errors import TRAIT_ERRORS
from nexus.backends.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    UpdateEventSchema,
)
from nexus.backends.connectors.cli.base import PathCLIBackend
from nexus.backends.connectors.cli.config import CLIConnectorConfig
from nexus.backends.connectors.cli.display_path import sanitize_filename

# Gmail/Calendar schemas live in their own packages (existing API connectors)
from nexus.backends.connectors.gmail.schemas import (
    DraftEmailSchema,
    ForwardEmailSchema,
    ReplyEmailSchema,
    SendEmailSchema,
)
from nexus.backends.connectors.gws.schemas import (
    AppendRowsSchema,
    CreateSpaceSchema,
    DeleteFileSchema,
    InsertTextSchema,
    ReplaceTextSchema,
    SendMessageSchema,
    UpdateCellsSchema,
    UpdateFileSchema,
    UploadFileSchema,
)

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)

_CONFIGS_DIR = Path(__file__).parent / "configs"
_DOC_MIME_TYPE = "application/vnd.google-apps.document"
_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _load_gws_config(filename: str) -> CLIConnectorConfig | None:
    """Load a GWS connector YAML config from the configs directory."""
    config_path = _CONFIGS_DIR / filename
    if config_path.exists():
        from nexus.backends.connectors.cli.loader import load_connector_config

        return load_connector_config(config_path)
    return None


# ============================================================================
# Sheets
# ============================================================================


@register_connector(
    "gws_sheets",
    description="Google Sheets via gws CLI",
    category="cli",
    service_name="gws",
)
class SheetsConnector(PathCLIBackend):
    """Google Sheets CLI connector via ``gws sheets``."""

    SKILL_NAME = "sheets"
    CLI_NAME = "gws"
    CLI_SERVICE = "sheets"
    AUTH_SOURCE = "gws-cli"

    SCHEMAS: dict[str, type] = {
        "append_rows": AppendRowsSchema,
        "update_cells": UpdateCellsSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "append_rows": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.INTENT),
        "update_cells": OpTraits(
            reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "SPREADSHEET_NOT_FOUND": ErrorDef(
            message="Spreadsheet not found",
            readme_section="operations",
            fix_example="spreadsheet_id: <valid spreadsheet ID or URL>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("sheets.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def list_dir(self, path: str = "/", context: "OperationContext | None" = None) -> list[str]:
        """List spreadsheets by querying Drive metadata and filtering by mime type."""
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        normalized = path.strip("/")
        if normalized:
            return []

        args = [
            "gws",
            "drive",
            "files",
            "list",
            "--params",
            json.dumps({"q": f'mimeType = "{_SHEET_MIME_TYPE}"', "pageSize": 100}),
        ]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        try:
            result = self._execute_cli(args, context=context, env=auth_env)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            return []

        try:
            data = json.loads(result.stdout)
        except Exception:
            return []

        files: list[dict[str, Any]]
        if isinstance(data, dict):
            raw_files = data.get("files", [])
            files = [item for item in raw_files if isinstance(item, dict)]
        elif isinstance(data, list):
            files = [item for item in data if isinstance(item, dict)]
        else:
            files = []

        entries = [
            str(item.get("name", "")).strip()
            for item in files
            if item.get("mimeType") == _SHEET_MIME_TYPE and str(item.get("name", "")).strip()
        ]
        return sorted(entries)


# ============================================================================
# Docs
# ============================================================================


@register_connector(
    "gws_docs",
    description="Google Docs via gws CLI",
    category="cli",
    service_name="gws",
)
class DocsConnector(PathCLIBackend):
    """Google Docs CLI connector via ``gws docs``."""

    SKILL_NAME = "docs"
    CLI_NAME = "gws"
    CLI_SERVICE = "docs"
    AUTH_SOURCE = "gws-cli"

    SCHEMAS: dict[str, type] = {
        "insert_text": InsertTextSchema,
        "replace_text": ReplaceTextSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "insert_text": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.INTENT),
        "replace_text": OpTraits(
            reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT
        ),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "DOCUMENT_NOT_FOUND": ErrorDef(
            message="Document not found",
            readme_section="operations",
            fix_example="document_id: <valid document ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("docs.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def _list_doc_entries(self, context: "OperationContext | None" = None) -> list[dict[str, Any]]:
        """Return Drive metadata for Google Docs with stable display names."""
        args = [
            "gws",
            "drive",
            "files",
            "list",
            "--params",
            json.dumps(
                {
                    "q": f'mimeType = "{_DOC_MIME_TYPE}"',
                    "pageSize": 100,
                    "fields": "files(id,name,mimeType,modifiedTime,size,quotaBytesUsed)",
                }
            ),
        ]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        try:
            result = self._execute_cli(args, context=context, env=auth_env)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path="/") from exc
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            raise BackendError(result.summary(), backend=self.name, path="/")

        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            raise BackendError(
                f"Failed to parse gws docs listing: {exc}", backend=self.name
            ) from exc

        raw_files = data.get("files", []) if isinstance(data, dict) else []
        files = [item for item in raw_files if isinstance(item, dict)]

        counts: dict[str, int] = {}
        for item in files:
            if item.get("mimeType") != _DOC_MIME_TYPE:
                continue
            name = str(item.get("name", "")).strip()
            if name:
                counts[name] = counts.get(name, 0) + 1

        entries: list[dict[str, Any]] = []
        for item in files:
            if item.get("mimeType") != _DOC_MIME_TYPE:
                continue
            name = str(item.get("name", "")).strip()
            doc_id = str(item.get("id", "")).strip()
            if not name or not doc_id:
                continue
            display_name = name if counts.get(name, 0) == 1 else f"{name} [{doc_id}]"
            size = item.get("size") or item.get("quotaBytesUsed") or 0
            try:
                size_value = int(size)
            except Exception:
                size_value = 0
            entries.append(
                {
                    "id": doc_id,
                    "name": display_name,
                    "raw_name": name,
                    "size": size_value,
                    "modified_at": item.get("modifiedTime"),
                    "is_directory": False,
                }
            )
        entries.sort(key=lambda item: str(item["name"]).lower())
        return entries

    def list_dir(self, path: str = "/", context: "OperationContext | None" = None) -> list[str]:
        """List Google Docs by querying Drive metadata and filtering to document mime types."""
        normalized = path.strip("/")
        if normalized:
            return []
        return [str(item["name"]) for item in self._list_doc_entries(context)]

    def list_dir_details(
        self, path: str = "/", context: "OperationContext | None" = None
    ) -> list[dict[str, Any]]:
        """Detailed listing for playground rendering."""
        normalized = path.strip("/")
        if normalized:
            return []
        return self._list_doc_entries(context)

    def _resolve_doc_id(self, backend_path: str, context: "OperationContext | None" = None) -> str:
        """Resolve a selected docs path back to a concrete document id."""
        name = backend_path.strip("/").rsplit("/", 1)[-1]
        if not name:
            raise ValueError("Google Docs path is required")

        match = re.search(r" \[([A-Za-z0-9_-]+)\]$", name)
        if match:
            return match.group(1)

        matches = [item for item in self._list_doc_entries(context) if item["raw_name"] == name]
        if len(matches) == 1:
            return str(matches[0]["id"])
        if len(matches) > 1:
            raise ValueError(
                f"Multiple Google Docs named '{name}'. Re-open the list and select the disambiguated entry."
            )
        raise ValueError(f"Document not found: {name}")

    def read_content(self, content_hash: str, context: Any = None) -> bytes:
        """Read a Google Doc by resolving the selected display name back to a document id."""
        from nexus.contracts.exceptions import BackendError

        backend_path = ""
        if context and hasattr(context, "backend_path") and context.backend_path:
            backend_path = str(context.backend_path)
        elif content_hash:
            backend_path = str(content_hash)

        try:
            doc_id = self._resolve_doc_id(backend_path, context)
        except Exception as exc:
            raise BackendError(str(exc), backend=self.name, path=backend_path) from exc

        args = [
            "gws",
            "docs",
            "documents",
            "get",
            "--params",
            json.dumps({"documentId": doc_id}),
            "--format",
            "json",
        ]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError

        try:
            result = self._execute_cli(args, context=context, env=auth_env)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=backend_path) from exc
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            raise BackendError(result.summary(), backend=self.name, path=backend_path)
        return result.stdout.encode("utf-8")


# ============================================================================
# Chat
# ============================================================================


@register_connector(
    "gws_chat",
    description="Google Chat via gws CLI",
    category="cli",
    service_name="gws",
)
class ChatConnector(PathCLIBackend):
    """Google Chat CLI connector via ``gws chat``."""

    SKILL_NAME = "chat"
    CLI_NAME = "gws"
    CLI_SERVICE = "chat"
    AUTH_SOURCE = "gws-cli"

    SCHEMAS: dict[str, type] = {
        "send_message": SendMessageSchema,
        "create_space": CreateSpaceSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "send_message": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "create_space": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "SPACE_NOT_FOUND": ErrorDef(
            message="Chat space not found",
            readme_section="operations",
            fix_example="space: <valid space name or ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("chat.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def list_dir(self, path: str = "/", context: "OperationContext | None" = None) -> list[str]:
        """List chat spaces at root; nested message browsing is delegated to the CLI config."""
        normalized = path.strip("/")
        if normalized:
            return super().list_dir(path, context=context)

        args = ["gws", "chat", "spaces", "list"]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        try:
            result = self._execute_cli(args, context=context, env=auth_env)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            summary = result.summary()
            combined = f"{result.stderr}\n{result.stdout}".lower()
            if "insufficient authentication scopes" in combined:
                raise BackendError(
                    "Google Chat requires additional OAuth scopes. "
                    "Run `nexus-fs auth connect gws oauth --user-email you@example.com` again "
                    "and approve Chat access, then retry `/mount gws://chat`.",
                    backend=self.name,
                    path=path,
                )
            raise BackendError(summary, backend=self.name, path=path)

        try:
            data = json.loads(result.stdout)
        except Exception:
            return []

        spaces: list[dict[str, Any]]
        if isinstance(data, dict):
            raw_spaces = data.get("spaces", [])
            spaces = [item for item in raw_spaces if isinstance(item, dict)]
        elif isinstance(data, list):
            spaces = [item for item in data if isinstance(item, dict)]
        else:
            spaces = []

        entries = []
        for item in spaces:
            name = str(item.get("name", "")).strip()
            if name:
                entries.append(f"{name}/")
        return sorted(entries)


# ============================================================================
# Drive
# ============================================================================


@register_connector(
    "gws_drive",
    description="Google Drive via gws CLI",
    category="cli",
    service_name="gws",
)
class DriveConnector(PathCLIBackend):
    """Google Drive CLI connector via ``gws drive``."""

    SKILL_NAME = "drive"
    CLI_NAME = "gws"
    CLI_SERVICE = "drive"
    AUTH_SOURCE = "gws-cli"

    SCHEMAS: dict[str, type] = {
        "upload_file": UploadFileSchema,
        "update_file": UpdateFileSchema,
        "delete_file": DeleteFileSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "upload_file": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
        "update_file": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.EXPLICIT),
        "delete_file": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.USER),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "FILE_NOT_FOUND": ErrorDef(
            message="File not found in Drive",
            readme_section="operations",
            fix_example="file_id: <valid Drive file ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("drive.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Preserve original filename from Drive metadata."""
        if metadata:
            name = metadata.get("name") or metadata.get("title")
            if name:
                return sanitize_filename(name)
        return f"{item_id}.yaml"

    def list_dir(self, path: str = "/", context: "OperationContext | None" = None) -> list[str]:
        """List Drive root entries with folder suffixes for browseable mounts."""
        normalized = path.strip("/")
        if normalized:
            return []

        args = ["gws", "drive", "files", "list"]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        try:
            result = self._execute_cli(args, context=context, env=auth_env)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            return []

        try:
            data = json.loads(result.stdout)
        except Exception:
            return []

        files: list[dict[str, Any]]
        if isinstance(data, dict):
            raw_files = data.get("files", [])
            files = [item for item in raw_files if isinstance(item, dict)]
        elif isinstance(data, list):
            files = [item for item in data if isinstance(item, dict)]
        else:
            files = []

        entries = []
        for item in files:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            if item.get("mimeType") == _FOLDER_MIME_TYPE:
                entries.append(f"{name}/")
            else:
                entries.append(name)
        return sorted(entries)


# ============================================================================
# Gmail
# ============================================================================


# Gmail inbox category subfolders (Issue #3256, Decision 16A).
# These map to Gmail's CATEGORY_* system labels.
_GMAIL_CATEGORIES: dict[str, str] = {
    "CATEGORY_PERSONAL": "PRIMARY",
    "CATEGORY_SOCIAL": "SOCIAL",
    "CATEGORY_UPDATES": "UPDATES",
    "CATEGORY_PROMOTIONS": "PROMOTIONS",
    "CATEGORY_FORUMS": "FORUMS",
}
_GMAIL_CATEGORY_FOLDERS = sorted(_GMAIL_CATEGORIES.values())


def _gmail_category_from_labels(labels: list[str] | None) -> str:
    """Derive the Gmail category subfolder from message labels.

    Returns the first matching category, or ``PRIMARY`` as default.
    """
    if not labels:
        return "PRIMARY"
    for label in labels:
        cat = _GMAIL_CATEGORIES.get(label)
        if cat:
            return cat
    return "PRIMARY"


@register_connector(
    "gws_gmail",
    description="Gmail via gws CLI",
    category="cli",
    service_name="gws",
)
class GmailConnector(PathCLIBackend):
    """Gmail CLI connector via ``gws gmail``.

    CLI-backed alternative to the existing PathGmailBackend API connector.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gmail"
    CLI_NAME = "gws"
    CLI_SERVICE = "gmail"
    AUTH_SOURCE = "gws-cli"

    SCHEMAS: dict[str, type] = {
        "send_email": SendEmailSchema,
        "reply_email": ReplyEmailSchema,
        "forward_email": ForwardEmailSchema,
        "create_draft": DraftEmailSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "send_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "reply_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "forward_email": OpTraits(reversibility=Reversibility.NONE, confirm=ConfirmLevel.USER),
        "create_draft": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "MISSING_RECIPIENTS": ErrorDef(
            message="Email requires at least one recipient",
            readme_section="operations",
            fix_example="to:\n  - user@example.com",
        ),
    }

    DIRECTORY_STRUCTURE = """\
/mnt/gmail/
  INBOX/                           # Unread + read inbox emails
    PRIMARY/                       # Primary category
    SOCIAL/                        # Social notifications
    UPDATES/                       # Updates and notifications
    PROMOTIONS/                    # Promotional emails
    FORUMS/                        # Forum and mailing list emails
  SENT/                            # Sent emails
    _new.yaml                      # ✏ Write here to SEND an email (irreversible)
    _reply.yaml                    # ✏ Write here to REPLY to an email
    _forward.yaml                  # ✏ Write here to FORWARD an email
  STARRED/                         # Starred emails
  IMPORTANT/                       # Important emails
  DRAFTS/                          # Draft emails
    _new.yaml                      # ✏ Write here to CREATE a draft (reversible)

/skills/gmail/                     # Skill docs (this file + schemas)
  README.md                         # This document
  schemas/
    send_email.yaml                # Schema for sending email
    reply_email.yaml               # Schema for replying
    forward_email.yaml             # Schema for forwarding
    create_draft.yaml              # Schema for drafts"""

    # Gmail label folders
    _LABELS = ["INBOX", "SENT", "STARRED", "IMPORTANT", "DRAFTS"]
    _last_history_id: str | None = None

    # Maximum messages returned by list_dir / list_dir_metadata combined.
    # One Gmail API page is 500 messages; 500 is fast for interactive use
    # and avoids fetching thousands of entries for large inboxes.
    # Override via CONNECTION_ARGS or subclass for data-dump workflows.
    MAX_LIST_RESULTS: int = 500
    # Short TTL (seconds) for the per-label ID list cache so that
    # list_dir and list_dir_metadata reuse the same API response when
    # called in sequence for the same path.
    _ID_LIST_CACHE_TTL: float = 5.0

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("gmail.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)
        # Per-instance cache: label_ids_key -> (timestamp, [(msg_id, thread_id)])
        self._id_list_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}

    # --- Shared paginated list helper ---

    def _paginated_message_list(
        self,
        label_ids: list[str],
        context: Any = None,
    ) -> list[tuple[str, str]]:
        """Fetch message (msg_id, thread_id) pairs for the given labels.

        Pages through ``messages.list`` until ``MAX_LIST_RESULTS`` is reached
        or the API signals no further pages.  Results are cached for
        ``_ID_LIST_CACHE_TTL`` seconds so that a ``list_dir`` / ``list_dir_metadata``
        pair for the same path shares one API round trip.

        The cache key includes the caller's user identity so that per-user
        data cannot leak across accounts on shared connector instances.

        Returns:
            List of ``(msg_id, thread_id)`` tuples, newest-first.
        """
        user_id = getattr(context, "user_id", None) or ""
        zone_id = getattr(context, "zone_id", None) or ""
        cache_key = f"{user_id}:{zone_id}:{','.join(sorted(label_ids))}"
        now = time.monotonic()
        cached = self._id_list_cache.get(cache_key)
        if cached is not None:
            cached_ts, cached_pairs = cached
            if now - cached_ts < self._ID_LIST_CACHE_TTL:
                return cached_pairs

        pairs: list[tuple[str, str]] = []
        page_token: str | None = None
        completed_cleanly = False

        while True:
            remaining = self.MAX_LIST_RESULTS - len(pairs)
            page_params: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(500, remaining),
                "labelIds": label_ids,
            }
            if page_token:
                page_params["pageToken"] = page_token

            result = self._execute_cli(
                [
                    "gws",
                    "gmail",
                    "users",
                    "messages",
                    "list",
                    "--params",
                    json.dumps(page_params),
                    "--format",
                    "yaml",
                ],
                context=context,
            )
            if not result.ok:
                break

            try:
                data = result.as_yaml() or {}
            except ValueError:
                logger.warning(
                    "[GMAIL] Failed to parse messages.list YAML for labels=%s", label_ids
                )
                break

            for msg in data.get("messages") or []:
                if isinstance(msg, dict) and msg.get("id"):
                    msg_id: str = str(msg["id"])
                    thread_id: str = str(msg.get("threadId") or msg_id)
                    pairs.append((msg_id, thread_id))

            page_token = data.get("nextPageToken")
            if not page_token or len(pairs) >= self.MAX_LIST_RESULTS:
                completed_cleanly = True
                break

        if len(pairs) >= self.MAX_LIST_RESULTS:
            logger.warning(
                "[GMAIL] Listing truncated at %d messages for labels=%s. "
                "Increase MAX_LIST_RESULTS on GmailConnector to see more.",
                self.MAX_LIST_RESULTS,
                label_ids,
            )

        result_pairs = pairs[: self.MAX_LIST_RESULTS]
        # Only cache after a fully successful pass — partial results from a
        # mid-pagination failure should not be served as authoritative for 5s.
        if completed_cleanly:
            self._id_list_cache[cache_key] = (now, result_pairs)
        return result_pairs

    # --- Batch metadata via list_dir_metadata protocol (Issue #3266) ---

    def list_dir_metadata(
        self,
        path: str = "/",
        context: Any = None,
    ) -> dict[str, dict[str, Any]] | None:
        """Batch-fetch message metadata using ``gws gmail +triage``.

        One CLI call returns subject, date, sender, and labels for up to 500
        messages in a directory, keyed by the ``threadId-msgId.yaml`` filename
        that ``list_dir()`` returns.  The sync service uses this to populate
        ``display_path()`` without per-message ``read_content`` calls.

        Returns ``None`` for root or label-only paths (no per-file metadata).
        """
        import json as _json

        path = path.strip("/")

        # Root or label-only listing — no per-file metadata to return.
        if not path:
            return None
        parts = path.split("/")
        label = parts[0]
        if label not in self._LABELS:
            return None
        # INBOX without a category subfolder — subfolder listing, not files.
        if label == "INBOX" and len(parts) == 1:
            return None

        # Determine the query label for +triage. Include the category label
        # explicitly so the 500-message cap surfaces the same messages
        # ``list_dir`` returned (R5-M2). Without this, the cap takes the
        # newest 500 INBOX-overall and post-filters by category, dropping
        # category-specific files that ``list_dir`` exposed.
        query_label = label
        category_filter: str | None = None
        category_label_id: str | None = None
        label_ids: list[str] = [label]
        if label == "INBOX" and len(parts) >= 2:
            category_filter = parts[1]
            for gmail_label, folder in _GMAIL_CATEGORIES.items():
                if folder == category_filter:
                    category_label_id = gmail_label
                    label_ids.append(gmail_label)
                    break

        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        # Fetch triage metadata in one call. Add the category label to the
        # search query when present so triage results match the listing.
        query = f"label:{query_label}"
        if category_label_id:
            query = f"{query} label:{category_label_id}"
        try:
            r = self._execute_cli(
                [
                    "gws",
                    "gmail",
                    "+triage",
                    "--max",
                    "500",
                    "--query",
                    query,
                    "--labels",
                    "--format",
                    "json",
                ],
                context=context,
            )
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        if not r.ok:
            logger.warning(
                "[GMAIL_LIST_DIR_META] +triage failed for label=%s: %s",
                query_label,
                r.stderr[:200],
            )
            return None

        try:
            raw = r.stdout[r.stdout.index("{") :]
            data = _json.loads(raw)
        except Exception:
            logger.warning(
                "[GMAIL_LIST_DIR_META] Failed to parse +triage JSON for label=%s", query_label
            )
            return None

        # Build metadata dict keyed by message ID.
        # The list_dir entries have format "threadId-msgId.yaml" — we need to
        # also index by bare msgId for lookup.
        id_to_meta: dict[str, dict[str, Any]] = {}
        messages = data.get("messages", [])
        for msg in messages:
            msg_id = msg.get("id", "")
            if not msg_id:
                continue
            meta = {
                "subject": msg.get("subject", ""),
                "date": msg.get("date", ""),
                "from": msg.get("from", ""),
                "labels": msg.get("labels", []),
                "labelIds": msg.get("labels", []),
            }
            id_to_meta[msg_id] = meta

        # Now map from list_dir filenames to metadata. Pass the same
        # label_ids list_dir uses so the cached pair set matches the
        # triage result set (R5-M2).
        try:
            pairs = self._paginated_message_list(label_ids, context=context)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        if not pairs:
            # Fall back to id-keyed metadata — callers can look up by
            # extracting msg_id from the filename.
            return id_to_meta

        filename_to_meta: dict[str, dict[str, Any]] = {}
        for msg_id, tid in pairs:
            file_meta = id_to_meta.get(msg_id)
            if not file_meta:
                continue
            # Filter by category for INBOX sublists.
            if category_filter:
                msg_category = _gmail_category_from_labels(file_meta.get("labels"))
                if msg_category != category_filter:
                    continue
            filename = f"{tid}-{msg_id}.yaml"
            filename_to_meta[filename] = file_meta

        logger.info(
            "[GMAIL_LIST_DIR_META] Batch metadata for %d files (label=%s)",
            len(filename_to_meta),
            query_label,
        )
        return filename_to_meta

    # --- Display path (Issue #3256) ---

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Generate human-readable email path with category subfolders.

        Format: ``{label}/{category}/{date}_{subject}.yaml``
        Example: ``INBOX/PRIMARY/2026-03-20_Re-Meeting-Notes.yaml``
        """
        meta = metadata or {}

        # Determine label folder (default: INBOX).
        labels: list[str] = meta.get("labels", meta.get("labelIds", []))
        label_folder = "INBOX"
        for lbl in labels:
            if lbl in self._LABELS:
                label_folder = lbl
                break

        # Build filename from subject and date.
        parts: list[str] = []
        date_str = meta.get("date", meta.get("internalDate", ""))
        if date_str:
            date_prefix = self._extract_date_prefix(date_str)
            if date_prefix:
                parts.append(date_prefix)
        subject = meta.get("subject", "")
        if subject:
            parts.append(sanitize_filename(subject, max_len=80))
        else:
            parts.append(item_id)

        filename = "_".join(parts) + ".yaml" if parts else f"{item_id}.yaml"

        # Category subfolder for INBOX only.
        if label_folder == "INBOX":
            category = _gmail_category_from_labels(labels)
            return f"{label_folder}/{category}/{filename}"

        return f"{label_folder}/{filename}"

    @staticmethod
    def _extract_date_prefix(date_str: str) -> str:
        """Extract YYYY-MM-DD from an ISO 8601 or RFC 2822 date string."""
        # Try ISO 8601 first (2026-03-20T10:00:00Z).
        if len(date_str) >= 10 and date_str[4:5] == "-":
            return date_str[:10]
        # Try to parse RFC 2822 (Mon, 20 Mar 2026 10:00:00 +0000).
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(date_str)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

    # --- Write path: use _prepare_stdin() hook instead of overriding write_content() ---

    def _prepare_stdin(self, operation: str, validated: Any, data: dict) -> str | None:
        """Return None — Gmail gws helper commands use flags, not stdin YAML."""
        if operation in ("send_email", "reply_email", "forward_email"):
            return None
        # create_draft uses the standard stdin YAML path.
        return super()._prepare_stdin(operation, validated, data)

    def _build_cli_args(self, operation: str, validated: Any, path: str) -> list[str]:
        """Build gws gmail CLI args from validated schema data.

        gws helper commands (+send, +reply, +forward) use flags, not stdin YAML.
        """
        args = ["gws", "gmail"]

        data = validated.model_dump(exclude_none=True) if hasattr(validated, "model_dump") else {}

        if operation == "send_email":
            args.append("+send")
            to = data.get("to", [])
            if isinstance(to, list):
                args.extend(["--to", ",".join(to)])
            else:
                args.extend(["--to", str(to)])
            args.extend(["--subject", data.get("subject", "")])
            args.extend(["--body", data.get("body", "")])
            cc = data.get("cc")
            if cc:
                args.extend(["--cc", ",".join(cc) if isinstance(cc, list) else str(cc)])
        elif operation == "reply_email":
            args.append("+reply")
            args.extend(["--to", data.get("message_id", "")])
            args.extend(["--body", data.get("body", "")])
        elif operation == "forward_email":
            args.append("+forward")
            to = data.get("to") or []
            if isinstance(to, list):
                args.extend(["--to", ",".join(to)])
            else:
                args.extend(["--to", str(to)])
            args.extend(["--body", data.get("body", "")])
        elif operation == "create_draft":
            args.extend(["users", "drafts", "create"])
        else:
            return super()._build_cli_args(operation, validated, path)

        args.extend(["--format", "yaml"])
        return args

    # --- Read/sync operations ---

    def get_history_id(self, context: Any = None) -> str | None:
        """Get current Gmail historyId for delta sync."""
        import json as _json

        r = self._execute_cli(
            [
                "gws",
                "gmail",
                "users",
                "getProfile",
                "--params",
                '{"userId":"me"}',
                "--format",
                "json",
            ],
            context=context,
        )
        if r.ok:
            try:
                # Skip "Using keyring backend" line
                raw = r.stdout[r.stdout.index("{") :]
                data = _json.loads(raw)
                return str(data.get("historyId", ""))
            except Exception:
                pass
        return None

    def get_delta_changes(
        self,
        since_history_id: str,
        context: Any = None,
    ) -> tuple[list[str], list[str], str | None]:
        """Get message IDs changed since a historyId.

        Returns (added_ids, deleted_ids, new_history_id).
        Uses Gmail history.list API for true delta sync.
        """
        import json as _json

        r = self._execute_cli(
            [
                "gws",
                "gmail",
                "users",
                "history",
                "list",
                "--params",
                _json.dumps(
                    {
                        "userId": "me",
                        "startHistoryId": since_history_id,
                        "maxResults": 500,
                    }
                ),
                "--format",
                "json",
            ],
            context=context,
        )
        if not r.ok:
            return [], [], None

        try:
            raw = r.stdout[r.stdout.index("{") :]
            data = _json.loads(raw)
        except Exception:
            return [], [], None

        added: list[str] = []
        deleted: list[str] = []
        for entry in data.get("history", []):
            for msg in entry.get("messagesAdded", []):
                mid = msg.get("message", {}).get("id", "")
                if mid:
                    added.append(mid)
            for msg in entry.get("messagesDeleted", []):
                mid = msg.get("message", {}).get("id", "")
                if mid:
                    deleted.append(mid)

        new_hid = data.get("historyId")
        return added, deleted, str(new_hid) if new_hid else None

    def list_dir(
        self,
        path: str = "/",
        context: Any = None,
    ) -> list[str]:
        """List Gmail labels, category subfolders, or messages."""
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        path = path.strip("/")

        if not path:
            # Root: return label folders
            return [f"{label}/" for label in self._LABELS]

        parts = path.split("/")
        label = parts[0]

        # INBOX listing: return category subfolders (Decision 16A).
        if label == "INBOX" and len(parts) == 1:
            return [f"{cat}/" for cat in _GMAIL_CATEGORY_FOLDERS]

        # Inside a label (or INBOX/CATEGORY): list messages.
        label_ids = [label]
        if label == "INBOX" and len(parts) >= 2:
            category_name = parts[1]
            # Map category folder name back to Gmail label ID.
            for gmail_label, folder in _GMAIL_CATEGORIES.items():
                if folder == category_name:
                    label_ids.append(gmail_label)
                    break

        try:
            pairs = self._paginated_message_list(label_ids, context=context)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc
        return [f"{tid}-{msg_id}.yaml" for msg_id, tid in pairs]

    def read_content(
        self,
        content_hash: str,
        context: Any = None,
    ) -> bytes:
        """Read a Gmail message as YAML via gws CLI.

        Fetches the full message with ``format=full`` so the body is available,
        walks the MIME payload tree to extract ``text/plain`` (or ``text/html``
        as a fallback), and returns all structured fields the API provides.
        Raises ``BackendError`` on CLI failure so callers get an explicit error
        instead of a silently empty file.
        """
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.backends.connectors.gws._gmail_utils import extract_body
        from nexus.contracts.exceptions import BackendError

        # Extract message ID from content_hash or context.backend_path.
        # Filename format: threadId-msgId.yaml  →  msg_id is the last segment.
        msg_id = content_hash
        filename = content_hash
        if context and hasattr(context, "backend_path") and context.backend_path:
            filename = context.backend_path.rstrip("/").rsplit("/", 1)[-1]
        # Strip .yaml suffix then take the last "-" segment regardless of source.
        stem = filename.replace(".yaml", "")
        if "-" in stem:
            msg_id = stem.split("-")[-1]

        try:
            result = self._execute_cli(
                [
                    "gws",
                    "gmail",
                    "users",
                    "messages",
                    "get",
                    "--params",
                    json.dumps({"userId": "me", "id": msg_id, "format": "full"}),
                    "--format",
                    "yaml",
                ],
                context=context,
            )
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=msg_id) from exc
        if not result.ok:
            raise BackendError(result.summary(), backend=self.name, path=msg_id)

        try:
            msg = result.as_yaml() or {}
        except ValueError as exc:
            raise BackendError(
                f"Failed to parse Gmail message response: {exc}",
                backend=self.name,
                path=msg_id,
            ) from exc

        headers = {
            h["name"]: h["value"]
            for h in (msg.get("payload", {}).get("headers") or [])
            if isinstance(h, dict) and h.get("name")
        }

        body = extract_body(msg.get("payload") or {})

        out: dict[str, Any] = {
            "id": msg.get("id", msg_id),
            "threadId": msg.get("threadId"),
            "labels": msg.get("labelIds", []),
            "historyId": msg.get("historyId"),
            "internalDate": msg.get("internalDate"),
            "snippet": msg.get("snippet", ""),
            "subject": headers.get("Subject", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
            "date": headers.get("Date", ""),
            "body": body,
        }
        out = {k: v for k, v in out.items() if v not in (None, "", [])}

        rendered: str = (
            yaml.dump(out, default_flow_style=False, allow_unicode=True, sort_keys=False) or ""
        )
        return rendered.encode("utf-8")

    def sync_delta(self, context: Any = None) -> dict[str, Any]:
        """Perform delta sync using Gmail historyId.

        Returns dict with added/deleted message IDs and new historyId.
        Call this instead of full list_dir for incremental updates.
        """
        if self._last_history_id is None:
            # First sync: get current historyId, return empty delta
            self._last_history_id = self.get_history_id(context=context)
            return {
                "added": [],
                "deleted": [],
                "history_id": self._last_history_id,
                "full_sync": True,
            }

        added, deleted, new_hid = self.get_delta_changes(self._last_history_id, context=context)
        if new_hid:
            self._last_history_id = new_hid

        return {
            "added": added,
            "deleted": deleted,
            "history_id": new_hid or self._last_history_id,
            "full_sync": False,
        }

    def get_file_info(self, path: str, context: Any = None) -> Any:
        """Return file metadata wrapped in a response object for sync service."""
        from datetime import datetime
        from types import SimpleNamespace

        from nexus.backends.base.backend import FileInfo

        if self.is_directory(path, context):
            fi = FileInfo(size=0, mtime=datetime.now(UTC))
        else:
            hid = self._last_history_id or self.get_history_id(context=context)
            fi = FileInfo(
                size=1,
                mtime=datetime.now(UTC),
                backend_version=hid,
                content_hash=f"gmail:{path}",
            )

        return SimpleNamespace(success=True, data=fi)

    def is_directory(self, path: str, context: Any = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        parts = path.split("/")
        # Label folder or category subfolder.
        return parts[0] in self._LABELS and not path.endswith(".yaml")


# ============================================================================
# Calendar
# ============================================================================


@register_connector(
    "gws_calendar",
    description="Google Calendar via gws CLI",
    category="cli",
    service_name="gws",
)
class CalendarConnector(PathCLIBackend):
    """Calendar CLI connector via ``gws calendar``.

    CLI-backed alternative to the existing PathCalendarBackend.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gcalendar"
    CLI_NAME = "gws"
    CLI_SERVICE = "calendar"
    AUTH_SOURCE = "gws-cli"

    DIRECTORY_STRUCTURE = """\
/mnt/calendar/
  primary/                         # Primary calendar
    2026-03/                       # Month-based grouping
      2026-03-21_10-00_Team-Standup.yaml
    _new.yaml                      # ✏ Write here to CREATE an event
    _update.yaml                   # ✏ Write here to UPDATE an event
    _delete.yaml                   # ✏ Write here to DELETE an event (irreversible)
  {calendarId}/                    # Other calendars (shared, holidays, etc.)
  .readme/
    README.md"""

    SCHEMAS: dict[str, type] = {
        "create_event": CreateEventSchema,
        "update_event": UpdateEventSchema,
        "delete_event": DeleteEventSchema,
    }
    OPERATION_TRAITS: dict[str, OpTraits] = {
        "create_event": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.INTENT),
        "update_event": OpTraits(reversibility=Reversibility.FULL, confirm=ConfirmLevel.EXPLICIT),
        "delete_event": OpTraits(reversibility=Reversibility.PARTIAL, confirm=ConfirmLevel.USER),
    }
    ERROR_REGISTRY: dict[str, ErrorDef] = {
        **TRAIT_ERRORS,
        "EVENT_NOT_FOUND": ErrorDef(
            message="Calendar event not found",
            readme_section="operations",
            fix_example="event_id: <valid event ID>",
        ),
    }

    # Maximum events returned per calendar list_dir call.
    MAX_LIST_RESULTS: int = 500

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("calendar.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)
        # Cache mapping calendarId -> human-readable display name.
        # Populated by _fetch_calendar_names() on first root list_dir.
        self._calendar_names: dict[str, str] = {}

    # --- Calendar name mapping ---

    def _fetch_calendar_names(self, context: Any = None) -> dict[str, str]:
        """Fetch calendar ID -> display name mapping via calendarList.

        Returns a dict like ``{"primary": "My Calendar",
        "family@group.calendar.google.com": "Family"}``.

        Lets ``ScopedAuthRequiredError`` propagate so callers can wrap it
        in ``BackendError`` (R5-H2). Other exceptions are swallowed — they
        represent transient CLI/parse failures, not auth-policy violations.
        """
        import re

        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError

        # Tolerate missing attribute (e.g., __new__ without __init__).
        if not hasattr(self, "_calendar_names"):
            self._calendar_names = {}
        if self._calendar_names:
            return self._calendar_names

        try:
            result = self._execute_cli(
                ["gws", "calendar", "calendarList", "list", "--format", "yaml"],
                context=context,
            )
        except ScopedAuthRequiredError:
            # Auth violation must surface — don't pretend it was a clean result.
            raise
        except Exception:
            return self._calendar_names
        if not result.ok:
            return self._calendar_names

        # Parse id/summary pairs from calendarList YAML output.
        ids = re.findall(r'id:\s*"([^"]+)"', result.stdout)
        summaries = re.findall(r'summary:\s*"([^"]*)"', result.stdout)
        for i, cid in enumerate(ids):
            name = summaries[i] if i < len(summaries) else cid
            self._calendar_names[cid] = name

        return self._calendar_names

    def _calendar_display_name(self, cal_id: str, context: Any = None) -> str:
        """Return a human-readable folder name for a calendar ID.

        On a cold cache with no context (e.g. sync service rendering a
        display path), return the raw calendar ID rather than triggering
        an unscoped CLI lookup against the host's default profile (R5-H2).
        """
        cached = getattr(self, "_calendar_names", None) or {}
        if not cached and context is None:
            return cal_id
        names = self._fetch_calendar_names(context=context)
        name = names.get(cal_id, cal_id)
        return sanitize_filename(name, max_len=60) if name != cal_id else cal_id

    def _resolve_calendar_id(self, folder_name: str, context: Any = None) -> str:
        """Resolve a display folder name back to a calendar ID.

        Handles both raw calendar IDs (``primary``, ``user@gmail.com``)
        and sanitized display names (``My-Calendar``). On cold cache with
        no context, treats ``folder_name`` as a literal calendar ID rather
        than triggering an unscoped lookup (R5-H2).
        """
        cached = getattr(self, "_calendar_names", None) or {}
        if not cached and context is None:
            return folder_name
        names = self._fetch_calendar_names(context=context)
        # Direct match on calendar ID.
        if folder_name in names:
            return folder_name
        # Reverse lookup: sanitized display name -> calendar ID.
        for cid, name in names.items():
            if sanitize_filename(name, max_len=60) == folder_name:
                return cid
        # Fallback: treat as literal calendar ID.
        return folder_name

    # --- Display path (Issue #3256) ---

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Generate human-readable event path with month grouping.

        Format: ``{calendar_name}/{YYYY-MM}/{date}_{time}_{summary}.yaml``
        Example: ``My-Calendar/2026-03/2026-03-21_10-00_Team-Standup.yaml``
        """
        meta = metadata or {}
        cal_id = meta.get("calendarId", "primary")
        cal_folder = self._calendar_display_name(cal_id)

        parts: list[str] = []
        month_folder = ""

        # Extract start date/time.
        start = meta.get("start", {})
        if isinstance(start, dict):
            date_str = start.get("dateTime", start.get("date", ""))
        else:
            date_str = str(start) if start else ""

        if date_str and len(date_str) >= 10:
            date_prefix = date_str[:10]  # YYYY-MM-DD
            month_folder = date_str[:7]  # YYYY-MM
            parts.append(date_prefix)
            # Add time if available (HH-MM).
            if len(date_str) >= 16 and "T" in date_str:
                time_part = date_str[11:16].replace(":", "-")
                parts.append(time_part)
        elif date_str:
            # All-day event with just a date.
            parts.append(date_str[:10] if len(date_str) >= 10 else date_str)

        summary = meta.get("summary", "")
        if summary:
            parts.append(sanitize_filename(summary, max_len=60))
        else:
            parts.append(item_id)

        filename = "_".join(parts) + ".yaml" if parts else f"{item_id}.yaml"

        if month_folder:
            return f"{cal_folder}/{month_folder}/{filename}"
        return f"{cal_folder}/{filename}"

    # --- Batch metadata via list_dir_metadata protocol (Issue #3266) ---

    def list_dir_metadata(
        self,
        path: str = "/",
        context: Any = None,
    ) -> dict[str, dict[str, Any]] | None:
        """Batch-fetch event metadata using ``gws calendar +agenda``.

        Returns ``{filename: {summary, start, calendarId, ...}}`` for all
        events in a calendar/month directory.  Returns ``None`` for root
        (calendar listing) since those are folders, not files.
        """
        import json as _json
        import re

        path = path.strip("/")

        # Root listing — no per-file metadata.
        if not path:
            return None

        parts = path.split("/")
        # Resolve human-readable folder name back to calendar ID.
        cal_id = self._resolve_calendar_id(parts[0], context=context)
        month_filter = parts[1] if len(parts) >= 2 else None

        # Fetch events from API (mirrors list_dir pagination logic).
        import calendar as _cal_mod
        from datetime import datetime

        _lookback_year = datetime.now(UTC).year - 3
        base_params: dict[str, Any] = {
            "calendarId": cal_id,
            "maxResults": self.MAX_LIST_RESULTS,
            "singleEvents": True,
            "orderBy": "startTime",
            "timeMin": f"{_lookback_year}-01-01T00:00:00Z",
        }
        if month_filter and re.match(r"^\d{4}-\d{2}$", month_filter):
            year, month = int(month_filter[:4]), int(month_filter[5:7])
            last_day = _cal_mod.monthrange(year, month)[1]
            base_params["timeMin"] = f"{month_filter}-01T00:00:00Z"
            base_params["timeMax"] = f"{month_filter}-{last_day}T23:59:59Z"

        all_items: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            remaining = self.MAX_LIST_RESULTS - len(all_items)
            if remaining <= 0:
                break
            page_params = {**base_params, "maxResults": min(self.MAX_LIST_RESULTS, remaining)}
            if page_token:
                page_params["pageToken"] = page_token

            from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
            from nexus.contracts.exceptions import BackendError

            try:
                result = self._execute_cli(
                    [
                        "gws",
                        "calendar",
                        "events",
                        "list",
                        "--params",
                        _json.dumps(page_params),
                        "--format",
                        "json",
                    ],
                    context=context,
                )
            except ScopedAuthRequiredError as exc:
                raise BackendError(str(exc), backend=self.name, path=path) from exc
            if not result.ok:
                break

            try:
                raw = result.stdout
                # Skip any preamble before the JSON.
                idx = raw.index("{")
                data = _json.loads(raw[idx:])
            except Exception:
                break

            page_items = data.get("items") or []
            remaining_after = self.MAX_LIST_RESULTS - len(all_items)
            all_items.extend(page_items[:remaining_after])
            page_token = data.get("nextPageToken")

            if not page_token or len(all_items) >= self.MAX_LIST_RESULTS:
                break

        items = all_items
        if not items:
            return None

        filename_to_meta: dict[str, dict[str, Any]] = {}
        for event in items:
            event_id = event.get("id", "")
            if not event_id:
                continue
            meta: dict[str, Any] = {
                "summary": event.get("summary", ""),
                "calendarId": cal_id,
            }
            start = event.get("start", {})
            if start:
                meta["start"] = start
            end = event.get("end", {})
            if end:
                meta["end"] = end

            filename = f"{event_id}.yaml"
            filename_to_meta[filename] = meta

        logger.info(
            "[CAL_LIST_DIR_META] Batch metadata for %d events (cal=%s)",
            len(filename_to_meta),
            cal_id,
        )
        return filename_to_meta

    # --- Read/sync operations ---

    def list_dir(
        self,
        path: str = "/",
        context: Any = None,
    ) -> list[str]:
        """List calendars or events.

        Root returns human-readable calendar folder names (via calendarList).
        """
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        try:
            return self._list_dir_impl(path, context)
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=path) from exc

    def _list_dir_impl(self, path: str, context: Any) -> list[str]:
        import json
        import re

        path = path.strip("/")

        if not path:
            # Root: list calendars with human-readable names.
            names = self._fetch_calendar_names(context=context)
            if names:
                return [f"{sanitize_filename(name, max_len=60)}/" for name in names.values()]
            return ["primary/"]

        # Inside a calendar (or calendar/month): list events
        import calendar as _cal_mod

        parts = path.split("/")
        # Resolve human-readable folder name back to calendar ID for API calls.
        cal_id = self._resolve_calendar_id(parts[0], context=context)
        month_filter = parts[1] if len(parts) >= 2 else None

        # For root listings we want to discover which months exist across all
        # recent history, so use a 3-year lookback from today.  For month-specific
        # listings the timeMin/timeMax are overridden below to the month bounds.
        from datetime import datetime

        _lookback_year = datetime.now(UTC).year - 3
        _three_years_ago = f"{_lookback_year}-01-01T00:00:00Z"

        # Root listing: expand recurring events (singleEvents=True) so each
        # occurrence contributes its actual start date to month discovery.
        # orderBy=updated is intentional: it surfaces recently active events first,
        # which guarantees that current months appear within MAX_LIST_RESULTS even
        # on calendars with years of dense recurring events.  orderBy=startTime
        # would instead return the oldest occurrences first, hiding current months
        # entirely on large calendars.  The trade-off is that months whose events
        # have not been modified recently may not appear in root if the total event
        # count exceeds MAX_LIST_RESULTS — accepted as a known limitation.
        # Month-specific listings use orderBy=startTime to return events in
        # chronological order within the requested month window.
        if month_filter and re.match(r"^\d{4}-\d{2}$", month_filter):
            year, month = int(month_filter[:4]), int(month_filter[5:7])
            last_day = _cal_mod.monthrange(year, month)[1]
            base_params: dict[str, Any] = {
                "calendarId": cal_id,
                "maxResults": self.MAX_LIST_RESULTS,
                "singleEvents": True,
                "orderBy": "startTime",
                "timeMin": f"{month_filter}-01T00:00:00Z",
                "timeMax": f"{month_filter}-{last_day}T23:59:59Z",
            }
        else:
            base_params = {
                "calendarId": cal_id,
                "maxResults": self.MAX_LIST_RESULTS,
                "singleEvents": True,
                "orderBy": "updated",
                "timeMin": _three_years_ago,
            }
        # Paginate until MAX_LIST_RESULTS or no further pages.  Clip each page's
        # maxResults to the remaining budget so the total never exceeds the cap.
        all_items: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            remaining = self.MAX_LIST_RESULTS - len(all_items)
            if remaining <= 0:
                break
            page_params = {**base_params, "maxResults": min(self.MAX_LIST_RESULTS, remaining)}
            if page_token:
                page_params["pageToken"] = page_token

            result = self._execute_cli(
                [
                    "gws",
                    "calendar",
                    "events",
                    "list",
                    "--params",
                    json.dumps(page_params),
                    "--format",
                    "yaml",
                ],
                context=context,
            )
            if not result.ok:
                break

            try:
                data = result.as_yaml() or {}
            except ValueError:
                break

            items = data.get("items") or []
            # Clip to remaining budget even if the API returns more than requested.
            remaining_after = self.MAX_LIST_RESULTS - len(all_items)
            all_items.extend(items[:remaining_after])
            page_token = data.get("nextPageToken")

            if not page_token or len(all_items) >= self.MAX_LIST_RESULTS:
                break

        event_ids = [item["id"] for item in all_items if isinstance(item, dict) and item.get("id")]

        # Calendar root listing: return month subfolders derived from event dates.
        if not month_filter:
            start_dates: list[str] = []
            for item in all_items:
                if not isinstance(item, dict):
                    continue
                start = item.get("start") or {}
                dt = start.get("dateTime", "") or start.get("date", "")
                if dt and len(dt) >= 7:
                    start_dates.append(dt[:7])
            months = sorted(set(start_dates))
            if months:
                return [f"{m}/" for m in months]
            # Fallback: no parseable dates, return flat event list.
            return [f"{eid}.yaml" for eid in event_ids]

        # Month listing: return event files.
        return [f"{eid}.yaml" for eid in event_ids]

    def read_content(
        self,
        content_hash: str,
        context: Any = None,
    ) -> bytes:
        """Read a calendar event as YAML via gws CLI."""
        from nexus.backends.connectors.cli.base import ScopedAuthRequiredError
        from nexus.contracts.exceptions import BackendError

        event_id = content_hash
        cal_id = "primary"
        try:
            if context and hasattr(context, "backend_path") and context.backend_path:
                parts = context.backend_path.strip("/").split("/")
                if len(parts) >= 2:
                    cal_id = self._resolve_calendar_id(parts[0], context=context)
                    event_id = parts[-1].replace(".yaml", "")

            result = self._execute_cli(
                [
                    "gws",
                    "calendar",
                    "events",
                    "get",
                    "--params",
                    json.dumps({"calendarId": cal_id, "eventId": event_id}),
                    "--format",
                    "yaml",
                ],
                context=context,
            )
        except ScopedAuthRequiredError as exc:
            raise BackendError(str(exc), backend=self.name, path=event_id) from exc
        if result.ok:
            return result.stdout.encode("utf-8")
        return b""

    def get_file_info(self, path: str, context: Any = None) -> Any:
        """Return file metadata wrapped in response for sync service."""
        from datetime import datetime
        from types import SimpleNamespace

        from nexus.backends.base.backend import FileInfo

        if self.is_directory(path, context):
            fi = FileInfo(size=0, mtime=datetime.now(UTC))
        else:
            fi = FileInfo(size=1, mtime=datetime.now(UTC), content_hash=f"cal:{path}")
        return SimpleNamespace(success=True, data=fi)

    def is_directory(self, path: str, context: Any = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        return not path.endswith(".yaml")
