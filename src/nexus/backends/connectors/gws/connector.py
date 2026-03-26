"""Concrete Google Workspace CLI connector classes.

Each class is a CLIConnector subclass with baked-in schemas, traits, and
CLI configuration. Instantiate directly or via ``create_connector_from_yaml()``
with the corresponding YAML config.

Phase 3 (Issue #3148).
Human-readable display paths added in Issue #3256.
"""

from __future__ import annotations

import json
import logging
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
from nexus.backends.connectors.cli.base import CLIConnector
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
class SheetsConnector(CLIConnector):
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
            skill_section="operations",
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


@register_connector(
    "gws_docs",
    description="Google Docs via gws CLI",
    category="cli",
    service_name="gws",
)
class DocsConnector(CLIConnector):
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
            skill_section="operations",
            fix_example="document_id: <valid document ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("docs.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def list_dir(self, path: str = "/", context: "OperationContext | None" = None) -> list[str]:
        """List Google Docs by querying Drive metadata and filtering to document mime types."""
        normalized = path.strip("/")
        if normalized:
            return []

        args = [
            "gws",
            "drive",
            "files",
            "list",
            "--params",
            json.dumps({"q": f'mimeType = "{_DOC_MIME_TYPE}"', "pageSize": 100}),
        ]
        token = self._get_user_token(context)
        auth_env = self._build_auth_env(token) if token else None
        result = self._execute_cli(args, context=context, env=auth_env)
        result = self._error_mapper.classify_result(result)
        if not result.ok:
            from nexus.contracts.exceptions import BackendError

            raise BackendError(result.summary(), backend=self.name, path=path)

        try:
            data = json.loads(result.stdout)
        except Exception as exc:
            from nexus.contracts.exceptions import BackendError

            raise BackendError(
                f"Failed to parse gws docs listing: {exc}", backend=self.name
            ) from exc

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
            if item.get("mimeType") == _DOC_MIME_TYPE and str(item.get("name", "")).strip()
        ]
        return sorted(entries)


# ============================================================================
# Chat
# ============================================================================


@register_connector(
    "gws_chat",
    description="Google Chat via gws CLI",
    category="cli",
    service_name="gws",
)
class ChatConnector(CLIConnector):
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
            skill_section="operations",
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


@register_connector(
    "gws_drive",
    description="Google Drive via gws CLI",
    category="cli",
    service_name="gws",
)
class DriveConnector(CLIConnector):
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
            skill_section="operations",
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


@register_connector(
    "gws_gmail",
    description="Gmail via gws CLI",
    category="cli",
    service_name="gws",
)
class GmailConnector(CLIConnector):
    """Gmail CLI connector via ``gws gmail``.

    CLI-backed alternative to the existing GmailConnectorBackend API connector.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gmail"
    CLI_NAME = "gws"
    CLI_SERVICE = "gmail"
    use_metadata_listing = False  # Sync reads from gws CLI, not metadata

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
            skill_section="operations",
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
  SKILL.md                         # This document
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

        # Inside a label (or INBOX/CATEGORY): list messages via CLI.
        label_ids = [label]
        if label == "INBOX" and len(parts) >= 2:
            category_name = parts[1]
            # Map category folder name back to Gmail label ID.
            for gmail_label, folder in _GMAIL_CATEGORIES.items():
                if folder == category_name:
                    label_ids.append(gmail_label)
                    break

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

        import yaml as _yaml

        return _yaml.dump(fields, default_flow_style=False, allow_unicode=True).encode("utf-8")

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


@register_connector(
    "gws_calendar",
    description="Google Calendar via gws CLI",
    category="cli",
    service_name="gws",
)
class CalendarConnector(CLIConnector):
    """Calendar CLI connector via ``gws calendar``.

    CLI-backed alternative to the existing GoogleCalendarConnectorBackend.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    Human-readable display paths added in Issue #3256.
    """

    SKILL_NAME = "gcalendar"
    CLI_NAME = "gws"
    CLI_SERVICE = "calendar"
    use_metadata_listing = False  # Sync reads from gws CLI, not metadata

    DIRECTORY_STRUCTURE = """\
/mnt/calendar/
  primary/                         # Primary calendar
    2026-03/                       # Month-based grouping
      2026-03-21_10-00_Team-Standup.yaml
    _new.yaml                      # ✏ Write here to CREATE an event
    _update.yaml                   # ✏ Write here to UPDATE an event
    _delete.yaml                   # ✏ Write here to DELETE an event (irreversible)
  {calendarId}/                    # Other calendars (shared, holidays, etc.)
  .skill/
    SKILL.md"""

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
            skill_section="operations",
            fix_example="event_id: <valid event ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = _load_gws_config("calendar.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    # --- Display path (Issue #3256) ---

    def display_path(self, item_id: str, metadata: dict[str, Any] | None = None) -> str:
        """Generate human-readable event path with month grouping.

        Format: ``{calendarId}/{YYYY-MM}/{date}_{time}_{summary}.yaml``
        Example: ``primary/2026-03/2026-03-21_10-00_Team-Standup.yaml``
        """
        meta = metadata or {}
        cal_id = meta.get("calendarId", "primary")

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
            return f"{cal_id}/{month_folder}/{filename}"
        return f"{cal_id}/{filename}"

    # --- Read/sync operations ---

    def list_dir(
        self,
        path: str = "/",
        context: Any = None,
    ) -> list[str]:
        """List calendars or events."""
        import json
        import re

        path = path.strip("/")

        if not path:
            # Root: list calendars
            result = self._execute_cli(
                ["gws", "calendar", "calendarList", "list", "--format", "yaml"],
            )
            if not result.ok:
                return ["primary/"]
            cal_ids = re.findall(r'id:\s*"([^"]+)"', result.stdout)
            return [f"{cid}/" for cid in cal_ids] if cal_ids else ["primary/"]

        # Inside a calendar (or calendar/month): list events
        parts = path.split("/")
        cal_id = parts[0]
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
                cal_id = parts[0]
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
