"""Concrete Google Workspace CLI connector classes.

Each class is a CLIConnector subclass with baked-in schemas, traits, and
CLI configuration. Instantiate directly or via ``create_connector_from_yaml()``
with the corresponding YAML config.

Phase 3 (Issue #3148).
"""

from __future__ import annotations

import logging
from datetime import UTC
from pathlib import Path
from typing import Any

from nexus.backends.base.registry import register_connector
from nexus.backends.connectors.base import (
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
)
from nexus.backends.connectors.calendar.schemas import (
    CreateEventSchema,
    DeleteEventSchema,
    UpdateEventSchema,
)
from nexus.backends.connectors.cli.base import CLIConnector
from nexus.backends.connectors.cli.config import CLIConnectorConfig

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

logger = logging.getLogger(__name__)

_CONFIGS_DIR = Path(__file__).parent / "configs"


@register_connector(
    "gws_sheets",
    description="Google Sheets via gws CLI",
    category="cli",
    service_name="sheets",
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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "SPREADSHEET_NOT_FOUND": ErrorDef(
            message="Spreadsheet not found",
            skill_section="operations",
            fix_example="spreadsheet_id: <valid spreadsheet ID or URL>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = self._load_config("sheets.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    @staticmethod
    def _load_config(filename: str) -> CLIConnectorConfig | None:
        config_path = _CONFIGS_DIR / filename
        if config_path.exists():
            from nexus.backends.connectors.cli.loader import load_connector_config

            return load_connector_config(config_path)
        return None


@register_connector(
    "gws_docs",
    description="Google Docs via gws CLI",
    category="cli",
    service_name="docs",
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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "DOCUMENT_NOT_FOUND": ErrorDef(
            message="Document not found",
            skill_section="operations",
            fix_example="document_id: <valid document ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("docs.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


@register_connector(
    "gws_chat",
    description="Google Chat via gws CLI",
    category="cli",
    service_name="chat",
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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "SPACE_NOT_FOUND": ErrorDef(
            message="Chat space not found",
            skill_section="operations",
            fix_example="space: <valid space name or ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("chat.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


@register_connector(
    "gws_drive",
    description="Google Drive via gws CLI",
    category="cli",
    service_name="drive",
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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "FILE_NOT_FOUND": ErrorDef(
            message="File not found in Drive",
            skill_section="operations",
            fix_example="file_id: <valid Drive file ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("drive.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)


@register_connector(
    "gws_gmail",
    description="Gmail via gws CLI",
    category="cli",
    service_name="gmail",
)
class GmailConnector(CLIConnector):
    """Gmail CLI connector via ``gws gmail``.

    CLI-backed alternative to the existing GmailConnectorBackend API connector.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    """

    SKILL_NAME = "gmail"
    CLI_NAME = "gws"
    CLI_SERVICE = "gmail"
    use_metadata_listing = True

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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "MISSING_RECIPIENTS": ErrorDef(
            message="Email requires at least one recipient",
            skill_section="operations",
            fix_example="to:\n  - user@example.com",
        ),
    }

    # Gmail label folders
    _LABELS = ["INBOX", "SENT", "STARRED", "IMPORTANT", "DRAFTS"]

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("gmail.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

    def list_dir(
        self,
        path: str = "/",
        context: Any = None,
    ) -> list[str]:
        """List Gmail labels or messages in a label folder."""
        import json
        import re

        path = path.strip("/")

        if not path:
            # Root: return label folders
            return [f"{label}/" for label in self._LABELS]

        # Inside a label folder: list messages
        label = path.split("/")[0]
        result = self._execute_cli(
            [
                "gws",
                "gmail",
                "users",
                "messages",
                "list",
                "--params",
                json.dumps({"userId": "me", "maxResults": 50, "labelIds": [label]}),
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

    def get_file_info(self, path: str, context: Any = None) -> Any:
        """Return file metadata so sync creates VFS entries."""
        from datetime import datetime

        from nexus.backends.base.backend import FileInfo

        if self.is_directory(path, context):
            return FileInfo(size=0, mtime=datetime.now(UTC))

        # For email files, return approximate metadata
        content = self.read_content("", context)
        return FileInfo(
            size=len(content),
            mtime=datetime.now(UTC),
            content_hash=None,
        )

    def is_directory(self, path: str, context: Any = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        return path.split("/")[0] in self._LABELS and not path.endswith(".yaml")


@register_connector(
    "gws_calendar",
    description="Google Calendar via gws CLI",
    category="cli",
    service_name="calendar",
)
class CalendarConnector(CLIConnector):
    """Calendar CLI connector via ``gws calendar``.

    CLI-backed alternative to the existing GoogleCalendarConnectorBackend.
    Uses gws CLI for all operations. Phase 3 (Issue #3148).
    """

    SKILL_NAME = "gcalendar"
    CLI_NAME = "gws"
    CLI_SERVICE = "calendar"
    use_metadata_listing = True

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
        "MISSING_AGENT_INTENT": ErrorDef(
            message="Operations require agent_intent",
            skill_section="required-format",
        ),
        "EVENT_NOT_FOUND": ErrorDef(
            message="Calendar event not found",
            skill_section="operations",
            fix_example="event_id: <valid event ID>",
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        config = SheetsConnector._load_config("calendar.yaml")
        kwargs.setdefault("config", config)
        super().__init__(**kwargs)

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

        # Inside a calendar: list events
        cal_id = path.split("/")[0]
        result = self._execute_cli(
            [
                "gws",
                "calendar",
                "events",
                "list",
                "--params",
                json.dumps(
                    {
                        "calendarId": cal_id,
                        "maxResults": 50,
                        "timeMin": "2026-01-01T00:00:00Z",
                    }
                ),
                "--format",
                "yaml",
            ],
        )
        if not result.ok:
            return []

        event_ids = re.findall(r'^\s+id:\s*"([^"]+)"', result.stdout, re.MULTILINE)
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
        """Return file metadata so sync creates VFS entries."""
        from datetime import datetime

        from nexus.backends.base.backend import FileInfo

        if self.is_directory(path, context):
            return FileInfo(size=0, mtime=datetime.now(UTC))

        content = self.read_content("", context)
        return FileInfo(
            size=len(content),
            mtime=datetime.now(UTC),
            content_hash=None,
        )

    def is_directory(self, path: str, context: Any = None) -> bool:
        path = path.strip("/")
        if not path:
            return True
        return not path.endswith(".yaml")
