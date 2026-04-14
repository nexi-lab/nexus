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
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


@register_connector("gws_sheets")
class SheetsConnector(PathCLIBackend):
    """Google Sheets CLI connector via ``gws sheets``."""

    SKILL_NAME = "sheets"
    CLI_NAME = "gws"
    CLI_SERVICE = "sheets"

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
        result = self._execute_cli(args, context=context, env=auth_env)
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


@register_connector("gws_docs")
class DocsConnector(PathCLIBackend):
    """Google Docs CLI connector via ``gws docs``."""

    SKILL_NAME = "docs"
    CLI_NAME = "gws"
    CLI_SERVICE = "docs"

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
        result = self._execute_cli(args, context=context, env=auth_env)
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            from nexus.contracts.exceptions import BackendError

            raise BackendError(result.summary(), backend=self.name, path="/")

        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            from nexus.contracts.exceptions import BackendError

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
        result = self._execute_cli(args, context=context, env=auth_env)
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            raise BackendError(result.summary(), backend=self.name, path=backend_path)
        return result.stdout.encode("utf-8")


# ============================================================================
# Chat
# ============================================================================


@register_connector("gws_chat")
class ChatConnector(PathCLIBackend):
    """Google Chat CLI connector via ``gws chat``."""

    SKILL_NAME = "chat"
    CLI_NAME = "gws"
    CLI_SERVICE = "chat"

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
        result = self._execute_cli(args, context=context, env=auth_env)
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            from nexus.contracts.exceptions import BackendError

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


@register_connector("gws_drive")
class DriveConnector(PathCLIBackend):
    """Google Drive CLI connector via ``gws drive``."""

    SKILL_NAME = "drive"
    CLI_NAME = "gws"
    CLI_SERVICE = "drive"

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
        result = self._execute_cli(args, context=context, env=auth_env)
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


@register_connector("gws_gmail")
class GmailConnector(PathCLIBackend):
    """Gmail CLI connector via ``gws gmail``.

    CLI-backed alternative to the existing PathGmailBackend API connector.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gmail"
    CLI_NAME = "gws"
    CLI_SERVICE = "gmail"

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

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("gmail.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

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
        import re

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

        # Determine the query label for +triage.
        query_label = label
        category_filter: str | None = None
        if label == "INBOX" and len(parts) >= 2:
            category_filter = parts[1]

        # Fetch triage metadata in one call.
        query = f"label:{query_label}"
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
        )
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

        # Now map from list_dir filenames to metadata.
        # We need the actual list_dir entries — fetch message list to get
        # threadId-msgId pairs, then match against triage metadata.
        result = self._execute_cli(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                _json.dumps({"userId": "me", "maxResults": 50, "labelIds": [label]}),
                "--format",
                "yaml",
            ],
        )
        if not result.ok:
            # Return the id-keyed metadata anyway — callers can look up
            # by extracting msg_id from the filename.
            return id_to_meta

        ids = re.findall(r'id:\s*"([^"]+)"', result.stdout)
        thread_ids = re.findall(r'threadId:\s*"([^"]+)"', result.stdout)

        filename_to_meta: dict[str, dict[str, Any]] = {}
        for i, msg_id in enumerate(ids):
            file_meta = id_to_meta.get(msg_id)
            if not file_meta:
                continue
            # Filter by category for INBOX sublists.
            if category_filter:
                msg_category = _gmail_category_from_labels(file_meta.get("labels"))
                if msg_category != category_filter:
                    continue
            tid = thread_ids[i] if i < len(thread_ids) else msg_id
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

    def get_history_id(self) -> str | None:
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

    def get_delta_changes(self, since_history_id: str) -> tuple[list[str], list[str], str | None]:
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
        import json
        import re

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

        # Use messages.list to get IDs with threadIds.
        result = self._execute_cli(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": "me", "maxResults": 50, "labelIds": label_ids}),
                "--format",
                "yaml",
            ],
        )
        if not result.ok:
            return []

        ids = re.findall(r'id:\s*"([^"]+)"', result.stdout)
        thread_ids = re.findall(r'threadId:\s*"([^"]+)"', result.stdout)
        entries = []
        for i, msg_id in enumerate(ids):
            tid = thread_ids[i] if i < len(thread_ids) else msg_id
            entries.append(f"{tid}-{msg_id}.yaml")
        return entries

    def read_content(
        self,
        content_hash: str,
        context: Any = None,
    ) -> bytes:
        """Read a Gmail message as YAML via gws CLI."""
        import json
        import re

        # Extract message ID from backend_path or content_hash
        msg_id = content_hash
        if context and hasattr(context, "backend_path") and context.backend_path:
            # Path format: INBOX/threadId-msgId.yaml
            filename = context.backend_path.rstrip("/").rsplit("/", 1)[-1]
            if "-" in filename:
                msg_id = filename.replace(".yaml", "").split("-")[-1]

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
        )
        if not result.ok:
            return b""

        # Extract useful fields into a clean YAML
        stdout = result.stdout
        fields: dict[str, Any] = {"id": msg_id}

        # Parse headers
        for header_name in ["Subject", "From", "To", "Date"]:
            match = re.search(rf'name:\s*"{header_name}"\s*\n\s*value:\s*"([^"]*)"', stdout)
            if match:
                fields[header_name.lower()] = match.group(1)

        # Parse snippet
        snippet_match = re.search(r'snippet:\s*"([^"]*)"', stdout)
        if snippet_match:
            fields["snippet"] = snippet_match.group(1)

        # Parse labels
        labels = re.findall(r'- "([A-Z_]+)"', stdout)
        if labels:
            fields["labels"] = labels

        import importlib

        yaml_module = importlib.import_module("yaml")
        rendered = str(yaml_module.dump(fields, default_flow_style=False, allow_unicode=True))

        return rendered.encode("utf-8")

    def sync_delta(self) -> dict[str, Any]:
        """Perform delta sync using Gmail historyId.

        Returns dict with added/deleted message IDs and new historyId.
        Call this instead of full list_dir for incremental updates.
        """
        if self._last_history_id is None:
            # First sync: get current historyId, return empty delta
            self._last_history_id = self.get_history_id()
            return {
                "added": [],
                "deleted": [],
                "history_id": self._last_history_id,
                "full_sync": True,
            }

        added, deleted, new_hid = self.get_delta_changes(self._last_history_id)
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
            hid = self._last_history_id or self.get_history_id()
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


@register_connector("gws_calendar")
class CalendarConnector(PathCLIBackend):
    """Calendar CLI connector via ``gws calendar``.

    CLI-backed alternative to the existing PathCalendarBackend.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gcalendar"
    CLI_NAME = "gws"
    CLI_SERVICE = "calendar"

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

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("calendar.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)
        # Cache mapping calendarId -> human-readable display name.
        # Populated by _fetch_calendar_names() on first root list_dir.
        self._calendar_names: dict[str, str] = {}

    # --- Calendar name mapping ---

    def _fetch_calendar_names(self) -> dict[str, str]:
        """Fetch calendar ID -> display name mapping via calendarList.

        Returns a dict like ``{"primary": "My Calendar",
        "family@group.calendar.google.com": "Family"}``.
        """
        import re

        # Tolerate missing attribute (e.g., __new__ without __init__).
        if not hasattr(self, "_calendar_names"):
            self._calendar_names = {}
        if self._calendar_names:
            return self._calendar_names

        try:
            result = self._execute_cli(
                ["gws", "calendar", "calendarList", "list", "--format", "yaml"],
            )
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

    def _calendar_display_name(self, cal_id: str) -> str:
        """Return a human-readable folder name for a calendar ID."""
        names = self._fetch_calendar_names()
        name = names.get(cal_id, cal_id)
        return sanitize_filename(name, max_len=60) if name != cal_id else cal_id

    def _resolve_calendar_id(self, folder_name: str) -> str:
        """Resolve a display folder name back to a calendar ID.

        Handles both raw calendar IDs (``primary``, ``user@gmail.com``)
        and sanitized display names (``My-Calendar``).
        """
        names = self._fetch_calendar_names()
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
        cal_id = self._resolve_calendar_id(parts[0])
        month_filter = parts[1] if len(parts) >= 2 else None

        # Fetch events from API (same as list_dir).
        params: dict[str, Any] = {
            "calendarId": cal_id,
            "maxResults": 250,
            "singleEvents": True,
            "orderBy": "startTime",
            "timeMin": "2025-01-01T00:00:00Z",
        }
        if month_filter and re.match(r"^\d{4}-\d{2}$", month_filter):
            import calendar as _cal_mod

            year, month = int(month_filter[:4]), int(month_filter[5:7])
            last_day = _cal_mod.monthrange(year, month)[1]
            params["timeMin"] = f"{month_filter}-01T00:00:00Z"
            params["timeMax"] = f"{month_filter}-{last_day}T23:59:59Z"

        result = self._execute_cli(
            [
                "gws",
                "calendar",
                "events",
                "list",
                "--params",
                _json.dumps(params),
                "--format",
                "json",
            ],
        )
        if not result.ok:
            return None

        try:
            raw = result.stdout
            # Skip any preamble before the JSON.
            idx = raw.index("{")
            data = _json.loads(raw[idx:])
        except Exception:
            return None

        items = data.get("items", [])
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
        import json
        import re

        path = path.strip("/")

        if not path:
            # Root: list calendars with human-readable names.
            names = self._fetch_calendar_names()
            if names:
                return [f"{sanitize_filename(name, max_len=60)}/" for name in names.values()]
            return ["primary/"]

        # Inside a calendar (or calendar/month): list events
        parts = path.split("/")
        # Resolve human-readable folder name back to calendar ID for API calls.
        cal_id = self._resolve_calendar_id(parts[0])
        month_filter = parts[1] if len(parts) >= 2 else None

        # Fetch events from API
        params: dict[str, Any] = {
            "calendarId": cal_id,
            "maxResults": 50,
            "timeMin": "2026-01-01T00:00:00Z",
        }
        # If filtering by month, narrow the time range
        if month_filter and re.match(r"^\d{4}-\d{2}$", month_filter):
            import calendar as _cal_mod

            year, month = int(month_filter[:4]), int(month_filter[5:7])
            last_day = _cal_mod.monthrange(year, month)[1]
            params["timeMin"] = f"{month_filter}-01T00:00:00Z"
            params["timeMax"] = f"{month_filter}-{last_day}T23:59:59Z"

        result = self._execute_cli(
            [
                "gws",
                "calendar",
                "events",
                "list",
                "--params",
                json.dumps(params),
                "--format",
                "yaml",
            ],
        )
        if not result.ok:
            return []

        event_ids = re.findall(r'^\s+id:\s*"([^"]+)"', result.stdout, re.MULTILINE)

        # Calendar root listing: return month subfolders derived from event dates.
        if not month_filter:
            start_dates = re.findall(r'dateTime:\s*"(\d{4}-\d{2})', result.stdout) + re.findall(
                r'date:\s*"(\d{4}-\d{2})', result.stdout
            )
            months = sorted(set(start_dates))
            if months:
                return [f"{m}/" for m in months]
            # Fallback: no parseable dates, return flat event list
            return [f"{eid}.yaml" for eid in event_ids]

        # Month listing: return event files
        return [f"{eid}.yaml" for eid in event_ids]

    def read_content(
        self,
        content_hash: str,
        context: Any = None,
    ) -> bytes:
        """Read a calendar event as YAML via gws CLI."""
        import json

        event_id = content_hash
        cal_id = "primary"
        if context and hasattr(context, "backend_path") and context.backend_path:
            parts = context.backend_path.strip("/").split("/")
            if len(parts) >= 2:
                cal_id = self._resolve_calendar_id(parts[0])
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
        )
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
